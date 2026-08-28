"""VEAI 2.6.4 GUI handoff engine (`upscale.engine = "topaz-veai-handoff"`).

Video Enhance AI 2.6.4 is GUI-only and has no CLI, so it can't be driven headlessly — but it's
watermark-free on an owned license and its Artemis models look great. This engine splits the upscale
around a single manual step so everything except the actual GUI run stays automatic:

  prep    source rip -> IVTC/decimate + autocrop + un-anamorph (square pixels) -> a VIDEO-ONLY
          intermediate (.mov) in the inbox, plus a manifest with the final geometry. Job parks.
  (you)   open Video Enhance AI, drag in every clip sitting in the inbox, run the saved Artemis LQ
          preset (frame interpolation OFF), output folder = the outbox.
  resume  when a size-stable output appears in the outbox, encode it to a 1080p H.264, mux the
          MASTER's original audio (Apple-ified) + OCR'd .eng.srt, deliver, reindex Jellyfin, then
          clean up every artifact (intermediate, Topaz output, manifest, local rip).

Audio and subtitles never go through Topaz — only video round-trips, so sync is guaranteed as long
as Topaz keeps the frame count. The resume duration-check is the safety net if frame interpolation
was left on (it would change the frame count and desync the audio).
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

from .config import Config
from . import status
from .naming import _ffprobe_bin
from .enhance import (
    EnhanceError, _run, _probe, _fps_float, _target_width, _fit_dims,
    detect_cadence, detect_crop, _check_duration, _duration,
)

_VIDEO_EXTS = {".mov", ".mp4", ".mkv", ".m4v", ".avi"}


def _p(cfg: Config, key: str, default: str) -> Path:
    """Config path that may be missing from the table (~ is NOT auto-expanded for these keys)."""
    return Path(cfg.get(key, default)).expanduser()


def handoff_dirs(cfg: Config) -> tuple[Path, Path]:
    """(inbox, outbox), created. Prepped clips land in inbox; VEAI writes finished clips to outbox."""
    inbox = _p(cfg, "upscale.topaz_handoff.inbox", "~/rip-movie/topaz/inbox")
    outbox = _p(cfg, "upscale.topaz_handoff.outbox", "~/rip-movie/topaz/outbox")
    inbox.mkdir(parents=True, exist_ok=True)
    outbox.mkdir(parents=True, exist_ok=True)
    return inbox, outbox


def _even(n: float) -> int:
    n = int(round(n))
    return n - (n % 2)


def _slug(title: str, year) -> str:
    """Recognizable, filesystem-safe intermediate stem — matches the library folder name."""
    base = f"{title} ({year})" if year else str(title)
    return "".join(c for c in base if c not in '/\\:*?"<>|').strip() or "movie"


# NTSC /1001 rates only — disc content is 23.976/29.97 (or 25 PAL); true 24.000/30.000 is virtually
# nonexistent on DVD, and a rounded decode count (23.976 -> 24.0) must land on 24000/1001, not "24".
_STD_FPS = [("24000/1001", 24000 / 1001), ("25", 25.0), ("30000/1001", 30000 / 1001)]


def _snap_fps(fps) -> str:
    """Snap a messy rate (VEAI stamps e.g. 1571924/65535 = 23.9857; a decode count rounds to 24.0)
    to the nearest clean NTSC/PAL standard so the output is exact-CFR and doesn't drift the audio."""
    f = _fps_float(fps) if isinstance(fps, str) else float(fps or 0)
    if f <= 0:
        return "24000/1001"
    return min(_STD_FPS, key=lambda kv: abs(kv[1] - f))[0]


def _target_fps(cfg: Config, veai_output_fps) -> str:
    """Output cadence for the rendition. Default 'source' = the clip's true rate snapped clean
    (23.976 film stays 23.976). '30'/'60' force that rate for smoother 60Hz playback if preferred."""
    mode = str(cfg.get("upscale.topaz_handoff.output_fps", "source")).lower()
    return {"30": "30000/1001", "29.97": "30000/1001", "60": "60000/1001",
            "25": "25", "24": "24000/1001"}.get(mode, _snap_fps(veai_output_fps))


