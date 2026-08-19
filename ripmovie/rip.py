"""Rip a disc title to MKV with makemkvcon, reporting progress.

Pairs with disc.py (scan + title selection): scan_disc -> select_titles -> rip_title.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Optional

from .config import Config


class RipError(Exception):
    pass


def rip_title(cfg: Config, title_index: int, output_dir: str,
              device: Optional[str] = None, progress: Callable[[str], None] = print,
              timeout: int = 4 * 3600) -> str:
    """Rip one title to output_dir and return the produced .mkv path."""
    makemkv = cfg.get("paths.makemkvcon", "makemkvcon")
    device = device or cfg.get("disc.device", "disc:0")
    minlen = int(cfg.get("disc.min_title_seconds", 300))
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in out.glob("*.mkv")}

    cmd = [makemkv, "-r", "--cache=1", "--progress=-same", f"--minlength={minlen}",
           "mkv", device, str(title_index), str(out)]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
    except FileNotFoundError as e:
        raise RipError(f"makemkvcon not found at {makemkv!r}") from e

    last_pct, err = -1, ""
    try:
        for line in proc.stdout:                       # robot output, one record per line
            line = line.strip()
            if line.startswith("PRGV:"):               # PRGV:current,total,max
                parts = line[5:].split(",")
                if len(parts) >= 3 and parts[2].isdigit() and int(parts[2]):
                    pct = round(100 * int(parts[1]) / int(parts[2]))
                    if pct >= last_pct + 5:
                        progress(f"ripping title {title_index}... {pct}%")
                        last_pct = pct
            elif line.startswith("MSG:"):
                low = line.lower()
                if "fail" in low or "error" in low:
                    err = line
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as e:
        proc.kill()
        raise RipError(f"rip timed out after {timeout}s") from e

    if proc.returncode != 0:
        raise RipError(err or f"makemkvcon exited {proc.returncode}")
    new = [p for p in out.glob("*.mkv") if p.name not in before]
    if not new:
        raise RipError("rip finished but produced no .mkv (bad/copy-protected disc?)")
    return str(max(new, key=lambda p: p.stat().st_mtime))
