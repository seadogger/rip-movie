# tools/

The `.py` runners here are versioned. The binaries, models, and PyTorch venv are **not** (they're
large and re-downloadable) — rebuild them with the steps below.

## Runners (in git)
- `upscale_torch.py` — spandrel/PyTorch-MPS frame upscaler (.pth models)
- `coreml_infer.py` — CoreML/ANE frame upscaler (.mlpackage) — the fast path
- `enhance_stream.py` — streaming pipeline worker: ffmpeg decode → ANE → OpenCV detail-transfer → ffmpeg encode
- `convert_coreml.py` — convert a spandrel .pth → CoreML .mlpackage (fp16, ANE)
- `profile_compose.py` — per-frame Pareto profiler

## Rebuild the PyTorch venv (Apple Silicon, needs `uv`)
```bash
uv venv --python 3.11 tools/torchenv
uv pip install --python tools/torchenv/bin/python torch spandrel pillow numpy coremltools opencv-python-headless
```

## Download + convert the models
```bash
mkdir -p tools/torch_models
base=https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0
for m in realesr-general-x4v3 realesr-animevideov3; do
  curl -fsL "$base/$m.pth" -o "tools/torch_models/$m.pth"
  tools/torchenv/bin/python tools/convert_coreml.py \
    "tools/torch_models/$m.pth" "tools/torch_models/$m.mlpackage"   # add "1080 1620" to bake in downscale
done
```
`enhance.py` auto-dispatches: a `<engine>.mlpackage` in `torch_models/` → CoreML/ANE (fastest);
else `<engine>.pth` → PyTorch/MPS; else the ncnn-vulkan binary (a slower fallback, download separately).

## Optional ncnn fallback + heavy still-image models
`realesrgan-ncnn-vulkan` (binary from the Real-ESRGAN-ncnn-vulkan releases) plus `.param/.bin` models
(animevideov3 from that repo; ultrasharp/remacri/etc. from the upscayl repo's `resources/models`).
Only needed if you don't use the CoreML/torch path.