def _model_note(cfg: Config, is_anim: bool) -> str:
    """Which VEAI model to recommend for this clip, by genre. Stamped into the filename so a mixed
    live-action + animation batch is unambiguous in the GUI."""
    if is_anim:
        return str(cfg.get("upscale.topaz_handoff.model_animation", "Gaia CG"))
    # `model_note` kept as a fallback for older configs that pre-date the live/anim split.
    return str(cfg.get("upscale.topaz_handoff.model_live_action",
                       cfg.get("upscale.topaz_handoff.model_note", "Artemis LQ")))


def _true_fps(cfg: Config, path: str, sample_s: int = 20) -> float:
    """The REAL frame rate, by decoding and counting. On soft-telecine DVDs both r_frame_rate AND
    avg_frame_rate lie (the whole container is stamped 29.97), and only decoding reveals the coded
    23.976 progressive frames. VEAI trusts the container rate, so a 29.97-stamped intermediate makes
    it duplicate ~1-in-5 frames to fill 29.97 -> the periodic judder. We stamp the TRUE rate instead."""
    ff = cfg.get("paths.ffmpeg", "ffmpeg")
    try:
        out = subprocess.run(
            [ff, "-hide_banner", "-ss", "120", "-i", path, "-t", str(sample_s), "-an", "-f", "null", "-"],
            capture_output=True, text=True, timeout=180).stderr
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 0.0
    m = re.findall(r"frame=\s*(\d+)", out)
    return (int(m[-1]) / sample_s) if m else 0.0


def _geometry(cfg: Config, source: str) -> dict:
    """Compute the pre-filter + final target geometry, mirroring enhance() but producing a clean
    square-pixel intermediate for Topaz (no 'scale back to model input' trick; no denoise)."""
    info = _probe(cfg, source)
    fps = _snap_fps(_true_fps(cfg, source) or _fps_float(info["fps"]))  # decoded rate, not the lying r_frame_rate

    # cadence: honor explicit config overrides, else auto-classify (idet + duplicate-frame rate)
    deint_cfg = str(cfg.get("upscale.dvd.deinterlace", "auto")).lower()
    ivtc_cfg = str(cfg.get("upscale.dvd.ivtc", "auto")).lower()
    if deint_cfg == "auto" and ivtc_cfg == "auto":
        kind, cad_vf, cad_fps = detect_cadence(cfg, source, _fps_float(fps))
    else:
        kind, cad_vf, cad_fps = "manual", "", None
        if deint_cfg == "yes":
            cad_vf += "bwdif,"
        if ivtc_cfg == "on":
            cad_vf += "decimate,"
            cad_fps = "24000/1001"
    if cad_fps:
        fps = cad_fps

    target_h = int(cfg.get("upscale.dvd.target_height", 1080))
    max_w = int(cfg.get("upscale.dvd.max_width", 1920))

    crop_vf = ""
    autocrop = str(cfg.get("upscale.dvd.autocrop", "auto")).lower()
    crop = detect_crop(cfg, source, info["width"], info["height"]) if autocrop != "off" else None
    if crop:
        cw, ch, cx, cy = crop
        crop_vf = f"crop={cw}:{ch}:{cx}:{cy},"
        disp_ar = (cw * info["sar"]) / ch          # true display aspect of the cropped picture
        inter_h = ch
        out_w, out_h = _fit_dims(disp_ar, max_w, target_h)
    else:
        disp_ar = info["dar"]
        inter_h = info["height"]
        out_w, out_h = _target_width(disp_ar, target_h), target_h

    # Un-anamorph to SQUARE pixels at native height so VEAI (which works on display frames) never
    # has to guess at anamorphic SAR. The final aspect is restored when resume() scales to out_wxh.
    inter_w = _even(inter_h * disp_ar)
    parts = [p for p in (cad_vf.strip(","), crop_vf.strip(",")) if p]
    parts.append(f"scale={inter_w}:{inter_h}:flags=lanczos,setsar=1")
    return {
        "vf": ",".join(parts), "fps": fps, "cadence": kind,
        "crop": bool(crop_vf), "inter_w": inter_w, "inter_h": inter_h,
        "out_w": out_w, "out_h": out_h, "duration": info["duration"],
    }


