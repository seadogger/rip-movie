"""End-to-end orchestration shared by `run` (file in) and `watch`/`disc` (disc in).

deliver_file:  finished file -> enhance -> name -> deliver -> Jellyfin identify
process_disc:  disc -> identify -> library-check -> rip -> deliver_file
Ambiguous discs (playlist obfuscation / episodic) go to a JSON review queue instead of guessing.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Callable, Optional

from . import status
from .config import Config


def _remove(*paths) -> int:
    """Delete the given files if present; return how many were removed."""
    n = 0
    for p in paths:
        try:
            os.remove(p)
            n += 1
        except OSError:
            pass
    return n


def _deliver(cfg: Config, file: str, title: str, year, tmdb_id, *,
             dry_run: bool, progress: Callable[[str], None]) -> dict:
    """Name one finished file to the schema, skip if already present, push, force-identify."""
    from .naming import target
    from .library import search
    from .deliver import push
    from . import jellyfin

    t = target(cfg, file, title, year)
    present = [f.name for r in search(cfg, title) if r.folder == t["folder"] for f in r.files]
    if t["filename"] in present:
        progress(f"  {t['filename']} already in library — skip")
        return {"status": "exists", "filename": t["filename"], "folder": t["folder"]}
    if dry_run:
        progress(f"  DRY RUN — would deliver {t['filename']}")
        return {"status": "dry_run", "filename": t["filename"], "folder": t["folder"]}
    res = push(cfg, file, t["rel"])
    jellyfin.force_identify(cfg, t["folder"], tmdb_id)
    progress(f"  delivered {t['filename']}")
    return {"status": "delivered", "dest": res["dest"], "filename": t["filename"],
            "folder": t["folder"]}


def deliver_master(cfg: Config, source: str, title: str, year, tmdb_id, *,
                   dry_run: bool = False, progress: Callable[[str], None] = print) -> dict:
    """Deliver the lossless source rip untouched (all audio incl. DTS, original bitmap subs)."""
    if not cfg.get("deliver.keep_source_master", True):
        return {"status": "disabled"}
    progress("[master] uploading the lossless master to Nextcloud + reindexing Jellyfin ...")
    if not dry_run:
        status.write(cfg, "delivering", title=title, year=year,
                     stage="uploading master → Nextcloud + Jellyfin", started=time.time())
    try:
        return _deliver(cfg, source, title, year, tmdb_id, dry_run=dry_run, progress=progress)
    finally:
        status.clear(cfg, "delivering")


def deliver_rendition(cfg: Config, source: str, title: str, year, tmdb_id, is_anim: bool, *,
                      sample: Optional[float] = None, dry_run: bool = False, keep: bool = False,
                      progress: Callable[[str], None] = print) -> dict:
    """AI-upscaled H.264 + Apple-native audio (DTS/TrueHD -> AC3, +AAC stereo) -> .mp4, plus an
    OCR'd English .srt sidecar. Cleans its own temps once the rendition is confirmed delivered."""
    from .enhance import enhance

    work = cfg.path_for("paths.work_dir")
    work.mkdir(parents=True, exist_ok=True)
    stem = Path(source).stem
    video = work / f"{stem}_up_video.mp4"
    final = work / f"{stem}_1080p.mp4"
    srt = work / f"{stem}_1080p.eng.srt"
    started = time.time()
    progf = cfg.path_for("paths.state_dir") / "status" / "upscale_progress.json"
    progf.parent.mkdir(parents=True, exist_ok=True)
    _remove(progf)                                       # clear any stale progress from a prior run

    status.write(cfg, "upscaling", title=title, year=year, stage="enhancing",
                 started=started, output=str(final), progress_file=str(progf))
    progress("[rendition] enhancing (AI upscale — the slow stage) ...")
    enhance(cfg, source, str(video), is_anim, sample_seconds=sample, mux_audio=False,
            progress_file=str(progf), progress=lambda s: progress("  " + s))
    return _finalize_rendition(cfg, source, str(video), str(final), str(srt), title, year, tmdb_id,
                               started=started, progf=progf, dry_run=dry_run, keep=keep,
                               progress=progress)


