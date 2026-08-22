"""Validate the Nextcloud movie library. Flags, per video file:
  - AUDIO: no English track, or the DEFAULT audio is non-English
  - SUBS:  a non-English subtitle set default/forced (would auto-display)
  - RUNTIME: file duration far from the TMDb runtime (wrong title / decoy playlist ripped)

Streams only each file's ~48 MB header for ffprobe (no full pull), so it's fast. Language checks use
the stream tags; untagged ('und') tracks are reported as unknown rather than failed.
"""
import sys, subprocess, json, re
sys.path.insert(0, "/Users/jason/Desktop/Development/rip-movie")
from ripmovie.cli import _load_secrets; _load_secrets()
from ripmovie.config import Config
from ripmovie import kube

cfg = Config.load("/Users/jason/Desktop/Development/rip-movie/config/rip-movie.toml")
FP = cfg.get("paths.ffmpeg", "ffmpeg").replace("ffmpeg", "ffprobe")
K = cfg.get("deliver.kubectl", {}); NS = K["nextcloud_namespace"]; CTX = K.get("context")
CONT = K.get("nextcloud_container"); DP = cfg.require("deliver.kubectl.data_path").rstrip("/")
POD = kube.pod_name(NS, K["nextcloud_pod_selector"], context=CTX)
MOVIES = f"{DP}/Videos/Movies"
TMDB = cfg.get("identify.tmdb_api_key", "")
EN = ("eng", "en", "english")


def _ls(path):
    try:
        return kube.exec_in(NS, POD, ["ls", "-la", path], container=CONT, context=CTX)
    except Exception:
        return ""


def videos(folder):
    out, vids = _ls(f"{MOVIES}/{folder}"), []
    for line in out.splitlines():
        p = line.split(None, 8)
        if len(p) < 9 or not p[4].isdigit():
            continue
        if p[8].lower().endswith((".mkv", ".mp4", ".m4v", ".avi")):
            vids.append((int(p[4]), p[8]))
    return vids


def _dd(remote, skip_mb, count_mb):
    p = kube.exec_popen(NS, POD, ["dd", f"if={remote}", "bs=1M", f"skip={skip_mb}", f"count={count_mb}"],
                        container=CONT, context=CTX)
    data = p.stdout.read(); p.stdout.close(); p.wait()
    return data


def probe(remote, size=0):
    # 1) header-only probe (works for mkv + fast-start mp4)
    p1 = kube.exec_popen(NS, POD, ["dd", f"if={remote}", "bs=1M", "count=48"], container=CONT, context=CTX)
    r = subprocess.run([FP, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", "-i", "pipe:"],
                       stdin=p1.stdout, capture_output=True, text=True)
    try: p1.stdout.close(); p1.terminate()
    except Exception: pass
    try:
        info = json.loads(r.stdout)
    except Exception:
        info = None
    if info and info.get("streams"):
        return info
    # 2) mp4 with moov-at-end: rebuild a sparse local file (head + tail) so ffprobe can seek to the moov
    if size and remote.lower().endswith((".mp4", ".m4v", ".mov")):
        try:
            tail_skip = max(0, size // 1048576 - 48)       # last ~48 MB (where the moov usually sits)
            head, tail = _dd(remote, 0, 3), _dd(remote, tail_skip, 48)
            import tempfile, os
            tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name
            with open(tmp, "wb") as f:
                f.write(head)
                f.truncate(size)                            # sparse file of the true total size
                f.seek(tail_skip * 1048576); f.write(tail)  # place the tail (moov) at its real offset
            r2 = subprocess.run([FP, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", tmp],
                                capture_output=True, text=True)
            os.unlink(tmp)
            j = json.loads(r2.stdout)
            return j if j.get("streams") else info
        except Exception:
            return info
    return info


def tmdb_runtime(title, year):
    if not TMDB:
        return 0
    import urllib.request, urllib.parse
    try:
        q = urllib.parse.urlencode({"api_key": TMDB, "query": title, "year": year or ""})
        s = json.load(urllib.request.urlopen(f"https://api.themoviedb.org/3/search/movie?{q}", timeout=15))
        results = s.get("results", [])
        if not results:
            return 0
        if year:                                           # prefer the exact-year film (avoid same-name mismatches)
            results = [r for r in results if r.get("release_date", "")[:4] == str(year)] or results
        mid = results[0]["id"]
        d = json.load(urllib.request.urlopen(f"https://api.themoviedb.org/3/movie/{mid}?api_key={TMDB}", timeout=15))
        return int(d.get("runtime") or 0)
    except Exception:
        return 0


def analyze(info, rt_min):
    streams = (info or {}).get("streams", [])
    aud = [s for s in streams if s.get("codec_type") == "audio"]
    sub = [s for s in streams if s.get("codec_type") == "subtitle"]
    lang = lambda s: (s.get("tags", {}) or {}).get("language", "und").lower()
    disp = lambda s, k: s.get("disposition", {}).get(k, 0)
    issues = []
    if not info:
        return ["UNREADABLE: could not probe header"]
    if aud:
        alangs = [lang(s) for s in aud]
        default = next((s for s in aud if disp(s, "default")), aud[0])
        if not any(l in EN for l in alangs):
            issues.append(f"AUDIO no English track — tracks: [{', '.join(alangs)}]")
        elif lang(default) not in EN and lang(default) != "und":
            issues.append(f"AUDIO default track is '{lang(default)}', not English")
    else:
        issues.append("AUDIO no audio stream found")
    for s in sub:
        if (disp(s, "default") or disp(s, "forced")) and lang(s) not in EN and lang(s) != "und":
            issues.append(f"SUBS '{lang(s)}' set {'forced' if disp(s,'forced') else 'default'} — would display")
    dur = float((info.get("format", {}) or {}).get("duration", 0) or 0)
    if dur and rt_min:
        dmin = dur / 60
        if abs(dmin - rt_min) > max(12, 0.15 * rt_min):
            issues.append(f"RUNTIME {dmin:.0f}min vs TMDb {rt_min}min — wrong title/decoy?")
    return issues


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    folders = [f for f in _ls(MOVIES).splitlines()[1:]
               if f.split(None, 8)[-1] not in ("", ".", "..") and f.startswith("d")]
    names = [f.split(None, 8)[8] for f in folders if len(f.split(None, 8)) >= 9]
    if only:
        names = [n for n in names if only.lower() in n.lower()]
    print(f"validating {len(names)} movies ...\n")
    flagged = 0
    for folder in sorted(names):
        m = re.match(r"(.+?) \((\d{4})\)", folder)
        title, year = (m.group(1), int(m.group(2))) if m else (folder, 0)
        rt = tmdb_runtime(title, year)
        for size, name in sorted(videos(folder), reverse=True):
            issues = analyze(probe(f"{MOVIES}/{folder}/{name}", size), rt)
            if issues:
                flagged += 1
                print(f"⚠ {name}")
                for i in issues:
                    print(f"     - {i}")
    print(f"\n{'='*50}\nflagged {flagged} file(s) across {len(names)} movies")


if __name__ == "__main__":
    main()