def prep(cfg: Config, job: dict, progress: Callable[[str], None] = print) -> dict:
    """Build the video-only intermediate for one job and drop it in the inbox. Returns a manifest
    (also carrying the source/title/year/tmdb so resume() can finish without the queue file)."""
    source = job["source"]
    title, year = job["title"], job.get("year")
    if not Path(source).exists():
        raise EnhanceError(f"source rip is gone: {source}")

    inbox, _ = handoff_dirs(cfg)
    g = _geometry(cfg, source)
    is_anim = bool(job.get("is_anim", False))
    model_note = _model_note(cfg, is_anim)                  # Artemis LQ (live) / Gaia CG (animation)
    stem = f"{_slug(title, year)} [{model_note}]"           # tag tells you which VEAI model to run
    codec = str(cfg.get("upscale.topaz_handoff.intermediate", "prores")).lower()
    ext = "mov" if codec == "prores" else "mp4"
    inter = inbox / f"{stem}.{ext}"

    ff = cfg.get("paths.ffmpeg", "ffmpeg")
    if codec == "prores":
        venc = ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le"]
    else:                                          # smaller: near-lossless H.264 for VEAI to ingest
        venc = ["-c:v", "libx264", "-crf", "12", "-preset", "medium", "-pix_fmt", "yuv420p"]

    # Optional windowed sample (cadence/crop are still detected on the FULL source above, so a short
    # clip exercises the same filter graph). Used to smoke-test the handoff before a full VEAI run.
    sample = job.get("sample")
    sstart = float(job.get("sample_start") or 0)
    if sample:
        inp = ["-ss", str(sstart), "-i", str(source), "-t", str(sample)]
        expected = min(float(sample), max(0.0, g["duration"] - sstart))
    else:
        inp = ["-i", str(source)]
        expected = g["duration"]

    # setpts=PTS-STARTPTS zeroes the first timestamp (a windowed `-ss` seek otherwise leaves a large
    # start PTS that makes the MOV muxer write a huge SPARSE file); -dn/-map_chapters drop the stray
    # data/timecode stream so VEAI ingests a clean, purely-video clip.
    vf = g["vf"] + ",setpts=PTS-STARTPTS"
    status.write(cfg, "upscaling", title=title, year=year, stage="prepping for Topaz",
                 started=time.time(), output=str(inter))
    progress(f"[topaz] prepping {stem}  cadence={g['cadence']} crop={'yes' if g['crop'] else 'no'} "
             f"-> {g['inter_w']}x{g['inter_h']} @ {g['fps']} CFR ({ext}), video-only"
             + (f"  [sample {sstart:.0f}..{sstart + float(sample):.0f}s]" if sample else ""))
    # Force the TRUE rate as constant CFR so the intermediate is stamped correctly (not the DVD's
    # lying 29.97 r_frame_rate) — otherwise VEAI duplicates frames to fill 29.97 and the output judders.
    _run([ff, "-y", *inp, "-vf", vf, "-r", g["fps"], "-fps_mode", "cfr",
          "-an", "-dn", "-map", "0:v:0", "-map_chapters", "-1", *venc, str(inter)])
    _check_duration(cfg, str(inter), expected, progress)        # cadence sanity before the handoff

    # Optionally split the prepped clip into ~N-minute segments so a VEAI freeze/leak on a long render
    # only costs one segment (and memory resets between parts). Segments stream-copy out of the intra
    # intermediate, then the whole clip is removed — the manifest carries the ordered segment list.
    chunk_s = _chunk_seconds(cfg)
    segments = None
    if chunk_s > 0 and expected > chunk_s + 5:                  # don't bother splitting a short clip
        segments = _segment_clip(cfg, str(inter), inbox, stem, ext, chunk_s, progress)
        Path(inter).unlink(missing_ok=True)                    # keep only the segments in the inbox

    status.clear(cfg, "upscaling")                              # -> lane flips to the "awaiting" state

    manifest = {
        "source": str(source), "title": title, "year": year,
        "tmdb_id": job.get("tmdb_id"), "is_anim": is_anim,
        "stem": stem, "intermediate": str(inter), "model": model_note,
        "out_w": g["out_w"], "out_h": g["out_h"], "fps": g["fps"],
        "expected_duration": expected, "created": time.time(),
    }
    if segments:
        manifest["segments"] = segments
    _write_howto(cfg)
    if segments:
        progress(f"[topaz] ready: {len(segments)} segments ({stem} p01…p{len(segments):02d}) are in the "
                 f"inbox. Run EACH through Video Enhance AI ({model_note}, output 1920-wide -> outbox), "
                 f"one at a time; the pipeline conforms + stitches them automatically once all are done.")
    else:
        progress(f"[topaz] ready: {inter.name} is in the inbox. Run it through Video Enhance AI "
                 f"({model_note}, output 1920-wide -> outbox); the pipeline finishes it automatically.")
    return manifest


