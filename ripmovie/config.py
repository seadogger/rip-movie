"""Configuration loading: TOML + ${ENV} expansion + ~ path expansion.

Zero third-party deps (tomllib is stdlib on Python 3.11+).
"""
from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Keys whose string values should be treated as filesystem paths (~ expanded).
_PATH_KEYS = {
    "work_dir", "log_dir", "state_dir", "makemkvcon", "handbrakecli",
    "ffmpeg", "rclone", "app",
}


def _expand(value: Any, key: str | None = None) -> Any:
    if isinstance(value, str):
        # Unset ${VARS} expand to "" so config still loads; commands that actually
        # need a given secret report it as missing themselves (see `config-check`).
        value = _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
        if key in _PATH_KEYS and (value.startswith("~") or value.startswith("~/")):
            value = str(Path(value).expanduser())
        return value
    if isinstance(value, dict):
        return {k: _expand(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v, key) for v in value]
    return value


class ConfigError(Exception):
    pass


class Config:
    """Dotted-path read access over the parsed TOML."""

    def __init__(self, data: dict, path: Path):
        self._data = data
        self.path = path

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        path = cls._resolve_path(path)
        if not path.exists():
            raise ConfigError(
                f"no config at {path}. Copy config/rip-movie.example.toml to config/rip-movie.toml."
            )
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
        return cls(_expand(raw), path)

    @staticmethod
    def _resolve_path(path: str | Path | None) -> Path:
        if path:
            return Path(path).expanduser()
        env = os.environ.get("RIP_MOVIE_CONFIG")
        if env:
            return Path(env).expanduser()
        # repo-local default: <repo>/config/rip-movie.toml
        return Path(__file__).resolve().parent.parent / "config" / "rip-movie.toml"

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self._data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def require(self, dotted: str) -> Any:
        val = self.get(dotted, _MISSING)
        if val is _MISSING:
            raise ConfigError(f"required config key missing: {dotted}")
        return val

    def path_for(self, dotted: str) -> Path:
        return Path(self.require(dotted)).expanduser()


_MISSING = object()
