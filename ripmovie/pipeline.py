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
    progress("[master] delivering the lossless source rip (all tracks) ...")
    return _deliver(cfg, source, title, year, tmdb_id, dry_run=dry_run, progress=progress)


def deliver_rendition(cfg: Config, source: str, title: str, year, tmdb_id, is_anim: bool, *,
                      sample: Optional[float] = None, dry_run: bool = False, keep: bool = False,
                      progress: Callable[[str], None] = print) -> dict:
    """AI-upscaled H.264 + Apple-native audio (DTS/TrueHD -> AC3, +AAC stereo) -> .mp4, plus an
    OCR'd English .srt sidecar. Cleans its own temps once the rendition is confirmed delivered."""
    from .enhance import enhance
    from .finalize import mux_rendition, make_subtitle_sidecar

    work = cfg.path_for("paths.work_dir")
    work.mkdir(parents=True, exist_ok=True)
    stem = Path(source).stem
    video = work / f"{stem}_up_video.mp4"
    final = work / f"{stem}_1080p.mp4"
    srt = work / f"{stem}_1080p.eng.srt"
    started = time.time()

    def _stage(s):
        status.write(cfg, "upscaling", title=title, year=year, stage=s,
                     started=started, output=str(final))

    _stage("enhancing")
    progress("[rendition] enhancing (AI upscale — the slow stage) ...")
    enhance(cfg, source, str(video), is_anim, sample_seconds=sample, mux_audio=False,
            progress=lambda s: progress("  " + s))
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
        n = _remove(video, final, srt)
        progress(f"[cleanup] removed {n} rendition temp file(s) from {work}")
    else:
        progress(f"[cleanup] rendition NOT delivered — keeping local files in {work} for retry")
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

    if not match:
        progress("no TMDb match — leaving the rip in place (name it manually with `push`)")
        return {"status": "ripped_unmatched", "file": ripped}

    # Deliver the master now (the movie appears in Jellyfin immediately at DVD quality); the slow
    # ~10h upscale is decoupled onto a queue so a stack of discs rips back-to-back.
    master = deliver_master(cfg, ripped, match.title, match.year, match.tmdb_id,
                            dry_run=dry_run, progress=progress)
    mode = str(cfg.get("upscale.mode", "queue")).lower()

    if mode == "queue" and not dry_run:
        job = enqueue_upscale(cfg, ripped, match.title, match.year, match.tmdb_id, match.is_animation)
        progress(f"[queue] rendition queued ({job.name}); rip kept as the worker's source. "
                 f"Run `rip-movie upscale-worker` to process the queue.")
        return {"status": "queued", "folder": match.folder, "master": master, "job": str(job)}

    # inline mode (or dry-run): upscale right here, then drop the rip once both tiers are in.
    rend = deliver_rendition(cfg, ripped, match.title, match.year, match.tmdb_id,
                             match.is_animation, dry_run=dry_run, progress=progress)
    m_ok = master.get("status") in ("delivered", "exists", "disabled")
    r_ok = rend.get("rendition", {}).get("status") in ("delivered", "exists")
    if dry_run:
        progress(f"[cleanup] dry-run — leaving rip at {ripped}")
    elif m_ok and r_ok:
        _remove(ripped)
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


def list_upscale_jobs(cfg: Config) -> list[dict]:
    """Pending jobs, oldest first. (.running / .failed are excluded — only *.json is pending.)"""
    out = []
    for f in sorted(_upscale_dir(cfg).glob("*.json"), key=lambda p: p.stat().st_mtime):
        d = json.loads(f.read_text())
        d["_file"] = str(f)
        out.append(d)
    return out


def run_upscale_worker(cfg: Config, once: bool = False, poll: int = 30,
                       progress: Callable[[str], None] = print) -> int:
    """Drain the upscale queue one job at a time. Builds + delivers each rendition, then removes
    the local rip. A failed job is parked as <slug>.failed (never silently retried)."""
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