def _finalize_rendition(cfg: Config, source: str, video: str, final: str, srt: str,
                        title: str, year, tmdb_id, *, started: float, progf, dry_run: bool,
                        keep: bool, extra_cleanup: tuple = (),
                        progress: Callable[[str], None] = print) -> dict:
    """Shared tail for every rendition engine: given an already-upscaled `video` (final geometry,
    H.264, no audio), mux Apple-native audio + OCR'd English subs, deliver both, then clean the
    temps ONLY once the rendition is confirmed in the library. `extra_cleanup` are engine-specific
    artifacts (e.g. the Topaz intermediate + output) removed alongside the normal temps on success."""
    from .finalize import mux_rendition, make_subtitle_sidecar

    def _stage(s):
        status.write(cfg, "upscaling", title=title, year=year, stage=s,
                     started=started, output=str(final), progress_file=str(progf))

    _stage("muxing audio")
    progress("[rendition] muxing Apple-native audio -> .mp4 ...")
    mux_rendition(cfg, str(video), source, str(final), progress=lambda s: progress("  " + s))
    _remove(video)                                      # video-only intermediate now consumed
    _stage("subtitle OCR")
    progress("[rendition] building English subtitle sidecar ...")
    have_srt = make_subtitle_sidecar(cfg, source, str(srt), "eng",
                                     progress=lambda s: progress("  " + s))

    _stage("delivering")
    results: dict = {}
    results["rendition"] = _deliver(cfg, str(final), title, year, tmdb_id,
                                    dry_run=dry_run, progress=progress)
    if have_srt:                                        # deliver .srt beside the .mp4, matching name
        results["subtitle"] = _deliver_sidecar(cfg, str(srt), results["rendition"], "eng",
                                               dry_run=dry_run, progress=progress)

    # Clean up ONLY after the rendition is confirmed in the library — never on failure or dry-run.
    delivered = not dry_run and results["rendition"].get("status") in ("delivered", "exists")
    if keep or dry_run:
        pass
    elif delivered:
        _stage("cleanup")
        n = _remove(video, final, srt, progf, *extra_cleanup)
        progress(f"[cleanup] removed {n} rendition temp file(s)")
        status.log_event(cfg, "cleaned", title=title, year=year,
                         detail=f"{n} rendition temp file(s)")
    else:
        progress("[cleanup] rendition NOT delivered — keeping local files for retry")
    status.clear(cfg, "upscaling")
    if delivered:
        status.complete(cfg, title=title, year=year, kind="rendition")
    return results


def process_file(cfg: Config, source: str, title: str, year, tmdb_id, is_anim: bool, *,
                 sample: Optional[float] = None, dry_run: bool = False, keep: bool = False,
                 progress: Callable[[str], None] = print) -> dict:
    """Two-tier delivery inline: master then rendition (used by the `run` command)."""
    results = {"master": deliver_master(cfg, source, title, year, tmdb_id,
                                        dry_run=dry_run, progress=progress)}
    results.update(deliver_rendition(cfg, source, title, year, tmdb_id, is_anim,
                                     sample=sample, dry_run=dry_run, keep=keep, progress=progress))
    return results


def _deliver_sidecar(cfg: Config, srt_local: str, rendition: dict, lang: str, *,
                     dry_run: bool, progress: Callable[[str], None]) -> dict:
    """Push the .srt next to its .mp4 with the Jellyfin-matching name: '<mp4 stem>.<lang>.srt'."""
    from .deliver import push
    sub = cfg.get("library.movies_subpath", "Videos/Movies").strip("/")
    mp4_name = rendition.get("filename", "")
    folder = rendition.get("folder") or (Path(mp4_name).stem if mp4_name else "")
    if not mp4_name or rendition.get("status") not in ("delivered", "exists"):
        progress("  rendition not delivered — skipping subtitle sidecar")
        return {"status": "skipped"}
    srt_name = f"{Path(mp4_name).stem}.{lang}.srt"
    rel = f"{sub}/{folder}/{srt_name}"
    if dry_run:
        progress(f"  DRY RUN — would deliver {srt_name}")
        return {"status": "dry_run", "filename": srt_name}
    push(cfg, srt_local, rel, refresh=False)            # occ scan picks it up; no Jellyfin identify
    progress(f"  delivered subtitle {srt_name}")
    return {"status": "delivered", "filename": srt_name}


