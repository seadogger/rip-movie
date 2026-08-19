"""Read + surgically edit the TOML config for the dashboard's config page.

Editing is a section-aware, in-place line replacement: only the value on a `key = value` line is
swapped, so every comment and bit of formatting is preserved. Secret values (`${VAR}`) live in
config/secrets.env — editing a secret writes there, and the raw `${VAR}` reference stays in the TOML.
`test_all` validates whatever keys are present (tool paths, dirs, TMDb key, cluster, Jellyfin).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

from .config import Config

_SECRET_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


# ---- read ---------------------------------------------------------------------------------------
def _flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


def _inline_comments(text: str) -> dict:
    """Map dotted-key -> trailing inline comment, by scanning the raw file with section tracking."""
    comments, section = {}, ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1].strip()
            continue
        m = re.match(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.*)$", line)
        if not m:
            continue
        key = m.group(1)
        c = re.search(r"#\s*(.*)$", m.group(2))
        dotted = f"{section}.{key}" if section else key
        if c:
            comments[dotted] = c.group(1).strip()
    return comments


def secrets_path(cfg_path: Path) -> Path:
    return Path(cfg_path).parent / "secrets.env"


def _env_set(cfg_path: Path) -> set:
    """Env var names that currently have a value (from the process env or secrets.env)."""
    have = {k for k, v in os.environ.items() if v}
    sp = secrets_path(cfg_path)
    if sp.exists():
        for line in sp.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if v.strip().strip('"').strip("'"):
                    have.add(k.strip())
    return have


def entries(cfg_path: str | Path) -> list[dict]:
    """One dict per editable key: {section, key, dotted, value, type, secret, env, secret_set, comment}."""
    cfg_path = Path(cfg_path)
    raw = tomllib.loads(cfg_path.read_text())
    flat = _flatten(raw)
    comments = _inline_comments(cfg_path.read_text())
    have = _env_set(cfg_path)
    out = []
    for dotted, val in flat.items():
        section, _, key = dotted.rpartition(".")
        m = _SECRET_RE.match(val) if isinstance(val, str) else None
        e = {"section": section, "key": key, "dotted": dotted,
             "comment": comments.get(dotted, "")}
        if m:
            e.update(secret=True, env=m.group(1), secret_set=(m.group(1) in have),
                     value="", type="secret")
        else:
            e.update(secret=False, value=val,
                     type=("bool" if isinstance(val, bool) else "int" if isinstance(val, int)
                           else "float" if isinstance(val, float) else "list" if isinstance(val, list)
                           else "str"))
        out.append(e)
    return out


# ---- write --------------------------------------------------------------------------------------
def _format(value, like_type: str) -> str:
    if like_type == "bool":
        return "true" if (value in (True, "true", "True", 1, "1")) else "false"
    if like_type == "int":
        return str(int(value))
    if like_type == "float":
        return str(float(value))
    if like_type == "list":
        items = value if isinstance(value, list) else [s.strip() for s in str(value).split(",") if s.strip()]
        return "[" + ", ".join(f'"{str(i)}"' for i in items) + "]"
    return '"' + str(value).replace('"', '\\"') + '"'


def _value_end(rest: str) -> int:
    if not rest:
        return 0
    if rest[0] == '"':
        j = 1
        while j < len(rest):
            if rest[j] == "\\":
                j += 2
                continue
            if rest[j] == '"':
                return j + 1
            j += 1
        return len(rest)
    if rest[0] == "[":
        depth = 0
        for j, ch in enumerate(rest):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return j + 1
        return len(rest)
    m = re.match(r"[^\s#]+", rest)
    return m.end() if m else 0


def set_value(cfg_path: str | Path, dotted: str, new_value) -> None:
    """Replace the value of an existing scalar/list key in place, preserving comments. Secret keys
    (${VAR}) are written to secrets.env instead, leaving the TOML reference untouched."""
    cfg_path = Path(cfg_path)
    ents = {e["dotted"]: e for e in entries(cfg_path)}
    if dotted not in ents:
        raise ValueError(f"unknown config key: {dotted}")
    ent = ents[dotted]
    if ent["secret"]:
        _set_secret(cfg_path, ent["env"], str(new_value))
        return

    section, key = ent["section"], ent["key"]
    formatted = _format(new_value, ent["type"])
    lines = cfg_path.read_text().splitlines(keepends=True)
    cur_section, done = "", False
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            cur_section = s[1:-1].strip()
            continue
        if cur_section != section:
            continue
        m = re.match(r"^(\s*" + re.escape(key) + r"\s*=\s*)(.*?)(\r?\n?)$", line)
        if m:
            prefix, rest, nl = m.group(1), m.group(2), m.group(3)
            end = _value_end(rest)
            lines[i] = f"{prefix}{formatted}{rest[end:]}{nl}"
            done = True
            break
    if not done:
        raise ValueError(f"could not locate {dotted} in {cfg_path}")
    cfg_path.write_text("".join(lines))


def _set_secret(cfg_path: Path, env_name: str, value: str) -> None:
    sp = secrets_path(cfg_path)
    lines = sp.read_text().splitlines() if sp.exists() else []
    row = f'{env_name}="{value}"'
    for i, line in enumerate(lines):
        if re.match(rf"^\s*{re.escape(env_name)}\s*=", line):
            lines[i] = row
            break
    else:
        lines.append(row)
    sp.write_text("\n".join(lines) + "\n")
    os.environ[env_name] = value        # so the running dashboard sees it immediately


# ---- test ---------------------------------------------------------------------------------------
def _bin_ok(target: str) -> tuple[bool, str]:
    p = shutil.which(target) or (target if Path(target).exists() else None)
    return (bool(p), p or "not found")


def test_all(cfg: Config) -> dict:
    """Validate whatever keys are present -> {dotted_key: {ok, detail}}. Best-effort + bounded."""
    r: dict = {}

    def add(k, ok, detail):
        r[k] = {"ok": bool(ok), "detail": detail}

    # tool binaries
    for key in ("makemkvcon", "handbrakecli", "ffmpeg", "rclone", "mkvmerge", "mkvextract",
                "tesseract", "torch_python"):
        val = cfg.get(f"paths.{key}")
        if val:
            ok, where = _bin_ok(str(Path(val).expanduser()))
            add(f"paths.{key}", ok, where)
    # tool scripts / model dirs
    for key in ("vobsub_ocr", "enhance_stream", "coreml_infer", "upscale_torch",
                "torch_models", "realesrgan", "realesrgan_models"):
        val = cfg.get(f"paths.{key}")
        if val:
            p = Path(val).expanduser()
            add(f"paths.{key}", p.exists(), str(p) if p.exists() else "missing")
    # working dirs (create-if-needed writability)
    for key in ("work_dir", "log_dir", "state_dir"):
        val = cfg.get(f"paths.{key}")
        if val:
            p = Path(val).expanduser()
            try:
                p.mkdir(parents=True, exist_ok=True)
                add(f"paths.{key}", os.access(p, os.W_OK), f"writable: {p}")
            except OSError as e:
                add(f"paths.{key}", False, str(e))
    # TMDb key
    key = cfg.get("identify.tmdb_api_key", "")
    if key:
        try:
            import urllib.request
            with urllib.request.urlopen(
                    f"https://api.themoviedb.org/3/configuration?api_key={key}", timeout=12) as resp:
                add("identify.tmdb_api_key", resp.status == 200, f"TMDb responded {resp.status}")
        except Exception as e:  # noqa: BLE001
            add("identify.tmdb_api_key", False, f"TMDb error: {str(e)[:80]}")
    else:
        add("identify.tmdb_api_key", False, "not set")
    # cluster: context, nextcloud pod + data_path, jellyfin pod
    try:
        from . import kube
        k = cfg.get("deliver.kubectl", {})
        ctx = k.get("context")
        add("deliver.kubectl.context", kube.reachable(ctx), f"context={ctx}")
        ns = k.get("nextcloud_namespace")
        pod = kube.pod_name(ns, k.get("nextcloud_pod_selector"), context=ctx)
        add("deliver.kubectl.nextcloud_pod_selector", True, pod)
        dp = cfg.get("deliver.kubectl.data_path", "")
        out = kube.exec_in(ns, pod, ["sh", "-c", f'test -d "{dp}" && echo ok || echo no'],
                           container=k.get("nextcloud_container"), context=ctx, timeout=20)
        add("deliver.kubectl.data_path", "ok" in out, dp)
    except Exception as e:  # noqa: BLE001
        add("deliver.kubectl.context", False, f"cluster: {str(e)[:80]}")
    try:
        from . import kube
        jpod = kube.pod_name(cfg.get("jellyfin.namespace"), cfg.get("jellyfin.pod_selector"),
                             context=cfg.get("deliver.kubectl.context"))
        add("jellyfin.pod_selector", True, jpod)
    except Exception as e:  # noqa: BLE001
        add("jellyfin.pod_selector", False, f"jellyfin: {str(e)[:80]}")
    if cfg.get("jellyfin.api_key"):
        try:
            from . import jellyfin
            res = jellyfin.refresh(cfg)
            add("jellyfin.api_key", str(res).startswith("http 2"), str(res))
        except Exception as e:  # noqa: BLE001
            add("jellyfin.api_key", False, str(e)[:80])
    return r
