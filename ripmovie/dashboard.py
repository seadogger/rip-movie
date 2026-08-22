"""Live pipeline dashboard: `rip-movie dashboard` -> http://localhost:8422.

A stdlib HTTP server renders a dark kanban of what the pipeline is doing right now — the disc in
the drive, the active rip, the upscale queue, the upscale in progress (with its sub-stage), recently
finished titles, and Nextcloud/Jellyfin health — plus a search bar over the movie + TV libraries.
The page polls /api/state; state comes from local status files + process inspection, with the slower
cluster checks cached so polling stays cheap.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import status
from .config import Config

# Each movie flows through these stages; the dashboard shows one swimlane per movie.
STAGE_ORDER = ["rip", "master", "upscale", "cleanup", "jellyfin"]


def _lane_stages(current: str, hd: bool = False, queued: bool = False, done: bool = False,
                 manual: bool = False, ready: bool = False) -> dict:
    """State of every stage for a movie whose current position is `current` (the pipeline is
    linear, so earlier stages are done, later ones pending). HD/4K skip the upscale. Colour scheme:
    `manual` = amber (pipeline working on the handoff — fetch/prep/finish), `ready` = blue slow-flash
    (prepped clip sitting in the inbox, waiting on the person to run Topaz)."""
    ci = STAGE_ORDER.index(current)
    out = {}
    for i, s in enumerate(STAGE_ORDER):
        if s == "upscale" and hd:
            out[s] = "skipped"
        elif done or i < ci:
            out[s] = "done"
        elif i == ci:
            out[s] = ("ready" if ready else "manual" if manual else "queued" if queued else "active")
        else:
            out[s] = "pending"
    return out


def _build_lanes(st: dict) -> list[dict]:
    lanes: list[dict] = []
    seen: set = set()

    def add(title, year, current, detail=None, hd=False, queued=False, done=False, manual=False,
            ready=False):
        if not title:
            return
        key = (re.sub(r"[^a-z0-9]", "", title.lower()), year)
        if key in seen:
            return
        seen.add(key)
        lanes.append({"title": title, "year": year, "current": current,
                      "stages": _lane_stages(current, hd, queued, done, manual, ready),
                      "detail": detail or {}})

    r = st.get("ripping")
    if r:
        add(r.get("title"), r.get("year"), "rip",
            {"pct": r.get("pct"), "size": r.get("size"), "elapsed": r.get("elapsed"), "note": r.get("disc")})
    d = st.get("delivering")
    if d:
        add(d.get("title"), d.get("year"), "master", {"elapsed": d.get("elapsed"), "note": "uploading"})
    u = st.get("upscaling")
    if u:
        cur = "cleanup" if re.search("clean", u.get("stage", ""), re.I) else "upscale"
        add(u.get("title"), u.get("year"), cur,
            {"pct": u.get("pct"), "eta": u.get("eta"), "size": u.get("size"),
             "elapsed": u.get("elapsed"), "note": u.get("stage")}, manual=True)  # amber = working
    for a in st.get("awaiting", []):
        add(a.get("title"), a.get("year"), "upscale",
            {"note": "ready — run it through Topaz"}, ready=True)  # blue slow-flash = your turn
    for j in st.get("queue", []):
        add(j.get("title"), j.get("year"), "upscale", {"note": "waiting for ANE"}, queued=True)
    for c in st.get("done", []):
        add(c.get("title"), c.get("year"), "jellyfin", hd=(c.get("kind") == "master"), done=True)
    return lanes

_cache: dict = {}


def _cached(key: str, ttl: float, fn):
    ent = _cache.get(key)
    now = time.time()
    if ent and now - ent[0] < ttl:
        return ent[1]
    val = fn()
    _cache[key] = (now, val)
    return val


def _ps_lines() -> list[str]:
    try:
        return subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True,
                              text=True, timeout=10).stdout.splitlines()
    except Exception:  # noqa: BLE001
        return []


def _running(substr: str, needle: str | None = None) -> bool:
    return any(substr in ln and "grep" not in ln and (needle is None or needle in ln)
               for ln in _ps_lines())


def _newest(path_or_dir: str, pattern: str = "*.mkv") -> int:
    """Size of the most-recently-MODIFIED matching file — i.e. the one currently being written.
    (The rips dir can hold several staged rips at once; the active one has the newest mtime.)"""
    p = Path(path_or_dir)
    files = [p] if p.is_file() else (list(p.glob(pattern)) if p.is_dir() else [])
    if not files:
        return 0
    return max(files, key=lambda f: f.stat().st_mtime).stat().st_size


def _drive(cfg: Config) -> dict:
    try:
        out = subprocess.run(["drutil", "status"], capture_output=True, text=True, timeout=15).stdout
    except Exception:  # noqa: BLE001
        return {"present": False}
    return {"present": ("Type:" in out and "No Media" not in out)}


def _cluster(cfg: Config) -> dict:
    from . import kube
    out = {"library": None, "shows": None, "nextcloud": False, "jellyfin": False}
    k = cfg.get("deliver.kubectl", {})
    ctx = k.get("context")
    try:
        ns = k.get("nextcloud_namespace")
        pod = kube.pod_name(ns, k.get("nextcloud_pod_selector"), context=ctx)
        dp = cfg.require("deliver.kubectl.data_path").rstrip("/")
        mv = cfg.get("library.movies_subpath", "Videos/Movies").strip("/")
        sh = cfg.get("library.shows_subpath", "Videos/TV_Shows").strip("/")
        res = kube.exec_in(ns, pod, ["sh", "-c",
                           f'ls "{dp}/{mv}" 2>/dev/null | wc -l; ls "{dp}/{sh}" 2>/dev/null | wc -l'],
                           container=k.get("nextcloud_container"), context=ctx, timeout=20)
        nums = [int(x) for x in res.split() if x.strip().isdigit()]
        out["library"] = nums[0] if nums else None
        out["shows"] = nums[1] if len(nums) > 1 else None
        out["nextcloud"] = True
    except Exception:  # noqa: BLE001
        pass
    try:
        kube.pod_name(cfg.get("jellyfin.namespace"), cfg.get("jellyfin.pod_selector"), context=ctx)
        out["jellyfin"] = True
    except Exception:  # noqa: BLE001
        pass
    return out


def _recent_pushes(cfg: Config) -> list[dict]:
    """The most-recently-delivered files in the library (a file's mtime = when it was pushed)."""
    from . import kube
    k = cfg.get("deliver.kubectl", {})
    ctx = k.get("context")
    try:
        ns = k.get("nextcloud_namespace")
        pod = kube.pod_name(ns, k.get("nextcloud_pod_selector"), context=ctx)
        dp = cfg.require("deliver.kubectl.data_path").rstrip("/")
        mv = cfg.get("library.movies_subpath", "Videos/Movies").strip("/")
        sh = cfg.get("library.shows_subpath", "Videos/TV_Shows").strip("/")
        cmd = (f'find "{dp}/{mv}" "{dp}/{sh}" -maxdepth 3 -type f '
               r'\( -name "*.mkv" -o -name "*.mp4" -o -name "*.srt" \) '
               r'-printf "%T@\t%f\n" 2>/dev/null | sort -rn | head -12')
        out = kube.exec_in(ns, pod, ["sh", "-c", cmd], container=k.get("nextcloud_container"),
                           context=ctx, timeout=25)
        pushes = []
        for line in out.splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2:
                try:
                    pushes.append({"file": parts[1], "ts": float(parts[0])})
                except ValueError:
                    pass
        return pushes
    except Exception:  # noqa: BLE001
        return []


def gather(cfg: Config) -> dict:
    st: dict = {"now": time.time()}
    st["drive"] = _cached("drive", 8, lambda: _drive(cfg))

    rip = status.read(cfg, "ripping")
    if rip:
        exp = rip.get("expected_bytes") or 0
        size = _newest(rip.get("out_dir", ""))
        rip["size"] = size
        rip["pct"] = min(99, round(100 * size / exp)) if exp else None
        rip["active"] = _running("makemkvcon", " mkv ")
        rip["elapsed"] = int(st["now"] - rip.get("started", st["now"]))
    st["ripping"] = rip

    dv = status.read(cfg, "delivering")
    if dv:
        dv["elapsed"] = int(st["now"] - dv.get("started", st["now"]))
    st["delivering"] = dv

    up = status.read(cfg, "upscaling")
    if up:
        outp = Path(up.get("output", ""))
        up["size"] = outp.stat().st_size if outp.exists() else 0
        up["active"] = _running("enhance_stream.py")
        up["elapsed"] = int(st["now"] - up.get("started", st["now"]))
        pf = up.get("progress_file")
        if pf:
            try:
                pr = json.loads(Path(pf).read_text())
                done, total = pr.get("done", 0), pr.get("total", 0)
                if done > 0 and total > 0:
                    up["pct"] = min(99, round(100 * done / total))
                    rate = done / max(1, up["elapsed"])          # frames / wall-second
                    up["eta"] = int((total - done) / rate) if rate > 0 else None
            except (OSError, ValueError):
                pass
    st["upscaling"] = up

    from .pipeline import list_upscale_jobs, _awaiting_jobs, _upscale_dir
    st["queue"] = [{"title": j["title"], "year": j.get("year")} for j in list_upscale_jobs(cfg)]
    st["awaiting"] = [{"title": j.get("title"), "year": j.get("year")} for j in _awaiting_jobs(cfg)]
    st["failed"] = [f.stem for f in _upscale_dir(cfg).glob("*.failed")]
    st["done"] = status.recent(cfg, 10)
    st["cleaned"] = status.recent_events(cfg, "cleaned", 8)
    st["cluster"] = _cached("cluster", 30, lambda: _cluster(cfg))
    st["pushes"] = _cached("pushes", 30, lambda: _recent_pushes(cfg))
    st["lanes"] = _build_lanes(st)
    return st


def search(cfg: Config, q: str) -> dict:
    from . import library
    q = (q or "").strip()
    if len(q) < 2:
        return {"q": q, "movies": [], "shows": []}
    mtree = _cached("movie_tree", 45, lambda: library.list_movie_tree(cfg))
    stree = _cached("show_tree", 45, lambda: library.list_show_tree(cfg))
    movies = library.search(cfg, q, tree=mtree)[:8]
    shows = library.search_shows(cfg, q, tree=stree)[:8]
    return {
        "q": q,
        "movies": [{"title": r.title, "year": r.year, "best": r.best_height,
                    "directplay": r.has_directplay,
                    "files": [{"name": f.name, "res": f.res, "codec": f.codec,
                               "gib": round(f.size_gib, 1)} for f in r.files]} for r in movies],
        "shows": [{"title": r.title, "year": r.year, "seasons": r.seasons,
                   "episodes": r.episodes} for r in shows],
    }


def library_view(cfg: Config) -> dict:
    """Every library movie classified for the upscale viewer, with live queue state folded in."""
    from . import library
    from .pipeline import list_upscale_jobs, _awaiting_jobs
    tree = _cached("movie_tree", 45, lambda: library.list_movie_tree(cfg))
    cands = library.upscale_candidates(cfg, tree=tree)
    norm = lambda t: re.sub(r"[^a-z0-9]", "", str(t or "").lower())
    queued = {norm(j.get("title")) for j in list_upscale_jobs(cfg)}
    awaiting = {norm(j.get("title")) for j in _awaiting_jobs(cfg)}
    items = []
    for c in cands:
        state = ("awaiting" if norm(c.title) in awaiting else
                 "queued" if norm(c.title) in queued else c.status)
        items.append({"folder": c.folder, "title": c.title, "year": c.year, "best": c.best_height,
                      "codec": c.source_codec, "size": c.size_gib, "status": state})
    counts = {k: sum(1 for c in cands if c.status == k) for k in ("candidate", "done", "hd")}
    counts["total"] = len(cands)
    return {"items": items, "counts": counts}


PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>rip·movie — pipeline</title>
<style>
:root{
 --bg:#0d1117;--card:#161b22;--card2:#1c2330;--line:#2a3038;--line2:#30363d;
 --tx:#e6edf3;--dim:#8b949e;--faint:#6e7681;
 --acc:#58a6ff;--accd:#1f6feb;--purple:#a371f7;
 --ok:#3fb950;--warn:#d29922;--bad:#f85149;
 --st-rip:var(--acc);--st-queue:var(--warn);--st-up:var(--purple);--st-done:var(--ok);--st-fail:var(--bad);
 --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
 --sans:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Helvetica,Arial,sans-serif}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font-family:var(--sans);font-size:14px;line-height:1.45;
 padding:22px 22px 40px;-webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto}