def find_output(cfg: Config, manifest: dict) -> Path | None:
    """A finished, size-stable VEAI output for this movie, or None. VEAI appends a model/preset
    suffix to the name, so we match by stem prefix and ignore the (differently-located) intermediate."""
    _, outbox = handoff_dirs(cfg)
    stem = manifest["stem"]
    settle = float(cfg.get("upscale.topaz_handoff.settle_seconds", 20))
    expected = float(manifest.get("expected_duration", 0) or 0)
    now = time.time()
    cands: list[Path] = []
    for p in outbox.iterdir():
        if not p.is_file() or p.suffix.lower() not in _VIDEO_EXTS:
            continue
        if not p.name.startswith(stem):
            continue
        stt = p.stat()
        if stt.st_size <= 0 or (now - stt.st_mtime) < settle:   # still being written
            continue
        cands.append(p)
    # Only accept a COMPLETE render: readable (mp4 moov atom present -> non-zero duration) AND
    # ~full length. While VEAI is still exporting, the file has no moov (duration 0) or is far too
    # short; skip it and keep polling so we never resume on a partial (a stalled write can hold a
    # steady size through the settle window and otherwise fool us into grabbing a fragment).
    for p in sorted(cands, key=lambda f: -f.stat().st_mtime):
        dur = _duration(cfg, str(p))
        if dur <= 0 or (expected > 0 and dur < expected - 5):
            continue
        return p
    return None


def _chunk_seconds(cfg: Config) -> float:
    """Segment length in seconds (0 = whole movie in one clip)."""
    return float(cfg.get("upscale.topaz_handoff.chunk_minutes", 0) or 0) * 60.0


def _segment_clip(cfg: Config, src: str, out_dir: Path, stem_base: str, ext: str,
                  chunk_s: float, progress: Callable[[str], None] = print) -> list[dict]:
    """Stream-copy an all-intra intermediate into ~chunk_s parts named '{stem_base} pNN.{ext}'.
    ProRes (and near-lossless H.264) cut cleanly at segment boundaries. Returns the segment list
    [{clip, stem, expected_duration}, ...] in play order — the stem is what find_outputs matches on."""
    ff = cfg.get("paths.ffmpeg", "ffmpeg")
    fmt = "mov" if ext == "mov" else "mp4"
    pat = str(out_dir / f"{stem_base} p%02d.{ext}")
    _run([ff, "-y", "-i", str(src), "-map", "0:v:0", "-c", "copy", "-f", "segment",
          "-segment_time", str(chunk_s), "-reset_timestamps", "1", "-segment_start_number", "1",
          "-segment_format", fmt, pat])
    # collect the parts WITHOUT glob — stem_base contains '[Proteus]' and '[...]' is a glob charclass
    prefix = f"{stem_base} p"
    parts = sorted(q for q in out_dir.iterdir()
                   if q.suffix.lower() == f".{ext}" and q.stem.startswith(prefix)
                   and q.stem[len(prefix):].isdigit())
    segs = [{"clip": str(p), "stem": p.stem, "expected_duration": _duration(cfg, str(p))} for p in parts]
    progress(f"[topaz] split into {len(segs)} segment(s) of ~{chunk_s/60:.0f} min each")
    return segs


def find_outputs(cfg: Config, manifest: dict) -> list[Path] | None:
    """Chunked jobs: the COMPLETE VEAI output for every segment, in order — or None if any is still
    missing/incomplete. Same complete-render guard as find_output, applied per segment, so resume only
    fires once the whole movie has been run through the GUI."""
    _, outbox = handoff_dirs(cfg)
    settle = float(cfg.get("upscale.topaz_handoff.settle_seconds", 20))
    now = time.time()
    outs = [p for p in outbox.iterdir() if p.is_file() and p.suffix.lower() in _VIDEO_EXTS]
    results: list[Path] = []
    for seg in manifest["segments"]:
        stem = seg["stem"]
        exp = float(seg.get("expected_duration", 0) or 0)
        found = None
        for p in sorted((q for q in outs if q.name.startswith(stem)), key=lambda f: -f.stat().st_mtime):
            stt = p.stat()
            if stt.st_size <= 0 or (now - stt.st_mtime) < settle:      # still being written
                continue
            dur = _duration(cfg, str(p))
            if dur <= 0 or (exp > 0 and dur < exp - 5):                # partial / no moov yet
                continue
            found = p
            break
        if not found:
            return None
        results.append(found)
    return results


