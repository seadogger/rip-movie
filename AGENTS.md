# AGENTS.md — working on rip-movie

Guidance for AI coding agents (and humans) working in this repo. Read `README.md` first for the
product overview; this file is the architecture + conventions + landmines.

## What it is

A hands-off pipeline that takes a physical disc (or an existing DVD-quality library title) to
Jellyfin: **identify → rip → deliver lossless master → AI-upscale SD to 1080p → deliver rendition**.
It runs on a Mac (Apple silicon) and delivers to a Nextcloud/Jellyfin homelab on a k3s cluster that
**cannot transcode**, so every watchable file must direct-play on Apple devices.

## Run & test

- **No third-party Python deps** in the package — stdlib only (`tomllib`, `urllib`, `http.server`,
  `subprocess`). Target Python 3.14. Do not add pip dependencies to `ripmovie/`. (The `tools/`
  upscalers use a separate venv — that's fine, they're subprocesses.)
- **Entry point:** `./bin/rip-movie <cmd>` (a launcher that puts the repo on `sys.path` and calls
  `ripmovie.cli.main`). There's no install step.
- **Smoke test after edits:** `python3 -m py_compile ripmovie/*.py`. There's no unit-test suite;
  validate behavior with `--dry-run` flags, short `--sample N` clips, and the live cluster.
- **Config lives in `config/rip-movie.toml`** (tracked, uses `${ENV}` placeholders — never put real
  keys here). Real secrets go in `config/secrets.env` (gitignored, auto-loaded by `cli._load_secrets`
  before config expansion). `TMDB_API_KEY` and `JELLYFIN_API_KEY` are expected there.

## Module map (`ripmovie/`)

| Module | Responsibility |
|---|---|
| `config.py` | TOML load + `${ENV}` + `~` expansion. `Config.get/require/path_for`. |
| `cli.py` | argparse entrypoint; one `cmd_*` per subcommand. |
| `disc.py` | MakeMKV disc scan + title selection heuristic (runtime match, dup-collapse, ambiguity → review). |
| `identify.py` | TMDb identify + `search_tmdb` (title/year → folder, runtime, tmdb_id, is_animation). |
| `library.py` | List/search the Nextcloud Movies+TV tree (parses res/codec from filenames). `upscale_candidates` classifies each movie for the viewer. |
| `naming.py` | Schema naming: `target()`, `res_tag()` (width-aware), `codec_tag()`, `probe()`. |
| `rip.py` | `rip_title` — makemkvcon robot-mode rip with per-phase progress. |
| `enhance.py` | Real-ESRGAN/CoreML-ANE upscale (streaming). Also the shared **cadence** (`detect_cadence`), **crop** (`detect_crop`), and **duration** (`_duration`, `_check_duration`) helpers. |
| `finalize.py` | `mux_rendition` (Apple-native audio), `make_subtitle_sidecar` (text extract or VobSub OCR). |
| `topaz.py` | **Topaz VEAI 2.6.4 handoff engine:** `prep` → `find_output` → `resume`, plus `_geometry`, `_true_fps`, `_snap_fps`, `_target_fps`, `_model_note`, the inbox how-to. |
| `pipeline.py` | Orchestration: `deliver_master`, `deliver_rendition`/`_finalize_rendition`, `process_disc`, the upscale queue, `run_upscale_worker` (dispatches by engine) / `run_topaz_handoff_worker`, `enqueue_existing`, `_ensure_local_source`. |
| `deliver.py` | `push` — stream to the Nextcloud pod (`.part` → atomic rename), chown, `occ files:scan`, Jellyfin refresh. |
| `kube.py` | Thin `kubectl` wrappers: `pod_name`, `exec_in`, `exec_stdin_file` (local→pod), `exec_stdout_file` (pod→local). |
| `jellyfin.py` | Jellyfin API over `kubectl exec curl`: `refresh`, `find_item`, `force_identify`. |
| `dashboard.py` | stdlib HTTP server: pipeline swimlanes (`gather`/`_build_lanes`), the `/library` upgrade-DVDs viewer (`library_view`), `/config` editor, search. |
| `status.py` | Tiny JSON status files under `state_dir/status/` the dashboard polls. |
| `config_edit.py` | Comment-preserving TOML read/edit for the config page. |
| `watch.py` | Disc-watch daemon, `eject`, `disc_present`. |