header{display:flex;align-items:center;gap:12px;margin-bottom:16px}
.brand{font-size:19px;font-weight:680;letter-spacing:-.2px}
.brand b{color:var(--acc)} .brand .sub{color:var(--dim);font-weight:400}
.pulse{width:8px;height:8px;border-radius:50%;background:var(--ok);
 box-shadow:0 0 0 0 rgba(63,185,80,.55);animation:pl 2.2s infinite}
@keyframes pl{0%{box-shadow:0 0 0 0 rgba(63,185,80,.5)}70%{box-shadow:0 0 0 7px rgba(63,185,80,0)}100%{box-shadow:0 0 0 0 rgba(63,185,80,0)}}
.live{margin-left:auto;color:var(--dim);font-family:var(--mono);font-size:12px}
.cfglink{color:var(--dim);text-decoration:none;font-size:12px;margin-left:14px}
.cfglink:hover{color:var(--acc)}
/* cluster health strip */
.hbar{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px}
.chip{display:flex;align-items:center;gap:7px;background:var(--card);border:1px solid var(--line);
 border-radius:20px;padding:5px 12px;font-size:12.5px;font-family:var(--mono);color:var(--dim)}
.chip b{color:var(--tx);font-weight:600}
.col .cd{font-size:10px;color:var(--faint);margin:-7px 0 11px;letter-spacing:.2px;font-family:var(--mono)}
/* search */
.search{position:relative;margin-bottom:16px}
.search input{width:100%;background:var(--card);border:1px solid var(--line2);border-radius:10px;
 color:var(--tx);font-family:var(--sans);font-size:14.5px;padding:11px 14px 11px 38px;outline:none}
