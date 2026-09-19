---
name: rip
description: >-
  One orchestrator for disc → library. Establishes the GOAL (which player, which storage,
  optional backup), collects credentials into config/secrets.env WITHOUT reading them, builds
  a delivery plan, then runs the per-disc loop (identify → confirm → rip → deliver → verify →
  clean). Handles movies and TV, CSS-protected discs, scratched discs, and drive brown-outs.
  Invoke with /rip. Use when the user wants to rip one or many discs into a media library.
---

# /rip — disc → library orchestrator

You are driving physical discs into a media library. This skill is the **single entry point**: it
figures out *what the user is trying to achieve*, gets the credentials in place **without ever
reading them**, builds the plan, and then runs each disc through the pipeline. Everything runs from
the repo root; the Python package is stdlib-only (`./bin/rip-movie`), and the interactive helpers
live under `~/rip-movie/work/.helpers/` (durable — not `/tmp`, which macOS purges).

Report each step's real output. Never claim a delivery the tool didn't confirm. Confirm the long
rip before starting it. Eject/seat/clean prompts go to the user; you never touch the physical disc.

---

## Phase 0 — Establish the goal (ask, don't assume)

On first use for a library, ask with **AskUserQuestion** (skip questions already answered by config):

1. **Content this session** — Movies · TV series · Mixed. (TV needs episode identification; movies
   need the TMDb title-confirmation gate.)
2. **Player** — Jellyfin · Plex · Emby · Files only. Determines the post-delivery refresh call.
3. **Where files land (the "store")** — pick one:
   - **Nextcloud (chunked WebDAV)** — upload straight to the Nextcloud user's files over HTTPS.
     The reliable path for a Nextcloud-backed library; survives large files and flaky sync clients.
   - **Mounted path** — an SMB/NFS/File-Provider mount the player already reads (copy the file in).
   - **Local directory** — the player indexes a local folder directly.
4. **Backup (optional)** — none · S3 · other. If S3, files also get `aws s3 cp`'d after delivery.

Then restate the goal in one line and the **path map** you'll use, e.g.:
> Rip **movies** → deliver via **chunked WebDAV** to Nextcloud `HomeMedia:/Videos/Movies` →
> refresh **Jellyfin**. (Nextcloud `data/.../Videos/Movies` == Jellyfin `/media/.../Videos/Movies`.)

The canonical worked example (the setup this repo was built for):
> Insert a disc → rip a raw `.mkv` → chunk-upload into a **Nextcloud** library that **Jellyfin**
> reads → Jellyfin refresh. The user supplied a **Nextcloud app-password** and a **Jellyfin API
> token**, and that was enough to go.

---

## Phase 1 — Credentials (the user fills them; you never read them)

**You must not read, cat, echo, or print secret values.** Have the user place them so the tooling
reads them at runtime via `${ENV}` expansion or the macOS Keychain. Tell them exactly what to add,
based on their Phase-0 answers, and how to enter it in this session with the `!` prefix (runs a
shell command whose output lands in the conversation — so *they* run the editor, not you):

- **Always:** `TMDB_API_KEY` (identify + runtime match) →
  `! printf 'TMDB_API_KEY=%s\n' 'YOUR_KEY' >> config/secrets.env`
- **Jellyfin:** `JELLYFIN_API_KEY` (Jellyfin → API Keys) → append to `config/secrets.env`.
- **Plex:** `PLEX_TOKEN` (X-Plex-Token) → `config/secrets.env`.
- **Nextcloud chunked WebDAV:** an **app-password** for the media user, stored in the **Keychain**
  (not a file), so nothing sensitive touches disk in the repo:
  `! security add-generic-password -s Nextcloud -a "<user>_app-password:https://<host>/:0" -w`
  (the helper reads it with `security find-generic-password … -w`).
