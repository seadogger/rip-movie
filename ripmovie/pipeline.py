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


def deliver_file(cfg: Config, file: str, title: str, year, tmdb_id, is_anim: bool, *,
                 sample: Optional[float] = None, dry_run: bool = False, keep: bool = False,
                 progress: Callable[[str], None] = print) -> dict:
    from .enhance import enhance
    from .naming import target
    from .library import search
    from .deliver import push
    from . import jellyfin

    work = cfg.path_for("paths.work_dir")
    work.mkdir(parents=True, exist_ok=True)
    upscaled = work / f"{Path(file).stem}_upscaled.mp4"
    progress("enhancing (AI upscale — the slow stage)...")
    enhance(cfg, file, str(upscaled), is_anim, sample_seconds=sample,
            progress=lambda s: progress("  " + s))

    t = target(cfg, str(upscaled), title, year)
    progress(f"named -> Movies/{t['folder']}/{t['filename']}")
    present = [f.name for r in search(cfg, title) if r.folder == t["folder"] for f in r.files]
    if t["filename"] in present:
        progress("already in library — skipping delivery")
        return {"status": "exists", "folder": t["folder"]}
    if dry_run:
        progress(f"DRY RUN — kept {upscaled}, delivery skipped")
        return {"status": "dry_run", "upscaled": str(upscaled)}

    res = push(cfg, str(upscaled), t["rel"])
    ident = jellyfin.force_identify(cfg, t["folder"], tmdb_id)
    progress(f"delivered -> {res['dest']}; {ident}")
    if not keep:
        try:
            os.remove(upscaled)
        except OSError:
            pass
    return {"status": "delivered", "dest": res["dest"], "folder": t["folder"]}


def process_disc(cfg: Config, force_title: Optional[int] = None, dry_run: bool = False,
                 progress: Callable[[str], None] = print) -> dict:
    from .disc import scan_disc, select_titles
    from .identify import identify, IdentifyError
    from .library import check_exists
    from .rip import rip_title

    scan = scan_disc(cfg)
    match = None
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

    sel = select_titles(scan, cfg)
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

    res = deliver_file(cfg, ripped, match.title, match.year, match.tmdb_id,
                       match.is_animation, dry_run=dry_run, progress=progress)
    try:
        os.remove(ripped)
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
