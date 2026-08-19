#!/usr/bin/env python
"""Streaming upscale: ffmpeg decode -> CoreML/ANE -> detail-transfer -> ffmpeg encode, via pipes.

No PNG round-trip, no chunking, one model load, audio muxed inline. Processes the whole segment
in a single pass. Detail-transfer (grain-extract/merge, luma-only) is done in numpy to match the
ffmpeg version we validated.
"""
import argparse
import json
import os
import queue
import subprocess
import sys
import threading

import coremltools as ct
import cv2
import numpy as np


def probe_dims(ffprobe: str, path: str) -> tuple[int, int]:
    out = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path],
        capture_output=True, text=True).stdout.strip()
    w, h = out.splitlines()[0].split("x")[:2]
    return int(w), int(h)


def _fps_float(x: str) -> float:
    try:
        return float(x.split("/")[0]) / float(x.split("/")[1]) if "/" in x else float(x)
    except (ValueError, ZeroDivisionError):
        return 24.0


def _duration(ffprobe: str, path: str) -> float:
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip()
        return float(out) if out else 0.0
    except (ValueError, subprocess.SubprocessError):
        return 0.0


def _write_progress(path: str, done: int, total: int) -> None:
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(json.dumps({"done": done, "total": total}))
        os.replace(tmp, path)
    except OSError:
        pass


def compose(ai_rgb: np.ndarray, src_rgb: np.ndarray, w: int, h: int, strength: float) -> np.ndarray:
    base = cv2.resize(ai_rgb, (w, h), interpolation=cv2.INTER_AREA)      # downscale AI to target
    if strength <= 0:
        return base
    srcup = cv2.resize(src_rgb, (w, h), interpolation=cv2.INTER_LANCZOS4)
    base_ycc = cv2.cvtColor(base, cv2.COLOR_RGB2YCrCb).astype(np.int16)
    src_y = cv2.cvtColor(srcup, cv2.COLOR_RGB2YCrCb)[:, :, 0]
    blur = cv2.GaussianBlur(src_y, (0, 0), 2)
    hf = src_y.astype(np.int16) - blur.astype(np.int16)                 # high-freq luma texture
    base_ycc[:, :, 0] = np.clip(base_ycc[:, :, 0] + (strength * hf).astype(np.int16), 0, 255)
    return cv2.cvtColor(base_ycc.astype(np.uint8), cv2.COLOR_YCrCb2RGB)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--ss", type=float, default=0.0)
    ap.add_argument("--t", type=float, default=0.0)
    ap.add_argument("--vf", default="")                 # pre-filter (deinterlace + denoise)
    ap.add_argument("--out-w", type=int, required=True)
    ap.add_argument("--target-h", type=int, required=True)
    ap.add_argument("--fps", default="24000/1001")
    ap.add_argument("--detail", type=float, default=0.0)
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--ffprobe", default="ffprobe")
    ap.add_argument("--no-audio", action="store_true", help="video only (audio muxed downstream)")
    ap.add_argument("--progress-file", default="", help="write {done,total} frame progress here")
    a = ap.parse_args()

    sw, sh = probe_dims(a.ffprobe, a.input)
    total_est = 0
    if a.progress_file:
        secs = a.t if a.t else _duration(a.ffprobe, a.input)
        total_est = int(secs * _fps_float(a.fps)) if secs else 0
    model = ct.models.MLModel(a.model, compute_units=ct.ComputeUnit.CPU_AND_NE)
    ikey = model._spec.description.input[0].name
    okey = model._spec.description.output[0].name

    dec = [a.ffmpeg, "-v", "error"]
    if a.ss:
        dec += ["-ss", str(a.ss)]
    dec += ["-i", a.input]
    if a.t:
        dec += ["-t", str(a.t)]
    if a.vf:
        dec += ["-vf", a.vf]
    dec += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]

    enc = [a.ffmpeg, "-v", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{a.out_w}x{a.target_h}", "-framerate", a.fps, "-i", "-"]
    if a.no_audio:
        # video-only: the proper audio/subtitle set is muxed from the source afterward (finalize)
        enc += ["-map", "0:v:0", "-c:v", "libx264", "-crf", "16", "-preset", "medium",
                "-pix_fmt", "yuv420p", a.output]
    else:
        if a.ss:
            enc += ["-ss", str(a.ss)]
        enc += ["-i", a.input]
        if a.t:
            enc += ["-t", str(a.t)]
        enc += ["-map", "0:v:0", "-map", "1:a?", "-c:v", "libx264", "-crf", "16",
                "-preset", "medium", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "256k",
                "-shortest", a.output]

    dp = subprocess.Popen(dec, stdout=subprocess.PIPE)
    ep = subprocess.Popen(enc, stdin=subprocess.PIPE)
    frame_bytes = sw * sh * 3
    q: "queue.Queue" = queue.Queue(maxsize=6)   # bounded so upscaled 16-MP frames don't pile up
    err: dict = {}

    def produce():
        # read source frames + upscale on the ANE; CoreML releases the GIL during inference,
        # so the main thread's compose/encode runs concurrently.
        try:
            while True:
                buf = dp.stdout.read(frame_bytes)
                if len(buf) < frame_bytes:
                    break
                src = np.frombuffer(buf, np.uint8).reshape(sh, sw, 3)
                x = src.transpose(2, 0, 1)[None].astype(np.float32) / 255
                # producer does ONLY predict (CoreML frees the GIL here); all numpy/cv2
                # post-processing happens in the consumer so the two overlap.
                q.put((model.predict({ikey: x})[okey], src))
        except Exception as e:  # noqa: BLE001
            err["produce"] = e
        finally:
            q.put(None)

    prod = threading.Thread(target=produce, daemon=True)
    prod.start()
    n = 0
    try:
        while True:
            item = q.get()
            if item is None:
                break
            ai_raw, src = item
            ai = (np.clip(ai_raw[0], 0, 1).transpose(1, 2, 0) * 255).astype(np.uint8)
            ep.stdin.write(compose(ai, src, a.out_w, a.target_h, a.detail).tobytes())
            n += 1
            if a.progress_file and n % 48 == 0:
                _write_progress(a.progress_file, n, total_est)
    finally:
        dp.stdout.close()
        ep.stdin.close()
        dp.wait()
        ep.wait()
        prod.join()
    if a.progress_file:
        _write_progress(a.progress_file, n, max(total_est, n))
    if "produce" in err:
        raise err["produce"]
    print(f"streamed {n} frames")
    return 0 if ep.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
