"""Push a finished file into the Nextcloud library and make Jellyfin see it.

Transport is `kubectl exec` (services are ClusterIP-only): stream the file into the pod, chown
to the web user, `occ files:scan` the target folder, then trigger a Jellyfin refresh. The file
is streamed to a `.part` and atomically renamed so a partial transfer never gets indexed.
"""
from __future__ import annotations

import os

from . import jellyfin, kube
from .config import Config


def _occ_scan_path(data_path: str, rel_dir: str) -> str:
    """/var/www/html/data/HomeMedia/files + Videos/Movies/X -> HomeMedia/files/Videos/Movies/X"""
    root = data_path.split("/data/", 1)[1] if "/data/" in data_path else "HomeMedia/files"
    return f"{root.rstrip('/')}/{rel_dir}"


def plan(cfg: Config, local_path: str, rel_path: str) -> dict:
    k = cfg.get("deliver.kubectl", {})
    data_path = cfg.require("deliver.kubectl.data_path").rstrip("/")
    dest_abs = f"{data_path}/{rel_path}"
    occ_scan = _occ_scan_path(data_path, os.path.dirname(rel_path))
    return {
        "namespace": k.get("nextcloud_namespace", "nextcloud"),
        "container": k.get("nextcloud_container", "nextcloud"),
        "context": k.get("context"),
        "web_user": k.get("web_user", "www-data"),
        "occ": cfg.get("deliver.kubectl.occ", "php /var/www/html/occ").split(),
        "dest_abs": dest_abs,
        "dest_dir": os.path.dirname(dest_abs),
        "occ_scan": occ_scan,
        "steps": [
            f"mkdir -p {os.path.dirname(dest_abs)}",
            f"stream {os.path.getsize(local_path):,} bytes -> {dest_abs}",
            f"chown -R {k.get('web_user', 'www-data')} <folder>",
            f"occ files:scan --path={occ_scan}",
            "jellyfin POST /Library/Refresh",
        ],
    }


def push(cfg: Config, local_path: str, rel_path: str, dry_run: bool = False,
         scan: bool = True, refresh: bool = True) -> dict:
    p = plan(cfg, local_path, rel_path)
    if dry_run:
        p["dry_run"] = True
        return p

    ns, container, ctx = p["namespace"], p["container"], p["context"]
    k = cfg.get("deliver.kubectl", {})
    pod = kube.pod_name(ns, k.get("nextcloud_pod_selector", "app.kubernetes.io/name=nextcloud"),
                        context=ctx)

    kube.exec_in(ns, pod, ["mkdir", "-p", p["dest_dir"]], container=container, context=ctx)
    # stream file -> dest.part -> mv (dest passed as $1 so spaces/·/apostrophes stay safe)
    kube.exec_stdin_file(
        ns, pod,
        ["sh", "-c", 'd="$1"; cat > "$d.part" && mv -f "$d.part" "$d"', "_", p["dest_abs"]],
        local_path, container=container, context=ctx,
    )
    kube.exec_in(ns, pod, ["chown", "-R", f"{p['web_user']}:{p['web_user']}", p["dest_dir"]],
                 container=container, context=ctx)

    result = {"dest": p["dest_abs"]}
    if scan:
        argv = ["runuser", "-u", p["web_user"], "--", *p["occ"],
                "files:scan", f"--path={p['occ_scan']}"]
        result["scan"] = kube.exec_in(ns, pod, argv, container=container, context=ctx, timeout=900)
    if refresh:
        result["jellyfin"] = jellyfin.refresh(cfg)
    return result