def process_disc(cfg: Config, force_title: Optional[int] = None,
                 name_hint: Optional[str] = None, year_hint: Optional[int] = None,
                 dry_run: bool = False, progress: Callable[[str], None] = print) -> dict:
    from .disc import scan_disc, select_titles
    from .identify import identify, search_tmdb, IdentifyError
    from .library import check_exists
    from .rip import rip_title

    scan = scan_disc(cfg)
    match = None
    if name_hint:                       # user-supplied title (recovers truncated ISO labels)
        key = cfg.get("identify.tmdb_api_key", "")
        match = search_tmdb(name_hint, key, year_hint) if key else None
        if match:
            progress(f"title hint {name_hint!r} -> {match.folder}")
        else:
            progress(f"title hint {name_hint!r} matched nothing on TMDb")
    if match is None:
        try:
            match = identify(scan, cfg)
        except IdentifyError as e:
            progress(f"identify: {e}")
    progress(f"disc {scan.label!r} -> {match.folder if match else '(no TMDb match)'}")

    if match:
        hit = check_exists(cfg, match.folder)
        if hit.exists:
            progress(f"ALREADY OWNED as {hit.matched!r} — nothing to do")
            return {"status": "owned", "folder": match.folder}

    sel = select_titles(scan, cfg, runtime_min=(match.runtime if match else None))
    if match and match.runtime:
        progress(f"TMDb runtime {match.runtime}m -> {sel.reason}")
    if sel.ambiguous and force_title is None:
        enqueue_review(cfg, scan, match, sel)
        progress(f"AMBIGUOUS: {sel.reason} — queued for review")
        return {"status": "review", "reason": sel.reason}
    title_idx = force_title if force_title is not None else sel.main_feature.index

    rip_dir = cfg.path_for("paths.work_dir") / "rips"
    exp = next((t.size_bytes for t in scan.titles if t.index == title_idx), 0)
    status.write(cfg, "ripping", title=(match.title if match else scan.label),
                 year=(match.year if match else None), disc=scan.label,
                 expected_bytes=exp, out_dir=str(rip_dir), started=time.time())
    progress(f"ripping title #{title_idx}...")
    try:
        ripped = rip_title(cfg, title_idx, str(rip_dir), progress=progress)
    finally:
        status.clear(cfg, "ripping")
    progress(f"ripped -> {ripped}")

    # The disc is only needed for the rip; the master upload + upscale work from the local copy.
    # Eject now so a stack of discs can be swapped through back-to-back.
    if cfg.get("disc.eject_when_done", True):
        from .watch import eject
        eject(cfg)
        progress("ejected — safe to load the next disc")

    if not match:
        progress("no TMDb match — leaving the rip in place (name it manually with `push`)")
        return {"status": "ripped_unmatched", "file": ripped}

    # Deliver the master now (the movie appears in Jellyfin immediately at source quality).
    master = deliver_master(cfg, ripped, match.title, match.year, match.tmdb_id,
                            dry_run=dry_run, progress=progress)
    m_ok = master.get("status") in ("delivered", "exists", "disabled")

    # Only SD/DVD sources are upscaled. The CoreML model is 480p-native, and 1080p/4K are already
    # full resolution — running them through it would downscale then re-upscale, hurting quality.
    # So HD/UHD ship as the ripped master and we're done.
    from .naming import probe
    try:
        height = probe(cfg, ripped)["height"]
    except Exception:  # noqa: BLE001
        height = 0
    sd = 0 < height <= int(cfg.get("upscale.dvd.sd_max_height", 576))

    if not sd:
        if not dry_run and m_ok:
            _remove(ripped)
            status.log_event(cfg, "cleaned", title=match.title, year=match.year,
                             detail="rip (HD/4K master only)")
            status.complete(cfg, title=match.title, year=match.year, kind="master")
            progress(f"[done] {match.folder} — {height or '?'}p master delivered (no upscale "
                     f"needed for HD/4K); local rip cleaned up")
        return {"status": "master_only", "folder": match.folder, "height": height, "master": master}

    mode = str(cfg.get("upscale.mode", "queue")).lower()
    if mode == "queue" and not dry_run:
        job = enqueue_upscale(cfg, ripped, match.title, match.year, match.tmdb_id, match.is_animation)
        progress(f"[queue] {height}p SD -> rendition queued ({job.name}); rip kept as the worker's "
                 f"source. Run `rip-movie upscale-worker` to process the queue.")
        return {"status": "queued", "folder": match.folder, "master": master, "job": str(job)}

    # inline mode (or dry-run): upscale right here, then drop the rip once both tiers are in.
    rend = deliver_rendition(cfg, ripped, match.title, match.year, match.tmdb_id,
                             match.is_animation, dry_run=dry_run, progress=progress)
    r_ok = rend.get("rendition", {}).get("status") in ("delivered", "exists")
    if dry_run:
        progress(f"[cleanup] dry-run — leaving rip at {ripped}")
    elif m_ok and r_ok:
        _remove(ripped)
        status.log_event(cfg, "cleaned", title=match.title, year=match.year, detail="rip")
        progress(f"[cleanup] both tiers in library — removed local rip {Path(ripped).name}")
    else:
        progress(f"[cleanup] delivery incomplete — keeping rip {ripped} for retry")
    return {"status": rend.get("rendition", {}).get("status", "error"), "master": master, **rend}


