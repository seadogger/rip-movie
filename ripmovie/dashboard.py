"""Live pipeline dashboard: `rip-movie dashboard` -> http://localhost:8787.

A stdlib HTTP server renders a dark kanban of what the pipeline is doing right now — the disc in
the drive, the active rip, the upscale queue, the upscale in progress (with its sub-stage), recently
finished titles, and Nextcloud/Jellyfin health — plus a search bar over the movie + TV libraries.
The page polls /api/state; state comes from local status files + process inspection, with the slower
cluster checks cached so polling stays cheap.
"""
from __future__ import annotations

import json
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import status
from .config import Config

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
.board{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px;align-items:start}
.col{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:13px 12px}
.col>h2{font-size:11px;text-transform:uppercase;letter-spacing:.9px;color:var(--dim);font-weight:700;
 display:flex;align-items:center;gap:8px;margin-bottom:12px}
.col>h2 .n{margin-left:auto;font-family:var(--mono);font-size:11px;font-weight:600;color:var(--tx);
 background:var(--card2);border:1px solid var(--line);border-radius:20px;padding:1px 9px;min-width:24px;text-align:center}
.card{position:relative;background:var(--card2);border:1px solid var(--line);border-radius:10px;
 padding:11px 12px 11px 15px;margin-bottom:9px;overflow:hidden}
