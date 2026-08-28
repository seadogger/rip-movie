"""Distillation / SR fine-tune harness (torchenv, CPU-safe).

Two data modes:
  synthetic  : HR frames -> on-the-fly DVD-like degradation -> (LR, HR) pairs   [tonight's prototype]
  paired     : pre-made matching LR/HR frame dirs                               [tomorrow: DVD->Proteus]

Student is warm-started from a pretrained Real-ESRGAN (SRVGG) via spandrel.
Loss = L1 + perceptual(VGG19). Saves checkpoints + before/after demo panels.
"""
import os, sys, glob, time, random, argparse, copy
import numpy as np
import cv2
import torch, torch.nn as nn, torch.nn.functional as F

def log(*a):
    print(*a, flush=True)

# ----- degradation: HR patch (RGB float[0,1] HWC) -> LR (÷scale) -----
def degrade(hr, scale=4):
    img = hr.copy()
    sigma = random.uniform(0.4, 2.6)                       # blur
    k = 2 * int(2 * sigma) + 1
    img = cv2.GaussianBlur(img, (k, k), sigma)
    h, w = img.shape[:2]
    interp = random.choice([cv2.INTER_AREA, cv2.INTER_LINEAR, cv2.INTER_CUBIC])
    lr = cv2.resize(img, (w // scale, h // scale), interpolation=interp)   # downscale (aliasing)
    if random.random() < 0.7:                              # noise
        lr = lr + np.random.randn(*lr.shape).astype(np.float32) * (random.uniform(1, 12) / 255.)
    lr = np.clip(lr, 0, 1)
    if random.random() < 0.85:                             # jpeg / mpeg-ish blocking
        q = random.randint(35, 90)
        bgr = (lr[:, :, ::-1] * 255).astype(np.uint8)
        enc = cv2.imencode('.jpg', bgr, [cv2.IMWRITE_JPEG_QUALITY, q])[1]
        lr = cv2.imdecode(enc, cv2.IMREAD_COLOR)[:, :, ::-1].astype(np.float32) / 255.
    return np.ascontiguousarray(np.clip(lr, 0, 1))

def load_rgb(path):
    im = cv2.imread(path, cv2.IMREAD_COLOR)
    return im[:, :, ::-1].astype(np.float32) / 255.        # BGR->RGB [0,1]

def to_t(x):  # HWC -> CHW tensor
    return torch.from_numpy(np.ascontiguousarray(x.transpose(2, 0, 1)))

# ----- dataset -----
class SRDataset(torch.utils.data.Dataset):
    def __init__(self, hr_dir, hr_size=192, scale=4, length=100000,
                 paired_lr=None, paired_hr=None):
        self.scale = scale; self.hr_size = hr_size; self.length = length
        self.paired = bool(paired_lr)
        if self.paired:
            self.lr = sorted(glob.glob(os.path.join(paired_lr, '*')))
            self.hr = sorted(glob.glob(os.path.join(paired_hr, '*')))
            assert len(self.lr) == len(self.hr) and self.lr, "paired dirs mismatch/empty"
        else:
            self.hr = sorted(glob.glob(os.path.join(hr_dir, '*.jpg')) +
                             glob.glob(os.path.join(hr_dir, '*.png')))
            assert self.hr, f"no HR frames in {hr_dir}"
    def __len__(self): return self.length
    def __getitem__(self, i):
        s, hs = self.scale, self.hr_size
        if self.paired:
            j = random.randrange(len(self.hr))
            hr = load_rgb(self.hr[j]); lr = load_rgb(self.lr[j])
            ls = hs // s                                  # LR crop size
            H, W = lr.shape[:2]
            if H < ls or W < ls or hr.shape[0] < H * s or hr.shape[1] < W * s:
                return self.__getitem__(i)                # frame too small / HR not exactly sxLR -> retry
            y = random.randint(0, H - ls); x = random.randint(0, W - ls)
            lrp = lr[y:y + ls, x:x + ls]; hrp = hr[y * s:y * s + hs, x * s:x * s + hs]
            return to_t(lrp), to_t(hrp)
        hr = load_rgb(self.hr[random.randrange(len(self.hr))])
        H, W = hr.shape[:2]
        if H < hs or W < hs:                               # too small -> retry another
            return self.__getitem__(i)
        y = random.randint(0, H - hs); x = random.randint(0, W - hs)
        hrp = hr[y:y + hs, x:x + hs]
        lrp = degrade(hrp, s)
        return to_t(lrp), to_t(hrp)

# ----- perceptual loss (VGG19), optional -----
class Perceptual(nn.Module):
    def __init__(self, dev, layers=16):                   # 16=relu3_3 (cheap), 35=relu5_4 (ESRGAN, heavy)
        super().__init__()
        import torchvision
        from torchvision.models import VGG19_Weights
        vgg = torchvision.models.vgg19(weights=VGG19_Weights.DEFAULT).features[:layers].eval()
        for p in vgg.parameters(): p.requires_grad_(False)
        self.vgg = vgg.to(dev)
        self.register_buffer('mean', torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1).to(dev))
        self.register_buffer('std', torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1).to(dev))
    def forward(self, sr, hr):
        n = lambda x: (x - self.mean) / self.std
        return F.l1_loss(self.vgg(n(sr)), self.vgg(n(hr)))

