# rip-movie

**Insert a disc, walk away.** `rip-movie` takes a physical DVD/Blu-ray from the tray to Jellyfin
with no babysitting: it rips the disc, delivers a lossless master to Nextcloud so Jellyfin picks it
up immediately, and — for standard-def discs — queues an AI upscale that lands a 1080p, Apple
direct-play rendition beside it. It can also **upgrade the DVDs already in your library**: point it
at your Movies folder, and it re-upscales every SD title in place.

```mermaid
flowchart LR
  disc([💿 disc]) --> rip[rip · MakeMKV]
  rip --> master[master .mkv<br/>→ Nextcloud → Jellyfin]
  master --> gate{≤ 576p?}
  gate -- "HD / 4K" --> done1([✅ in Jellyfin])
  gate -- "DVD / SD" --> queue[[upscale queue]]
  lib([📼 existing library<br/>DVD-quality movies]) --> queue
  queue --> worker[worker · prep clip]
  worker --> inbox[/inbox/]
  inbox -. "you: one VEAI batch" .-> outbox[/outbox/]
  outbox --> resume[resume · mux + deliver]
  resume --> rend[1080p .mp4 + .srt<br/>→ Nextcloud → Jellyfin]
  rend --> clean[cleanup] --> done2([✅ in Jellyfin])
```

Ripping is disc-bound (~20 min); the AI upscale runs through **Topaz Video Enhance AI** as a
**semi-automated handoff** (one GUI batch you kick off, everything else automatic). The two are
**decoupled by a queue**: a disc rips and delivers its master fast, then its upscale waits its turn.

## Watch it live

`rip-movie dashboard` serves a live view at `http://localhost:8422`:

- **Pipeline board** — one swimlane per movie through Rip → Master → Nextcloud → Upscale → Cleanup →
  In Jellyfin. Lane colors: **amber** = the pipeline is working on it, **blue slow-flash** = a clip
  is prepped in your inbox and waiting on you to run Topaz, **green** = in Jellyfin.
- **📼 upgrade DVDs** (`/library`) — every movie already in your library, classified by quality.
  DVD-quality titles get a one-click **Queue upscale** button (or Queue all).
- **⚙ config** (`/config`) — edit `rip-movie.toml` from the browser and run a health check.
- A **library search** over movies + TV, and a **recent-pushes** feed.

![The rip-movie dashboard — one swimlane per movie](docs/dashboard.png)

## The environment this is built for

| Piece | Detail |
|---|---|
| Rip station | Mac (Apple silicon) + USB ASUS BW-16D1HT BD/DVD drive |
| Tools | `makemkvcon`, `ffmpeg`, `tesseract` + `mkvtoolnix` (subtitle OCR), `kubectl` |
| Upscaler | **Topaz Video Enhance AI 2.6.4** (GUI, watermark-free on an owned license), model **Proteus** |
| Library store | Nextcloud user **HomeMedia** → `Videos/Movies/` and `Videos/TV_Shows/` on CephFS |
| Player | Jellyfin on a **4× Raspberry Pi 5 (k3s)** cluster — **no hardware encoder, never transcodes** |
| Clients | Apple TV, iPhone, iPad only — **no DTS decode**, so everything must **direct-play** |

Because the Pi 5 cluster can't transcode, every watchable file must direct-play on Apple devices.
That single constraint drives every encoding decision below.

## Architecture (C4 model)

Three zoom levels — the system in its world, the runtime pieces, then the code inside the pipeline.

### Level 1 — System context

Who and what `rip-movie` talks to. The user does two physical things (load discs, run one Topaz
batch); everything else is automated.