# --- review queue -------------------------------------------------------------
def _review_dir(cfg: Config) -> Path:
    d = cfg.path_for("paths.state_dir") / "review"
    d.mkdir(parents=True, exist_ok=True)
    return d


def enqueue_review(cfg: Config, scan, match, sel) -> Path:
    entry = {
        "disc": scan.label,
        "device": scan.device,
        "match": match.folder if match else None,
        "reason": sel.reason,
        "candidates": [{"index": t.index, "hms": t.hms, "chapters": t.chapters,
                        "gib": round(t.size_gib, 1)} for t in (sel.candidates or sel.eligible[:8])],
    }
    fn = _review_dir(cfg) / (re.sub(r"[^A-Za-z0-9]+", "_", scan.label or "disc")[:40] + ".json")
    fn.write_text(json.dumps(entry, indent=2))
    return fn


def list_reviews(cfg: Config) -> list[dict]:
    out = []
    for f in sorted(_review_dir(cfg).glob("*.json")):
        d = json.loads(f.read_text())
        d["_file"] = str(f)
        out.append(d)
    return out


# --- upscale queue ------------------------------------------------------------
# Ripping is disc-bound (~20 min); upscaling is ANE-bound (~10h) and the ANE is a single device.
# So process_disc enqueues a job (keeping the local rip as its source) and a lone worker drains
# the queue serially. Job lifecycle by file suffix: <slug>.json (pending) -> .running -> .failed.
def _upscale_dir(cfg: Config) -> Path:
    d = cfg.path_for("paths.state_dir") / "upscale_queue"
    d.mkdir(parents=True, exist_ok=True)
    return d


def enqueue_upscale(cfg: Config, source: str, title: str, year, tmdb_id, is_anim: bool) -> Path:
    job = {"source": str(source), "title": title, "year": year, "tmdb_id": tmdb_id,
           "is_anim": bool(is_anim)}
    slug = re.sub(r"[^A-Za-z0-9]+", "_", f"{title}_{year or ''}").strip("_")[:60] or "job"
    fn = _upscale_dir(cfg) / f"{slug}.json"
    fn.write_text(json.dumps(job, indent=2))
    return fn


def enqueue_existing(cfg: Config, folder: str, source_file: str, title: str, year, tmdb_id,
                     is_anim: bool) -> Path:
    """Queue an upscale for a movie already in the library. The master lives in Nextcloud, so the
    job records its pod path (`source_remote`); the worker pulls it local (`source`) at process time
    so a big queue doesn't fetch everything up front."""
    from .library import movies_dir
    remote = f"{movies_dir(cfg)}/{folder}/{source_file}"
    local = str(cfg.path_for("paths.work_dir") / "rips" / source_file)
    job = {"source": local, "source_remote": remote, "title": title, "year": year,
           "tmdb_id": tmdb_id, "is_anim": bool(is_anim), "from_library": folder}
    slug = re.sub(r"[^A-Za-z0-9]+", "_", f"{title}_{year or ''}").strip("_")[:60] or "job"
    fn = _upscale_dir(cfg) / f"{slug}.json"
    fn.write_text(json.dumps(job, indent=2))
    return fn


def queue_library_upscale(cfg: Config, folder: str) -> dict:
    """Resolve a library folder to an upscale job: find its SD master, look up TMDb (genre -> engine
    model + id for later Jellyfin identify), and enqueue. Returns a small status dict for the UI."""
    from .library import upscale_candidates
    from .identify import search_tmdb
    cand = next((c for c in upscale_candidates(cfg) if c.folder == folder), None)
    if not cand:
        return {"ok": False, "error": f"not in library: {folder}"}
    if cand.status != "candidate":
        return {"ok": False, "error": f"{folder} is {cand.status}, not an upscale candidate"}
    # avoid duplicates: already pending/awaiting/running for this title?
    key = re.sub(r"[^a-z0-9]", "", cand.title.lower())
    for j in list_upscale_jobs(cfg) + _awaiting_jobs(cfg):
        if re.sub(r"[^a-z0-9]", "", str(j.get("title", "")).lower()) == key:
            return {"ok": False, "error": f"{cand.title} is already queued"}
    tmdb_id, is_anim = None, False
    apikey = cfg.get("identify.tmdb_api_key", "")
    if apikey:
        m = search_tmdb(cand.title, apikey, int(cand.year) if cand.year.isdigit() else None)
        if m:
            tmdb_id, is_anim = m.tmdb_id, m.is_animation
    year = int(cand.year) if cand.year.isdigit() else None
    enqueue_existing(cfg, folder, cand.source_file, cand.title, year, tmdb_id, is_anim)
    return {"ok": True, "title": cand.title, "year": cand.year, "is_anim": is_anim}


