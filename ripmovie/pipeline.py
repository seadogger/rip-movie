"""End-to-end orchestration shared by `run` (file in) and `watch`/`disc` (disc in).

deliver_file:  finished file -> enhance -> name -> deliver -> Jellyfin identify
process_disc:  disc -> identify -> library-check -> rip -> deliver_file
Ambiguous discs (playlist obfuscation / episodic) go to a JSON review queue instead of guessing.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable, Optional

from .config import Config


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
        return {"status": "exists", "filename": t["filename"]}
    if dry_run:
        progress(f"  DRY RUN — would deliver {t['filename']}")
        return {"status": "dry_run", "filename": t["filename"]}
    res = push(cfg, file, t["rel"])
    jellyfin.force_identify(cfg, t["folder"], tmdb_id)
    progress(f"  delivered {t['filename']}")
    return {"status": "delivered", "dest": res["dest"], "filename": t["filename"]}


def process_file(cfg: Config, source: str, title: str, year, tmdb_id, is_anim: bool, *,
                 sample: Optional[float] = None, dry_run: bool = False, keep: bool = False,
                 progress: Callable[[str], None] = print) -> dict:
    """Rip-once, two-tier: deliver the lossless source master AND an upscaled 1080p rendition.

    Master = the raw rip untouched (all tracks/languages/subs). Rendition = AI-upscaled video +
    Apple-friendly audio (originals + AC3 for DTS/TrueHD + AAC stereo) + English subs, in MKV.
    """
    from .enhance import enhance
    from .finalize import mux_tracks

    results: dict = {}
    if cfg.get("deliver.keep_source_master", True):
        progress("[master] delivering the lossless source rip (all tracks) ...")
        results["master"] = _deliver(cfg, source, title, year, tmdb_id,
                                     dry_run=dry_run, progress=progress)

    work = cfg.path_for("paths.work_dir")
    work.mkdir(parents=True, exist_ok=True)
    video = work / f"{Path(source).stem}_up_video.mp4"
    final = work / f"{Path(source).stem}_1080p.mkv"
    progress("[rendition] enhancing (AI upscale — the slow stage) ...")
    enhance(cfg, source, str(video), is_anim, sample_seconds=sample, mux_audio=False,
            progress=lambda s: progress("  " + s))
    progress("[rendition] muxing audio (+AC3 for DTS) + english subtitles ...")
    mux_tracks(cfg, str(video), source, str(final), progress=lambda s: progress("  " + s))
    results["rendition"] = _deliver(cfg, str(final), title, year, tmdb_id,
                                    dry_run=dry_run, progress=progress)
    if not keep:
        for f in (video, final):
            try:
                os.remove(f)
            except OSError:
                pass
    return results


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
    progress(f"ripping title #{title_idx}...")
    ripped = rip_title(cfg, title_idx, str(rip_dir), progress=progress)
    progress(f"ripped -> {ripped}")

    if not match:
        progress("no TMDb match — leaving the rip in place (name it manually with `push`)")
        return {"status": "ripped_unmatched", "file": ripped}

    res = process_file(cfg, ripped, match.title, match.year, match.tmdb_id,
                       match.is_animation, dry_run=dry_run, progress=progress)
    try:
        os.remove(ripped)   # master is delivered to the library (backed up); local temp not needed
    except OSError:
        pass
    return res


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
