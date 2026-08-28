"""Pull an HD movie and extract diverse HR frames for the distillation prototype dataset."""
import sys, os, subprocess, glob
sys.path.insert(0, "/Users/jason/Desktop/Development/rip-movie")
from ripmovie.cli import _load_secrets; _load_secrets()
from ripmovie.config import Config
from ripmovie import kube
cfg = Config.load("/Users/jason/Desktop/Development/rip-movie/config/rip-movie.toml")
FF = cfg.get("paths.ffmpeg", "ffmpeg")
DATA = "/Users/jason/rip-movie/distill/hr_frames"
os.makedirs(DATA, exist_ok=True)
k = cfg.get("deliver.kubectl", {}); ns = k["nextcloud_namespace"]; ctx = k.get("context")
pod = kube.pod_name(ns, k["nextcloud_pod_selector"], context=ctx); cont = k.get("nextcloud_container")
dp = cfg.require("deliver.kubectl.data_path").rstrip("/")

folder = "The Mummy (1999)"; fname = "The Mummy (1999) - 1080p Microsoft HEVC .mp4"
remote = f"{dp}/Videos/Movies/{folder}/{fname}"
local = "/Users/jason/rip-movie/distill/_src_movie.mp4"

print(f"pulling HD movie: {fname} ...", flush=True)
kube.exec_stdout_file(ns, pod, ["cat", remote], local, container=cont, context=ctx)
print(f"  pulled {os.path.getsize(local)/1e9:.1f} GB", flush=True)

# extract ~1 frame every 3.5s across the whole film (diverse content), high-quality JPEG HR targets
print("extracting HR frames (1 every 3.5s) ...", flush=True)
subprocess.run([FF, "-y", "-i", local, "-vf", "fps=1/3.5,scale=-2:1080",
                "-q:v", "2", os.path.join(DATA, "hr_%05d.jpg")],
               capture_output=True, check=True)
n = len(glob.glob(os.path.join(DATA, "*.jpg")))
print(f"  extracted {n} HR frames -> {DATA}", flush=True)
os.remove(local)
print("  removed source movie (kept frames)", flush=True)
print(f"DONE: {n} HR frames, {sum(os.path.getsize(f) for f in glob.glob(DATA+'/*.jpg'))/1e9:.2f} GB", flush=True)