- **S3 backup:** `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (or an AWS profile) in
  `config/secrets.env`, plus the target bucket/prefix in `config/rip-movie.toml`.

`config/secrets.env` is **gitignored**; `config/rip-movie.toml` holds only `${ENV}` placeholders and
non-secret hostnames. Never commit a real key. After they've added them, continue — you validate by
*use*, not by reading.

---

## Phase 2 — Build the plan and preflight

Assemble the concrete pipeline from the goal:

```
rip  →  deliver-to-store  →  refresh-player  →  [backup-to-S3]  →  verify  →  gated-cleanup
```

Then validate before touching a disc:

```
./bin/rip-movie config-check      # MakeMKV license, player API 200, store reachable, TMDb key set
```

Every line must be ✓. The two that silently kill a run: **MakeMKV license** (beta key lapses
~monthly) and the **player API** (stale token → 401). If the store is red, stop — a rip with no
delivery target just wastes ~20–60 min.

Open the dashboard so the user can watch: `pgrep -f "rip-movie dashboard" >/dev/null ||
(./bin/rip-movie dashboard >/tmp/rip-dashboard.log 2>&1 &)` → **http://127.0.0.1:8422**.

---

## Phase 3 — The per-disc loop

For **each** disc the user loads, run this loop. Announce the disc, then go.

### 3a. Identify
```
./bin/rip-movie identify          # disc label, eligible titles, main-feature pick, TMDb match, library status
```
- **Movie, clean TMDb match** → go to the confirmation gate (3b).
- **Movie, no match** (junk volume label like `COURAGEUNDERFIREDTSVER3`) → ask title (+year), pass
  `--name/--year` later; the runtime match handles the rest.
- **Ambiguous** (several near-equal-length titles) → list the `#index`es, ask which is the feature.
- **Already in the library** → report it's a **duplicate** and stop (unless the user wants to replace
  or has a better edition — a Special Edition may be a *different* film; verify by runtime/content).
- **TV disc** → skip the movie gate; go to 3d (episode mapping).

### 3b. Confirm (movies only — the gate before the long rip)
Summarize with **AskUserQuestion**: TMDb folder+year, main-title index+runtime, and the delivery
target. Options: Confirm · Pick a different title · Fix the title (name+year) · Cancel. A wrong title
otherwise wastes a ~30–60 min rip. Do not rip until confirmed.

### 3c. Rip (with fallbacks)
Rip the main title with MakeMKV (`./bin/rip-movie rip [--title N]`, or the detached
`~/rip-movie/work/.helpers/rip_batch.sh` for episodic/multi-title discs). Detach long rips so the
harness can't reap them:
`( nohup caffeinate -imsu bash "$H/rip_batch.sh" <idx…> >LOG 2>&1 </dev/null & )`.
Rip **into a separate dir** whenever a previous delivery might still be reading `rips/`.

**Fallbacks, in order of what the disc throws at you:**
- **CSS-protected disc that stalls / `SCRAMBLED SECTOR WITHOUT AUTHENTICATION` / makemkvcon hangs on
  `disc:0`** — MakeMKV's LibreDrive mode didn't engage. Decrypt-mirror instead:
  `dvdbackup -M -i /dev/rdiskN -o <dir>` (uses libdvdcss to fetch CSS keys), then run
  `makemkvcon … mkv "file:<dir>/<DiscTitle>" <idx> <out>` on the **local decrypted copy** — lossless
  and consistent with direct rips. Install once: `brew install dvdbackup` (pulls libdvdcss).
- **Scratched disc / read stall / partial file** — image with **ddrescue**:
  `ddrescue -n -b 2048 <src> <out> <mapfile>` (fast forward pass), then `ddrescue -R -n …` (reverse
  pass) to grab the tail past the bad sector. The mapfile lets you resume across power-cycles; a
  95–99% recovery yields a watchable file with a brief mid-glitch.
- **Drive brown-out** (`DRV:0,256`, mount vanishes, `Device not configured`) — the USB drive dropped
  off. Ask the user to **unplug/replug the drive's USB and reinsert the disc**, then resume (ddrescue
  reverse pass / re-rip). A wobbly disc that won't mount: ask them to **reseat it on the center hub**;
  a dirty disc that "Failed to open": ask them to **wipe it** center-to-edge.