.search input::placeholder{color:var(--faint)}
.search input:focus{border-color:var(--acc);box-shadow:0 0 0 3px rgba(88,166,255,.15)}
.search .mag{position:absolute;left:13px;top:50%;transform:translateY(-50%);color:var(--faint);font-size:15px}
.results{position:absolute;top:calc(100% + 6px);left:0;right:0;z-index:20;background:var(--card);
 border:1px solid var(--line2);border-radius:12px;box-shadow:0 12px 32px -12px rgba(0,0,0,.7);
 padding:8px;max-height:60vh;overflow:auto;display:none}
.results.on{display:block}
.results .grp{font-size:10.5px;text-transform:uppercase;letter-spacing:.8px;color:var(--dim);
 font-weight:700;padding:8px 8px 5px}
.res{display:flex;align-items:center;gap:10px;padding:8px;border-radius:8px}
.res:hover{background:var(--card2)}
.res .ic{font-size:15px;width:18px;text-align:center;flex:none}
.res .t{font-weight:580}.res .t .yr{color:var(--dim);font-weight:400}
.res .m{color:var(--dim);font-family:var(--mono);font-size:11.5px;margin-top:1px}
.res .tag{margin-left:auto;font-family:var(--mono);font-size:11px;font-weight:600;
 border-radius:6px;padding:2px 8px;white-space:nowrap;flex:none}
.tag.own{background:rgba(63,185,80,.15);color:var(--ok)}
.tag.low{background:rgba(210,153,34,.16);color:var(--warn)}
.res .none{color:var(--faint);font-style:italic;padding:6px 2px}
.flow{color:var(--faint);font-family:var(--mono);font-size:11px;letter-spacing:.2px;margin-bottom:16px;
 display:flex;flex-wrap:wrap;gap:7px;align-items:center}
