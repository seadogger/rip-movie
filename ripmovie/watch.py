"""Optical-drive watcher: wait for a disc, run the pipeline, eject, repeat."""
from __future__ import annotations

import subprocess
import time
from typing import Callable

from . import pipeline
from .config import Config


def disc_present(cfg: Config) -> bool:
    try:
        out = subprocess.run(["drutil", "status"], capture_output=True, text=True, timeout=15).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return "Type:" in out and "No Media" not in out


def eject(cfg: Config) -> None:
    try:
        subprocess.run(["drutil", "eject"], capture_output=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def watch(cfg: Config, progress: Callable[[str], None] = print, poll: int = 10) -> int:
    progress("watching the optical drive — insert a disc (Ctrl-C to stop)")
    while True:
        if disc_present(cfg):
            progress("disc detected — processing")
            try:
                r = pipeline.process_disc(cfg, progress=progress)
                progress(f"==> {r.get('status')}")
            except Exception as e:  # noqa: BLE001 - a bad disc shouldn't kill the daemon
                progress(f"error: {e}")
            if cfg.get("disc.eject_when_done", True):
                eject(cfg)
                progress("ejected")
            while disc_present(cfg):          # wait for removal before arming again
                time.sleep(poll)
            progress("waiting for next disc...")
        time.sleep(poll)