Verify the rip: full length, decodes clean (`ffmpeg -v error -i FILE -f null -`), duration within
2.5 s of the source (the pipeline's safety-net refuses drifted renditions).

### 3d. TV episode mapping (only for series discs)
DVDs list segments/episodes plus a **play-all** and **trailers/menus** — rip the real content titles,
skip the play-all (its runtime ≈ the sum of the others) and the short bumpers.
- **Identify each title by its on-screen title card**, not disc order (off-air/compilation discs are
  in arbitrary order). Build a contact sheet and read it:
  `ffmpeg -ss 60 -to 220 -i FILE -vf "fps=1/3,scale=280:-1,tile=6x9" -frames:v 1 sheet.png`, then
  Read the PNG. The theme/intro precedes the segment title card — sample deep enough (60–220 s; some
  shows the card is behind a long logo intro, resample 90–180 s).
- **Map to the player's metadata source** (TheTVDB for Jellyfin's default): fetch the series'
  official season page and get the exact `S##E##` for each title. Some series number by **7-min
  segment** (Animaniacs, Care Bears) — file each segment under its own number; a whole broadcast
  episode filed under its lead segment is the tractable best-effort if you don't split.
- **Dual/double-length episodes** → one file named `S01E16-E17`. **De-dup** identical-runtime titles
  by title card before delivering. **Cross-promo trailers of other shows** → skip.
- When identity genuinely can't be read (live-action with no title cards, VHS noise), **ask the user**
  what's on the disc case rather than guess — mislabeling a season is worse than one question.

### 3e. Deliver → verify → refresh → backup → clean
Deliver by the store chosen in Phase 0:
- **Chunked WebDAV** — `~/rip-movie/work/.helpers/chunked_upload.sh <local> <remote-under-files-root>
  <transfer-id>`: MKCOL an upload session, PUT 10 MB chunks streamed with `dd` (no full-file copy, no
  suffix cap), MOVE-assemble with `OC-Total-Length`. **Pre-create destination folders** (Show/Season)
  or the assemble-MOVE 409s. Creds come from the Keychain app-password — never inlined.
- **Mounted path** — copy to a `.part` then `os.replace` (atomic; a partial never gets indexed).
- **Local dir** — same atomic write into the player's folder.

**Verify byte-exact**: PROPFIND (WebDAV) or `stat` the destination and compare size to the local
source. Only then:
- **Refresh the player** — Jellyfin: `POST /Library/Refresh` with `Authorization: MediaBrowser
  Token="…"` (NOT `X-Emby-Token`/`?api_key`). Plex: `GET /library/sections/<id>/refresh?X-Plex-Token=…`.
- **Backup to S3** (if configured) — `aws s3 cp <local> s3://<bucket>/<prefix>/<rel>`.
- **Gated cleanup** — remove local temporaries **only after** delivery is confirmed; a failed run
  keeps its files for retry. Clean the decrypt-mirror too (it's multi-GB).

### 3f. Next disc
Report what landed (`S##E##` list or the movie folder), then eject and prompt for the next disc.
Keep a running tally across the set.

---

## Naming (matches the library's existing pattern)

Let the package compute names so they match what the player already indexes:
`naming.target(cfg, file, title, year)` → `Videos/Movies/{Title} ({Year})/{Title} ({Year}) -
{resTag} {codecTag}.{ext}`. TV: `Videos/TV_Shows/{Show}/Season {NN}/{Show} - S##E## - {Episode} -
{resTag} {codecTag}.mkv`. `resTag` is width-aware (a 1920×828 scope film is `1080p`); `codecTag` ∈
`AVC/HEVC/MPEG/Microsoft`.

## Landmines (carry these forward)
- **Success-gated cleanup** — never delete a local rip until delivery is byte-verified.
- **Duration safety-net** — >2.5 s drift from source = refuse the rendition; surface it, don't work
  around it.
- **Soft-telecine** — DVD film is 23.976 flagged 29.97; stamp true CFR before any upscale or VEAI
  judders (see AGENTS.md / topaz.py).
- **Jellyfin auth** header is `MediaBrowser Token="…"`. **VEAI writes `moov` last** — never resume on
  a partial render. **Modules don't hot-reload** — restart `dashboard`/`watch`/`upscale-worker` after
  editing their code.
- **Scratchpad is ephemeral** — durable helpers live in `~/rip-movie/work/.helpers/`; recreate them
  from this repo if a multi-day gap purged `/tmp`.

## When to hand off to the narrower skill
For a *single, already-clean* disc where the user just wants the confirmed movie flow with the live
dashboard and no store/goal setup, the **`rip-disc`** skill is the focused version. `/rip` is the
orchestrator that establishes the goal first and handles many discs, TV, and problem discs.