```mermaid
flowchart TB
  user([👤 User<br/>loads discs · runs 1 VEAI batch]):::person
  subgraph sys[rip-movie · the system]
    rm[["disc → library pipeline<br/>+ dashboard + daemons"]]
  end
  drive([💿 Optical drive<br/>MakeMKV / libdvdcss]):::ext
  tmdb([🎬 TMDb<br/>title · runtime · id]):::ext
  tvdb([📺 TheTVDB<br/>episode numbers]):::ext
  store([🗄️ Media store<br/>Nextcloud / mount / local]):::ext
  player([▶️ Player<br/>Jellyfin / Plex]):::ext
  veai([✨ Topaz VEAI 2.6.4<br/>GUI upscaler]):::ext
  s3([☁️ Backup · S3<br/>optional]):::ext

  user --> rm
  drive --> rm
  rm <--> tmdb
  rm <--> tvdb
  rm --> store --> player
  rm -. "prep clip / resume render" .-> veai
  rm -. optional .-> s3
  classDef person fill:#1f6feb,color:#fff,stroke:#0b4;
  classDef ext fill:#eee,color:#333,stroke:#999;
```

### Level 2 — Containers

The runtime pieces and how they exchange work. The **queue decouples** the fast rip from the slow,
semi-manual upscale.

```mermaid
flowchart TB
  cli["CLI · ./bin/rip-movie<br/>(argparse entrypoint)"]
  subgraph pkg[ripmovie/ · orchestration package · stdlib only]
    pipe["pipeline.py<br/>process_disc · deliver · upscale queue"]
    engines["rip · identify · naming · finalize<br/>enhance · topaz"]
  end
  watch["watch daemon<br/>disc-in → rip → deliver → eject"]
  worker["upscale-worker daemon<br/>drains queue · prep→resume"]
  dash["dashboard · stdlib http.server<br/>swimlanes · /library · /config · :8422"]
  state[("state_dir/<br/>status · upscale_queue · review")]
  helpers["helpers · ~/rip-movie/work/.helpers/<br/>rip_batch · chunked_upload · ddrescue"]
  tools["external tools<br/>makemkvcon · ffmpeg · tesseract<br/>dvdbackup/libdvdcss · ddrescue · VEAI"]
  store([media store]):::ext
  player([player]):::ext

  cli --> pkg
  watch --> pipe
  worker --> pipe
  pipe --> engines --> tools
  pipe --> state
  worker --> state
  dash --> state
  dash --> pipe
  pipe --> store --> player
  helpers --> tools
  helpers --> store
  classDef ext fill:#eee,color:#333,stroke:#999;
```

### Level 3 — Components (inside the pipeline)

The modules in `ripmovie/` and their responsibilities (full table in **[AGENTS.md](./AGENTS.md)**).

```mermaid
flowchart LR
  disc["disc.py<br/>scan · title-select"] --> pipeline
  identify["identify.py<br/>TMDb match"] --> pipeline
  naming["naming.py<br/>schema names"] --> pipeline
  rip["rip.py<br/>MakeMKV rip"] --> pipeline
  pipeline{{"pipeline.py<br/>orchestration"}}
  pipeline --> enhance["enhance.py<br/>cadence · crop · duration"]
  pipeline --> topaz["topaz.py<br/>VEAI handoff"]
  pipeline --> finalize["finalize.py<br/>mux + subs OCR"]
  pipeline --> deliver["deliver.py<br/>store copy"]
  pipeline --> jellyfin["jellyfin.py<br/>refresh · identify"]
  pipeline --> status["status.py<br/>dashboard JSON"]
  library["library.py<br/>tree · upscale-candidates"] --> dashboard["dashboard.py"]
  config["config.py<br/>TOML + env expansion"] --- pipeline
```

## Two-tier output: lossless master + Apple rendition

Every movie gets **two files in the same folder**, matching the library's existing pattern:

1. **Master** — a lossless, untouched stream-copy of the disc (`.mkv`). All audio tracks in their
   original codec (DTS/TrueHD included), original bitmap subtitles, nothing re-encoded. This is the
   *rip-once* keeper: the physical disc is never needed again, and better renditions can be
   re-derived from it as upscalers improve.