def _capture_pairs(cfg: Config, prep_clip: str, veai_output: str, out_w: int, out_h: int,
                   ocrop, slug: str, progress: Callable[[str], None] = print) -> None:
    """Bank aligned (LR = prep/DVD input, HR = Proteus output) frame pairs for training a distilled
    model that mimics Proteus. Sampled 1-in-N; LR is sized to exactly out/4 so HR = 4x LR (the
    student's fixed scale). Best-effort: any failure is swallowed so it can't break delivery."""
    if not bool(cfg.get("distill.capture_pairs", False)):
        return
    every = int(cfg.get("distill.sample_every", 15))
    pdir = _p(cfg, "distill.pairs_dir", "~/rip-movie/distill/pairs")
    lrd, hrd = pdir / "lr", pdir / "hr"
    lrd.mkdir(parents=True, exist_ok=True); hrd.mkdir(parents=True, exist_ok=True)
    ff = cfg.get("paths.ffmpeg", "ffmpeg")
    lw, lh = round(out_w / 4), round(out_h / 4)          # LR size; HR = 4x this => exact student scale
    hw, hh = lw * 4, lh * 4
    sel = rf"select='not(mod(n\,{every}))'"              # same n on both inputs => aligned frames
    pre = slug.replace(" ", "_")[:40]
    # LR: the prep clip (VEAI's actual input domain), downsized to lw x lh
    _run([ff, "-y", "-i", prep_clip, "-vf", f"{sel},scale={lw}:{lh}:flags=area,setsar=1",
          "-vsync", "0", "-q:v", "2", str(lrd / f"{pre}_%05d.jpg")])
    # HR: the Proteus output, VEAI bars cropped + conformed to hw x hh (matching sampled frames)
    crop = f"crop={ocrop[0]}:{ocrop[1]}:{ocrop[2]}:{ocrop[3]}," if ocrop else ""
    _run([ff, "-y", "-i", veai_output, "-vf", f"{sel},{crop}scale={hw}:{hh}:flags=lanczos,setsar=1",
          "-vsync", "0", "-q:v", "2", str(hrd / f"{pre}_%05d.jpg")])
    n = min(len(list(lrd.glob(f"{pre}_*.jpg"))), len(list(hrd.glob(f"{pre}_*.jpg"))))
    progress(f"[distill] banked {n} training pairs (1-in-{every}) -> {pdir}")


def _conform(cfg: Config, output: str, out_w: int, out_h: int, dst: Path,
             progress: Callable[[str], None] = print, label: str = "") -> tuple | None:
    """Crop any letterbox VEAI re-added (16:9 preset on a scope clip), scale to out_w x out_h, and
    force EXACT constant frame rate -> a clean video-only H.264 at dst. Returns the detected crop (or
    None) so pair-capture reuses the same geometry. The CFR stamp is what keeps A/V from drifting."""
    ff = cfg.get("paths.ffmpeg", "ffmpeg")
    oi = _probe(cfg, str(output))
    ocrop = detect_crop(cfg, str(output), oi["width"], oi["height"])
    vf = ""
    if ocrop:
        cw, ch, cx, cy = ocrop
        vf = f"crop={cw}:{ch}:{cx}:{cy},"
        progress(f"[topaz] {label}VEAI re-added bars -> cropping to {cw}x{ch} then conforming")
    vf += f"scale={out_w}:{out_h}:flags=lanczos,setsar=1,format=yuv420p"
    tgt_fps = _target_fps(cfg, oi["fps"])
    progress(f"[topaz] {label}encoding VEAI output ({oi['width']}x{oi['height']} @ {oi['fps']}) -> "
             f"{out_w}x{out_h} H.264 @ {tgt_fps} CFR ...")
    _run([ff, "-y", "-i", str(output), "-vf", vf, "-r", tgt_fps, "-fps_mode", "cfr",
          "-an", "-dn", "-map", "0:v:0", "-map_chapters", "-1",
          "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p", str(dst)])
    return ocrop


