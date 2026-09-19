"""Talk to the Jellyfin API over **direct HTTP** to `jellyfin.base_url` (e.g. https://jellyfin.host).

No kubectl. The homelab server usually has a private-CA cert Python won't verify, so honor
`jellyfin.verify_tls`.

NB: Jellyfin 12.x rejects `X-Emby-Token` / `?api_key=` (401). The working auth form is the
header  Authorization: MediaBrowser Token="KEY".
"""
from __future__ import annotations

import http.client
import json
import ssl
import urllib.parse

from .config import Config


def _base_url(cfg: Config) -> str:
    return (cfg.get("jellyfin.base_url", "") or "").rstrip("/")


def _http(cfg: Config, method: str, path: str, *, body: bytes | None = None,
          content_type: str | None = None, timeout: int = 30) -> tuple[int, bytes]:
    """One request against jellyfin.base_url. Returns (status, body)."""
    u = urllib.parse.urlsplit(_base_url(cfg))
    if u.scheme == "https":
        ctx = ssl.create_default_context()
        if not bool(cfg.get("jellyfin.verify_tls", True)):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout, context=ctx)
    else:
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    headers = {"Authorization": f'MediaBrowser Token="{cfg.get("jellyfin.api_key", "")}"'}
    if content_type:
        headers["Content-Type"] = content_type
    try:
        conn.request(method, u.path.rstrip("/") + path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def list_movie_paths(cfg: Config) -> list[str]:
    """Every Movie item's on-disk Path, as Jellyfin sees it (used to cross-check that the ripping Mac
    and Jellyfin point at the same storage)."""
    status, body = _http(cfg, "GET", "/Items?Recursive=true&IncludeItemTypes=Movie&Fields=Path")
    if status != 200:
        raise RuntimeError(f"http {status}")
    return [it.get("Path") or "" for it in json.loads(body).get("Items", [])]


def find_item(cfg: Config, path_contains: str) -> str | None:
    """Jellyfin item id whose Path contains the given folder/file substring."""
    status, body = _http(cfg, "GET", "/Items?Recursive=true&IncludeItemTypes=Movie&Fields=Path")
    if status != 200:
        return None
    for it in json.loads(body).get("Items", []):
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
    payload = json.dumps({"ProviderIds": {"Tmdb": str(tmdb_id)}}).encode()
    code, _ = _http(cfg, "POST", f"/Items/RemoteSearch/Apply/{item_id}?ReplaceAllImages=true",
                    body=payload, content_type="application/json")
    return f"identified tmdb={tmdb_id} (http {code})"


def health(cfg: Config) -> tuple[bool, str]:
    """Confirm the Jellyfin API is up AND our api_key authenticates — hits `/System/Info` with the
    real auth header, so a stale/missing key is caught here, before a rip."""
    if not cfg.get("jellyfin.api_key", ""):
        return False, "no jellyfin.api_key set (refresh/identify will be skipped)"
    if not _base_url(cfg):
        return False, "no jellyfin.base_url set"
    try:
        code, raw = _http(cfg, "GET", "/System/Info")
    except OSError as e:
        return False, str(e)
    if code != 200:
        hint = " (api_key rejected)" if code == 401 else ""
        return False, f"http {code}{hint}"
    try:
        info = json.loads(raw)
        return True, f"http 200 — {info.get('ServerName', '?')} (Jellyfin {info.get('Version', '?')})"
    except (ValueError, AttributeError):
        return True, "http 200"


def refresh(cfg: Config) -> str:
    if not cfg.get("jellyfin.api_key", ""):
        return "skipped (no jellyfin.api_key)"
    if not _base_url(cfg):
        return "skipped (no jellyfin.base_url)"
    code, _ = _http(cfg, "POST", "/Library/Refresh")
    return f"http {code}" + ("" if code == 204 else " (expected 204)")
