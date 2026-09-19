"""Push a finished file into the Nextcloud library and make Jellyfin see it.

Delivery is a **plain file copy** into `deliver.local.dest_root` — the Nextcloud **File Provider**
(virtual files) mount at `~/Library/CloudStorage/Nextcloud-…`, which surfaces the library as an
ordinary Finder folder. The Nextcloud desktop client uploads the copy on-demand and owns the
credentials, so the pipeline carries **no auth** and never touches WebDAV or kubectl. `dest_root` is
the local path that maps to the SAME storage Jellyfin reads (validated by `check_same_location`); the
rel_path (`Videos/Movies/…`) appends to it. Once the copy lands we trigger a Jellyfin refresh.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import jellyfin
from .config import Config


class DeliverError(Exception):
    """The delivery target (the mounted library folder) was missing or unwritable."""


def _local_root(cfg: Config) -> Path:
    raw = cfg.get("deliver.local.dest_root", "")
    if not raw:
        raise DeliverError("[deliver.local].dest_root is empty — point it at the mounted library "
                           "folder (the Nextcloud File Provider mount under ~/Library/CloudStorage)")
    return Path(raw).expanduser()


def dest_exists(cfg: Config, rel_path: str) -> bool:
    """Is this exact rel_path already in the library? (mount lookup — replaces the old kubectl
    `list_movie_tree` dup-check)."""
    try:
        return (_local_root(cfg) / rel_path).exists()
    except DeliverError:
        return False


def push(cfg: Config, local_path: str, rel_path: str, dry_run: bool = False,
         scan: bool = True, refresh: bool = True) -> dict:
    """Copy `local_path` into the mounted library at `rel_path`, then refresh Jellyfin.

    `scan` is accepted for API compatibility but unused — the sync client indexes the write natively,
    so there is no `occ files:scan`.
    """
    root = _local_root(cfg)
    dest = root / rel_path
    if dry_run:
        return {"dry_run": True, "dest": str(dest), "steps": [
            f"mkdir -p {dest.parent}",
            f"copy {os.path.getsize(local_path):,} bytes -> {dest.name}.part",
            f"rename -> {dest.name} (atomic, same volume)",
            "Nextcloud File Provider uploads it on-demand — no auth, no kubectl",
            "jellyfin refresh" if refresh else "jellyfin refresh: skipped",
        ]}
    if not root.exists():
        raise DeliverError(f"dest_root is not mounted/available: {root}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")   # copy to .part then atomic-rename so a partial copy
    shutil.copyfile(local_path, part)            # is never seen by the sync client / Jellyfin
    os.replace(part, dest)                        # (the client's ignore-list skips *.part)
    result = {"dest": str(dest), "transport": "local"}
    if refresh:                                  # kick Jellyfin now that the file has landed
        try:
            result["jellyfin"] = jellyfin.refresh(cfg)
        except Exception as e:                   # noqa: BLE001 - a refresh hiccup must not fail delivery
            result["jellyfin"] = f"refresh failed: {e}"
    return result


def check_local(cfg: Config) -> tuple[bool, str]:
    """The delivery folder is set, mounted, and writable."""
    try:
        root = _local_root(cfg)
    except DeliverError as e:
        return False, str(e)
    if not root.exists():
        return False, f"not mounted: {root}"
    if not os.access(root, os.W_OK):
        return False, f"not writable: {root}"
    return True, f"writable ({root})"


def check_same_location(cfg: Config, sample: int = 25) -> tuple[bool, str]:
    """Confirm the ripping Mac and Jellyfin see the SAME library — the write location and the read
    location are one and the same storage. Read-only: list movie folders under the local delivery
    root and check they show up in Jellyfin's item paths. If the local library is empty we can't
    assert it yet (a fresh mount), so that's a warning, not a hard pass/fail."""
    try:
        root = _local_root(cfg)
    except DeliverError as e:
        return False, str(e)
    movies = root / cfg.get("deliver.local.movies_subpath", "Videos/Movies")
    if not movies.is_dir():
        return False, f"no Movies dir under the mount ({movies}) — is dest_root the library root?"
    local_folders = [p.name for p in sorted(movies.iterdir()) if p.is_dir()][:sample]
    if not local_folders:
        return False, f"{movies} is empty — nothing to cross-check yet"
    try:
        jf_paths = jellyfin.list_movie_paths(cfg)
    except Exception as e:  # noqa: BLE001
        return False, f"jellyfin path list failed: {e}"
    matched = [f for f in local_folders if any(f in p for p in jf_paths)]
    if matched:
        return True, f"same storage — {len(matched)}/{len(local_folders)} local folders visible to Jellyfin (e.g. {matched[0]!r})"
    return False, (f"MISMATCH — none of {len(local_folders)} local movie folders appear in Jellyfin's "
                   f"paths; the Mac's dest_root and Jellyfin's library are different storage")