.flow b{color:var(--dim);font-weight:600}.flow .arw{color:var(--acc);opacity:.55}
/* swimlanes — one per movie */
.legend{display:flex;flex-wrap:wrap;gap:14px;margin-bottom:10px;color:var(--faint);font-size:11px;font-family:var(--mono)}
.legend span{display:flex;align-items:center;gap:6px}
.board{display:flex;flex-direction:column;gap:10px}
.lane{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 15px 14px}
.lane-head{display:flex;align-items:baseline;gap:9px;margin-bottom:12px}
.lane-title{font-weight:640;font-size:14.5px;letter-spacing:-.1px}
.lane-title .yr{color:var(--dim);font-weight:400}
.lane-cur{margin-left:auto;font-family:var(--mono);font-size:11px;font-weight:600;border-radius:20px;padding:2px 10px}
.cur-active{background:rgba(88,166,255,.16);color:var(--acc)}
.cur-queued{background:rgba(210,153,34,.16);color:var(--warn)}
.cur-manual{background:rgba(210,153,34,.16);color:var(--warn)}
.cur-ready{background:rgba(88,166,255,.18);color:var(--acc)}
.cur-done{background:rgba(63,185,80,.15);color:var(--ok)}
.steps{display:flex;align-items:flex-start}
.step{flex:1;min-width:76px;display:flex;flex-direction:column;align-items:center;position:relative;text-align:center}
.step::before{content:"";position:absolute;top:6px;left:-50%;width:100%;height:2px;background:var(--line);z-index:0}
.step:first-child::before{display:none}
.sdot{width:14px;height:14px;border-radius:50%;background:var(--card);border:2px solid var(--line);z-index:1}
.slabel{font-size:10px;font-weight:600;color:var(--dim);margin-top:7px;line-height:1.25}
.sdet{font-family:var(--mono);font-size:9.5px;color:var(--acc);margin-top:2px;line-height:1.2}
.step.done .sdot{background:var(--ok);border-color:var(--ok)}
.step.done .slabel{color:var(--tx)}
.step.done::before{background:var(--ok)}
.step.active .sdot{background:var(--acc);border-color:var(--acc);box-shadow:0 0 0 4px rgba(88,166,255,.18);animation:pl2 1.8s infinite}
.step.active .slabel{color:var(--acc)}
.step.active::before{background:linear-gradient(90deg,var(--ok),var(--acc))}
.step.queued .sdot{background:var(--warn);border-color:var(--warn)}
.step.queued .slabel{color:var(--warn)}.step.queued::before{background:var(--ok)}
.step.manual .sdot{background:var(--warn);border-color:var(--warn);box-shadow:0 0 0 4px rgba(210,153,34,.18);animation:pl3 1.9s infinite}
.step.manual .slabel{color:var(--warn)}.step.manual::before{background:var(--ok)}
@keyframes pl3{0%{box-shadow:0 0 0 0 rgba(210,153,34,.4)}70%{box-shadow:0 0 0 6px rgba(210,153,34,0)}100%{box-shadow:0 0 0 0 rgba(210,153,34,0)}}
/* ready = prepped clip in the inbox, waiting on you -> blue, slow flash */
.step.ready .sdot{background:var(--acc);border-color:var(--acc);animation:pl4 2.6s ease-in-out infinite}
.step.ready .slabel{color:var(--acc);font-weight:700}.step.ready::before{background:var(--ok)}
@keyframes pl4{0%,100%{box-shadow:0 0 0 0 rgba(88,166,255,0);opacity:.55}50%{box-shadow:0 0 0 9px rgba(88,166,255,.12);opacity:1}}
.step.skipped .sdot{border-style:dashed;background:transparent;opacity:.6}
.step.skipped .slabel{color:var(--faint);text-decoration:line-through}
.step.skipped::before,.step.pending::before{background:var(--line)}
@keyframes pl2{0%{box-shadow:0 0 0 0 rgba(88,166,255,.35)}70%{box-shadow:0 0 0 6px rgba(88,166,255,0)}100%{box-shadow:0 0 0 0 rgba(88,166,255,0)}}
.empty{color:var(--faint);font-size:13px;font-style:italic;padding:26px 3px;text-align:center}
/* recent push history */
.pushes{margin-top:16px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 15px 14px}
.pushes h3{font-size:11px;text-transform:uppercase;letter-spacing:.9px;color:var(--dim);font-weight:700;margin-bottom:10px;display:flex;align-items:center;gap:7px}
.pushes .p{display:flex;align-items:center;gap:10px;padding:5px 0;border-bottom:1px solid var(--line);font-size:12.5px}
.pushes .p:last-child{border:0}
.pushes .p .ic{font-size:12px;width:16px;text-align:center;flex:none}
.pushes .p .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pushes .p .ago{margin-left:auto;font-family:var(--mono);font-size:11px;color:var(--faint);flex:none}
.dot{width:8px;height:8px;border-radius:50%;flex:none}
.dot.g{background:var(--ok);box-shadow:0 0 6px rgba(63,185,80,.5)}.dot.r{background:var(--bad)}
footer{margin-top:22px;color:var(--faint);font-family:var(--mono);font-size:11px;display:flex;gap:8px;flex-wrap:wrap}
footer .k{color:var(--dim)}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}
</style></head><body><div class=wrap>
<header><div class=brand>rip<b>·</b>movie <span class=sub>pipeline</span></div>
<span class=pulse></span><div class=live id=ts>connecting…</div>
<a class=cfglink href="/library">📼 upgrade DVDs</a>
<a class=cfglink href="/config">⚙ config</a></header>
<div class=hbar id=health></div>
<div class=search><span class=mag>⌕</span>
 <input id=q type=search autocomplete=off spellcheck=false placeholder="Search your library — movies &amp; TV shows…">
 <div class=results id=results></div></div>