## The pluggable upscale engine

`upscale.engine` selects the DVD→1080p path; `run_upscale_worker` dispatches on it:

- **`topaz-veai-handoff`** (current): `run_topaz_handoff_worker`. Per queued job, each loop:
  1. **prep** pending `*.json` jobs → `topaz.prep` builds a video-only clip in the inbox, parks the
     job as `<slug>.awaiting` (the manifest).
  2. **resume** any `*.awaiting` job whose *complete* render appears in the outbox → `topaz.resume`.
- **`realesrgan`**: the ANE streaming path via `enhance.enhance` inline in `deliver_rendition`.

Both share `_finalize_rendition` (mux audio + OCR subs + deliver + success-gated cleanup) and
`_ensure_local_source` (pull a library master from Nextcloud if the job's `source` isn't local).

### Job lifecycle (files in `state_dir/upscale_queue/`)

`<slug>.json` (pending) → `.running` (claimed) → `.awaiting` (prepped, manifest) → `.resuming` →
delivered (file removed) or `.failed`. `queue_library_upscale` writes a job with `source_remote`
(the pod path) for existing-library upscales.

## Non-obvious things that WILL bite you

- **Soft-telecine frame rate.** DVD film is 23.976 progressive but the container advertises 29.97 —
  and **both `r_frame_rate` and `avg_frame_rate` lie**; only decoding reveals the truth (`ffmpeg -t N
  -f null` frame count). `topaz._true_fps` decodes-and-counts; `_snap_fps` snaps to NTSC `/1001`
  rates only (no exact "24"/"30", so a rounded 24.0 → `24000/1001`). The prep MUST stamp the true
  rate (`-r … -fps_mode cfr`) or VEAI duplicates frames → judder.
- **VEAI writes MP4 with the `moov` atom at the end.** A mid-render file is unreadable (duration 0)
  or short. `find_output` requires a *complete, full-length* render (duration ≈ manifest's expected)
  before resuming — never resume on a partial. A steady file size through the settle window does NOT
  mean "done."
- **Duration safety-net.** `_check_duration` refuses any rendition whose length drifts >2.5s from
  the source. Keep it — it's what stops partial/desynced renditions from reaching the library.
- **VEAI re-adds letterbox** when its output preset is 16:9 for a scope clip. `resume` auto-crops it
  back (`detect_crop`) before conforming to the target geometry.
- **Jellyfin auth:** header `Authorization: MediaBrowser Token="KEY"` (NOT `X-Emby-Token` / `?api_key`
  — those 401). Library scan = `POST /Library/Refresh` (async, 204). Force-match a movie with
  `POST /Items/RemoteSearch/Apply/{id}` `{"ProviderIds":{"Tmdb":"<id>"}}`.
- **Path map:** Nextcloud `data/HomeMedia/files/Videos/Movies` = Jellyfin
  `/media/data/HomeMedia/files/Videos/Movies`. Files with no video extension are invisible to
  Jellyfin — naming always appends the real container ext.
- **`occ` runs as www-data:** `su -s /bin/sh www-data -c '<occ> files:scan --path="HomeMedia/…"'`.
- **The dashboard is stdlib `http.server` and loads code at start** — restart it to pick up code
  changes. The `/library` page fetches once (no auto-poll); a browser refresh reflects new state.
- **Modules don't hot-reload** — a long-running `watch`/`upscale-worker`/`dashboard` keeps the code
  it started with. Restart daemons after editing their code.

## Conventions

- Match the surrounding style: terse, purposeful comments that explain *why* (the cadence/geometry/
  sync reasoning), dataclasses for structured results, `Callable progress=print` for status output,
  best-effort `try/except` around cluster calls so one failure doesn't crash a run.
- Delivery/cleanup is **success-gated**: only remove local temps once delivery is confirmed; a failed
  run keeps its files for retry.
- Commit to `main` (solo repo, all history on main). Keep secrets out of commits — `rip-movie.toml`
  is placeholders-only, `secrets.env` is gitignored.