def _ensure_local_source(cfg: Config, job: dict, progress: Callable[[str], None] = print) -> str:
    """If a job's source is a library master still in Nextcloud, pull it local before processing.
    Publishes a live 'pulling master' status so the fetch shows as an active lane on the dashboard
    (otherwise a claimed-but-not-yet-prepping job would vanish between 'queued' and 'prepping')."""
    src = job.get("source", "")
    remote = job.get("source_remote")
    if not remote or (src and Path(src).exists()):
        return src
    from . import kube
    k = cfg.get("deliver.kubectl", {})
    ns, ctx = k.get("nextcloud_namespace"), k.get("context")
    pod = kube.pod_name(ns, k.get("nextcloud_pod_selector"), context=ctx)
    Path(src).parent.mkdir(parents=True, exist_ok=True)
    status.write(cfg, "upscaling", title=job.get("title"), year=job.get("year"),
                 stage="pulling master from Nextcloud", started=time.time(), output=src)
    progress(f"[fetch] pulling master from Nextcloud → {Path(src).name} ...")
    kube.exec_stdout_file(ns, pod, ["cat", remote], src,
                          container=k.get("nextcloud_container"), context=ctx)
    gb = Path(src).stat().st_size / 1e9 if Path(src).exists() else 0
    progress(f"[fetch] pulled {gb:.2f} GB")
    return src


def list_upscale_jobs(cfg: Config) -> list[dict]:
    """Pending jobs, oldest first. (.running / .failed are excluded — only *.json is pending.)"""
    out = []
    for f in sorted(_upscale_dir(cfg).glob("*.json"), key=lambda p: p.stat().st_mtime):
        d = json.loads(f.read_text())
        d["_file"] = str(f)
        out.append(d)
    return out


def _awaiting_jobs(cfg: Config) -> list[dict]:
    """Topaz-handoff jobs that have been prepped and are parked waiting for the VEAI GUI run.
    (Each .awaiting file IS the manifest written by topaz.prep.)"""
    out = []
    for f in sorted(_upscale_dir(cfg).glob("*.awaiting"), key=lambda p: p.stat().st_mtime):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        d["_file"] = str(f)
        out.append(d)
    return out


def run_upscale_worker(cfg: Config, once: bool = False, poll: int = 30,
                       progress: Callable[[str], None] = print) -> int:
    """Drain the upscale queue one job at a time. Builds + delivers each rendition, then removes
    the local rip. A failed job is parked as <slug>.failed (never silently retried).

    The engine is chosen by `upscale.engine`: the default `realesrgan` upscales inline on the ANE;
    `topaz-veai-handoff` instead preps each job to the inbox and finishes it when the VEAI GUI run
    lands the output (see run_topaz_handoff_worker)."""
    engine = str(cfg.get("upscale.engine", "realesrgan")).lower()
    if engine in ("topaz-veai-handoff", "topaz-handoff", "veai-handoff"):
        return run_topaz_handoff_worker(cfg, once=once, poll=poll, progress=progress)

    import time
    progress("upscale worker started" + (" (single pass)" if once else " — draining queue (Ctrl-C to stop)"))
    while True:
        jobs = list_upscale_jobs(cfg)
        if not jobs:
            if once:
                return 0
            time.sleep(poll)
            continue
        job = jobs[0]
        jf = Path(job["_file"])
        running = jf.with_suffix(".running")
        try:
            os.rename(jf, running)                       # claim (atomic) so a 2nd worker won't grab it
        except OSError:
            continue
        src = job["source"]
        progress(f"[upscale] {job['title']} ({job.get('year')})  <- {Path(src).name}")
        try:
            src = _ensure_local_source(cfg, job, progress=progress)   # pull from Nextcloud if needed
            if not Path(src).exists():
                raise FileNotFoundError(f"source rip is gone: {src}")
            res = deliver_rendition(cfg, src, job["title"], job.get("year"), job.get("tmdb_id"),
                                    job.get("is_anim", False), progress=progress)
            if res.get("rendition", {}).get("status") not in ("delivered", "exists"):
                raise RuntimeError("rendition was not delivered")
            _remove(src)
            running.unlink(missing_ok=True)
            progress(f"[upscale] done: {job['title']} — rendition delivered, rip cleaned up")
        except Exception as e:  # noqa: BLE001 - one bad job shouldn't kill the worker
            os.replace(running, jf.with_suffix(".failed"))
            progress(f"[upscale] FAILED {job['title']}: {e} (parked as {jf.with_suffix('.failed').name})")
        if once:
            return 0


