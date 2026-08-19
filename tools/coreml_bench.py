import sys
import time
from pathlib import Path

import coremltools as ct
import numpy as np
from PIL import Image

pkg, indir = sys.argv[1], Path(sys.argv[2])
frames = sorted(indir.glob("*.png"))[:20]
arrs = [np.asarray(Image.open(p).convert("RGB")).transpose(2, 0, 1)[None].astype(np.float32) / 255
        for p in frames]

units = {
    "ALL (CPU+GPU+ANE)": ct.ComputeUnit.ALL,
    "CPU_AND_NE": ct.ComputeUnit.CPU_AND_NE,
    "CPU_AND_GPU": ct.ComputeUnit.CPU_AND_GPU,
}
tf = 5604.9 * 30000 / 1001
for name, cu in units.items():
    try:
        model = ct.models.MLModel(pkg, compute_units=cu)
        okey = model._spec.description.output[0].name
        model.predict({"x": arrs[0]})  # warmup / compile
        t = time.time()
        for a in arrs:
            model.predict({"x": a})
        d = (time.time() - t) / len(arrs)
        print(f"  {name:20s} {d:.3f} s/frame ({1/d:.2f} fps) -> full movie ~{d*tf/3600:.1f} h")
    except Exception as e:  # noqa: BLE001
        print(f"  {name:20s} FAILED: {str(e)[:120]}")