2. **Rendition** — the watchable copy, generated when the source needs it:

   | Master | Rendition |
   |---|---|
   | **DVD / MPEG-2 (≤576p)** | **AI upscale → 1080p H.264** `.mp4` + OCR'd English `.srt` |
   | H.264 / HEVC 1080p or 4K | none — the master already direct-plays |

   HD/4K sources are **not upscaled** — they're already full resolution — so they ship as the ripped
   master.

### Naming schema (reverse-engineered from the existing library)

```
Movies/{Title} ({Year})/{Title} ({Year}) - {resTag} {codecTag}.{ext}
  e.g.  Armageddon (1998)/Armageddon (1998) - 480p MPEG.mkv     ← master
        Armageddon (1998)/Armageddon (1998) - 1080p AVC.mp4     ← rendition
        Armageddon (1998)/Armageddon (1998) - 1080p AVC.eng.srt ← OCR'd subtitles
```
`resTag` → `480p`/`720p`/`1080p`/`2160p` (width-aware, so a 1920×828 scope film is `1080p`, not
`720p`) · `codecTag` → `AVC`/`HEVC`/`MPEG`/`Microsoft` (VC-1).

## The upscale — a Topaz handoff

DVD upscaling runs through a **pluggable engine** (`upscale.engine`):

- **`topaz-veai-handoff`** (default, best quality) — Topaz Video Enhance AI 2.6.4. It's GUI-only (no
  CLI) and watermark-free on an owned license, so the pipeline can't drive it headlessly. Instead it
  **splits the upscale around one manual step** and automates everything else.
- **`realesrgan`** — Real-ESRGAN compiled to CoreML on the Neural Engine. Fully automated (no manual
  step), but its halo/wash-out tradeoffs are why we landed on Topaz.

### How the handoff works

```
prep    source → IVTC/decimate + autocrop + un-anamorph → a VIDEO-ONLY clip in ~/rip-movie/topaz/inbox/
        (correctly stamped 23.976, tagged [Proteus] for live action / [Proteus no-grain] for animation)
you     open Video Enhance AI, drag in every inbox clip, run your saved Proteus preset (output 1920-wide
        → ~/rip-movie/topaz/outbox/), and walk away — VEAI processes the batch back-to-back
resume  when a COMPLETE, full-length render appears in the outbox, the pipeline crops off any letterbox
        VEAI re-added, locks a clean 23.976 CFR, muxes the master's original audio (Apple-ified) + OCR'd
        subs, delivers the 1080p rendition, and cleans up
```

Audio and subtitles **never go through Topaz** — only video round-trips, so A/V sync is guaranteed
as long as the frame count is preserved. Two things the prep gets right that otherwise cause grief:

- **Frame rate.** DVD film is 23.976 progressive but the container is flagged 29.97 (soft telecine).
  VEAI trusts that flag and duplicates ~1-in-5 frames to "fill" 29.97 → periodic judder. The prep
  **decodes the true rate and stamps the clip 23.976 CFR**, so VEAI never duplicates.
- **Geometry.** A 2.35 scope film letterboxed on a 4:3 DVD is de-barred and un-anamorphed before
  Topaz (so it upscales the *real* picture), and VEAI's re-added 16:9 padding is auto-cropped after.

**Model:** Proteus (Fine Tune) for everything — the only difference is **Grain: ON for live action**
(fights the plastic look), **OFF for animation** (cel/CGI is clean; grain looks wrong). Save two
VEAI presets; each inbox clip's `[tag]` says which to run.

**Audio (rendition):** every source track kept — Apple-native codecs (AC3/AAC/E-AC3) copied as-is,
DTS/TrueHD transcoded to AC3 5.1, plus one AAC stereo default. The *original* DTS stays in the
master. **Subtitles:** English only — an existing text track is used directly, otherwise the DVD's
bitmap VobSub is OCR'd to a sidecar `.srt` with tesseract (~98% accurate).

## Upgrade your existing DVD library

Open **📼 upgrade DVDs** on the dashboard. It scans your Movies folder, classifies every title
(DVD-quality / already-upscaled / HD), and lets you **queue any ≤576p movie that has no HD copy**.
When the worker picks one up it **pulls the master back from Nextcloud**, preps it to the inbox, and
— after your VEAI batch — delivers the 1080p rendition beside the DVD master. So a whole backlog
becomes: tick the movies → let the worker prep them → one overnight VEAI batch → they upgrade
themselves.

