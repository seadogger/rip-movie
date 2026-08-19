#!/usr/bin/env python
"""Convert a spandrel .pth SR model to a CoreML ML Program (fp16, ANE-capable).

Bakes a downscale to a fixed output height into the graph so the ANE emits ~1080p directly
instead of 4x=16MP that the CPU then has to shrink. Native (square-pixel) aspect is kept;
the pipeline does the cheap final aspect stretch. argv: <src.pth> <dst.mlpackage> [out_h out_w]
"""
import sys

import coremltools as ct
import torch
from spandrel import ModelLoader

src, dst = sys.argv[1], sys.argv[2]
out_h = int(sys.argv[3]) if len(sys.argv) > 3 else 0
out_w = int(sys.argv[4]) if len(sys.argv) > 4 else 0
H, W = 480, 720  # DVD (NTSC) frame size -> fixed shape, ANE-friendly


class Head(torch.nn.Module):
    def __init__(self, net, oh, ow):
        super().__init__()
        self.net, self.oh, self.ow = net, oh, ow

    def forward(self, x):
        y = self.net(x)
        if self.oh:
            y = torch.nn.functional.interpolate(
                y, size=(self.oh, self.ow), mode="bilinear", align_corners=False, antialias=True)
        return y


net = ModelLoader().load_from_file(src).model.eval().float().cpu()
model = Head(net, out_h, out_w)
with torch.no_grad():
    traced = torch.jit.trace(model, torch.rand(1, 3, H, W))

mlmodel = ct.convert(
    traced,
    inputs=[ct.TensorType(name="x", shape=(1, 3, H, W))],
    compute_units=ct.ComputeUnit.ALL,
    compute_precision=ct.precision.FLOAT16,
    minimum_deployment_target=ct.target.macOS13,
    convert_to="mlprogram",
)
mlmodel.save(dst)
print("outputs:", [o.name for o in mlmodel._spec.description.output], "->", out_h or "native", "h")
print("saved", dst)
