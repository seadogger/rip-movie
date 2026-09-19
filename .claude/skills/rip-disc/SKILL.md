---
name: rip-disc
description: >
  Rip the physical disc currently in the drive to Jellyfin with a human title-confirmation gate and a
  live dashboard to watch it. Use when the user wants to rip a DVD/Blu-ray, "rip this disc", "rip the
  movie in the drive", or run the disc pipeline. Flow: preflight-validate (MakeMKV license + Jellyfin
  API + delivery target) → identify → CONFIRM the title → rip → deliver by copying into the Nextcloud
  File Provider folder (no auth, no kubectl) → refresh Jellyfin → clean up the local rip. Does NOT
  upscale — that is handled separately by rip-movies-premium. Not for pushing a ripped file (use `push`).
---

# rip-disc — confirmed disc → Jellyfin

Drive the rip-movie pipeline for the disc in the drive, with a **title-confirmation gate before the
long rip** and a **live dashboard** to monitor it. Everything runs through `./bin/rip-movie` from the
repo root (stdlib only, no install). Report each step's real output; never claim a delivery the CLI
didn't confirm.

**Scope note — no upscale here.** This skill delivers the ripped master to Jellyfin and stops. The
DVD→1080p AI upscale is being built as a separate, fully-automated project (**rip-movies-premium**);
do not queue or run any upscale from this skill.

## 1. Open the dashboard (monitor the whole run here)
The dashboard is a stdlib HTTP server showing live pipeline swimlanes + recent deliveries. Make sure
it's running and give the user the link up front:
```
pgrep -f "rip-movie dashboard" >/dev/null || (./bin/rip-movie dashboard >/tmp/rip-dashboard.log 2>&1 &)
```
Then tell the user: **http://127.0.0.1:8422** (port is `dashboard.port` in config; default 8422).
Keep this link visible — every step below shows up there.

## 2. Preflight — validate the whole path before touching a disc
```
./bin/rip-movie config-check
```
Every line must be ✓. This actively verifies the two things that silently break a run:
- **MakeMKV license: registered (…)** — the beta key lapses ~monthly; a dead key blocks the rip.
- **jellyfin api: http 200** — direct HTTP to `jellyfin.base_url`, so a stale api_key is caught now.
- **nextcloud webdav: reachable** (delivery target) and TMDb key set.

If MakeMKV shows expired/registration → the user must refresh the key in the MakeMKV app first. If
webdav or Jellyfin is red → stop and surface it; a rip with no delivery target just wastes time.

## 3. Identify — the confirmation data (no rip yet)
```
./bin/rip-movie identify
```
Scans the disc without touching it and prints: disc label + eligible titles (`#idx HH:MM:SS Nch GiB`),
the **main feature** it would rip (or `AMBIGUOUS`), the **TMDb match** (`folder [genres] (id)`), and
**library status**. Handle:
- **Already in library** → report and stop; nothing to rip.
- **No TMDb match** → ask the user for title (+year); you'll pass `--title/--year` to `push` later.
- **Ambiguous** → list the candidate `#index`es and ask which is the movie (→ `rip --title N`).
- **Clean match** → go to the gate.

## 4. Confirm with the user (the gate)
Summarize what will be ripped and delivered, then ask for explicit confirmation with
**AskUserQuestion**: TMDb folder + year, the main-feature title index + runtime, and that it delivers
to Jellyfin over WebDAV. Options: **Confirm** / **Pick a different title** (`#index`) / **Fix the
title** (name+year) / **Cancel**. Do not run step 5 until the user confirms. This gate is the point:
a wrong title otherwise wastes a ~30–60 min rip.

## 5. Rip → deliver → refresh
```
./bin/rip-movie rip                 # main feature (add --title N if the user chose one)
```
Rips the title to `work_dir/rips/…mkv`, normalizes subtitle defaults, and **ejects the disc** (swap
the next one in while delivery runs). Note the `ripped -> <path>` it prints.

Then deliver that file to the library:
```
./bin/rip-movie push "<ripped .mkv>" --title "<Title>" --year <YYYY>
```
`push` probes/names the file, then delivers via the configured transport (**`local`** → copies into
the Nextcloud **File Provider** folder, which the sync client uploads natively; **no auth, no kubectl,
no `occ files:scan`**) and triggers a **Jellyfin library refresh** over direct HTTP once the copy
lands. Confirm the output shows `delivered -> …` and `jellyfin: http 204`. Watch it on the dashboard.

## 6. Clean up the local copy
`push` does not delete its source, so once delivery is confirmed, remove the local rip:
```
rm -f "<ripped .mkv>"
```
Only after `push` reported success — a failed/partial delivery keeps the file for retry (don't tidy
it away). If the rip failed the duration safety-net or delivery errored, leave the file and surface
why.

## 7. Verify
Confirm it actually landed rather than trusting the log:
```
./bin/rip-movie search "<title>"    # should show the folder now in the library
```
…and the movie is visible on the dashboard / in Jellyfin.

## Delivery internals (so you can reason about failures)
- **Delivery is a plain copy.** `push` copies the finished file into `deliver.local.dest_root`, the
  **Nextcloud File Provider (virtual files) mount** at `~/Library/CloudStorage/Nextcloud-…-HomeMedia`.
  The Nextcloud client uploads it on-demand; **no credentials live in the pipeline** (Keychain owns
  them) and **no WebDAV or kubectl** touches the file path.
- **`config-check` verifies same storage.** `same location (Mac == Jellyfin)` cross-checks that the
  movie folders under the mount are the same ones Jellyfin's API reports — what you copy in is what
  Jellyfin reads. If this is red, delivery would write to the wrong place — stop.
- **The mount is on-demand.** The whole 3.7 TB tree shows as 0-byte placeholders; copying a new file
  in uploads it without downloading anything. If the mount is missing, virtual files is off in the
  Nextcloud client (`isVfsEnabled` in its `nextcloud.cfg`), not a pipeline problem.
- **Jellyfin** (`refresh`, `find_item`, `force_identify`, `health`) is all **direct HTTP** to
  `jellyfin.base_url` (`Authorization: MediaBrowser Token="…"`) — no kubectl. A refresh hiccup is
  best-effort and must not fail an otherwise-good delivery.
- **Still-kubectl (not delivery):** the library *browse/search* paths (`rip-movie search`, the
  dashboard `/library` viewer) still list the pod's file tree via kubectl. That's a read feature, not
  the copy — unaffected here.

## Landmines
- **Delivery is success-gated.** Only remove the local rip once `push` confirms delivery; a failed
  run keeps its files for retry.
- **Duration safety-net** can reject a rendition >2.5s off the source — surface the reason, don't work
  around it.
- **No upscale here** — SD titles ship as the DVD-quality master; rip-movies-premium handles 1080p.
- We invoke the CLI directly (not a daemon), so no daemon restart is needed — except the **dashboard**
  loads code at start; if you changed dashboard code, restart it.
