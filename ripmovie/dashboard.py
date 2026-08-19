"""Live pipeline dashboard: `rip-movie dashboard` -> http://localhost:8787.

A stdlib HTTP server renders a kanban of what the pipeline is doing right now — the disc in the
drive, the active rip, the upscale queue, the upscale in progress (with its sub-stage), recently
finished titles, and Nextcloud/Jellyfin health. The page polls /api/state; state comes from local
status files + process inspection, with the slower cluster checks cached so polling stays cheap.
"""
from __future__ import annotations

import json
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
    p = Path(path_or_dir)
    files = [p] if p.is_file() else (list(p.glob(pattern)) if p.is_dir() else [])
    return max((f.stat().st_size for f in files), default=0)


def _drive(cfg: Config) -> dict:
    try:
        out = subprocess.run(["drutil", "status"], capture_output=True, text=True, timeout=15).stdout
    except Exception:  # noqa: BLE001
        return {"present": False}
    return {"present": ("Type:" in out and "No Media" not in out)}


def _cluster(cfg: Config) -> dict:
    from . import kube
    out = {"library": None, "nextcloud": False, "jellyfin": False}
    k = cfg.get("deliver.kubectl", {})
    ctx = k.get("context")
    try:
        ns = k.get("nextcloud_namespace")
        pod = kube.pod_name(ns, k.get("nextcloud_pod_selector"), context=ctx)
        dp = cfg.require("deliver.kubectl.data_path").rstrip("/")
        sub = cfg.get("library.movies_subpath", "Videos/Movies").strip("/")
        res = kube.exec_in(ns, pod, ["sh", "-c", f'ls "{dp}/{sub}" 2>/dev/null | wc -l'],
                           container=k.get("nextcloud_container"), context=ctx, timeout=20)
        out["library"] = int((res.strip().split() or ["0"])[0])
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

    up = status.read(cfg, "upscaling")
    if up:
        outp = Path(up.get("output", ""))
        up["size"] = outp.stat().st_size if outp.exists() else 0
        up["active"] = _running("enhance_stream.py")
        up["elapsed"] = int(st["now"] - up.get("started", st["now"]))
    st["upscaling"] = up

    from .pipeline import list_upscale_jobs, _upscale_dir
    st["queue"] = [{"title": j["title"], "year": j.get("year")} for j in list_upscale_jobs(cfg)]
    st["failed"] = [f.stem for f in _upscale_dir(cfg).glob("*.failed")]
    st["done"] = status.recent(cfg, 12)
    st["cluster"] = _cached("cluster", 30, lambda: _cluster(cfg))
    return st


PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>rip-movie</title><style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0d1117;--card:#161b22;--card2:#1c2330;--bd:#2a3038;--tx:#e6edf3;--dim:#8b949e;
--ok:#3fb950;--warn:#d29922;--bad:#f85149;--acc:#58a6ff;--accd:#1f6feb}
body{background:var(--bg);color:var(--tx);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;padding:20px}
header{display:flex;align-items:center;gap:14px;margin-bottom:18px}
h1{font-size:20px;font-weight:650;letter-spacing:.2px}
h1 .m{color:var(--acc)}
.pulse{width:9px;height:9px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 0 rgba(63,185,80,.6);animation:p 2s infinite}
@keyframes p{0%{box-shadow:0 0 0 0 rgba(63,185,80,.5)}70%{box-shadow:0 0 0 8px rgba(63,185,80,0)}100%{box-shadow:0 0 0 0 rgba(63,185,80,0)}}
.sub{color:var(--dim);font-size:12px;margin-left:auto}
.board{display:grid;grid-template-columns:repeat(auto-fit,minmax(215px,1fr));gap:14px}
.col{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:12px;min-height:120px}
.col h2{font-size:11px;text-transform:uppercase;letter-spacing:.7px;color:var(--dim);margin-bottom:11px;display:flex;gap:7px;align-items:center}
.col h2 .n{margin-left:auto;background:var(--card2);color:var(--tx);border-radius:20px;padding:1px 8px;font-size:11px}
.card{background:var(--card2);border:1px solid var(--bd);border-radius:9px;padding:10px 11px;margin-bottom:9px}
.card:last-child{margin-bottom:0}
.card .t{font-weight:600;font-size:13.5px}
.card .meta{color:var(--dim);font-size:11.5px;margin-top:3px}
.stage{display:inline-block;background:var(--accd);color:#fff;font-size:10.5px;font-weight:600;border-radius:5px;padding:1px 7px;margin-top:7px;text-transform:uppercase;letter-spacing:.4px}
.bar{height:6px;background:#0d1117;border-radius:4px;margin-top:8px;overflow:hidden}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,var(--accd),var(--acc));border-radius:4px;transition:width .6s}
.bar.ind>i{width:35%;animation:slide 1.4s ease-in-out infinite;background:linear-gradient(90deg,transparent,var(--acc),transparent)}
@keyframes slide{0%{margin-left:-35%}100%{margin-left:100%}}
.empty{color:#5b6470;font-size:12px;font-style:italic;padding:6px 2px}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px}
.g{background:var(--ok)}.r{background:var(--bad)}.y{background:var(--warn)}
.health .row{display:flex;align-items:center;padding:5px 0;font-size:13px;border-bottom:1px solid var(--bd)}
.health .row:last-child{border:0}.health .row b{margin-left:auto;font-weight:600}
.done .card{padding:7px 10px}.done .t{font-weight:500;font-size:12.5px}
.fail{border-color:#5c2b2b;background:#20161a}
</style></head><body>
<header><span class=pulse></span><h1>rip<span class=m>·</span>movie <span style=color:var(--dim);font-weight:400>pipeline</span></h1>
<span class=sub id=sub>connecting…</span></header>
<div class=board id=board></div>
<script>
const E=(t,c,h)=>{const e=document.createElement(t);if(c)e.className=c;if(h!=null)e.innerHTML=h;return e};
const human=n=>{n=+n||0;const u=["B","KB","MB","GB","TB"];let i=0;while(n>=1024&&i<4){n/=1024;i++}return n.toFixed(1)+" "+u[i]};
const dur=s=>{s=+s||0;const h=s/3600|0,m=(s%3600)/60|0;return h?`${h}h ${m}m`:`${m}m ${s%60|0}s`};
function col(icon,title,n){const c=E("div","col");c.append(E("h2",null,`<span>${icon}</span><span>${title}</span>`+(n!=null?`<span class=n>${n}</span>`:"")));return c}
function card(html,cls){return E("div","card"+(cls?" "+cls:""),html)}
function bar(pct){return `<div class="bar${pct==null?" ind":""}"><i style="width:${pct==null?35:pct}%"></i></div>`}
async function tick(){
 let s;try{s=await(await fetch("api/state")).json()}catch(e){document.getElementById("sub").textContent="offline — retrying";return}
 document.getElementById("sub").textContent="updated "+new Date().toLocaleTimeString();
 const b=document.getElementById("board");b.innerHTML="";
 // Disc + Rip
 const c1=col("💿","Disc / Rip");
 if(s.ripping){const r=s.ripping;c1.append(card(`<div class=t>${r.title||r.disc||"ripping"}${r.year?" ("+r.year+")":""}</div>
  <div class=meta>disc ${r.disc||"?"} · ${human(r.size)}${r.pct!=null?" · "+r.pct+"%":""} · ${dur(r.elapsed)}</div>
  <div class=stage>${r.active?"ripping":"finishing"}</div>${bar(r.pct)}`));}
 else if(s.drive&&s.drive.present)c1.append(card(`<div class=t>disc inserted</div><div class=meta>idle — not yet processing</div>`));
 else c1.append(E("div","empty","drive empty"));
 b.append(c1);
 // Upscale queue
 const c2=col("🎞️","Upscale Queue",s.queue.length);
 if(s.queue.length)s.queue.forEach((j,i)=>c2.append(card(`<div class=t>${j.title}${j.year?" ("+j.year+")":""}</div><div class=meta>#${i+1} in line</div>`)));
 else c2.append(E("div","empty","no jobs waiting"));
 s.failed.forEach(f=>c2.append(card(`<div class=t>⚠ ${f}</div><div class=meta>failed — needs a look</div>`,"fail")));
 b.append(c2);
 // Upscaling now
 const c3=col("⚡","Upscaling (ANE)");
 if(s.upscaling){const u=s.upscaling;c3.append(card(`<div class=t>${u.title}${u.year?" ("+u.year+")":""}</div>
  <div class=meta>${human(u.size)} · ${dur(u.elapsed)}${u.active?"":" · idle"}</div>
  <div class=stage>${u.stage||"working"}</div>${bar(null)}`));}
 else c3.append(E("div","empty","ANE idle"));
 b.append(c3);
 // Done
 const c4=col("✅","Recently Done",s.done.length||null);
 const c4b=E("div","done");
 if(s.done.length)s.done.forEach(d=>c4b.append(card(`<div class=t>${d.title}${d.year?" ("+d.year+")":""}</div>`)));
 else c4b.append(E("div","empty","nothing yet"));
 c4.append(c4b);b.append(c4);
 // Cluster
 const cl=s.cluster||{},c5=col("☁️","Library / Cluster");
 const h=E("div","health");
 const row=(l,ok,extra)=>`<div class=row><span class="dot ${ok?'g':'r'}"></span>${l}<b>${extra!=null?extra:(ok?"up":"down")}</b></div>`;
 h.innerHTML=row("Nextcloud",cl.nextcloud)+row("Jellyfin",cl.jellyfin)+row("Movies",cl.library!=null,cl.library!=null?cl.library:"?");
 c5.append(h);b.append(c5);
}
tick();setInterval(tick,2500);
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    cfg: Config = None  # set by serve()

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        if self.path.startswith("/api/state"):
            body = json.dumps(gather(self.cfg)).encode()
            self._send(200, "application/json", body)
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


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8787,
          progress=print) -> int:
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
