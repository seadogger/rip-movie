"""Disc scanning and title selection -- the "black art".

Drives `makemkvcon -r info` (robot/parseable output), parses the title list, and applies a
longest-title-with-ambiguity-guard heuristic so decoy Blu-ray playlists and episodic discs get
kicked to the review queue instead of silently ripping the wrong thing.
"""
from __future__ import annotations

import csv
import io
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from .config import Config

# makemkv ap_ItemAttributeId codes we care about
ATTR_NAME = 2
ATTR_CHAPTERS = 8
ATTR_DURATION = 9
ATTR_SIZE_HUMAN = 10
ATTR_SIZE_BYTES = 11
ATTR_SOURCE_FILE = 16       # underlying .mpls / VTS
ATTR_SEGMENTS_MAP = 26
ATTR_OUTPUT_FILE = 27
ATTR_VOLUME_NAME = 32


class DiscError(Exception):
    pass


@dataclass
class Title:
    index: int
    duration_sec: int = 0
    chapters: int = 0
    size_bytes: int = 0
    source_file: str = ""       # e.g. 00800.mpls
    output_name: str = ""       # e.g. title_t00.mkv
    name: str = ""

    @property
    def hms(self) -> str:
        h, rem = divmod(self.duration_sec, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    @property
    def size_gib(self) -> float:
        return self.size_bytes / (1024 ** 3)


@dataclass
class DiscScan:
    device: str
    disc_name: str
    volume_name: str
    titles: list[Title] = field(default_factory=list)
    raw: str = ""

    @property
    def label(self) -> str:
        """Best human label for TMDb search seeding."""
        return self.disc_name or self.volume_name or ""


@dataclass
class Selection:
    ambiguous: bool
    reason: str
    main_feature: Optional[Title]          # longest eligible title (a suggestion even if ambiguous)
    selected: list[Title]                  # what to actually rip (empty when ambiguous)
    candidates: list[Title]                # near-longest titles that made it ambiguous
    eligible: list[Title]                  # all titles >= min_title_seconds, longest first


def _parse_duration(text: str) -> int:
    parts = text.strip().split(":")
    if not all(p.isdigit() for p in parts):
        return 0
    parts = [int(p) for p in parts]
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec


def _split_robot_line(line: str) -> tuple[str, list[str]]:
    """'TINFO:0,9,0,"1:47:00"' -> ('TINFO', ['0','9','0','1:47:00'])."""
    tag, _, rest = line.partition(":")
    if not rest:
        return tag, []
    reader = csv.reader(io.StringIO(rest))
    try:
        fields = next(reader)
    except StopIteration:
        fields = []
    return tag, fields


def scan_disc(cfg: Config, device: Optional[str] = None, timeout: int = 600) -> DiscScan:
    device = device or cfg.get("disc.device", "disc:0")
    makemkv = cfg.get("paths.makemkvcon", "makemkvcon")
    minlen = int(cfg.get("disc.min_title_seconds", 300))
    cmd = [makemkv, "-r", "--cache=1", f"--minlength={minlen}", "info", device]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise DiscError(f"makemkvcon not found at {makemkv!r}: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise DiscError(f"makemkvcon info timed out after {timeout}s") from e

    out = proc.stdout
    scan = DiscScan(device=device, disc_name="", volume_name="", raw=out)
    titles: dict[int, Title] = {}
    cinfo: dict[int, str] = {}
    saw_disc = False

    for line in out.splitlines():
        tag, fields = _split_robot_line(line)
        if tag == "DRV" and len(fields) >= 6 and fields[1] != "256":
            saw_disc = saw_disc or bool(fields[5].strip())
        elif tag == "CINFO" and len(fields) >= 3:
            try:
                cinfo[int(fields[0])] = fields[2]
            except ValueError:
                pass
        elif tag == "TINFO" and len(fields) >= 4:
            try:
                tid, attr = int(fields[0]), int(fields[1])
            except ValueError:
                continue
            val = fields[3]
            t = titles.setdefault(tid, Title(index=tid))
            if attr == ATTR_DURATION:
                t.duration_sec = _parse_duration(val)
            elif attr == ATTR_CHAPTERS:
                t.chapters = int(val) if val.isdigit() else 0
            elif attr == ATTR_SIZE_BYTES:
                t.size_bytes = int(val) if val.isdigit() else 0
            elif attr == ATTR_SOURCE_FILE:
                t.source_file = val
            elif attr == ATTR_OUTPUT_FILE:
                t.output_name = val
            elif attr == ATTR_NAME:
                t.name = val

    scan.disc_name = cinfo.get(ATTR_NAME, "")
    scan.volume_name = cinfo.get(ATTR_VOLUME_NAME, "")
    scan.titles = [titles[k] for k in sorted(titles)]

    if not scan.titles:
        detail = _first_error(out) or "no titles found (is a disc inserted and readable?)"
        raise DiscError(f"disc scan produced no titles: {detail}")
    return scan


def _first_error(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("MSG:"):
            _, fields = _split_robot_line(line)
            # MSG:code,flags,count,message,format,...  -> field[3] is the human message
            if len(fields) >= 4 and ("fail" in fields[3].lower() or "error" in fields[3].lower()):
                return fields[3]
    return ""


def select_titles(scan: DiscScan, cfg: Config) -> Selection:
    minlen = int(cfg.get("disc.min_title_seconds", 300))
    ratio = float(cfg.get("disc.ambiguous_ratio", 0.90))

    eligible = sorted(
        (t for t in scan.titles if t.duration_sec >= minlen),
        key=lambda t: t.duration_sec,
        reverse=True,
    )
    if not eligible:
        return Selection(True, "no title longer than the minimum length", None, [], [], [])

    longest = eligible[0]
    near = [t for t in eligible if t.duration_sec >= ratio * longest.duration_sec]

    if len(near) >= 2:
        reason = (
            f"{len(near)} titles within {int(ratio*100)}% of the longest "
            f"({longest.hms}); likely playlist obfuscation or an episodic disc"
        )
        return Selection(True, reason, longest, [], near, eligible)

    return Selection(False, "single dominant title", longest, [longest], [], eligible)