def load_student(pth, dev):
    from spandrel import ModelLoader
    net = ModelLoader().load_from_file(pth).model.to(dev)
    return net

def make_demo(pretrained, net, ds, dev, path, n=3, scale=4, size=256):
    pretrained.eval(); net.eval()
    rows = []
    with torch.no_grad():
        for _ in range(n):
            lr, hr = ds[0]
            lr = lr.unsqueeze(0).to(dev); hr = hr.unsqueeze(0).to(dev)
            base = F.interpolate(lr, scale_factor=scale, mode='bicubic', align_corners=False).clamp(0, 1)
            pre = pretrained(lr).clamp(0, 1); ft = net(lr).clamp(0, 1)
            row = torch.cat([base, pre, ft, hr], dim=3)     # [LRbicubic | pretrained | finetuned | HR]
            rows.append(row)
    panel = torch.cat(rows, dim=2)[0].cpu().numpy().transpose(1, 2, 0)
    cv2.imwrite(path, (panel[:, :, ::-1] * 255).clip(0, 255).astype(np.uint8))
    net.train()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--hr_dir', default='/Users/jason/rip-movie/distill/hr_frames')
    ap.add_argument('--paired_lr'); ap.add_argument('--paired_hr')
    ap.add_argument('--out', default='/Users/jason/rip-movie/distill/run1')
    ap.add_argument('--model', default='/Users/jason/Desktop/Development/rip-movie/tools/torch_models/realesr-general-x4v3.pth')
    ap.add_argument('--device', default='cpu')
    ap.add_argument('--iters', type=int, default=20000)
    ap.add_argument('--batch', type=int, default=6)
    ap.add_argument('--hr_size', type=int, default=192)
    ap.add_argument('--lr', type=float, default=1e-4)
    ap.add_argument('--perceptual', type=float, default=0.1)
    ap.add_argument('--vgg_layers', type=int, default=16)   # 16=relu3_3 (fast) | 35=relu5_4 (heavy)
    ap.add_argument('--save_every', type=int, default=2000)
    ap.add_argument('--demo_every', type=int, default=1000)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    dev = a.device
    torch.set_num_threads(max(1, os.cpu_count() - 1))

    log(f"[cfg] device={dev} iters={a.iters} batch={a.batch} hr={a.hr_size} lr={a.lr} perc={a.perceptual}")
    net = load_student(a.model, dev); net.train()
    pretrained = load_student(a.model, dev); pretrained.eval()
    for p in pretrained.parameters(): p.requires_grad_(False)

    perc = None
    if a.perceptual > 0:
        try:
            perc = Perceptual(dev, a.vgg_layers); log(f"[loss] L1 + perceptual(VGG19[:{a.vgg_layers}])")
        except Exception as e:
            log(f"[loss] VGG unavailable ({str(e)[:60]}) -> L1 only"); a.perceptual = 0
    if perc is None: log("[loss] L1 only")

    ds = SRDataset(a.hr_dir, a.hr_size, 4, a.iters * a.batch, a.paired_lr, a.paired_hr)
    log(f"[data] {'paired' if ds.paired else 'synthetic'} | {len(ds.hr)} source frames")
    dl = torch.utils.data.DataLoader(ds, batch_size=a.batch, shuffle=False, num_workers=4, drop_last=True)
    opt = torch.optim.Adam(net.parameters(), lr=a.lr, betas=(0.9, 0.99))

    make_demo(pretrained, net, ds, dev, os.path.join(a.out, "demo_000000.png"))
    t0 = time.time(); running = 0.0; it = 0
    for lr, hr in dl:
        lr, hr = lr.to(dev), hr.to(dev)
        sr = net(lr).clamp(0, 1)
        loss = F.l1_loss(sr, hr)
        if perc is not None: loss = loss + a.perceptual * perc(sr, hr)
        opt.zero_grad(); loss.backward(); opt.step()
        running += loss.item(); it += 1
        if it % 50 == 0:
            r = it / max(1e-9, time.time() - t0)
            log(f"[{it}/{a.iters}] loss={running/50:.4f}  {r:.2f} it/s  eta {(a.iters-it)/max(r,1e-9)/3600:.1f}h")
            running = 0.0
        if it % a.demo_every == 0:
            make_demo(pretrained, net, ds, dev, os.path.join(a.out, f"demo_{it:06d}.png"))
        if it % a.save_every == 0:
            torch.save(net.state_dict(), os.path.join(a.out, f"ckpt_{it:06d}.pth"))
            torch.save(net.state_dict(), os.path.join(a.out, "ckpt_latest.pth"))
        if it >= a.iters: break
    torch.save(net.state_dict(), os.path.join(a.out, "ckpt_final.pth"))
    make_demo(pretrained, net, ds, dev, os.path.join(a.out, "demo_final.png"))
    log(f"DONE {it} iters in {(time.time()-t0)/3600:.2f}h -> {a.out}")

if __name__ == '__main__':
    main()
