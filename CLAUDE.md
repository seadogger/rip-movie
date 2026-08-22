# CLAUDE.md

This project keeps its agent guidance in **[AGENTS.md](./AGENTS.md)** (architecture, module map,
conventions, and the non-obvious landmines). Read it before making changes.

@AGENTS.md

## Quick reminders for Claude Code

- **Stdlib only** in `ripmovie/` — no pip deps. Smoke-test edits with `python3 -m py_compile
  ripmovie/*.py`; there's no test suite (use `--dry-run` / `--sample N` and the live cluster).
- **Run it** via `./bin/rip-movie <cmd>` (no install).
- **Restart daemons after editing their code** — `dashboard`, `watch`, and `upscale-worker` don't
  hot-reload; a running process keeps the code it started with.
- **Secrets:** `config/rip-movie.toml` is `${ENV}` placeholders only (tracked); real keys live in
  `config/secrets.env` (gitignored). Never commit a real key.
- **Biggest footguns** (details in AGENTS.md): soft-telecine frame-rate lie (decode to get the true
  rate; stamp 23.976 CFR or VEAI judders); never resume on an incomplete VEAI render (`moov` atom is
  written last); the duration safety-net must stay; Jellyfin auth is `Authorization: MediaBrowser
  Token="…"`.
- Commit to `main`.
