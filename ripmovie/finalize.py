"""Finalize the upscaled rendition for Apple direct-play.

The lossless .mkv MASTER (delivered separately) keeps everything untouched — every audio track in its
original codec (DTS/TrueHD included) and the original bitmap subtitles. This module builds the
watchable RENDITION beside it:

    build_rendition -> {Title} - 1080p AVC.mp4   (H.264 + Apple-native audio; guaranteed direct-play)
                       {Title} - 1080p AVC.eng.srt  (English subs, OCR'd from the DVD's bitmap subs)

MP4 can't carry DTS or bitmap subtitles, so DTS/TrueHD are transcoded to AC3 (the untouched original
lives in the master) and the DVD's English VobSub track is OCR'd to a sidecar .srt (tesseract).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable, Optional

from .config import Config
from .naming import _ffprobe_bin

# Apple devices decode these in an MP4 natively; anything else is transcoded to AC3 for the rendition.
_APPLE_AUDIO = {"aac", "ac3", "eac3", "mp3", "alac"}
_BITMAP_SUBS = {"dvd_subtitle", "hdmv_pgs_subtitle", "dvb_subtitle"}
_TEXT_SUBS = {"subrip", "ass", "ssa", "mov_text", "text", "webvtt"}


class FinalizeError(Exception):
    pass


def _streams(cfg: Config, path: str) -> list[dict]:
    out = subprocess.run(
        [_ffprobe_bin(cfg), "-v", "error", "-print_format", "json", "-show_streams", path],
        capture_output=True, text=True, timeout=120)
    return json.loads(out.stdout or "{}").get("streams", [])


def _lang(s: dict) -> str:
    return (s.get("tags", {}).get("language") or "und").lower()


def mux_rendition(cfg: Config, video: str, source: str, out: str,
                  progress: Callable[[str], None] = print) -> str:
    """Mux the upscaled (video-only) file with Apple-native audio -> a direct-play .mp4.

    Audio: keep every wanted source track, copying Apple-native codecs as-is and transcoding
    anything else (DTS/TrueHD/FLAC/PCM) to AC3 5.1; add one AAC stereo track and make it default.
    No subtitles in the container (they ride alongside as an OCR'd .srt).
    """
    ff = cfg.get("paths.ffmpeg", "ffmpeg")
    audio = [s for s in _streams(cfg, source) if s.get("codec_type") == "audio"]
    a_langs = cfg.get("encode.audio.languages", "all")
    keep_a = lambda s: a_langs == "all" or _lang(s) in a_langs or _lang(s) == "und"
    kept = [(i, s) for i, s in enumerate(audio) if keep_a(s)]
    if not kept:                                            # never ship a silent rendition
        kept = list(enumerate(audio))[:1]

    cmd = [ff, "-y", "-i", str(video), "-i", str(source), "-map", "0:v:0", "-c:v", "copy"]
    out_a, transcoded = 0, 0
    for i, s in kept:
        codec = (s.get("codec_name") or "").lower()
        cmd += ["-map", f"1:a:{i}"]
        if codec in _APPLE_AUDIO:
            cmd += [f"-c:a:{out_a}", "copy"]
        else:                                               # DTS/TrueHD/... -> AC3 (master keeps original)
            ch = int(s.get("channels") or 2)
            cmd += [f"-c:a:{out_a}", "ac3", f"-b:a:{out_a}", "640k"]
            if ch > 6:
                cmd += [f"-ac:a:{out_a}", "6"]
            cmd += [f"-metadata:s:a:{out_a}", f"title=AC3 (from {codec.upper()})"]
            transcoded += 1
        out_a += 1
    # universal AAC stereo fallback (default track)
    cmd += ["-map", f"1:a:{kept[0][0]}", f"-c:a:{out_a}", "aac", f"-ac:a:{out_a}", "2",
            f"-b:a:{out_a}", "256k", f"-metadata:s:a:{out_a}", "title=AAC Stereo"]
    default_a = out_a
    out_a += 1
    for n in range(out_a):
        cmd += [f"-disposition:a:{n}", "default" if n == default_a else "0"]
    cmd += ["-movflags", "+faststart", str(out)]

    progress(f"mux: {out_a} audio ({len(kept)} kept, {transcoded} -> AC3, +AAC stereo) -> {Path(out).name}")
    p = subprocess.run(cmd, capture_output=True, timeout=7200)
    if p.returncode != 0:
        raise FinalizeError(p.stderr.decode("utf-8", "replace")[-800:] or "mux failed")
    return str(out)


def make_subtitle_sidecar(cfg: Config, source: str, out_srt: str, lang: str = "eng",
                          progress: Callable[[str], None] = print) -> Optional[str]:
    """Produce an English .srt beside the rendition. Text subs are extracted directly; DVD/PGS
    bitmap subs are OCR'd with tesseract. Returns the .srt path, or None if there are no subs."""
    subs = [s for s in _streams(cfg, source) if s.get("codec_type") == "subtitle"]
    want = [s for s in subs if _lang(s) == lang] or [s for s in subs if _lang(s) in ("und",)]
    if not want:
        progress(f"no {lang} subtitles in source — rendition ships without a sidecar")
        return None
    # prefer an existing TEXT track (perfect, no OCR) over a bitmap track
    want.sort(key=lambda s: 0 if (s.get("codec_name") or "").lower() in _TEXT_SUBS else 1)
    s0 = want[0]
    codec = (s0.get("codec_name") or "").lower()
    sidx = subs.index(s0)
    ff = cfg.get("paths.ffmpeg", "ffmpeg")

    if codec in _TEXT_SUBS:                                 # already text -> just extract
        p = subprocess.run([ff, "-y", "-i", source, "-map", f"0:s:{sidx}", "-c:s", "srt", out_srt],
                           capture_output=True, timeout=600)
        if p.returncode != 0:
            raise FinalizeError("text subtitle extract failed: "
                                + p.stderr.decode("utf-8", "replace")[-400:])
        progress(f"extracted {lang} text subtitles -> {Path(out_srt).name}")
        return out_srt

    if codec in _BITMAP_SUBS:                               # bitmap -> OCR
        py = cfg.path_for("paths.torch_python")
        tool = cfg.path_for("paths.vobsub_ocr")
        argv = [str(py), str(tool), "--input", source, "--output", out_srt, "--lang", lang,
                "--mkvmerge", cfg.get("paths.mkvmerge", "mkvmerge"),
                "--mkvextract", cfg.get("paths.mkvextract", "mkvextract"),
                "--tesseract", cfg.get("paths.tesseract", "tesseract")]
        progress(f"OCR {lang} bitmap subtitles (tesseract) -> {Path(out_srt).name} ...")
        p = subprocess.run(argv, capture_output=True, text=True, timeout=3600)
        if p.returncode != 0 or not Path(out_srt).exists():
            progress(f"  subtitle OCR failed ({p.stderr.strip()[-200:]}); shipping without a sidecar")
            return None
        progress(f"  {p.stderr.strip().splitlines()[-1] if p.stderr.strip() else 'done'}")
        return out_srt

    progress(f"subtitle codec {codec!r} unsupported — skipping sidecar")
    return None
