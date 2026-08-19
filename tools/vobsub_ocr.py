#!/usr/bin/env python3
"""VobSub (DVD bitmap subtitle) -> SRT via tesseract OCR.

DVD subtitles are timed RLE bitmaps, not text, so MP4 can't carry them and they must be OCR'd to
make a sidecar .srt for the Apple direct-play rendition. Pipeline:

    mkvextract tracks SRC <id>:base   ->  base.idx (timestamps+palette) + base.sub (MPEG-PS SPUs)
    parse .idx timestamps/filepos  ->  for each: demux the SPU from .sub, decode the RLE bitmap,
    render a clean black-on-white image, run tesseract, assemble SRT.

Run under a python with numpy + Pillow (the project's torchenv). stdlib + those two only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image


# ---- container: find + extract the VobSub track ------------------------------------------------
def find_vobsub_track(mkvmerge: str, src: str, lang: str) -> int | None:
    out = subprocess.run([mkvmerge, "-J", src], capture_output=True, text=True).stdout
    info = json.loads(out or "{}")
    cand = []
    for t in info.get("tracks", []):
        if t.get("type") != "subtitles":
            continue
        props = t.get("properties", {})
        codec = (t.get("codec", "") + props.get("codec_id", "")).upper()
        if "VOBSUB" not in codec and "S_VOBSUB" not in codec:
            continue
        tlang = (props.get("language_ietf") or props.get("language") or "und").lower()[:3]
        cand.append((tlang, int(t["id"]), props.get("track_name", "")))
    if not cand:
        return None
    for tlang, tid, _ in cand:                       # prefer the requested language
        if tlang == lang[:3]:
            return tid
    return cand[0][1]                                # else the first VobSub track


def extract_vobsub(mkvextract: str, src: str, track_id: int, base: str) -> tuple[str, str]:
    subprocess.run([mkvextract, "tracks", src, f"{track_id}:{base}.idx"],
                   capture_output=True, check=True)
    return f"{base}.idx", f"{base}.sub"


# ---- .idx parsing ------------------------------------------------------------------------------
def parse_idx(path: str) -> tuple[tuple[int, int], list[list[int]], list[tuple[int, int]]]:
    size = (720, 480)
    palette: list[list[int]] = []
    entries: list[tuple[int, int]] = []                # (start_ms, filepos)
    for line in open(path, encoding="latin-1"):
        line = line.strip()
        if line.startswith("size:"):
            w, h = line.split(":", 1)[1].strip().lower().split("x")
            size = (int(w), int(h))
        elif line.startswith("palette:"):
            for hexc in line.split(":", 1)[1].split(","):
                v = int(hexc.strip(), 16)
                palette.append([(v >> 16) & 255, (v >> 8) & 255, v & 255])
        elif line.startswith("timestamp:"):
            m = re.search(r"timestamp:\s*(\d+):(\d+):(\d+):(\d+),\s*filepos:\s*([0-9a-fA-F]+)", line)
            if m:
                h, mi, s, ms = (int(m.group(i)) for i in range(1, 5))
                start = ((h * 60 + mi) * 60 + s) * 1000 + ms
                entries.append((start, int(m.group(5), 16)))
    return size, palette, entries


# ---- .sub demux: assemble one SPU payload starting at filepos -----------------------------------
def read_spu(data: bytes, pos: int) -> bytes:
    buf = bytearray()
    spu_size = None
    n = len(data)
    while pos + 4 <= n:
        if data[pos:pos + 3] != b"\x00\x00\x01":
            break
        sid = data[pos + 3]
        if sid == 0xBA:                               # pack header (14 bytes + stuffing)
            if pos + 14 > n:
                break
            stuffing = data[pos + 13] & 0x07
            pos += 14 + stuffing
            continue
        if pos + 6 > n:
            break
        plen = int.from_bytes(data[pos + 4:pos + 6], "big")
        pkt_end = pos + 6 + plen
        if sid == 0xBD:                               # private_stream_1 = subpictures
            hdr_len = data[pos + 8]
            payload = data[pos + 9 + hdr_len:pkt_end]
            if payload:
                buf += payload[1:]                    # drop the substream-id byte (0x20+n)
                if spu_size is None and len(buf) >= 2:
                    spu_size = int.from_bytes(buf[0:2], "big")
        elif sid in (0xB9, 0xBB):                     # end / system header
            if sid == 0xB9:
                break
        pos = pkt_end
        if spu_size is not None and len(buf) >= spu_size:
            break
    return bytes(buf[:spu_size]) if spu_size else bytes(buf)


# ---- SPU decode: control sequence + 2-field RLE -> palette-index image --------------------------
class _Nibbles:
    def __init__(self, data: bytes, start: int):
        self.data, self.i, self.hi = data, start, True

    def get(self) -> int:
        if self.i >= len(self.data):
            return 0
        b = self.data[self.i]
        if self.hi:
            self.hi = False
            return b >> 4
        self.hi = True
        self.i += 1
        return b & 0x0F

    def align(self) -> None:
        if not self.hi:
            self.hi = True
            self.i += 1


def _run(nb: _Nibbles) -> tuple[int, int]:
    v = nb.get()
    if v < 0x4:
        v = (v << 4) | nb.get()
        if v < 0x10:
            v = (v << 4) | nb.get()
            if v < 0x40:
                v = (v << 4) | nb.get()
    return v >> 2, v & 0x3                             # (count, color); count 0 => rest of line


def decode_spu(spu: bytes) -> tuple[np.ndarray, list[int], int] | None:
    if len(spu) < 4:
        return None
    ctrl = int.from_bytes(spu[2:4], "big")
    alpha = [0, 0xF, 0xF, 0xF]
    area = None
    off = (0, 0)
    duration_ms = 3000
    pos = ctrl
    guard = 0
    while pos + 4 <= len(spu) and guard < 64:
        guard += 1
        delay = int.from_bytes(spu[pos:pos + 2], "big")
        nxt = int.from_bytes(spu[pos + 2:pos + 4], "big")
        p = pos + 4
        stop_seen = False
        while p < len(spu):
            cmd = spu[p]; p += 1
            if cmd == 0xFF:
                break
            if cmd in (0x00, 0x01):
                pass
            elif cmd == 0x02:
                stop_seen = True
            elif cmd == 0x03:
                p += 2                                 # palette mapping (unused: we binarize)
            elif cmd == 0x04:                          # SET_CONTR: nibbles are [c3,c2,c1,c0]
                b = spu[p:p + 2]; p += 2
                alpha = [b[1] & 0xF, b[1] >> 4, b[0] & 0xF, b[0] >> 4]
            elif cmd == 0x05:
                b = spu[p:p + 6]; p += 6
                x1 = (b[0] << 4) | (b[1] >> 4); x2 = ((b[1] & 0xF) << 8) | b[2]
                y1 = (b[3] << 4) | (b[4] >> 4); y2 = ((b[4] & 0xF) << 8) | b[5]
                area = (x1, y1, x2, y2)
            elif cmd == 0x06:
                b = spu[p:p + 4]; p += 4
                off = (int.from_bytes(b[0:2], "big"), int.from_bytes(b[2:4], "big"))
            else:
                break
        if stop_seen:
            duration_ms = int(delay * 1024 / 90.0)     # delay units = 1024/90000 s
        if nxt == pos or nxt < ctrl:
            break
        pos = nxt
    if area is None:
        return None
    x1, y1, x2, y2 = area
    w, h = x2 - x1 + 1, y2 - y1 + 1
    if w <= 0 or h <= 0 or w > 4096 or h > 4096:
        return None
    img = np.zeros((h, w), dtype=np.uint8)             # palette indices 0..3
    for field, start in enumerate(off):
        nb = _Nibbles(spu, start)
        y = field
        while y < h:
            x = 0
            while x < w:
                count, color = _run(nb)
                if count == 0:
                    count = w - x
                count = min(count, w - x)
                if color:
                    img[y, x:x + count] = color
                x += count
            nb.align()
            y += 2
    return img, alpha, max(duration_ms, 500)


def render(img: np.ndarray, alpha: list[int], scale: int = 3) -> Image.Image:
    """Non-transparent pixels -> black glyphs on a white page (ideal for tesseract)."""
    visible = np.zeros(img.shape, dtype=bool)
    for idx in range(4):
        if alpha[idx] > 0:
            visible |= (img == idx)
    page = np.where(visible, 0, 255).astype(np.uint8)
    im = Image.fromarray(page, "L")
    im = im.resize((im.width * scale, im.height * scale), Image.LANCZOS)
    return im


# ---- OCR ---------------------------------------------------------------------------------------
def ocr_image(im: Image.Image, tesseract: str, lang: str) -> str:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp = f.name
    try:
        # pad with a white border; tesseract dislikes glyphs touching the edge
        from PIL import ImageOps
        ImageOps.expand(im, border=20, fill=255).save(tmp)
        r = subprocess.run([tesseract, tmp, "stdout", "-l", lang, "--psm", "6"],
                           capture_output=True, text=True)
        text = r.stdout.strip()
    finally:
        os.unlink(tmp)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    text = text.replace("|", "I").replace("=", "-")
    return text


def ms_to_ts(ms: int) -> str:
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--lang", default="eng")
    ap.add_argument("--mkvmerge", default="mkvmerge")
    ap.add_argument("--mkvextract", default="mkvextract")
    ap.add_argument("--tesseract", default="tesseract")
    ap.add_argument("--jobs", type=int, default=8)
    a = ap.parse_args()

    tid = find_vobsub_track(a.mkvmerge, a.input, a.lang)
    if tid is None:
        print("no VobSub track found", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as td:
        base = os.path.join(td, "sub")
        idx_path, sub_path = extract_vobsub(a.mkvextract, a.input, tid, base)
        size, palette, entries = parse_idx(idx_path)
        data = open(sub_path, "rb").read()
        print(f"vobsub track {tid}: {len(entries)} events, {size[0]}x{size[1]}", file=sys.stderr)

        def work(i: int):
            start, filepos = entries[i]
            end = entries[i + 1][0] if i + 1 < len(entries) else start + 4000
            spu = read_spu(data, filepos)
            dec = decode_spu(spu)
            if not dec:
                return None
            img, alpha, dur = dec
            text = ocr_image(render(img, alpha), a.tesseract, a.lang)
            if not text:
                return None
            return (start, min(end, start + dur), text)

        with ThreadPoolExecutor(max_workers=a.jobs) as ex:
            results = list(ex.map(work, range(len(entries))))

    cues = [r for r in results if r]
    with open(a.output, "w", encoding="utf-8") as fo:
        for n, (start, end, text) in enumerate(cues, 1):
            fo.write(f"{n}\n{ms_to_ts(start)} --> {ms_to_ts(end)}\n{text}\n\n")
    print(f"wrote {len(cues)} cues -> {a.output}", file=sys.stderr)
    return 0 if cues else 3


if __name__ == "__main__":
    sys.exit(main())