<div class=board id=board></div>
<div class=pushes id=pushes></div>
<footer><span class=k>source</span> MakeMKV<span class=k>· upscale</span> Real-ESRGAN on CoreML/ANE
<span class=k>· deliver</span> Nextcloud → Jellyfin</footer></div>
<script>
const E=(t,c,h)=>{const e=document.createElement(t);if(c)e.className=c;if(h!=null)e.innerHTML=h;return e};
const esc=s=>String(s).replace(/[&<>]/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[m]));
const human=n=>{n=+n||0;const u=["B","KB","MB","GB","TB"];let i=0;while(n>=1024&&i<4){n/=1024;i++}return n.toFixed(1)+" "+u[i]};
const dur=s=>{s=+s||0;const h=s/3600|0,m=(s%3600)/60|0;return h?`${h}h ${m}m`:`${m}m ${(s%60|0)}s`};
const yr=y=>y?` <span class=yr>(${esc(y)})</span>`:"";
const STAGES=[["rip","Rip"],["master","Master → Nextcloud"],["upscale","Upscale"],["cleanup","Cleanup"],["jellyfin","In Jellyfin"]];
function stepDetail(L,k){
 if(L.current!==k)return "";
 const d=L.detail||{},bits=[];
 if(d.pct!=null)bits.push(d.pct+"%");
 if(d.eta!=null)bits.push("ETA "+dur(d.eta));
 if(d.elapsed!=null&&d.pct==null&&d.eta==null)bits.push(dur(d.elapsed));
 if(d.note)bits.push(esc(d.note));
 return bits.length?`<div class=sdet>${bits.join(" · ")}</div>`:"";
}
function lane(L){
 const el=E("div","lane");
 const st=L.stages||{};
 const curState=st[L.current]||"active";
 const badge=curState==="done"?"cur-done":curState==="ready"?"cur-ready":curState==="manual"?"cur-manual":curState==="queued"?"cur-queued":"cur-active";
 const label=L.current==="jellyfin"?"in Jellyfin":curState==="ready"?"ready for Topaz":curState==="manual"?"working":(curState==="queued"?"queued":(STAGES.find(x=>x[0]===L.current)||["","working"])[1]);
 el.append(E("div","lane-head",`<div class=lane-title>${esc(L.title)}${yr(L.year)}</div>
  <div class="lane-cur ${badge}">${esc(label)}</div>`));
 const steps=E("div","steps");
 STAGES.forEach(([k,lab])=>{
  const s=st[k]||"pending";
  steps.append(E("div","step "+s,`<span class=sdot></span><span class=slabel>${lab}</span>${stepDetail(L,k)}`));
 });
 el.append(steps);return el;
}
function render(s){
 const cl=s.cluster||{},H=document.getElementById("health");
 const chip=(l,ok,v)=>`<div class=chip><span class="dot ${ok?'g':'r'}"></span>${l}${v!=null?` <b>${v}</b>`:""}</div>`;
 H.innerHTML=chip("Nextcloud",cl.nextcloud,cl.nextcloud?"online":"down")
  +chip("Jellyfin",cl.jellyfin,cl.jellyfin?"online":"down")
  +chip("Movies",cl.library!=null,cl.library!=null?cl.library:"?")
  +chip("TV shows",cl.shows!=null,cl.shows!=null?cl.shows:"?")
  +(s.drive&&s.drive.present?chip("Disc",true,"inserted"):"");
 const b=document.getElementById("board");b.innerHTML="";
 const lanes=s.lanes||[];
 if(!lanes.length)b.append(E("div","empty","Pipeline idle — insert a disc, or a finished title will appear here."));
 else lanes.forEach(L=>b.append(lane(L)));
 // recent push history (newest delivery first)
 const P=document.getElementById("pushes"),ps=s.pushes||[];
 if(!ps.length){P.innerHTML="";}
 else{
  const ago=t=>{const d=(Date.now()/1000-t)|0;return d<60?d+"s ago":d<3600?(d/60|0)+"m ago":d<86400?(d/3600|0)+"h ago":(d/86400|0)+"d ago"};
  const ic=f=>/\.srt$/i.test(f)?"💬":/\.mp4$/i.test(f)?"🎞️":"💿";
  P.innerHTML="<h3><span>📤</span>Recent pushes → Jellyfin</h3>"+
   ps.map(p=>`<div class=p><span class=ic>${ic(p.file)}</span><span class=nm>${esc(p.file)}</span><span class=ago>${ago(p.ts)}</span></div>`).join("");
 }
}
// search
const R=document.getElementById("results"),Q=document.getElementById("q");let tmr;
function resRow(icon,title,year,meta,tag){
 return `<div class=res><span class=ic>${icon}</span><div><div class=t>${esc(title)}${yr(year)}</div>${meta?`<div class=m>${meta}</div>`:""}</div>${tag||""}</div>`;
}
function showResults(d){
 if(!d.q){R.classList.remove("on");R.innerHTML="";return}
 let html="";
 html+='<div class=grp>Movies</div>';
 if(d.movies.length)d.movies.forEach(m=>{
   const low=(m.best&&m.best<1080)||!m.directplay;
   const f=m.files[0]||{};
   const tag=low?`<span class="tag low">${m.best?m.best+"p":""} — upscale</span>`:`<span class="tag own">in library</span>`;
   html+=resRow("🎬",m.title,m.year,`${m.files.length} file${m.files.length>1?"s":""}${f.res?" · "+f.res+" "+(f.codec||""):""}`,tag);});
 else html+='<div class=none>no movie match</div>';
 html+='<div class=grp>TV Shows</div>';
 if(d.shows.length)d.shows.forEach(s=>html+=resRow("📺",s.title,s.year,`${s.seasons} season${s.seasons!=1?"s":""} · ${s.episodes} episode${s.episodes!=1?"s":""}`,`<span class="tag own">in library</span>`));
 else html+='<div class=none>no TV match</div>';
 R.innerHTML=html;R.classList.add("on");
}
Q.addEventListener("input",()=>{clearTimeout(tmr);const q=Q.value.trim();
 if(q.length<2){R.classList.remove("on");return}
 tmr=setTimeout(async()=>{try{showResults(await(await fetch("api/search?q="+encodeURIComponent(q))).json())}catch(e){}},220);});
