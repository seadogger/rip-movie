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
<meta name=viewport content="width=device-width,initial-scale=1"><title>rip·movie — pipeline</title>
<style>
:root{
 --ground:#0c0f14;--panel:#12171f;--panel2:#171d27;--line:#242c38;
 --tx:#e7edf4;--dim:#8593a3;--faint:#5a6675;--accent:#4aa8ff;--accent-deep:#2b6fd6;
 --good:#43c463;--warn:#e0a020;--bad:#f2564d;
 --stripe-rip:var(--accent);--stripe-queue:var(--warn);--stripe-up:#9d7bff;
 --stripe-done:var(--good);--stripe-fail:var(--bad);
 --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
 --sans:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Helvetica,Arial,sans-serif;
 --shadow:0 1px 0 rgba(255,255,255,.02),0 6px 20px -12px rgba(0,0,0,.6)}
@media (prefers-color-scheme:light){:root{
 --ground:#eef1f5;--panel:#fff;--panel2:#f6f8fb;--line:#dde3ec;--tx:#141b24;--dim:#5a6773;
 --faint:#8b97a4;--accent:#1f6feb;--accent-deep:#1a5fc4;
 --shadow:0 1px 2px rgba(20,30,45,.06),0 8px 24px -16px rgba(20,30,45,.25)}}
:root[data-theme=dark]{--ground:#0c0f14;--panel:#12171f;--panel2:#171d27;--line:#242c38;--tx:#e7edf4;
 --dim:#8593a3;--faint:#5a6675;--accent:#4aa8ff;--accent-deep:#2b6fd6}
:root[data-theme=light]{--ground:#eef1f5;--panel:#fff;--panel2:#f6f8fb;--line:#dde3ec;--tx:#141b24;
 --dim:#5a6773;--faint:#8b97a4;--accent:#1f6feb;--accent-deep:#1a5fc4}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--ground);color:var(--tx);font-family:var(--sans);font-size:14px;line-height:1.45;
 padding:24px 22px 40px;-webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto}
header{display:flex;align-items:baseline;gap:14px;margin-bottom:4px}
.brand{font-size:20px;font-weight:680;letter-spacing:-.2px;display:flex;align-items:center;gap:9px}
.brand .disc{width:18px;height:18px;border-radius:50%;box-shadow:inset 0 0 0 1px var(--line);
 background:radial-gradient(circle at 50% 50%,var(--ground) 0 22%,var(--accent) 24% 30%,var(--ground) 32% 38%,var(--accent-deep) 40%);
 animation:spin 6s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.brand b{color:var(--accent)}.brand .sub{color:var(--dim);font-weight:400;letter-spacing:0}
.live{margin-left:auto;display:flex;align-items:center;gap:8px;color:var(--dim);font-family:var(--mono);font-size:12px}
.pulse{width:8px;height:8px;border-radius:50%;background:var(--good);box-shadow:0 0 0 0 rgba(67,196,99,.55);animation:pl 2.2s infinite}
@keyframes pl{0%{box-shadow:0 0 0 0 rgba(67,196,99,.5)}70%{box-shadow:0 0 0 7px rgba(67,196,99,0)}100%{box-shadow:0 0 0 0 rgba(67,196,99,0)}}
.flow{color:var(--faint);font-family:var(--mono);font-size:11.5px;letter-spacing:.3px;margin:14px 0 18px;
 display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.flow b{color:var(--dim);font-weight:600}.flow .arw{color:var(--accent);opacity:.6}
.board{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px;align-items:start}
.col{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:13px 12px;box-shadow:var(--shadow)}
.col>h2{font-size:11px;text-transform:uppercase;letter-spacing:.9px;color:var(--dim);font-weight:700;
 display:flex;align-items:center;gap:8px;margin-bottom:12px}
.col>h2 .ic{font-size:13px}
.col>h2 .n{margin-left:auto;font-family:var(--mono);font-size:11px;font-weight:600;color:var(--tx);
 background:var(--panel2);border:1px solid var(--line);border-radius:20px;padding:1px 9px;min-width:24px;text-align:center}
.card{position:relative;background:var(--panel2);border:1px solid var(--line);border-radius:10px;
 padding:11px 12px 11px 15px;margin-bottom:9px;overflow:hidden}
.card:last-child{margin-bottom:0}
.card::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--st,var(--line))}
.card.rip{--st:var(--stripe-rip)}.card.queue{--st:var(--stripe-queue)}.card.up{--st:var(--stripe-up)}
.card.done{--st:var(--stripe-done)}.card.fail{--st:var(--stripe-fail)}
.card .t{font-weight:620;font-size:13.5px;letter-spacing:-.1px}.card .t .yr{color:var(--dim);font-weight:400}
.card .meta{color:var(--dim);font-family:var(--mono);font-size:11.5px;margin-top:4px;font-variant-numeric:tabular-nums}
.pill{display:inline-flex;align-items:center;font-size:10px;font-weight:700;letter-spacing:.5px;
 text-transform:uppercase;border-radius:5px;padding:2px 7px;margin-top:8px}
.pill.b{background:color-mix(in srgb,var(--accent) 18%,transparent);color:var(--accent)}
.pill.p{background:color-mix(in srgb,var(--stripe-up) 20%,transparent);color:var(--stripe-up)}
.pill.g{background:color-mix(in srgb,var(--good) 18%,transparent);color:var(--good)}
.bar{height:5px;background:var(--ground);border-radius:4px;margin-top:9px;overflow:hidden;position:relative}
.bar>i{display:block;height:100%;border-radius:4px;background:linear-gradient(90deg,var(--accent-deep),var(--accent));transition:width .6s}
.bar.ind::after{content:"";position:absolute;inset:0;width:38%;border-radius:4px;
 background:linear-gradient(90deg,transparent,var(--stripe-up),transparent);animation:sl 1.5s ease-in-out infinite}
@keyframes sl{0%{transform:translateX(-100%)}100%{transform:translateX(280%)}}
.empty{color:var(--faint);font-size:12px;font-style:italic;padding:8px 3px}
.health .row{display:flex;align-items:center;gap:9px;padding:7px 2px;font-size:13px;border-bottom:1px solid var(--line)}
.health .row:last-child{border:0}
.health .row b{margin-left:auto;font-family:var(--mono);font-weight:600;font-variant-numeric:tabular-nums}
.dot{width:8px;height:8px;border-radius:50%;flex:none}
.dot.g{background:var(--good);box-shadow:0 0 6px color-mix(in srgb,var(--good) 60%,transparent)}.dot.r{background:var(--bad)}
footer{margin-top:22px;color:var(--faint);font-family:var(--mono);font-size:11px;display:flex;gap:8px;flex-wrap:wrap}
footer .k{color:var(--dim)}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}
</style></head><body><div class=wrap>
<header><div class=brand><span class=disc></span>rip<b>·</b>movie <span class=sub>pipeline</span></div>
<div class=live><span class=pulse></span><span id=ts>connecting…</span></div></header>
<div class=flow><b>disc</b><span class=arw>→</span>rip<span class=arw>→</span>master to Jellyfin
<span class=arw>→</span>queue<span class=arw>→</span>upscale (ANE)<span class=arw>→</span>crop · mux · OCR
<span class=arw>→</span>deliver<span class=arw>→</span>cleanup</div>
<div class=board id=board></div>
<footer><span class=k>source</span> DVD/Blu-ray · MakeMKV<span class=k>· upscale</span> Real-ESRGAN on CoreML/ANE
<span class=k>· deliver</span> Nextcloud → Jellyfin</footer></div>
<script>
const E=(t,c,h)=>{const e=document.createElement(t);if(c)e.className=c;if(h!=null)e.innerHTML=h;return e};
const human=n=>{n=+n||0;const u=["B","KB","MB","GB","TB"];let i=0;while(n>=1024&&i<4){n/=1024;i++}return n.toFixed(1)+" "+u[i]};
const dur=s=>{s=+s||0;const h=s/3600|0,m=(s%3600)/60|0;return h?`${h}h ${m}m`:`${m}m ${(s%60|0)}s`};
const yr=y=>y?` <span class=yr>(${y})</span>`:"";
function col(icon,title,n){const c=E("div","col");
 c.append(E("h2",null,`<span class=ic>${icon}</span><span>${title}</span>`+(n!=null?`<span class=n>${n}</span>`:"")));return c}
