#!/usr/bin/env python
"""Upscale PNG frames with a spandrel-loaded .pth model on Apple MPS.

Mirrors the realesrgan-ncnn-vulkan CLI shape: -i <dir|file> -o <dir|file> -m <model.pth>.
Batches frames per forward pass (--batch) to saturate the GPU — one-frame-at-a-time wastes most
of the time on kernel-launch/sync overhead. Used for compact .pth models (realesr-general-x4v3),
which are fast on MPS unlike the heavy RRDBNet ncnn models.
"""
import argparse
import os
import sys
from pathlib import Path

import numpy as np
import torch
from PIL import Image
from spandrel import ImageModelDescriptor, ModelLoader


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", required=True, help="input dir or file")
    ap.add_argument("-o", required=True, help="output dir or file")
    ap.add_argument("-m", required=True, help="model .pth")
    ap.add_argument("--fp16", action="store_true", help="half precision (faster on MPS)")
    ap.add_argument("--batch", type=int, default=4, help="frames per forward pass")
    args = ap.parse_args()

    device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
    model = ModelLoader().load_from_file(args.m)
    if not isinstance(model, ImageModelDescriptor):
        print("not an image model", file=sys.stderr)
        return 2
    model.to(device).eval()
    dtype = torch.float16 if args.fp16 else torch.float32
    if args.fp16:
        model.model.half()

    is_dir = os.path.isdir(args.i)
    inputs = sorted(Path(args.i).glob("*.png")) if is_dir else [Path(args.i)]
    if is_dir:
        Path(args.o).mkdir(parents=True, exist_ok=True)
    batch = max(1, args.batch)

    for start in range(0, len(inputs), batch):
        group = inputs[start:start + batch]
        arrs = [np.asarray(Image.open(p).convert("RGB")) for p in group]
        x = (torch.from_numpy(np.stack(arrs)).permute(0, 3, 1, 2)
             .to(device=device, dtype=dtype).div(255))
        with torch.no_grad():
            y = model(x).float().clamp(0, 1)
        out = (y.mul(255).round().byte().permute(0, 2, 3, 1).cpu().numpy())
        for p, arr in zip(group, out):
            dst = Path(args.o) / p.name if is_dir else Path(args.o)
            Image.fromarray(arr).save(dst)

    print(f"done {len(inputs)} (batch={batch})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
