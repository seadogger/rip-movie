"""AI upscale stage: deinterlace(if needed) -> denoise/deblock -> Real-ESRGAN -> reassemble.

Runs the movie in CHUNKS: a full film is ~176k frames, and dumping them all as PNGs would need
hundreds of GB. Each chunk extracts its (denoised) frames, upscales them, encodes a lossless-ish
chunk, then deletes the frames before moving on. Chunks are concatenated and the original audio is
muxed back in.

Engine is chosen by genre (config upscale.dvd.engines): animation -> Real-ESRGAN animevideov3,
live-action -> Remacri. The denoise pre-pass is mandatory — upscalers turn MPEG-2 blocks and
mosquito noise into permanent fake "detail" otherwise.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

from .config import Config
from .naming import _ffprobe_bin

# 4x-only ESRGAN models (their scale is fixed regardless of the config scale)
_FIXED_4X = ("remacri", "ultrasharp", "x4plus", "-4x")


class EnhanceError(Exception):
    pass


def _run(argv: list[str], timeout: int = 86400) -> None:
    # default 24h: a full-movie upscale streams for ~8-11h (the old 2h default killed it mid-run)
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise EnhanceError(f"timed out after {timeout}s: {argv[0]}") from e
    if p.returncode != 0:
        raise EnhanceError(p.stderr.decode("utf-8", "replace")[-800:] or f"failed: {argv[0]}")


def _probe(cfg: Config, path: str) -> dict:
    fp = _ffprobe_bin(cfg)
    out = subprocess.run(
        [fp, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=r_frame_rate,field_order,width,height,sample_aspect_ratio,"
         "display_aspect_ratio:format=duration",
         "-print_format", "json", path],
        capture_output=True, text=True, timeout=120,
    )
    if out.returncode != 0:
        raise EnhanceError(f"ffprobe failed: {out.stderr.strip()}")
    d = json.loads(out.stdout)
    v = d["streams"][0]
    return {
        "fps": v.get("r_frame_rate", "24000/1001"),
        "field_order": v.get("field_order", "progressive"),
        "width": int(v.get("width", 0) or 0),
        "height": int(v.get("height", 0) or 0),
        "dar": _dar(v),
        "duration": float(d.get("format", {}).get("duration", 0) or 0),
    }


def _dar(v: dict) -> float:
    """Display aspect ratio as a float, honoring anamorphic (non-square) pixels."""
    dar = v.get("display_aspect_ratio", "")
    if dar and ":" in dar and dar != "0:1":
        n, den = dar.split(":")
        if den and int(den) != 0:
            return int(n) / int(den)
    w, h = int(v.get("width", 0) or 0), int(v.get("height", 0) or 0)
    sar = v.get("sample_aspect_ratio", "1:1")
    sn, sd = (sar.split(":") + ["1"])[:2] if ":" in sar else ("1", "1")
    sar_f = (int(sn) / int(sd)) if sd and int(sd) else 1.0
    return (w / h * sar_f) if h else (16 / 9)


def _target_width(dar: float, height: int) -> int:
    w = round(height * dar)
    return w - (w % 2)  # keep even for yuv420p


def _fps_float(fps: str) -> float:
    try:
        if "/" in fps:
            n, d = fps.split("/")
            return int(n) / int(d)
        return float(fps)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _dup_effective_fps(cfg: Config, path: str, sample_s: int = 10) -> float:
    """Effective rate after dropping duplicate frames — ~24 on 3:2-telecined 24fps film."""
    ff = cfg.get("paths.ffmpeg", "ffmpeg")
    try:
        out = subprocess.run(
            [ff, "-hide_banner", "-ss", "120", "-i", path, "-t", str(sample_s),
             "-vf", "mpdecimate", "-an", "-f", "null", "-"],
            capture_output=True, text=True, timeout=180).stderr
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 0.0
    m = re.findall(r"frame=\s*(\d+)", out)
    return (int(m[-1]) / sample_s) if m else 0.0


def _idet_stats(cfg: Config, path: str, frames: int = 400) -> dict:
    """ffmpeg idet: how many frames are combed (interlaced) vs progressive."""
    ff = cfg.get("paths.ffmpeg", "ffmpeg")
    try:
        out = subprocess.run(
            [ff, "-hide_banner", "-ss", "120", "-i", path, "-vf", "idet",
             "-frames:v", str(frames), "-an", "-f", "null", "-"],
            capture_output=True, text=True, timeout=180).stderr
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {"interlaced": 0, "prog": 0, "total": 0}
    m = re.findall(r"Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*"
                   r"Progressive:\s*(\d+)\s*Undetermined:\s*(\d+)", out)
    if not m:
        return {"interlaced": 0, "prog": 0, "total": 0}
    tff, bff, prog, undet = (int(x) for x in m[-1])
    return {"interlaced": tff + bff, "prog": prog, "total": tff + bff + prog + undet}


def detect_cadence(cfg: Config, path: str, src_fps: float) -> tuple[str, str, str | None]:
    """Classify source cadence -> (kind, vf_prefix, out_fps_or_None). One real detector instead
    of trusting the (lying) stream field_order flag: idet says combed-vs-progressive, the
    duplicate-frame rate says film(24)-vs-video(30)."""
    eff = _dup_effective_fps(cfg, path)
    film_rate = src_fps > 28.0 and 0 < eff < 27.0        # ~1-in-5 dups => 24fps film in a 30fps stream
    st = _idet_stats(cfg, path)
    combed = st["interlaced"] / max(1, st["total"]) > 0.20
    if combed and film_rate:
        return "hard-telecine", "fieldmatch,decimate,", "24000/1001"   # rebuild frames, then 24fps
    if combed:
        return "interlaced", "bwdif,", None                            # true 29.97i video
    if film_rate:
        return "progressive-telecine", "decimate,", "24000/1001"       # dup frames -> 24fps
    return "progressive", "", None                                     # clean, passthrough


def choose_model(cfg: Config, is_animation: bool, pinned: Optional[str] = None) -> str:
    if pinned and pinned != "auto":
        return pinned
    eng = cfg.get("upscale.dvd.engines", {})
    return eng.get("animation" if is_animation else "live_action", "realesr-animevideov3")


def _scale_for(cfg: Config, model: str) -> int:
    if any(tok in model.lower() for tok in _FIXED_4X):
        return 4
    return int(cfg.get("upscale.dvd.scale", 3))


def enhance(cfg: Config, input_path: str, output_path: str, is_animation: bool,
            model: Optional[str] = None, sample_seconds: Optional[float] = None,
            sample_start: float = 0.0, progress: Callable[[str], None] = print) -> dict:
    ff = cfg.get("paths.ffmpeg", "ffmpeg")
    model = choose_model(cfg, is_animation, model or cfg.get("upscale.dvd.engine", "auto"))
    scale = _scale_for(cfg, model)

    # Engine dispatch, fastest first: a <model>.mlpackage in torch_models -> CoreML/ANE runner
    # (~6x); else a <model>.pth -> PyTorch/MPS runner; else the ncnn-vulkan binary (animevideov3).
    tmodels = cfg.path_for("paths.torch_models") if cfg.get("paths.torch_models") else None
    model_ml = (tmodels / f"{model}.mlpackage") if tmodels else None
    model_pth = (tmodels / f"{model}.pth") if tmodels else None
    use_coreml = bool(model_ml and model_ml.exists())
    use_torch = bool(not use_coreml and model_pth and model_pth.exists())
    if use_coreml or use_torch:
        py = cfg.path_for("paths.torch_python")
        runner = cfg.path_for("paths.coreml_infer" if use_coreml else "paths.upscale_torch")
        model_file = model_ml if use_coreml else model_pth
        if not Path(py).exists():
            raise EnhanceError(f"venv python not found: {py}")
    else:
        rbin = cfg.path_for("paths.realesrgan")
        rmodels = cfg.path_for("paths.realesrgan_models")
        if not Path(rbin).exists():
            raise EnhanceError(f"realesrgan binary not found: {rbin}")
    target_h = int(cfg.get("upscale.dvd.target_height", 1080))
    denoise = cfg.get("upscale.dvd.denoise", "hqdn3d=4:3:6:4.5,deband")
    chunk_s = float(cfg.get("upscale.dvd.chunk_seconds", 30))

    info = _probe(cfg, input_path)
    fps = info["fps"]
    out_w = _target_width(info["dar"], target_h)   # honor anamorphic DAR (e.g. 16:9 DVD)
    detail_strength = float(cfg.get("upscale.dvd.detail_transfer", 0.0) or 0.0)
    if sample_seconds:
        offset = float(sample_start or 0)
        total = min(float(sample_seconds), info["duration"] - offset)
    else:
        offset, total = 0.0, info["duration"]

    # cadence: honor explicit config overrides, else auto-classify (idet + duplicate-frame rate)
    deint_cfg = str(cfg.get("upscale.dvd.deinterlace", "auto")).lower()
    ivtc_cfg = str(cfg.get("upscale.dvd.ivtc", "auto")).lower()
    if deint_cfg == "auto" and ivtc_cfg == "auto":
        kind, cad_vf, cad_fps = detect_cadence(cfg, input_path, _fps_float(fps))
    else:
        kind, cad_vf, cad_fps = "manual", "", None
        if deint_cfg == "yes":
            cad_vf += "bwdif,"
        if ivtc_cfg == "on":
            cad_vf += "decimate,"
            cad_fps = "24000/1001"
    if cad_fps:
        fps = cad_fps
    vf_pre = cad_vf + denoise

    # CoreML path: stream the whole segment through pipes (no PNG round-trip, no chunking).
    if use_coreml and cfg.get("paths.enhance_stream"):
        stream = cfg.path_for("paths.enhance_stream")
        argv = [str(py), str(stream), "--input", input_path, "--output", str(output_path),
                "--model", str(model_file), "--vf", vf_pre, "--out-w", str(out_w),
                "--target-h", str(target_h), "--fps", fps, "--detail", str(detail_strength),
                "--ffmpeg", ff, "--ffprobe", _ffprobe_bin(cfg)]
        if sample_seconds:
            argv += ["--ss", str(offset), "--t", str(total)]
        progress(f"streaming CoreML/ANE  engine={model} target={out_w}x{target_h} "
                 f"detail={detail_strength} cadence={kind} fps={fps} "
                 f"range={offset:.0f}..{offset + total:.0f}s")
        _run(argv)
        return {"output": str(output_path), "model": model, "scale": scale,
                "target_height": target_h, "chunks": 0, "mode": "stream"}

    work = cfg.path_for("paths.work_dir") / f"enhance_{Path(input_path).stem[:24]}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    progress(f"engine={model} scale={scale}x target={out_w}x{target_h} "
             f"(DAR {info['dar']:.3f}) cadence={kind} "
             f"range={offset:.0f}s..{offset + total:.0f}s")

    n_chunks = max(1, math.ceil(total / chunk_s))
    chunk_files: list[Path] = []
    try:
        for i in range(n_chunks):
            start = offset + i * chunk_s
            length = min(chunk_s, total - i * chunk_s)
            fin, fout = work / f"in_{i}", work / f"out_{i}"
            fin.mkdir(); fout.mkdir()
            _run([ff, "-y", "-ss", f"{start}", "-i", input_path, "-t", f"{length}",
                  "-vf", vf_pre, "-vsync", "0", f"{fin}/%08d.png"])
            frames = len(list(fin.glob("*.png")))
            if frames == 0:
                break
            if use_coreml or use_torch:
                cmd = [str(py), str(runner), "-i", str(fin), "-o", str(fout), "-m", str(model_file)]
                if use_torch:
                    cmd.append("--fp16")
                _run(cmd)
            else:
                _run([str(rbin), "-i", str(fin), "-o", str(fout),
                      "-n", model, "-s", str(scale), "-m", str(rmodels)])
            chunk_mp4 = work / f"chunk_{i:04d}.mp4"
            if detail_strength > 0:
                # detail-transfer: re-inject the source's high-freq luma texture (weave/grain/
                # pavement) onto the AI frames, which otherwise smooth it away. Both are scaled to
                # the target, the source's high-pass is grain-extracted and merged, luma-only.
                graph = (
                    f"[0:v]scale={out_w}:{target_h}:flags=lanczos,format=yuv444p[base];"
                    f"[1:v]scale={out_w}:{target_h}:flags=lanczos,format=yuv444p,split[s1][s2];"
                    f"[s2]gblur=sigma=2[sb];"
                    f"[s1][sb]blend=all_mode=grainextract[g];"
                    f"[g]lutyuv=y='128+(val-128)*{detail_strength}':u=128:v=128[gs];"
                    f"[base][gs]blend=all_mode=grainmerge,format=yuv420p,setsar=1[out]"
                )
                _run([ff, "-y", "-framerate", fps, "-i", f"{fout}/%08d.png",
                      "-framerate", fps, "-i", f"{fin}/%08d.png",
                      "-filter_complex", graph, "-map", "[out]",
                      "-c:v", "libx264", "-crf", "16", "-preset", "medium",
                      "-pix_fmt", "yuv420p", str(chunk_mp4)])
            else:
                _run([ff, "-y", "-framerate", fps, "-i", f"{fout}/%08d.png",
                      "-vf", f"scale={out_w}:{target_h}:flags=lanczos,setsar=1",
                      "-c:v", "libx264", "-crf", "16", "-preset", "medium",
                      "-pix_fmt", "yuv420p", str(chunk_mp4)])
            chunk_files.append(chunk_mp4)
            shutil.rmtree(fin); shutil.rmtree(fout)
            progress(f"  chunk {i + 1}/{n_chunks} ({frames} frames)")

        if not chunk_files:
            raise EnhanceError("no frames were produced")

        # concat video-only, then mux original audio (trimmed to sample length)
        listf = work / "concat.txt"
        listf.write_text("".join(f"file '{c.name}'\n" for c in chunk_files))
        video_only = work / "video.mp4"
        _run([ff, "-y", "-f", "concat", "-safe", "0", "-i", str(listf),
              "-c", "copy", str(video_only)])
        aud = ["-ss", f"{offset}", "-t", f"{total}", "-i", input_path] if sample_seconds \
            else ["-i", input_path]
        _run([ff, "-y", "-i", str(video_only), *aud,
              "-map", "0:v:0", "-map", "1:a?", "-c:v", "copy", "-c:a", "aac", "-b:a", "256k",
              str(output_path)])
    finally:
        shutil.rmtree(work, ignore_errors=True)

    return {"output": output_path, "model": model, "scale": scale,
            "target_height": target_h, "chunks": len(chunk_files)}