document.addEventListener("click",e=>{if(!e.target.closest(".search"))R.classList.remove("on")});
async function tick(){
 let s;try{s=await(await fetch("api/state")).json()}catch(e){document.getElementById("ts").textContent="offline — retrying";return}
 document.getElementById("ts").textContent="live · "+new Date().toLocaleTimeString();render(s);
}
tick();setInterval(tick,2500);
</script></body></html>"""


CONFIG_PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>rip·movie — config</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--card2:#1c2330;--line:#2a3038;--line2:#30363d;--tx:#e6edf3;
 --dim:#8b949e;--faint:#6e7681;--acc:#58a6ff;--ok:#3fb950;--bad:#f85149;--warn:#d29922;
 --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
 --sans:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Helvetica,Arial,sans-serif}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font-family:var(--sans);font-size:14px;padding:22px 22px 60px}
.wrap{max-width:960px;margin:0 auto}
header{display:flex;align-items:center;gap:14px;margin-bottom:6px}
.brand{font-size:19px;font-weight:680}.brand b{color:var(--acc)}.brand .sub{color:var(--dim);font-weight:400}
a.back{color:var(--acc);text-decoration:none;font-size:13px}
.tools{margin-left:auto;display:flex;gap:10px;align-items:center}
button{background:var(--card2);color:var(--tx);border:1px solid var(--line2);border-radius:8px;
 padding:7px 14px;font:inherit;font-size:13px;font-weight:600;cursor:pointer}
button:hover{border-color:var(--acc)}button:disabled{opacity:.6;cursor:default}
.note{color:var(--dim);font-size:12px;margin:6px 0 18px}
.sec{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:6px 16px 12px;margin-bottom:14px}
.sec>h2{font-family:var(--mono);font-size:12px;color:var(--acc);font-weight:700;letter-spacing:.3px;
 padding:12px 0 4px;border-bottom:1px solid var(--line);margin-bottom:6px}
.row{display:grid;grid-template-columns:210px 1fr;gap:10px 16px;padding:10px 0;border-bottom:1px solid var(--line);align-items:start}
.row:last-child{border:0}
.k{font-family:var(--mono);font-size:13px;font-weight:600;padding-top:7px;word-break:break-word}
.k .st{display:inline-block;font-size:9px;font-weight:700;color:var(--warn);background:rgba(210,153,34,.15);
 border-radius:4px;padding:1px 5px;margin-left:6px;text-transform:uppercase;letter-spacing:.4px;vertical-align:middle}
.val{display:flex;flex-direction:column;gap:5px}
.inp{width:100%;max-width:520px;background:var(--card2);border:1px solid var(--line2);border-radius:8px;
 color:var(--tx);font:inherit;font-family:var(--mono);font-size:13px;padding:8px 11px;outline:none}
.inp:focus{border-color:var(--acc);box-shadow:0 0 0 3px rgba(88,166,255,.14)}
input[type=checkbox].inp{width:auto;max-width:none;transform:scale(1.25);cursor:pointer;accent-color:var(--acc)}
.cmt{color:var(--dim);font-size:11.5px;line-height:1.4;max-width:560px}
.badge{font-family:var(--mono);font-size:11px;grid-column:2;color:var(--faint)}
.badge.ok{color:var(--ok)}.badge.bad{color:var(--bad)}
.row.saved{background:rgba(63,185,80,.06)}.row.err{background:rgba(248,81,73,.08)}
.row{transition:background .3s}
</style></head><body><div class=wrap>
<header><div class=brand>rip<b>·</b>movie <span class=sub>config</span></div>
<div class=tools><a class=back href="/">← dashboard</a><button id=test>Run tests</button></div></header>
<div class=note>Edits save on change and are written back to <code>rip-movie.toml</code> (comments preserved).
Secrets are stored in <code>secrets.env</code>. Some changes (ports, daemons) apply on the next run.</div>
<div id=cfg></div></div>
<script>
const E=(t,c,h)=>{const e=document.createElement(t);if(c)e.className=c;if(h!=null)e.innerHTML=h;return e};
const esc=s=>String(s).replace(/[&<>"]/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[m]));
const cid=s=>"b_"+String(s).replace(/[^a-z0-9]/gi,"_");
function row(e){
 const r=E("div","row");
 r.append(E("div","k",esc(e.key)+(e.secret?' <span class=st>secret</span>':'')));
 const val=E("div","val");let inp;
 if(e.type==="bool"){inp=E("input");inp.type="checkbox";inp.checked=e.value===true;}
 else if(e.type==="int"||e.type==="float"){inp=E("input");inp.type="number";inp.value=e.value;if(e.type==="float")inp.step="any";}
 else if(e.type==="secret"){inp=E("input");inp.type="password";inp.placeholder=e.secret_set?"•••••••• (set — blank keeps it)":"not set";}
 else if(e.type==="list"){inp=E("input");inp.type="text";inp.value=(e.value||[]).join(", ");}
 else{inp=E("input");inp.type="text";inp.value=e.value;}
 inp.className="inp";inp.addEventListener("change",()=>save(e,inp,r));
 val.append(inp);
 if(e.comment)val.append(E("div","cmt",esc(e.comment)));
 const badge=E("div","badge");badge.id=cid(e.dotted);
 r.append(val);r.append(badge);return r;
}
async function save(e,inp,r){
 let v;
 if(e.type==="bool")v=inp.checked;
 else if(e.type==="secret"){if(!inp.value)return;v=inp.value;}
 else v=inp.value;
 let ok=false;try{ok=(await(await fetch("api/config",{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify({key:e.dotted,value:v})})).json()).ok;}catch(x){}
 r.classList.remove("saved","err");void r.offsetWidth;r.classList.add(ok?"saved":"err");
 setTimeout(()=>r.classList.remove("saved","err"),1400);
 if(e.type==="secret"&&ok){inp.value="";inp.placeholder="•••••••• (set — blank keeps it)";}
}
async function load(){
 const ents=await(await fetch("api/config")).json();
 const bySec={};ents.forEach(e=>(bySec[e.section]=bySec[e.section]||[]).push(e));
 const root=document.getElementById("cfg");root.innerHTML="";
 Object.keys(bySec).forEach(sec=>{
  const c=E("div","sec");c.append(E("h2",null,esc(sec||"(root)")));
  bySec[sec].forEach(e=>c.append(row(e)));root.append(c);
 });
}
document.getElementById("test").addEventListener("click",async()=>{
 const btn=document.getElementById("test");btn.textContent="Testing…";btn.disabled=true;
 try{const res=await(await fetch("api/config/test")).json();
  Object.entries(res).forEach(([k,v])=>{const b=document.getElementById(cid(k));
   if(b){b.className="badge "+(v.ok?"ok":"bad");b.textContent=(v.ok?"✓ ":"✗ ")+v.detail;}});
 }catch(x){}
 btn.textContent="Run tests";btn.disabled=false;
});
load();
</script></body></html>"""


