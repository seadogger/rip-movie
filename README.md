# rip-movie

Hands-off pipeline that takes a physical disc from tray to Jellyfin: rip → (optional AI
upscale) → encode → name to schema → deliver into Nextcloud → refresh Jellyfin.

You put a disc in the drive. `rip-movie` does the rest.

## The environment this is built for

| Piece | Detail |
|---|---|
| Rip station | This Mac (Apple M1, macOS) + USB ASUS BW-16D1HT BD/DVD drive |
| Tools | `makemkvcon`, `HandBrakeCLI`, `ffmpeg`, `rclone`, `kubectl` |
| Library store | Nextcloud user **HomeMedia** → `Videos/Movies/` on a 6 TB CephFS |
| Player | Jellyfin, reading that CephFS **read-only** at `/media/data/HomeMedia/files/Videos/Movies/` |
| Cluster | 4× Raspberry Pi 5 (k3s). **No hardware video encoder** — Jellyfin can never transcode in real time |
| Clients | Apple TV, iPhone, iPad only (all HEVC 10-bit/HDR capable; no browsers; **no DTS decode**) |

Because the Pi 5 cluster cannot transcode, every file we store must **direct-play** on Apple
devices. That drives every encoding decision below.

## Naming schema (reverse-engineered from the existing library)

```
Movies/{Title} ({Year})/{Title} ({Year}) - {resTag} {codecTag}.{ext}
  e.g.  Movies/The Santa Clause 2 (2002)/The Santa Clause 2 (2002) - 1080p AVC.mkv
```
- `resTag`   → `480p` | `720p` | `1080p` | `2160p`
- `codecTag` → `AVC` (H.264) · `HEVC` (H.265) · `MPEG` (MPEG-2 remux) · `Microsoft` (VC-1 remux)

## Output strategy: lossless master + conditional direct-play rendition

We keep what your library already does, made automatic and consistent:

1. **Archival master** — the lossless disc remux (video untouched from disc, all audio tracks,
   HDR/10-bit preserved). This is the "rip once, never touch the disc again" keeper, and it lets
   us re-derive better versions later as upscalers improve. `.mkv`.
