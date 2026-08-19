"""Finalize: mux the upscaled (video-only) file with the source's audio + subtitle tracks -> MKV.

Policy (all-Apple clients + rip-once archival):
- KEEP every wanted source audio track in its ORIGINAL codec (DTS / TrueHD / AC3 / … preserved)
- for any track Apple can't decode (DTS / DTS-HD / TrueHD), ADD an AC3 5.1 transcode
- ADD one AAC stereo track as the universal fallback, and make it the default
- keep only the configured subtitle languages (bitmap DVD/PGS subs — MKV holds them; MP4 can't)
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

from .config import Config
from .naming import _ffprobe_bin

# Apple devices decode these natively; anything else needs an added AC3 for direct play.
_APPLE_AUDIO = {"aac", "ac3", "eac3", "mp3", "alac"}
_INCOMPATIBLE = {"dts", "truehd", "mlp"}   # codec_name 'dts' also covers DTS-HD (via profile)


class FinalizeError(Exception):
    pass


def _streams(cfg: Config, path: str) -> list[dict]:
    out = subprocess.run(
        [_ffprobe_bin(cfg), "-v", "error", "-print_format", "json", "-show_streams", path],
        capture_output=True, text=True, timeout=120)
    return json.loads(out.stdout or "{}").get("streams", [])


def _lang(s: dict) -> str:
    return (s.get("tags", {}).get("language") or "und").lower()


def mux_tracks(cfg: Config, video: str, source: str, out: str,
               progress: Callable[[str], None] = print) -> str:
    ff = cfg.get("paths.ffmpeg", "ffmpeg")
    streams = _streams(cfg, source)
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    a_langs = cfg.get("encode.audio.languages", "all")          # "all" or a list of codes
    s_langs = cfg.get("encode.subtitles.languages", ["eng"])
    keep_a = lambda s: a_langs == "all" or _lang(s) in a_langs or _lang(s) == "und"

    cmd = [ff, "-y", "-i", str(video), "-i", str(source), "-map", "0:v:0", "-c:v", "copy"]
    kept = [(i, s) for i, s in enumerate(audio) if keep_a(s)]
    out_a, default_a = 0, None

    for i, s in kept:                                           # 1. originals, untouched
        cmd += ["-map", f"1:a:{i}", f"-c:a:{out_a}", "copy"]
        out_a += 1
    for i, s in kept:                                           # 2. AC3 for DTS/TrueHD
        if s.get("codec_name", "").lower() in _INCOMPATIBLE:
            cmd += ["-map", f"1:a:{i}", f"-c:a:{out_a}", "ac3", f"-b:a:{out_a}", "640k",
                    f"-metadata:s:a:{out_a}", f"title=AC3 (from {s['codec_name'].upper()})"]
            out_a += 1
    if kept:                                                    # 3. AAC stereo fallback (default)
        cmd += ["-map", f"1:a:{kept[0][0]}", f"-c:a:{out_a}", "aac", f"-ac:a:{out_a}", "2",
                f"-b:a:{out_a}", "256k", f"-metadata:s:a:{out_a}", "title=AAC Stereo"]
        default_a = out_a
        out_a += 1

    out_s = 0                                                   # 4. subtitles (chosen langs)
    for j, s in enumerate(subs):
        if _lang(s) in s_langs:
            cmd += ["-map", f"1:s:{j}", f"-c:s:{out_s}", "copy"]
            out_s += 1

    if default_a is not None:                                   # default = the Apple-safe AAC
        for n in range(out_a):
            cmd += [f"-disposition:a:{n}", "default" if n == default_a else "0"]
    cmd += [str(out)]

    progress(f"mux: {out_a} audio ({len(kept)} kept + AC3-for-DTS + AAC stereo), "
             f"{out_s} sub -> {Path(out).name}")
    p = subprocess.run(cmd, capture_output=True, timeout=7200)
    if p.returncode != 0:
        raise FinalizeError(p.stderr.decode("utf-8", "replace")[-800:] or "mux failed")
    return str(out)
