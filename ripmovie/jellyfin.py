"""Trigger a Jellyfin library scan via curl inside the pod (services are ClusterIP-only).

NB: Jellyfin 12.x rejects `X-Emby-Token` / `?api_key=` (401). The working auth form is the
header  Authorization: MediaBrowser Token="KEY".
"""
from __future__ import annotations

import json

from . import kube
from .config import Config


def _conn(cfg: Config):
    j = cfg.get("jellyfin", {})
    ns = j.get("namespace", "jellyfin")
    pod = kube.pod_name(ns, j.get("pod_selector", "app.kubernetes.io/name=jellyfin"))
    auth = f'Authorization: MediaBrowser Token="{j.get("api_key", "")}"'
    base = f"http://localhost:{j.get('port', 8096)}"
    return ns, pod, auth, base


def find_item(cfg: Config, path_contains: str) -> str | None:
    """Jellyfin item id whose Path contains the given folder/file substring."""
    ns, pod, auth, base = _conn(cfg)
    out = kube.exec_in(ns, pod, ["curl", "-s", "-H", auth,
                                 f"{base}/Items?Recursive=true&IncludeItemTypes=Movie&Fields=Path"])
    for it in json.loads(out).get("Items", []):
        if path_contains in (it.get("Path") or ""):
            return it.get("Id")
    return None


def force_identify(cfg: Config, path_contains: str, tmdb_id) -> str:
    """Pin a movie item to a known TMDb id so Jellyfin won't fuzzy-mismatch it."""
    if not tmdb_id:
        return "skipped (no tmdb id)"
    item_id = find_item(cfg, path_contains)
    if not item_id:
        return f"item not found for {path_contains!r}"
    ns, pod, auth, base = _conn(cfg)
    code = kube.exec_in(ns, pod, [
        "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-X", "POST",
        "-H", auth, "-H", "Content-Type: application/json",
        f"{base}/Items/RemoteSearch/Apply/{item_id}?ReplaceAllImages=true",
        "-d", json.dumps({"ProviderIds": {"Tmdb": str(tmdb_id)}}),
    ]).strip()
    return f"identified tmdb={tmdb_id} (http {code})"


def refresh(cfg: Config) -> str:
    j = cfg.get("jellyfin", {})
    key = j.get("api_key", "")
    if not key:
        return "skipped (no jellyfin.api_key)"
    ns = j.get("namespace", "jellyfin")
    pod = kube.pod_name(ns, j.get("pod_selector", "app.kubernetes.io/name=jellyfin"))
    port = j.get("port", 8096)
    auth = f'Authorization: MediaBrowser Token="{key}"'
    code = kube.exec_in(
        ns, pod,
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-X", "POST",
         "-H", auth, f"http://localhost:{port}/Library/Refresh"],
    ).strip()
    return f"http {code}" + ("" if code == "204" else " (expected 204)")