## Title selection — the "black art"

Commercial discs expose many titles; picking wrong wastes hours. The heuristic, strongest signal
first:

- **TMDb runtime match** — pick the title whose duration is closest to the movie's known runtime.
  This cuts straight through decoy playlists and episodic discs.
- **Duplicate collapse** — identical duration + chapter count = the same feature listed twice.
- **Ambiguity guard** — otherwise, if several titles are near-equal length, the disc goes to a
  **review queue** rather than guessing.

DVD volume labels are often unusable (`ARMAGEDN`, `COURAGEUNDERFIREDTSVER3`). When auto-identify
can't recover one, pass `--name "Armageddon" --year 1998` and the runtime match handles the rest.

## Skills (Claude Code)

The repo ships **agent skills** under `.claude/skills/` — runbooks an agent (or a human) follows to
drive the pipeline. Invoke with `/<name>`.

| Skill | Scope | What it does |
|---|---|---|
| **`/rip`** | Orchestrator (movies + TV, many discs) | Establishes the **goal** (player, store, optional S3 backup), has the user place credentials in `config/secrets.env`/Keychain **without the agent reading them**, builds the delivery plan, runs `config-check`, then loops each disc: identify → confirm → rip → deliver → verify → refresh → clean. Handles **CSS-protected discs** (`dvdbackup`+libdvdcss decrypt-mirror), **scratched discs** (`ddrescue` forward+reverse), **drive brown-outs** (power-cycle guidance), and **TV episode identification by on-screen title card → TheTVDB**. |
| **`/rip-disc`** | Focused (one confirmed disc → Jellyfin) | The narrow flow: preflight → identify → **title-confirmation gate** → rip → `push` to the store → Jellyfin refresh → verify, with the live dashboard up. No goal/store setup — assumes the repo's default Nextcloud+Jellyfin target. |

