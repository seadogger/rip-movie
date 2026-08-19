#!/usr/bin/env python
"""Upscale PNG frames with a CoreML ML Program on the Apple Neural Engine.

Mirrors the ncnn/torch runner CLI: -i <dir|file> -o <dir|file> -m <model.mlpackage>.
~6x faster than PyTorch-MPS for compact SR models by using the ANE (CPU_AND_NE).
"""
import argparse
import os
import sys
from pathlib import Path

import coremltools as ct
import numpy as np
from PIL import Image


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", required=True)
    ap.add_argument("-o", required=True)
    ap.add_argument("-m", required=True, help="model .mlpackage")
    ap.add_argument("--units", default="CPU_AND_NE",
                    choices=["ALL", "CPU_AND_NE", "CPU_AND_GPU"])
    args = ap.parse_args()

    model = ct.models.MLModel(args.m, compute_units=getattr(ct.ComputeUnit, args.units))
    ikey = model._spec.description.input[0].name
    okey = model._spec.description.output[0].name

    is_dir = os.path.isdir(args.i)
    inputs = sorted(Path(args.i).glob("*.png")) if is_dir else [Path(args.i)]
    if is_dir:
        Path(args.o).mkdir(parents=True, exist_ok=True)

    for p in inputs:
        a = (np.asarray(Image.open(p).convert("RGB"))
             .transpose(2, 0, 1)[None].astype(np.float32) / 255)
        out = model.predict({ikey: a})[okey]
        arr = (np.clip(out[0], 0, 1).transpose(1, 2, 0) * 255).round().astype(np.uint8)
        dst = Path(args.o) / p.name if is_dir else Path(args.o)
        Image.fromarray(arr).save(dst, compress_level=1)  # throwaway frames: fast write > small size

    print(f"done {len(inputs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