LIBRARY_PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>rip·movie — upgrade DVDs</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--card2:#1c2330;--line:#2a3038;--line2:#30363d;--tx:#e6edf3;
 --dim:#8b949e;--faint:#6e7681;--acc:#58a6ff;--purple:#a371f7;--ok:#3fb950;--bad:#f85149;--warn:#d29922;
 --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
 --sans:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Helvetica,Arial,sans-serif}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font-family:var(--sans);font-size:14px;padding:22px 22px 60px;-webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto}
header{display:flex;align-items:center;gap:14px;margin-bottom:6px}
.brand{font-size:19px;font-weight:680}.brand b{color:var(--acc)}.brand .sub{color:var(--dim);font-weight:400}
a.back{color:var(--acc);text-decoration:none;font-size:13px;margin-left:auto}
.note{color:var(--dim);font-size:12.5px;margin:8px 0 16px;line-height:1.5}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}
.chip{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:5px 13px;font-size:12.5px;font-family:var(--mono);color:var(--dim)}
.chip b{color:var(--tx)} .chip.cand b{color:var(--warn)}
.bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px}
.segs{display:flex;background:var(--card);border:1px solid var(--line2);border-radius:9px;overflow:hidden}
.segs button{background:transparent;color:var(--dim);border:0;padding:7px 14px;font:inherit;font-size:12.5px;font-weight:600;cursor:pointer}
.segs button.on{background:var(--card2);color:var(--tx)}
.bar input{flex:1;min-width:160px;background:var(--card);border:1px solid var(--line2);border-radius:9px;color:var(--tx);font:inherit;font-size:13.5px;padding:8px 12px;outline:none}
.bar input:focus{border-color:var(--acc)}
.qall{background:var(--accd,#1f6feb);color:#fff;border:1px solid var(--acc);border-radius:9px;padding:8px 14px;font:inherit;font-size:12.5px;font-weight:650;cursor:pointer}
.qall:hover{filter:brightness(1.1)} .qall:disabled{opacity:.5;cursor:default}
.list{display:flex;flex-direction:column;gap:7px}
.row{display:flex;align-items:center;gap:12px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px}
.row .t{font-weight:600;font-size:14px} .row .t .yr{color:var(--dim);font-weight:400}
.row .meta{margin-left:2px;color:var(--faint);font-family:var(--mono);font-size:11.5px}
.res{font-family:var(--mono);font-size:11px;font-weight:700;border-radius:6px;padding:2px 8px}
.res.sd{background:rgba(248,81,73,.15);color:var(--bad)} .res.hd{background:rgba(63,185,80,.14);color:var(--ok)}
.spacer{margin-left:auto}
.act{display:flex;align-items:center;gap:9px}
button.q{background:transparent;color:var(--acc);border:1px solid var(--acc);border-radius:8px;padding:6px 13px;font:inherit;font-size:12.5px;font-weight:650;cursor:pointer;white-space:nowrap}
button.q:hover{background:rgba(88,166,255,.12)} button.q:disabled{opacity:.5;cursor:default}
.pill{font-family:var(--mono);font-size:11px;font-weight:600;border-radius:20px;padding:3px 11px;white-space:nowrap}
.pill.queued{background:rgba(210,153,34,.16);color:var(--warn)}
.pill.awaiting{background:rgba(210,153,34,.16);color:var(--warn)}
.pill.done{background:rgba(63,185,80,.15);color:var(--ok)}
.pill.hd{background:rgba(139,148,158,.15);color:var(--dim)}
.empty{color:var(--faint);font-style:italic;padding:26px;text-align:center}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}
</style></head><body><div class=wrap>
<header><div class=brand>rip<b>·</b>movie <span class=sub>upgrade DVDs</span></div>
<a class=back href="/">← dashboard</a></header>
<div class=note>Movies already in your Nextcloud library, classified by quality. <b>DVD-quality</b> titles (≤576p
with no HD copy) can be queued straight to the upscale worker — it pulls the master from Nextcloud,
runs it through the Topaz handoff (lands in the inbox), and delivers a 1080p rendition back beside it.</div>
<div class=chips id=chips></div>
<div class=bar>
 <div class=segs id=segs>
  <button data-f=candidate class=on>DVD-quality</button>
  <button data-f=all>All</button>
  <button data-f=done>Upscaled</button>
  <button data-f=hd>HD</button>
 </div>
 <input id=filter type=search placeholder="Filter by title…" autocomplete=off>
 <button class=qall id=qall>Queue all DVD-quality</button>
</div>
<div class=list id=list><div class=empty>Loading library…</div></div>
</div>
<script>
const E=(t,c,h)=>{const e=document.createElement(t);if(c)e.className=c;if(h!=null)e.innerHTML=h;return e};
const esc=s=>String(s).replace(/[&<>]/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[m]));
let DATA=[], FILT="candidate", Q="";
const isCand=x=>x.status==="candidate";
function chips(c){document.getElementById("chips").innerHTML=
  `<div class="chip cand">DVD-quality <b>${c.candidate||0}</b></div>`+
  `<div class=chip>Already upscaled <b>${c.done||0}</b></div>`+
  `<div class=chip>HD / 4K <b>${c.hd||0}</b></div>`+
  `<div class=chip>Total <b>${c.total||0}</b></div>`;}
