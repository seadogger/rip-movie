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
    sar = v.get("sample_aspect_ratio", "1:1")
    sn, sd = (sar.split(":") + ["1"])[:2] if ":" in sar else ("1", "1")
    sar_f = (int(sn) / int(sd)) if sd and int(sd) else 1.0
    return {
        "fps": v.get("r_frame_rate", "24000/1001"),
        "field_order": v.get("field_order", "progressive"),
        "width": int(v.get("width", 0) or 0),
        "height": int(v.get("height", 0) or 0),
        "dar": _dar(v),
        "sar": sar_f or 1.0,
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


def _duration(cfg: Config, path: str) -> float:
    fp = _ffprobe_bin(cfg)
    try:
        out = subprocess.run([fp, "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", path], capture_output=True, text=True,
                             timeout=60).stdout.strip()
        return float(out) if out else 0.0
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return 0.0


def _check_duration(cfg: Config, output_path: str, expected: float,
                    progress: Callable[[str], None]) -> None:
    """A/V-safety net: the rendition's video must match the source duration, or a cadence /
    framerate mistake would desync the audio. Fail loud instead of shipping a broken file."""
    got = _duration(cfg, output_path)
    if expected <= 0 or got <= 0:
        return
    drift = got - expected
    if abs(drift) > 2.5:
        raise EnhanceError(
            f"rendition duration {got:.1f}s vs source {expected:.1f}s (drift {drift:+.1f}s) — "
            f"cadence/framerate bug; audio would desync. Refusing to deliver.")
    progress(f"duration OK: {got:.1f}s vs source {expected:.1f}s (drift {drift:+.1f}s)")


def detect_cadence(cfg: Config, path: str, src_fps: float) -> tuple[str, str, str | None]:
    """Classify source cadence -> (kind, vf_prefix, out_fps). The streaming decode honors the
    container rate (r_frame_rate), so a soft-telecined DVD decodes to 29.97 WITH the 3:2 pulldown
    frames present; `decimate` removes exactly those (uniform 4/5) -> clean 23.976 with duration
    preserved. Only 29.97 sources get IVTC; native 24/25 film is passed through untouched. The
    downstream duration check (enhance) is the safety net if a mixed-cadence disc slips through."""
    eff = _dup_effective_fps(cfg, path)
    film_rate = src_fps > 28.0 and 0 < eff < 27.0        # ~1-in-5 dups => 24fps film in a 30fps stream
    st = _idet_stats(cfg, path)
    combed = st["interlaced"] / max(1, st["total"]) > 0.20
    if film_rate:
        # Telecined film -> PROPER field-matched inverse-telecine, not a blind 1-in-5 drop. fieldmatch
        # reconstructs the original 24 film frames (so the kept frames are the real cadence) and its
        # decimate companion removes the redundant one. Blind `decimate` drops by position and judders
        # on fast pans when the pulldown dups aren't exact.
        return "telecine", "fieldmatch,decimate,", "24000/1001"
    if combed:
        return "interlaced", "bwdif,", None                            # true 29.97i video
    return "progressive", "", None                                     # clean (native 24/25 or 30): keep


def detect_crop(cfg: Config, path: str, w: int, h: int) -> Optional[tuple[int, int, int, int]]:
    """Find the active picture inside baked-in black bars (letterbox/pillarbox/windowbox).

    Samples cropdetect across several timestamps (dodging fades/dark scenes) and takes the most
    common rectangle. Returns (cw, ch, cx, cy) in storage pixels, or None if <5% would be removed
    (i.e. effectively full-frame) so clean transfers are never cropped.
    """
    ff = cfg.get("paths.ffmpeg", "ffmpeg")
    counts: dict[tuple[int, int, int, int], int] = {}
    # Sample points must fall INSIDE the clip: the fixed 120..5000s spots miss entirely on a short
    # clip (e.g. a 60s sample), so fall back to points spread across whatever duration we have.
    dur = _duration(cfg, path)
    points = [t for t in (120, 600, 1500, 3000, 5000) if dur <= 0 or t < dur - 5]
    if not points:
        points = [round(dur * f) for f in (0.15, 0.4, 0.65, 0.85)] if dur > 0 else [1]
    for ss in points:
        try:
            out = subprocess.run(
                [ff, "-hide_banner", "-ss", str(ss), "-i", path, "-vf", "cropdetect=24:2:0",
                 "-frames:v", "200", "-an", "-f", "null", "-"],
                capture_output=True, text=True, timeout=180).stderr
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
        for m in re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", out):
            key = tuple(int(x) for x in m)
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    cw, ch, cx, cy = max(counts.items(), key=lambda kv: kv[1])[0]
    if cw <= 0 or ch <= 0 or cw > w or ch > h:
        return None
    if 1 - (cw * ch) / (w * h) < 0.05:               # <5% removed -> not worth cropping
        return None
    return (cw, ch, cx, cy)


def _fit_dims(disp_ar: float, max_w: int, max_h: int) -> tuple[int, int]:
    """Largest even w x h that fits within (max_w, max_h) at the given display aspect ratio."""
    if disp_ar >= max_w / max_h:                      # wider than the frame -> bound by width
        ow, oh = max_w, round(max_w / disp_ar)
    else:                                             # taller -> bound by height
        ow, oh = round(max_h * disp_ar), max_h
    return ow - (ow % 2), oh - (oh % 2)


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
            sample_start: float = 0.0, mux_audio: bool = True, progress_file: str = "",
            progress: Callable[[str], None] = print) -> dict:
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
    out_h = target_h

    # Auto-crop baked-in black bars (4:3 letterbox / windowbox DVDs) so we upscale the REAL picture,
    # not the bars, and deliver a frame that fills the TV. The model input is fixed-size, so we crop
    # then scale the active picture back to the source WxH for the SR; the true aspect is restored in
    # the final resize (out_w x out_h).
    crop_vf = ""
    autocrop = str(cfg.get("upscale.dvd.autocrop", "auto")).lower()
    crop = detect_crop(cfg, input_path, info["width"], info["height"]) if autocrop != "off" else None
    if crop:
        cw, ch, cx, cy = crop
        crop_vf = f"crop={cw}:{ch}:{cx}:{cy},"
        max_w = int(cfg.get("upscale.dvd.max_width", 1920))
        disp_ar = (cw * info["sar"]) / ch            # true display aspect of the cropped picture
        out_w, out_h = _fit_dims(disp_ar, max_w, target_h)

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
    # Assemble the pre-filter from non-empty parts (any of cadence/crop/denoise may be off) so we
    # never emit a stray/double comma. Crop changes the size -> scale back to the model's input.
    parts = [p for p in (cad_vf.strip(","), crop_vf.strip(","), denoise.strip(",")) if p]
    if crop_vf:
        parts.append(f"scale={info['width']}:{info['height']}")
    vf_pre = ",".join(parts)

    # CoreML path: stream the whole segment through pipes (no PNG round-trip, no chunking).
    if use_coreml and cfg.get("paths.enhance_stream"):
        stream = cfg.path_for("paths.enhance_stream")
        argv = [str(py), str(stream), "--input", input_path, "--output", str(output_path),
                "--model", str(model_file), "--vf", vf_pre, "--out-w", str(out_w),
                "--target-h", str(out_h), "--fps", fps, "--detail", str(detail_strength),
                "--ffmpeg", ff, "--ffprobe", _ffprobe_bin(cfg)]
        if sample_seconds:
            argv += ["--ss", str(offset), "--t", str(total)]
        if not mux_audio:
            argv.append("--no-audio")
        if progress_file:
            argv += ["--progress-file", progress_file]
        progress(f"streaming CoreML/ANE  engine={model} out={out_w}x{out_h} "
                 f"detail={detail_strength} cadence={kind} crop={'yes' if crop_vf else 'no'} "
                 f"fps={fps} range={offset:.0f}..{offset + total:.0f}s")
        _run(argv)
        _check_duration(cfg, str(output_path), total, progress)   # A/V-sync safety net
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
                    f"[0:v]scale={out_w}:{out_h}:flags=lanczos,format=yuv444p[base];"
                    f"[1:v]scale={out_w}:{out_h}:flags=lanczos,format=yuv444p,split[s1][s2];"
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
                      "-vf", f"scale={out_w}:{out_h}:flags=lanczos,setsar=1",
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