def _resume_setup(cfg: Config, manifest: dict, stage: str):
    """Shared work-path + status boilerplate for resume / resume_chunked."""
    from .pipeline import _remove
    source = manifest["source"]
    work = cfg.path_for("paths.work_dir"); work.mkdir(parents=True, exist_ok=True)
    stem = Path(source).stem
    paths = {"video": work / f"{stem}_up_video.mp4", "final": work / f"{stem}_1080p.mp4",
             "srt": work / f"{stem}_1080p.eng.srt", "work": work, "stem": stem}
    started = time.time()
    progf = cfg.path_for("paths.state_dir") / "status" / "upscale_progress.json"
    progf.parent.mkdir(parents=True, exist_ok=True); _remove(progf)
    status.write(cfg, "upscaling", title=manifest["title"], year=manifest.get("year"),
                 stage=stage, started=started, output=str(paths["final"]), progress_file=str(progf))
    return paths, started, progf


def resume(cfg: Config, manifest: dict, output: str,
           progress: Callable[[str], None] = print, dry_run: bool = False) -> dict:
    """Finish a VEAI-upscaled clip: encode -> 1080p H.264, then mux master audio + OCR'd subs and
    deliver. Cleans the intermediate + Topaz output alongside the normal rendition temps on success.
    dry_run builds the final .mp4 locally but skips delivery + cleanup (used to smoke-test)."""
    from .pipeline import _finalize_rendition
    source = manifest["source"]
    title, year, tmdb_id = manifest["title"], manifest.get("year"), manifest.get("tmdb_id")
    out_w, out_h = manifest["out_w"], manifest["out_h"]
    p, started, progf = _resume_setup(cfg, manifest, "encoding Topaz output → 1080p")

    ocrop = _conform(cfg, str(output), out_w, out_h, p["video"], progress, label=f"{title}: ")
    _check_duration(cfg, str(p["video"]), manifest["expected_duration"], progress)  # A/V-sync safety net

    try:                                                 # tap: bank DVD->Proteus pairs for distillation
        _capture_pairs(cfg, manifest["intermediate"], output, out_w, out_h, ocrop, p["stem"], progress)
    except Exception as e:                               # never let capture break delivery
        progress(f"[distill] pair capture skipped: {e}")

    return _finalize_rendition(
        cfg, source, str(p["video"]), str(p["final"]), str(p["srt"]), title, year, tmdb_id,
        started=started, progf=progf, dry_run=dry_run, keep=False,
        extra_cleanup=(manifest["intermediate"], output), progress=progress)


def resume_chunked(cfg: Config, manifest: dict, outputs: list,
                   progress: Callable[[str], None] = print, dry_run: bool = False) -> dict:
    """Finish a CHUNKED job: conform each segment's VEAI output to the target geometry/CFR, concat the
    parts in order into one video (stream-copy — identical codec/params), then mux the master audio +
    OCR'd subs and deliver. Cleans every segment clip, Topaz output, and part on success."""
    from .pipeline import _finalize_rendition
    source = manifest["source"]
    title, year, tmdb_id = manifest["title"], manifest.get("year"), manifest.get("tmdb_id")
    out_w, out_h = manifest["out_w"], manifest["out_h"]
    segments = manifest["segments"]
    p, started, progf = _resume_setup(cfg, manifest, f"encoding {len(segments)} Topaz segments → 1080p")
    ff = cfg.get("paths.ffmpeg", "ffmpeg")

    parts: list[Path] = []
    for i, (seg, out) in enumerate(zip(segments, outputs)):
        part = p["work"] / f"{p['stem']}_up_p{i:02d}.mp4"
        ocrop = _conform(cfg, str(out), out_w, out_h, part, progress,
                         label=f"{title} p{i + 1:02d}/{len(segments)}: ")
        parts.append(part)
        try:                                             # bank this segment's DVD->Proteus pairs
            _capture_pairs(cfg, seg["clip"], str(out), out_w, out_h, ocrop, f"{p['stem']}_p{i:02d}", progress)
        except Exception as e:
            progress(f"[distill] pair capture skipped (p{i:02d}): {e}")

    # concat the conformed parts — same encoder settings on every part, so a demuxer stream-copy is
    # lossless and instant. (Escape ' for the concat list per ffmpeg's rules.)
    listf = p["work"] / f"{p['stem']}_concat.txt"
    listf.write_text("".join(f"file '{str(pp).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n"
                             for pp in parts))
    progress(f"[topaz] {title}: concatenating {len(parts)} conformed segments -> one rendition")
    _run([ff, "-y", "-f", "concat", "-safe", "0", "-i", str(listf), "-c", "copy",
          "-map", "0:v:0", str(p["video"])])
    _check_duration(cfg, str(p["video"]), manifest["expected_duration"], progress)  # A/V-sync safety net

    extra = (tuple(s["clip"] for s in segments) + tuple(str(o) for o in outputs)
             + tuple(str(pp) for pp in parts) + (str(listf),))
    return _finalize_rendition(
        cfg, source, str(p["video"]), str(p["final"]), str(p["srt"]), title, year, tmdb_id,
        started=started, progf=progf, dry_run=dry_run, keep=False,
        extra_cleanup=extra, progress=progress)


