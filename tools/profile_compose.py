"""Pareto profile of the per-frame cost: predict vs each compose sub-step."""
import sys
import time
from pathlib import Path

import coremltools as ct
import cv2
import numpy as np

model_path, indir, W, H = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
model = ct.models.MLModel(model_path, compute_units=ct.ComputeUnit.CPU_AND_NE)
ikey = model._spec.description.input[0].name
okey = model._spec.description.output[0].name
frames = sorted(Path(indir).glob("*.png"))
srcs = [cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB) for p in frames]

acc = {"predict": 0, "to_uint8": 0, "downscale_16MP": 0, "src_upscale": 0,
       "cvt+blur+merge": 0, "pipe_write_bytes": 0}
model.predict({ikey: srcs[0].transpose(2, 0, 1)[None].astype(np.float32) / 255})  # warmup
N = len(srcs)
for src in srcs:
    x = src.transpose(2, 0, 1)[None].astype(np.float32) / 255
    t = time.time(); raw = model.predict({ikey: x})[okey]; acc["predict"] += time.time() - t
    t = time.time(); ai = (np.clip(raw[0], 0, 1).transpose(1, 2, 0) * 255).astype(np.uint8); acc["to_uint8"] += time.time() - t
    t = time.time(); base = cv2.resize(ai, (W, H), interpolation=cv2.INTER_AREA); acc["downscale_16MP"] += time.time() - t
    t = time.time(); srcup = cv2.resize(src, (W, H), interpolation=cv2.INTER_LANCZOS4); acc["src_upscale"] += time.time() - t
    t = time.time()
    b = cv2.cvtColor(base, cv2.COLOR_RGB2YCrCb).astype(np.int16)
    sy = cv2.cvtColor(srcup, cv2.COLOR_RGB2YCrCb)[:, :, 0]
    hf = sy.astype(np.int16) - cv2.GaussianBlur(sy, (0, 0), 2).astype(np.int16)
    b[:, :, 0] = np.clip(b[:, :, 0] + (0.8 * hf).astype(np.int16), 0, 255)
    out = cv2.cvtColor(b.astype(np.uint8), cv2.COLOR_YCrCb2RGB)
    acc["cvt+blur+merge"] += time.time() - t
    t = time.time(); _ = out.tobytes(); acc["pipe_write_bytes"] += time.time() - t

total = sum(acc.values())
print(f"{'stage':20s} {'ms/frame':>10s} {'share':>7s}")
for k, v in sorted(acc.items(), key=lambda kv: -kv[1]):
    print(f"{k:20s} {v/N*1000:10.1f} {v/total*100:6.1f}%")
print(f"{'TOTAL (serial CPU)':20s} {total/N*1000:10.1f}")