.card:last-child{margin-bottom:0}
.card::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--st,var(--line))}
.card.rip{--st:var(--st-rip)}.card.queue{--st:var(--st-queue)}.card.up{--st:var(--st-up)}
.card.done{--st:var(--st-done)}.card.fail{--st:var(--st-fail)}
.card.del{--st:#39b7c9}.card.cln{--st:#8b949e}
.card .t{font-weight:620;font-size:13.5px;letter-spacing:-.1px}.card .t .yr{color:var(--dim);font-weight:400}
.card .meta{color:var(--dim);font-family:var(--mono);font-size:11.5px;margin-top:4px;font-variant-numeric:tabular-nums}
.pill{display:inline-flex;font-size:10px;font-weight:700;letter-spacing:.5px;text-transform:uppercase;
 border-radius:5px;padding:2px 7px;margin-top:8px}
.pill.b{background:rgba(88,166,255,.16);color:var(--acc)}
.pill.p{background:rgba(163,113,247,.18);color:var(--purple)}
.pill.c{background:rgba(57,183,201,.16);color:#39b7c9}
.bar{height:5px;background:var(--bg);border-radius:4px;margin-top:9px;overflow:hidden;position:relative}
.bar>i{display:block;height:100%;border-radius:4px;background:linear-gradient(90deg,var(--accd),var(--acc));transition:width .6s}
.bar.ind::after{content:"";position:absolute;inset:0;width:38%;border-radius:4px;
 background:linear-gradient(90deg,transparent,var(--purple),transparent);animation:sl 1.5s ease-in-out infinite}
@keyframes sl{0%{transform:translateX(-100%)}100%{transform:translateX(280%)}}
.empty{color:var(--faint);font-size:12px;font-style:italic;padding:8px 3px}
.health .row{display:flex;align-items:center;gap:9px;padding:7px 2px;font-size:13px;border-bottom:1px solid var(--line)}
.health .row:last-child{border:0}
.health .row b{margin-left:auto;font-family:var(--mono);font-weight:600;font-variant-numeric:tabular-nums}
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
function col(icon,title,desc,n){const c=E("div","col");
 c.append(E("h2",null,`<span>${icon}</span><span>${title}</span>`+(n!=null?`<span class=n>${n}</span>`:"")));
 if(desc)c.append(E("div","cd",desc));return c}
function card(cls,html){return E("div","card"+(cls?" "+cls:""),html)}
function bar(pct){return `<div class="bar${pct==null?" ind":""}">${pct==null?"":`<i style="width:${pct}%"></i>`}</div>`}
function render(s){
 const cl=s.cluster||{},H=document.getElementById("health");
 const chip=(l,ok,v)=>`<div class=chip><span class="dot ${ok?'g':'r'}"></span>${l}${v!=null?` <b>${v}</b>`:""}</div>`;
 H.innerHTML=chip("Nextcloud",cl.nextcloud,cl.nextcloud?"online":"down")
  +chip("Jellyfin",cl.jellyfin,cl.jellyfin?"online":"down")
  +chip("Movies",cl.library!=null,cl.library!=null?cl.library:"?")
  +chip("TV shows",cl.shows!=null,cl.shows!=null?cl.shows:"?");
 const b=document.getElementById("board");b.innerHTML="";
 // Disc & Rip
 const c1=col("💿","Disc &amp; Rip","MakeMKV → lossless master");
 if(s.ripping){const r=s.ripping;c1.append(card("rip",`<div class=t>${esc(r.title||r.disc||"ripping")}${yr(r.year)}</div>
  <div class=meta>${esc(r.disc||"?")} · ${human(r.size)}${r.pct!=null?" · "+r.pct+"%":""} · ${dur(r.elapsed)}</div>
  <span class="pill b">${r.active?"ripping":"finishing"}</span>${bar(r.pct)}`));}
 else if(s.drive&&s.drive.present)c1.append(card("",`<div class=t>Disc inserted</div><div class=meta>idle — not processing</div>`));
 else c1.append(E("div","empty","drive empty"));
 b.append(c1);
 // Master upload -> Nextcloud + Jellyfin
 const c2=col("☁️","Master → Cluster","upload + Jellyfin reindex");
 if(s.delivering){const d=s.delivering;c2.append(card("del",`<div class=t>${esc(d.title)}${yr(d.year)}</div>
  <div class=meta>${dur(d.elapsed)}</div><span class="pill c">${esc(d.stage||"uploading")}</span>${bar(null)}`));}
 else c2.append(E("div","empty","no active upload"));
 b.append(c2);
 // Upscale queue
 const c3=col("🎞️","Upscale Queue","DVD/SD only · needs the ANE",s.queue.length);
 if(s.queue.length)s.queue.forEach((j,i)=>c3.append(card("queue",`<div class=t>${esc(j.title)}${yr(j.year)}</div><div class=meta>#${i+1} · waiting for ANE</div>`)));
 else c3.append(E("div","empty","no jobs waiting"));
 (s.failed||[]).forEach(f=>c3.append(card("fail",`<div class=t>${esc(f)}</div><div class=meta>failed — needs a look</div>`)));
 b.append(c3);
 // Upscaling now (with ETA)
 const c4=col("⚡","Upscaling · ANE","crop → SR → mux → OCR");
 if(s.upscaling){const u=s.upscaling;const eta=u.eta!=null?` · ETA ${dur(u.eta)}`:"";
  c4.append(card("up",`<div class=t>${esc(u.title)}${yr(u.year)}</div>
   <div class=meta>${u.pct!=null?u.pct+"% · ":""}${human(u.size)} · ${dur(u.elapsed)}${eta}</div>
   <span class="pill p">${esc(u.stage||"working")}</span>${bar(u.pct!=null?u.pct:null)}`));}
 else c4.append(E("div","empty","ANE idle"));
 b.append(c4);
 // Cleanup
 const c5=col("🧹","Cleanup","free local temp space");
 const cleaning=s.upscaling&&/clean/i.test(s.upscaling.stage||"");
 if(cleaning)c5.append(card("cln",`<div class=t>${esc(s.upscaling.title)}</div><div class=meta>clearing temp files…</div>`));
 if(s.cleaned&&s.cleaned.length)s.cleaned.forEach(c=>c5.append(card("done",`<div class=t>${esc(c.title)}${yr(c.year)}</div><div class=meta>freed ${esc(c.detail||"temps")}</div>`)));
 else if(!cleaning)c5.append(E("div","empty","nothing to clean"));
 b.append(c5);
 // Done
 const c6=col("✅","In Jellyfin","direct-play ready",s.done.length||null);
 if(s.done.length)s.done.forEach(d=>c6.append(card("done",`<div class=t>${esc(d.title)}${yr(d.year)}</div>`)));
 else c6.append(E("div","empty","nothing finished yet"));
 b.append(c6);
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


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8787, progress=print) -> int:
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