2. **Direct-play rendition — generated only when the master's codec isn't Apple-native:**

   | Master video codec | Direct-play rendition |
   |---|---|
   | MPEG-2 (DVD)       | **AI upscale → 1080p, HEVC** (this becomes the watchable copy) |
   | VC-1 ("Microsoft") | HEVC 1080p |
   | H.264 (AVC)        | none — already direct-plays |
   | HEVC               | none — already direct-plays |

   Rendition codec is **HEVC** (all-Apple clients; ~40 % smaller than H.264, keeps HDR).
   Every rendition is guaranteed an **AAC stereo + AC3/E-AC3 5.1** pair so Apple never has to
   transcode audio (Apple devices can't decode DTS).

## Upscaling (pluggable)

Upscaling is a per-source **configuration choice**, never a quality judgment made by the tool.
The enhancer is a swappable engine:

| engine key           | what it is | notes |
|----------------------|------------|-------|
| `topaz-tvai`         | Topaz Video AI 3+ CLI (`ffmpeg -vf tvai_up`) | best for live-action; needs current license + macOS support |
| `topaz-veai-handoff` | Topaz Video Enhance AI 2.6.4 via watch-folder | free (you own it); one manual GUI batch per DVD |
| `realesrgan`         | Real-ESRGAN (`realesr-animevideov3` / `x4plus`) | free, M1 GPU, great on animation |
| `anime4k`            | Anime4K GPU shader | free, real-time, animation only |
| `nnedi`              | ffmpeg nnedi3 edge-directed 2× | free, light, no models |
| `none`               | no upscale (Lanczos resize only if needed) | |

The pipeline **auto-selects** an engine from the movie's TMDb genre (Animation vs live-action)
unless you pin one per title. Deinterlacing (QTGMC/bwdif) always runs first on interlaced DVDs —
that, not resolution, is where most DVD quality comes from.

## Pipeline stages

```
watch → identify → library-check → rip → enhance → encode → name → deliver → refresh
```
1. **watch** — detect a disc in the drive (poll `drutil`/`diskutil`).
2. **identify** — read the volume label + `makemkvcon` title scan; match against TMDb → `Title (Year)`.
3. **library-check** — already in `Movies/Title (Year)/`? If yes, eject and stop.
4. **rip** — `makemkvcon` the main title(s) to a temp `.mkv` (see title selection).
5. **enhance** — optional AI upscale + deinterlace per config.
6. **encode** — build master + conditional direct-play HEVC rendition; fix audio tracks.
7. **name** — apply the schema.
8. **deliver** — push into Nextcloud (via `kubectl`) and index it.
9. **refresh** — trigger a Jellyfin library scan.

Ambiguous discs (see below) drop into a **review queue** instead of guessing.

## Title selection — the "black art"

Commercial discs expose many titles; the feature is usually the longest, but Blu-rays use
*playlist obfuscation* (dozens of near-duplicate/decoy playlists) and some discs are episodic.
Heuristic:
- Ignore titles shorter than `disc.min_title_seconds` (default 300 s).
- Pick the longest remaining title.
- If the 2nd-longest is ≥ `disc.ambiguous_ratio` (default 0.90) of the longest, or several titles
  share a near-identical duration, mark the disc **ambiguous** → review queue (no guess).

## Usage

```bash
rip-movie config-check                      # validate config, tools, cluster reachability
rip-movie search wall-e                     # do I already own this? (by title, no disc)
rip-movie identify                          # scan the current disc, print the TMDb match + library status
rip-movie enhance FILE --title "…"          # AI-upscale a file to 1080p (denoise→engine→detail-transfer)
rip-movie push FILE --title "…"             # name to schema + deliver to Nextcloud + refresh Jellyfin
rip-movie run FILE --title "…" [--dry-run]  # a finished file: enhance → name → deliver → force-identify
rip-movie rip [--title N]                    # rip the disc in the drive to an mkv (auto-selects main title)
rip-movie disc [--title N] [--dry-run]       # a disc, end-to-end: identify → rip → upscale → deliver
rip-movie watch                              # daemon: wait for discs and process each automatically
rip-movie review                             # list/resolve ambiguous discs (playlist obfuscation / episodic)
```

Two entry points: **`run FILE`** starts from an already-ripped file; **`disc` / `watch`** start from a
physical disc (scan → TMDb identify → skip if already owned → MakeMKV rip → the same `run` back half).
Ambiguous discs go to the review queue instead of guessing the wrong title.

The AI upscale is a single ANE-streaming pass (~8–11 h/movie, overnight). `run FILE --title "WALL·E"`
upscales a ripped DVD and lands it in Jellyfin as `WALL·E (2008) - 1080p AVC.mp4`, pinned to the
right TMDb id so Jellyfin can't mis-match it.

`search` and the disc `identify` share one library check, so "did I already rip this?" gives the
same answer whether you type the title or drop the disc in. Both also flag when a title is present
but only in a low-res or non-direct-play form (e.g. WALL·E at 480p MPEG-2) — a candidate for a
better rip/upscale.

## Configuration

Copy `config/rip-movie.example.yaml` to `config/rip-movie.yaml` and fill in secrets (TMDb key,
Nextcloud/Jellyfin access). See that file for every knob.

## Status

**Full pipeline built, disc-to-Jellyfin.** Implemented + tested: config/`config-check`; `search`
(title → library, quality flags, TMDb enrichment) with a robust TMDb matcher (spelling variants +
similarity — handles WALL·E); disc scan + title-selection heuristic + `identify`; **`enhance`** —
cadence classifier (idet + inverse-telecine) → light denoise → CoreML/ANE upscale (animevideov3 /
general-x4v3) → detail-transfer → aspect-correct 1080p, **streaming** (no PNG round-trip), ~8–11 h/movie;
**`push`**/`deliver` (schema-name → Nextcloud via kubectl → `occ files:scan` → Jellyfin refresh +
TMDb force-identify); **`rip`** (MakeMKV); **`run`** (file→library) and **`disc`**/**`watch`**
(disc→library) orchestration + review queue.

Remaining polish: `status`, optional HEVC master encode, hard-telecine tuning.
