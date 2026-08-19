"""Tiny JSON status files under <state_dir>/status/ so the dashboard can see live stage state.

The pipeline writes 'ripping'/'upscaling' snapshots as it works and appends finished titles to
completed.jsonl. Everything is best-effort — a missing/garbled file just means "nothing there".
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .config import Config


def _dir(cfg: Config) -> Path:
    d = cfg.path_for("paths.state_dir") / "status"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write(cfg: Config, name: str, **data) -> None:
    data["updated"] = time.time()
    try:
        (_dir(cfg) / f"{name}.json").write_text(json.dumps(data))
    except OSError:
        pass


def clear(cfg: Config, name: str) -> None:
    try:
        (_dir(cfg) / f"{name}.json").unlink()
    except OSError:
        pass


def read(cfg: Config, name: str):
    try:
        return json.loads((_dir(cfg) / f"{name}.json").read_text())
    except (OSError, ValueError):
        return None


def complete(cfg: Config, **entry) -> None:
    entry["finished"] = time.time()
    try:
        with open(_dir(cfg) / "completed.jsonl", "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def recent(cfg: Config, n: int = 12) -> list[dict]:
    p = _dir(cfg) / "completed.jsonl"
    try:
        lines = p.read_text().splitlines()[-n:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out[::-1]
