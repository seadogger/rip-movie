"""Map a finished video file to the library schema.

Probes the file (codec/resolution/container) and builds:
    Movies/{Title} ({Year})/{Title} ({Year}) - {resTag} {codecTag}.{ext}
Always derives the extension from the real container so a file can't land extensionless.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .config import Config

# ffprobe codec_name -> schema codec tag (matches the existing library's conventions)
_CODEC_TAG = {
    "mpeg2video": "MPEG", "mpeg1video": "MPEG",
    "h264": "AVC", "hevc": "HEVC", "vc1": "Microsoft",
    "av1": "AV1", "vp9": "VP9",
}
_STD_HEIGHTS = [2160, 1440, 1080, 720, 576, 480, 360, 240]
# ffprobe format_name -> canonical container extension
_CONTAINER_EXT = [("matroska", "mkv"), ("mp4", "mp4"), ("mov", "mp4"),
                  ("mpegts", "ts"), ("avi", "avi")]


class NamingError(Exception):
    pass


def _ffprobe_bin(cfg: Config) -> str:
    p = Path(cfg.get("paths.ffmpeg", "ffmpeg"))
    cand = p.with_name("ffprobe")
    return str(cand) if p.name.startswith("ffmpeg") else "ffprobe"


def probe(cfg: Config, path: str) -> dict:
    out = subprocess.run(
        [_ffprobe_bin(cfg), "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    if out.returncode != 0:
        raise NamingError(f"ffprobe failed: {out.stderr.strip()}")
    data = json.loads(out.stdout)
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        raise NamingError("no video stream found")
    return {
        "codec": v.get("codec_name", ""),
        "width": int(v.get("width", 0) or 0),
        "height": int(v.get("height", 0) or 0),
        "format_name": data.get("format", {}).get("format_name", ""),
    }


def res_tag(height: int) -> str:
    h = min(_STD_HEIGHTS, key=lambda s: abs(s - height))
    return f"{h}p"


def codec_tag(codec_name: str) -> str:
    return _CODEC_TAG.get(codec_name, codec_name.upper())


def container_ext(format_name: str, source_path: str) -> str:
    for needle, ext in _CONTAINER_EXT:
        if needle in format_name:
            return ext
    return (Path(source_path).suffix.lstrip(".").lower() or "mkv")


def target(cfg: Config, local_path: str, title: str, year) -> dict:
    """Return the schema folder/filename/relative-path for a source file."""
    info = probe(cfg, local_path)
    restag = res_tag(info["height"])
    codectag = codec_tag(info["codec"])
    ext = container_ext(info["format_name"], local_path)
    folder = f"{title} ({year})" if year else title
    filename = f"{folder} - {restag} {codectag}.{ext}"
    sub = cfg.get("library.movies_subpath", "Videos/Movies").strip("/")
    return {
        "info": info, "restag": restag, "codectag": codectag, "ext": ext,
        "folder": folder, "filename": filename, "rel": f"{sub}/{folder}/{filename}",
    }