function visible(){return DATA.filter(x=>{
  const f=FILT==="all"?true:x.status===FILT||(FILT==="candidate"&&(x.status==="queued"||x.status==="awaiting"));
  const q=!Q||(x.title+" "+x.year).toLowerCase().includes(Q);
  return f&&q;});}
function rowEl(x){
  const r=E("div","row");
  const sd=x.best&&x.best<=576;
  const res=`<span class="res ${sd?'sd':'hd'}">${x.best?x.best+'p':'?'}</span>`;
  let act;
  if(x.status==="candidate") act=`<button class=q data-folder="${esc(x.folder)}">Queue upscale ↑</button>`;
  else if(x.status==="queued") act=`<span class="pill queued">queued</span>`;
  else if(x.status==="awaiting") act=`<span class="pill awaiting">awaiting Topaz</span>`;
  else if(x.status==="done") act=`<span class="pill done">upscaled ✓</span>`;
  else act=`<span class="pill hd">HD — no upscale</span>`;
  r.innerHTML=`${res}<div><div class=t>${esc(x.title)}${x.year?` <span class=yr>(${esc(x.year)})</span>`:""}</div>`+
    `<div class=meta>${x.codec?esc(x.codec)+" · ":""}${x.size?x.size+" GiB":""}</div></div>`+
    `<div class=spacer></div><div class=act>${act}</div>`;
  const b=r.querySelector("button.q");
  if(b)b.addEventListener("click",()=>queue(b.dataset.folder,b));
  return r;
}
function render(){
  chips(window._counts||{});
  const L=document.getElementById("list");L.innerHTML="";
  const vis=visible();
  if(!vis.length){L.append(E("div","empty","Nothing here."));return;}
  vis.forEach(x=>L.append(rowEl(x)));
  document.getElementById("qall").disabled=!vis.some(isCand);
}
async function queue(folder,btn){
  btn.disabled=true;btn.textContent="Queueing…";
  try{const r=await(await fetch("api/queue-upscale",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({folder})})).json();
    const item=DATA.find(x=>x.folder===folder);
    if(r.ok){if(item)item.status="queued";}
    else{btn.textContent=r.error&&/already/.test(r.error)?"already queued":"failed";
         if(item)item.status="queued";setTimeout(render,900);return;}
  }catch(e){btn.textContent="error";return;}
  render();
}
async function queueAll(){
  const cands=visible().filter(isCand);
  document.getElementById("qall").disabled=true;
  for(const x of cands){try{await fetch("api/queue-upscale",{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify({folder:x.folder})});
    x.status="queued";render();}catch(e){}}
}
document.querySelectorAll("#segs button").forEach(b=>b.addEventListener("click",()=>{
  document.querySelectorAll("#segs button").forEach(o=>o.classList.remove("on"));
  b.classList.add("on");FILT=b.dataset.f;render();}));
document.getElementById("filter").addEventListener("input",e=>{Q=e.target.value.trim().toLowerCase();render();});
document.getElementById("qall").addEventListener("click",queueAll);
async function load(){
  try{const d=await(await fetch("api/library")).json();
    DATA=d.items||[];window._counts=d.counts||{};render();}
  catch(e){document.getElementById("list").innerHTML='<div class=empty>Could not load library.</div>';}
}
load();
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    cfg: Config = None  # set by serve()

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        from . import config_edit
        if self.path.startswith("/api/state"):
            self._send(200, "application/json", json.dumps(gather(self.cfg)).encode())
        elif self.path.startswith("/api/search"):
            q = parse_qs(urlparse(self.path).query).get("q", [""])[0]
            self._send(200, "application/json", json.dumps(search(self.cfg, q)).encode())
        elif self.path.startswith("/api/config/test"):
            cfg = Config.load(self.cfg.path)          # reload so freshly-saved secrets are tested
            self._send(200, "application/json", json.dumps(config_edit.test_all(cfg)).encode())
        elif self.path == "/api/config":
            self._send(200, "application/json", json.dumps(config_edit.entries(self.cfg.path)).encode())
        elif self.path.startswith("/api/library"):
            self._send(200, "application/json", json.dumps(library_view(self.cfg)).encode())
        elif self.path in ("/library", "/library.html"):
            self._send(200, "text/html; charset=utf-8", LIBRARY_PAGE.encode())
        elif self.path in ("/config", "/config.html"):
            self._send(200, "text/html; charset=utf-8", CONFIG_PAGE.encode())
        elif self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PAGE.encode())
        else:
            self._send(404, "text/plain", b"not found")

    def do_POST(self):
        from . import config_edit
        if self.path == "/api/config":
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                config_edit.set_value(self.cfg.path, body["key"], body["value"])
                self._send(200, "application/json", b'{"ok":true}')
            except Exception as e:  # noqa: BLE001
                self._send(400, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
        elif self.path == "/api/queue-upscale":
            from .pipeline import queue_library_upscale
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                res = queue_library_upscale(self.cfg, body["folder"])
                _cache.pop("movie_tree", None)         # reflect the new queue state on next poll
                self._send(200, "application/json", json.dumps(res).encode())
            except Exception as e:  # noqa: BLE001
                self._send(400, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
        else:
            self._send(404, "text/plain", b"not found")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8422, progress=print) -> int:
    _Handler.cfg = cfg
    httpd = ThreadingHTTPServer((host, port), _Handler)
    progress(f"dashboard -> http://{host}:{port}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        progress("\nstopped")
    finally:
        httpd.server_close()
    return 0