# ---- one-time inbox how-to ----------------------------------------------------------------------
def _write_howto(cfg: Config) -> None:
    inbox, outbox = handoff_dirs(cfg)
    note = inbox / "READ-ME-FIRST.txt"
    if note.exists():
        return
    m_live = _model_note(cfg, False)
    m_anim = _model_note(cfg, True)
    note.write_text(
        "rip-movie — Topaz Video Enhance AI 2.6.4 handoff\n"
        "================================================\n\n"
        "The clips in THIS folder are prepped, video-only masters waiting for one manual step.\n\n"
        "WHICH PRESET to run is stamped into each clip's filename, e.g.\n"
        f"      Armageddon (1998) [{m_live}].mov        <- live action\n"
        f"      WALL-E (2008) [{m_anim}].mov      <- animation\n"
        f"  • Live action  ->  {m_live}\n"
        f"  • Animation    ->  {m_anim}\n"
        "  Both use the PROTEUS model — the ONLY difference is Grain: ON for live action, OFF for\n"
        "  animation (cel/CGI is inherently clean, so grain looks wrong on it). Save two Proteus\n"
        "  presets in VEAI (grained / no-grain) so each batch is just drag + Start.\n\n"
        "SEGMENTED movies: a long film may be split into parts named '… p01', '… p02', … — these are\n"
        "  ONE movie. Run them all (batching is fine; VEAI does them back-to-back and saves each as it\n"
        "  finishes). If a run freezes partway, the parts that already landed in the output folder are\n"
        "  safe — just re-drag the REMAINING parts and Start again. The pipeline waits until every part\n"
        "  is done, then stitches them back into a single rendition.\n\n"
        "One-time setup per model (save each as a preset so future batches are just drag + Start):\n"
        "  • OUTPUT SIZE      : 1080p — set the output to 1920 wide (let Topaz do the FULL upscale).\n"
        "                       These clips are already de-barred + de-anamorphed, so 1920 wide lands\n"
        "                       the correct ~1920x828 scope frame. If only scale factors are offered,\n"
        "                       pick the one nearest 1920 wide; OVERSHOOT is fine (the pipeline\n"
        "                       downscales cleanly) — a flat 2x is the only 'too small' choice.\n"
        "  • Frame rate       : SAME AS INPUT — turn OFF frame interpolation / Chronos\n"
        "                       (interpolation changes the frame count and desyncs the audio;\n"
        "                        the pipeline will refuse a clip whose duration drifted)\n"
        "  • Grain / other    : leave default / off\n"
        "  • Output codec     : ProRes or H.264 High (quality matters, size doesn't — re-encoded)\n"
        f"  • OUTPUT FOLDER    : {outbox}\n\n"
        "Each run:\n"
        "  1. Open Video Enhance AI.\n"
        "  2. Drag in EVERY clip from this inbox folder (batch them — it processes back-to-back).\n"
        "  3. Click Start Processing and walk away.\n\n"
        "As each finished clip lands in the output folder, rip-movie automatically muxes the\n"
        "original audio + English subtitles, delivers it to Nextcloud, refreshes Jellyfin, and\n"
        "deletes the leftover files. Nothing else for you to do.\n"
    )