function card(cls,html){return E("div","card"+(cls?" "+cls:""),html)}
function bar(pct){return `<div class="bar${pct==null?" ind":""}">${pct==null?"":`<i style="width:${pct}%"></i>`}</div>`}
function render(s){
 const b=document.getElementById("board");b.innerHTML="";
 const c1=col("💿","Disc &amp; Rip");
 if(s.ripping){const r=s.ripping;c1.append(card("rip",`<div class=t>${r.title||r.disc||"ripping"}${yr(r.year)}</div>
  <div class=meta>${r.disc||"?"} · ${human(r.size)}${r.pct!=null?" · "+r.pct+"%":""} · ${dur(r.elapsed)}</div>
  <span class="pill b">${r.active?"ripping":"finishing"}</span>${bar(r.pct)}`));}
 else if(s.drive&&s.drive.present)c1.append(card("",`<div class=t>Disc inserted</div><div class=meta>idle — not processing</div>`));
 else c1.append(E("div","empty","drive empty"));
 b.append(c1);
 const c2=col("🎞️","Upscale Queue",s.queue.length);
 if(s.queue.length)s.queue.forEach((j,i)=>c2.append(card("queue",`<div class=t>${j.title}${yr(j.year)}</div><div class=meta>#${i+1} in line · waiting for ANE</div>`)));
 else c2.append(E("div","empty","no jobs waiting"));
 (s.failed||[]).forEach(f=>c2.append(card("fail",`<div class=t>${f}</div><div class=meta>failed — needs a look</div>`)));
 b.append(c2);
 const c3=col("⚡","Upscaling · ANE");
 if(s.upscaling){const u=s.upscaling;c3.append(card("up",`<div class=t>${u.title}${yr(u.year)}</div>
  <div class=meta>${human(u.size)} · ${dur(u.elapsed)}${u.active?"":" · idle"}</div>
  <span class="pill p">${u.stage||"working"}</span>${bar(null)}`));}
 else c3.append(E("div","empty","ANE idle"));
 b.append(c3);
 const c4=col("✅","Recently Done",s.done.length||null);
 if(s.done.length)s.done.forEach(d=>c4.append(card("done",`<div class=t>${d.title}${yr(d.year)}</div>`)));
 else c4.append(E("div","empty","nothing finished yet"));
 b.append(c4);
 const cl=s.cluster||{},c5=col("☁️","Library &amp; Cluster");
 const h=E("div","health");
 const row=(l,ok,extra)=>`<div class=row><span class="dot ${ok?'g':'r'}"></span>${l}<b>${extra!=null?extra:(ok?"online":"down")}</b></div>`;
 h.innerHTML=row("Nextcloud",cl.nextcloud)+row("Jellyfin",cl.jellyfin)+row("Movies indexed",cl.library!=null,cl.library!=null?cl.library:"?");
 c5.append(h);b.append(c5);
}
async function tick(){
 let s;try{s=await(await fetch("api/state")).json()}catch(e){document.getElementById("ts").textContent="offline — retrying";return}
 document.getElementById("ts").textContent="live · "+new Date().toLocaleTimeString();
 render(s);
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
