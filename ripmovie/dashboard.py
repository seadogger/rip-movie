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


def _lane_stages(current: str, hd: bool = False, queued: bool = False, done: bool = False) -> dict:
    """State of every stage for a movie whose current position is `current` (the pipeline is
    linear, so earlier stages are done, later ones pending). HD/4K skip the upscale."""
    ci = STAGE_ORDER.index(current)
    out = {}
    for i, s in enumerate(STAGE_ORDER):
        if s == "upscale" and hd:
            out[s] = "skipped"
        elif done or i < ci:
            out[s] = "done"
        elif i == ci:
            out[s] = "queued" if queued else "active"
        else:
            out[s] = "pending"
    return out


def _build_lanes(st: dict) -> list[dict]:
    lanes: list[dict] = []
    seen: set = set()

    def add(title, year, current, detail=None, hd=False, queued=False, done=False):
        if not title:
            return
        key = (re.sub(r"[^a-z0-9]", "", title.lower()), year)
        if key in seen:
            return
        seen.add(key)
        lanes.append({"title": title, "year": year, "current": current,
                      "stages": _lane_stages(current, hd, queued, done), "detail": detail or {}})

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
             "elapsed": u.get("elapsed"), "note": u.get("stage")})
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

    from .pipeline import list_upscale_jobs, _upscale_dir
    st["queue"] = [{"title": j["title"], "year": j.get("year")} for j in list_upscale_jobs(cfg)]
    st["failed"] = [f.stem for f in _upscale_dir(cfg).glob("*.failed")]
    st["done"] = status.recent(cfg, 10)
    st["cleaned"] = status.recent_events(cfg, "cleaned", 8)
    st["cluster"] = _cached("cluster", 30, lambda: _cluster(cfg))
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
.step.skipped .sdot{border-style:dashed;background:transparent;opacity:.6}
.step.skipped .slabel{color:var(--faint);text-decoration:line-through}
.step.skipped::before,.step.pending::before{background:var(--line)}
@keyframes pl2{0%{box-shadow:0 0 0 0 rgba(88,166,255,.35)}70%{box-shadow:0 0 0 6px rgba(88,166,255,0)}100%{box-shadow:0 0 0 0 rgba(88,166,255,0)}}
.empty{color:var(--faint);font-size:13px;font-style:italic;padding:26px 3px;text-align:center}
.dot{width:8px;height:8px;border-radius:50%;flex:none}
.dot.g{background:var(--ok);box-shadow:0 0 6px rgba(63,185,80,.5)}.dot.r{background:var(--bad)}
footer{margin-top:22px;color:var(--faint);font-family:var(--mono);font-size:11px;display:flex;gap:8px;flex-wrap:wrap}
footer .k{color:var(--dim)}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}
</style></head><body><div class=wrap>
<header><div class=brand>rip<b>·</b>movie <span class=sub>pipeline</span></div>
<span class=pulse></span><div class=live id=ts>connecting…</div></header>
<div class=hbar id=health></div>
<div class=search><span class=mag>⌕</span>
 <input id=q type=search autocomplete=off spellcheck=false placeholder="Search your library — movies &amp; TV shows…">
 <div class=results id=results></div></div>
<div class=board id=board></div>
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
 const badge=curState==="done"?"cur-done":curState==="queued"?"cur-queued":"cur-active";
 const label=L.current==="jellyfin"?"in Jellyfin":(curState==="queued"?"queued":(STAGES.find(x=>x[0]===L.current)||["","working"])[1]);
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
 if(!lanes.length){b.append(E("div","empty","Pipeline idle — insert a disc, or a finished title will appear here."));return;}
 lanes.forEach(L=>b.append(lane(L)));
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


class _Handler(BaseHTTPRequestHandler):
    cfg: Config = None  # set by serve()

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        if self.path.startswith("/api/state"):
            self._send(200, "application/json", json.dumps(gather(self.cfg)).encode())
        elif self.path.startswith("/api/search"):
            q = parse_qs(urlparse(self.path).query).get("q", [""])[0]
            self._send(200, "application/json", json.dumps(search(self.cfg, q)).encode())
        elif self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PAGE.encode())
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