Capabilities the `/rip` skill encodes (learned the hard way, so they don't get re-discovered):

- **Title-card episode ID** — off-air/compilation discs are in arbitrary order; identify each title
  from a `ffmpeg` contact-sheet of its title card and map to **TheTVDB** `S##E##` (segment-level for
  Animaniacs/Care Bears; dual-episode files as `S01E16-E17`; skip play-alls and cross-promo trailers).
- **Chunked WebDAV** delivery for large files / flaky sync clients (10 MB `dd`-streamed chunks).
- **`dvdbackup` + libdvdcss** decrypt-mirror when MakeMKV's LibreDrive won't engage on a CSS disc,
  then a lossless rip from the local copy.
- **`ddrescue`** forward+reverse imaging (with resumable mapfile) for scratched discs.
- **Secrets discipline** — the agent never reads keys; they live in gitignored `secrets.env` / the
  Keychain and are consumed only via `${ENV}` expansion at runtime.

## Services, daemons & dashboards

| Service | Command | Role |
|---|---|---|
| **Dashboard** | `rip-movie dashboard` | stdlib `http.server` at **`:8422`** — pipeline swimlanes (Rip→Master→Store→Upscale→Cleanup→In Player), the **📼 `/library`** upgrade-DVDs viewer, the **⚙ `/config`** comment-preserving TOML editor + health check, library search, and a recent-pushes feed. Loads code at start — **restart to pick up changes**. |
| **Disc watcher** | `rip-movie watch` | Daemon: waits for a disc, rips it, delivers the master, queues the upscale, ejects — then waits for the next. |
| **Upscale worker** | `rip-movie upscale-worker` | Daemon: drains the upscale queue serially (Topaz handoff *or* Real-ESRGAN), pulling masters back from the store for existing-library re-upscales. |
| **Status files** | (`status.py`) | Tiny JSON under `state_dir/status/` the dashboard polls — the glue between daemons and the live view. |

Daemons **don't hot-reload** — restart `watch`/`upscale-worker`/`dashboard` after editing their code.

## Commands

```bash
rip-movie config-check                  # validate config, tools, cluster reachability
rip-movie dashboard                     # live dashboard + upgrade-DVDs viewer at http://localhost:8422
rip-movie search wall-e                 # is it in the library? (movies + TV, no disc needed)

rip-movie watch                         # daemon: rip each inserted disc → deliver master → queue → eject
rip-movie disc [--name … --year …]      # process the disc in the drive once (identify → rip → master → queue)
rip-movie upscale-worker                # daemon: drain the upscale queue (Topaz handoff or Real-ESRGAN)
rip-movie queue                         # list pending / awaiting-Topaz / failed upscale jobs
rip-movie status                        # drive, running rip/upscale, queue depth

rip-movie rip [--title N]               # just rip the disc to an mkv (auto-selects the main title)
rip-movie run FILE --title "…"          # a ripped file, end-to-end (Real-ESRGAN engine, inline)
rip-movie push FILE --title "…"         # name to schema + deliver to Nextcloud + refresh Jellyfin
rip-movie review                        # resolve ambiguous discs queued for a manual title pick
```

The **walk-away setup** is two daemons: `watch` rips discs and queues their upscales; `upscale-worker`
drains the queue. With the Topaz engine, the worker preps clips to your inbox and finishes each one
when its render lands in the outbox — so your only manual step is one VEAI batch.

## Delivery & cleanup

Delivery drops the finished file into the media store and refreshes the player — **no `kubectl`, no
`occ` in the write path**. Two transports, picked by what the store needs:

- **File Provider mount copy** (the package default, `deliver.local`) — copy into the Nextcloud
  virtual-files mount to a `.part`, then `os.replace` (atomic; a partial never gets indexed). The
  sync client uploads it on demand; credentials stay in the Keychain, nothing in the pipeline.
- **Chunked WebDAV** (the interactive/large-file path, `~/rip-movie/work/.helpers/chunked_upload.sh`)
  — MKCOL an upload session, PUT 10 MB chunks streamed with `dd` (no full-file copy, no split-suffix
  cap), MOVE-assemble with `OC-Total-Length`. This is what survives multi-GB files and a flaky sync
  client. Pre-create Show/Season folders or the assemble-MOVE 409s.

Then Jellyfin is refreshed over **direct HTTP** (`Authorization: MediaBrowser Token="…"`) and
force-identified to the right TMDb id. A **duration safety-net** refuses any rendition whose length
drifts >2.5 s from its source (a partial/aborted upscale can never land). Every delivery is
**byte-verified** (destination size == local size) and local temporaries are removed **only after**
that check — so a failed run always keeps its files for retry.

> `kubectl` remains only on the **library-read** paths (the `/library` viewer and `search`), never on
> the delivery path.

## Configuration

Everything lives in `config/rip-movie.toml`; secrets (TMDb key, Jellyfin API key) come from
`config/secrets.env` (gitignored) via `${ENV_VAR}` expansion. Key knobs: `upscale.engine`
(`topaz-veai-handoff`/`realesrgan`), `upscale.mode` (`queue`/`inline`), the `[upscale.topaz_handoff]`
table (inbox/outbox, `output_fps`, per-genre Proteus tags), `upscale.dvd.autocrop`,
`upscale.dvd.sd_max_height`, `encode.audio.languages`, `deliver.keep_source_master`.

## Status

Full pipeline, disc-to-Jellyfin, validated on real discs. Topaz VEAI handoff (prep → VEAI → resume)
with the soft-telecine frame-rate fix, VEAI-letterbox auto-crop, and complete-render guard; the
decoupled queue + worker with on-demand Nextcloud pull for re-upscaling existing library titles; the
live swimlane dashboard + upgrade-DVDs viewer; two-tier delivery to the correct Nextcloud path +
Jellyfin index; VobSub→SRT OCR. See `AGENTS.md` for the architecture and developer notes.