def run_topaz_handoff_worker(cfg: Config, once: bool = False, poll: int = 30,
                             progress: Callable[[str], None] = print) -> int:
    """VEAI 2.6.4 GUI handoff engine. Each loop does two independent things:

      1. PREP any pending queue jobs (*.json) into video-only intermediates in the inbox, parking
         each as its manifest (<slug>.awaiting). This is fast, so a stack of discs all land in the
         inbox for you to batch through Video Enhance AI in one GUI run.
      2. RESUME any parked job whose size-stable output has appeared in the outbox: encode -> 1080p,
         mux the master's audio + OCR'd subs, deliver, reindex, and clean up every artifact.

    A failed prep/resume is parked as <slug>.failed (never silently retried)."""
    import time
    from . import topaz

    inbox, outbox = topaz.handoff_dirs(cfg)
    progress(f"topaz handoff worker started — inbox={inbox}  outbox={outbox}"
             + (" (single pass)" if once else "  (Ctrl-C to stop)"))
    while True:
        # 1) prep pending jobs -> inbox
        for job in list_upscale_jobs(cfg):
            jf = Path(job["_file"])
            running = jf.with_suffix(".running")
            try:
                os.rename(jf, running)                    # claim (atomic)
            except OSError:
                continue
            try:
                _ensure_local_source(cfg, job, progress=progress)     # pull from Nextcloud if needed
                manifest = topaz.prep(cfg, job, progress=progress)
                jf.with_suffix(".awaiting").write_text(json.dumps(manifest, indent=2))
                running.unlink(missing_ok=True)
            except Exception as e:  # noqa: BLE001
                os.replace(running, jf.with_suffix(".failed"))
                progress(f"[topaz] PREP FAILED {job.get('title')}: {e} "
                         f"(parked as {jf.with_suffix('.failed').name})")

        # 2) resume any parked job whose VEAI output has landed. Chunked jobs wait for EVERY segment.
        for man in _awaiting_jobs(cfg):
            af = Path(man["_file"])
            chunked = bool(man.get("segments"))
            outs = topaz.find_outputs(cfg, man) if chunked else topaz.find_output(cfg, man)
            if not outs:
                continue
            resuming = af.with_suffix(".resuming")
            try:
                os.rename(af, resuming)                    # claim
            except OSError:
                continue
            try:
                res = (topaz.resume_chunked(cfg, man, outs, progress=progress) if chunked
                       else topaz.resume(cfg, man, str(outs), progress=progress))
                if res.get("rendition", {}).get("status") not in ("delivered", "exists"):
                    raise RuntimeError("rendition was not delivered")
                _remove(man["source"])                    # rip no longer needed (both tiers in)
                status.log_event(cfg, "cleaned", title=man.get("title"), year=man.get("year"),
                                 detail="rip + Topaz intermediate")
                resuming.unlink(missing_ok=True)
                progress(f"[topaz] done: {man.get('title')} — rendition delivered, artifacts cleaned")
            except Exception as e:  # noqa: BLE001
                os.replace(resuming, af.with_suffix(".failed"))
                progress(f"[topaz] RESUME FAILED {man.get('title')}: {e} "
                         f"(parked as {af.with_suffix('.failed').name})")

        awaiting = _awaiting_jobs(cfg)
        if once:
            return 0
        if awaiting:
            progress(f"[topaz] {len(awaiting)} clip(s) awaiting your VEAI run "
                     f"({', '.join(m.get('stem', '?') for m in awaiting[:4])}"
                     f"{' …' if len(awaiting) > 4 else ''}) — polling {outbox}")
        time.sleep(poll)
