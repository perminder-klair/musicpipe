# CLAUDE.md — musicpipe

Guidance for Claude Code when working in this repo.

## What this is

A single Docker Compose project that owns the full self-hosted music stack:
Apple Music downloader → MusicBrainz tagger → Navidrome streamer + a web UI
(`musicpipe`) that's the state of record for deletes, blocklists, and
bandwidth-saving skips. Consolidates the previous three-project arrangement
(`navidrome-slskd/`, `gamdl/`, `gamdl-ui/`) into one branded stack.

## Pipeline

```
watchlist URL ──► GET gamdl-ui:/pre-check (skip if complete or fresh-clean)
        │                                    ▲
        │                                    │  same oracle consulted by
        │                                    │  UI runner + cron run.sh
        ▼  gamdl (cron 03:00 Europe/London, or on-demand from UI)
     /mnt/sata/navidrome/Incoming/   (tagged AAC 256 kbps .m4a)
        │
        │  indexer.index_incoming()  → upserts tracks.incoming_path
        │
        ▼  auto-import (every 5 min, or instantly on .trigger-import)
     1. POST gamdl-ui:/maintenance/filter-incoming
          - strips blocked + deleted track IDs
          - strips cross-inode duplicates (re-downloads of Library tracks)
          - REPLACES with hardlink to the Library twin when possible
            (preserves gamdl's skip cache, breaks re-download loops)
     2. beet import -q /music/Incoming   (move: yes)
     3. chown -R PUID:PGID /music/Library /music/Incoming
        │
        │  indexer.index_library()  → upserts tracks.library_path
        │
        ▼  every 1 h (or on-demand Subsonic rescan from UI)
     navidrome  →  your public host  /  port 4533
```

Handoffs are filesystem + one HTTP call. Coupling points:

- `beets/.trigger-import` — touched by the UI or the filter endpoint to
  wake the sidecar within ~5s instead of waiting up to `AUTO_IMPORT_INTERVAL`.
- `beets/.import-status` — `running`|`idle`, polled by the UI for the
  "Beets" status card.
- `gamdl-ui:/pre-check` — called by `gamdl/run.sh` before each URL.
- `gamdl-ui:/maintenance/filter-incoming` — called by `scripts/auto-import.sh`
  before each `beet import`.

## State of record: `tracks` table

`gamdl-ui/ui-data/ui.db` → `tracks` is keyed on **Apple Music track ID** (the
`cnID` atom written by gamdl, extracted via mutagen). Every m4a in Incoming
or Library has one row. Columns:

- `am_track_id` (PK), `am_album_id`, `isrc`, `mb_track_id/album_id`
- `title`, `artist`, `album`, `album_artist`
- `incoming_path`, `library_path` — nullable, present when the file exists
  at that location
- `downloaded_at`, `imported_at` — mtimes at first observation
- `deleted_at`, `blocklisted` — durable "don't reappear" flags
- `last_indexed_at` — sweeper bookkeeping

Two observers populate it:
1. `runner._run_one()` calls `indexer.index_incoming(source_url=…)` after
   every UI-triggered sync.
2. `indexer._indexer_loop()` in `main.py` runs `index_all()` every 120s
   (covers cron-produced downloads + beets imports). First sweep on
   startup doubles as the Library backfill.

Rows are never deleted. Clearing `incoming_path` or `library_path` to NULL
on a missing file is the only pruning. This preserves `deleted_at` /
`blocklisted` so the decisions survive future re-downloads.

## Filter-incoming (the skip oracle)

`indexer.filter_incoming()` is called by `auto-import.sh` before every
`beet import`. For each m4a in Incoming, by Apple Music track ID:

| Condition | Action | Counter |
|---|---|---|
| `deleted_at` or `blocklisted` | unlink | `removed_blocked` |
| has `library_path`, **same inode** as Library twin | **leave alone** | `kept_hardlinks` |
| has `library_path`, different inode (re-download) | unlink + recreate as hardlink to Library twin | `replaced_with_hardlink` |
| has `library_path`, different inode, hardlink fails | plain unlink (beets' `duplicate_action: remove` catches it) | `removed_duplicate` |

**The inode check is load-bearing.** Pre-Phase-D legacy hardlinks share
inodes with their Library twins; they cost nothing on disk and serve as
gamdl's built-in skip cache. Stripping them would force re-downloads on
the next cron for every artist/playlist URL.

The **replace-with-hardlink** path needs a unified `/music` mount in
`gamdl-ui` so `os.link()` doesn't hit `EXDEV` across the separate
`/downloads` and `/library` bind mounts.

## Pre-check (don't invoke gamdl when you don't need to)

`gamdl-ui/app/pre_filter.py` exposes:

- `should_skip(url, kind)` — UI runner uses this: Phase C album
  completeness only (if all expected tracks are present + not-deleted,
  skip the subprocess entirely).
- `should_skip_for_cron(url, kind)` — cron's `run.sh` uses this: Phase C
  + **fresh-clean-run** (if the last run for this URL was `ok`, added
  zero tracks, and happened within `FRESH_RUN_WINDOW_SEC` (20h), skip).

Skipped runs still record a `runs` row with `status='ok'` and a synthetic
log file carrying the skip reason, so "view log" on the Runs page works.

## Services (single docker-compose.yml, single network)

| Service       | Image                       | Port  | Purpose |
|---------------|-----------------------------|-------|---------|
| `hdd-check`   | alpine                      | —     | Healthcheck sentinel; gates everything else on `/mnt/sata/navidrome/Library` being mounted. |
| `navidrome`   | deluan/navidrome:latest     | 4533  | Streamer / Subsonic API. Reads `/music` (Library) ro, owns `/data` (NavidromeData). |
| `beets`       | linuxserver/beets:latest    | —     | Interactive (`docker exec -it beets bash`) for manual `beet import`, `beet ls`, etc. |
| `auto-import` | linuxserver/beets:latest    | —     | Background loop running `scripts/auto-import.sh`. Curls `/maintenance/filter-incoming` before every import + chowns to PUID:PGID afterwards. |
| `gamdl`       | local build (`./gamdl`)     | —     | Ubuntu 24.04 + ffmpeg + Bento4 + gamdl 2.9.3 + cron. `run.sh` curls `/pre-check` before each URL. |
| `gamdl-ui`    | local build (`./gamdl-ui`)  | 4150  | FastAPI dashboard ("musicpipe"). Watchlist CRUD, sync runner, live SSE log, tracks DB, filter + delete endpoints, Library browser, Runs history, Navidrome/Beets cards. Tailscale-only. |

All six share the project-scoped `music-network` bridge
(`musicpipe_music-network`). `gamdl-ui` has three relevant mounts:
`/downloads` (Incoming, rw), `/library` (Library, **rw** since Phase B
added delete), and `/music` (unified, for cross-mount hardlink ops).

## UI layout

Three pages, all sharing `_nav.html` + `_modals.html` + `_shared_scripts.html`:

- `/` **Dashboard** — status box, live SSE log, watchlist (add, tabs
  including "Needs attention", per-card ♪ opens tracks modal). No
  service cards, no runs history — those moved to Library and Runs.
- `/library` **Library** — Navidrome + Beets service cards on top, then
  counts header + track browser with search/filter and per-row del /
  del+block / undelete / blocklist actions.
- `/runs` **Runs** — full-width auto-refreshing runs table; each row
  opens the log modal in place.

## Common commands

```bash
docker compose up -d --build              # bring up everything
docker compose ps                         # status
docker compose logs -f gamdl-ui           # tail one service
docker compose down                       # stop everything

# Manual gamdl run (bypass cron) — still hits /pre-check per URL
docker exec -u gamdl gamdl /usr/local/bin/run.sh
tail -f gamdl/logs/run.log

# Manual beets shell
docker exec -it beets bash               # then: beet import /music/Incoming

# Force the auto-import sidecar to run now
touch beets/.trigger-import

# Force a full indexer sweep (idempotent)
curl -X POST http://localhost:4150/maintenance/reindex

# Preview what filter-incoming would do
curl -X POST http://localhost:4150/maintenance/filter-incoming

# Pre-check probe
curl 'http://localhost:4150/pre-check?url=<url-encoded>&kind=album'

# Check cron + TZ in gamdl
docker exec gamdl cat /etc/cron.d/gamdl  # 0 3 * * *
docker exec gamdl date                   # Europe/London
```

## Architecture quirks (do not break these)

- **uid/gid 1000/1001 is hard-coded** in `gamdl/Dockerfile` and
  `gamdl-ui/Dockerfile`; `PUID/PGID` match in `.env` for the beets
  containers. Library files on `/mnt/sata/navidrome/` are owned 1000:1001.
  `auto-import.sh` re-chowns after every import to keep it that way —
  previously `beet` ran as root here (the compose overrides lsio's s6
  entrypoint), which made the UI's delete endpoint fail with EACCES.
- **Bento4 is fetched from `www.bok.net` at gamdl image build time.**
  If that host is down, the `gamdl` build stalls — swap to a Bento4
  GitHub release mirror.
- **gamdl writes raw, beets auto-import moves + re-tags.** Don't point
  gamdl directly at the Library; the Beets sidecar produces the
  canonical Library layout. Since Phase D the sidecar uses `move: yes`
  (previously `hardlink: yes`), so new imports empty Incoming.
- **Don't strip same-inode Incoming files.** They're legacy pre-Phase-D
  hardlinks sharing inodes with their Library twin. Zero disk cost,
  serve as gamdl's skip cache. The filter's inode check preserves them.
- **gamdl-ui needs the `/music` unified mount** for hardlink ops in
  `filter_incoming`. Separate `/downloads` + `/library` mounts are also
  present for the other code paths; removing `/music` regresses the
  replace-with-hardlink path to `EXDEV`.
- **`/mnt/sata/navidrome/{Incoming,Library,NavidromeData}` is shared
  state**, not project-local. Compose references these via
  `${INCOMING_DIR}`, `${LIBRARY_DIR}`, `${NAVIDROME_DATA}`, `${MUSIC_ROOT}`
  from `.env`.
- **`gamdl-ui` is Tailscale-only** by design (manages cookies + can
  trigger downloads + can unlink Library files). Not exposed via the
  Cloudflare Tunnel.
- **Runner parses stdout split on `\r`**, not just `\n`. yt-dlp emits
  progress frames without newlines, so a single `readline()` chunk can
  contain many progress frames plus the next `[Track N/TOTAL] Downloading`
  announcement glued to the tail. Not splitting on `\r` caused the track
  counter to miss every actual download (only skipped tracks got clean
  standalone lines).

## File reference

| Path                            | Purpose                                                |
|---------------------------------|--------------------------------------------------------|
| `docker-compose.yml`            | All six services on `music-network`; gamdl-ui has `/downloads` + `/library` (rw) + `/music` (unified for hardlinks) |
| `.env`                          | Filesystem paths, `PUID`/`PGID`, ports, Navidrome + Last.fm API creds |
| `scripts/auto-import.sh`        | Beets sidecar loop — curls filter-incoming, runs `beet import`, chowns Library + Incoming |
| `scripts/{setup,fix_permissions,tag-music,setup_symlinks}.sh` | One-shot ops helpers |
| `beets/config.yaml`             | `move: yes`, `incremental: no`, `duplicate_action: keep` (was `remove`, which wiped partially-re-downloaded albums; briefly `merge`, which crashed relocating stale DB entries — `keep` imports new tracks as a separate aunique folder, no wipe/no crash; Navidrome groups by tags) |
| `beets/state.pickle` / `musiclibrary.blb` | Beets import history (preserve across rebuilds) |
| `gamdl/Dockerfile`              | Build for the cron downloader (has python3 + wget for pre-check) |
| `gamdl/crontab`                 | `0 3 * * *` Europe/London                              |
| `gamdl/run.sh`                  | Watchlist iterator; curls `/pre-check` per URL, logs `SKIP $kind: $url (reason)` on skip |
| `gamdl/config/{artists,playlists,albums}.txt` | One Apple Music URL per line; `#` comments OK |
| `gamdl/config/cookies.txt`      | Netscape-format AM session cookies (gitignored, chmod 600) |
| `gamdl-ui/Dockerfile`           | FastAPI on 4150, uid 1000 / gid 1001, mutagen installed |
| `gamdl-ui/requirements.txt`     | fastapi, uvicorn, jinja2, httpx, gamdl, **mutagen** |
| `gamdl-ui/app/main.py`          | FastAPI routes: dashboard, library, runs, filter endpoints, pre-check, library actions |
| `gamdl-ui/app/store.py`         | SQLite schema + helpers — `meta`, `runs`, `tracks` tables |
| `gamdl-ui/app/indexer.py`       | mutagen tag reader + `index_incoming/library/all` + `filter_incoming` + `delete_library_file` |
| `gamdl-ui/app/pre_filter.py`    | `canonical_id` (dedupe), `should_skip`, `should_skip_for_cron` |
| `gamdl-ui/app/runner.py`        | Subprocess manager — CR-aware stdout parser, pre-filter short-circuit, post-run indexer call |
| `gamdl-ui/app/watchlist.py`     | Reads/writes `gamdl/config/*.txt`; `DuplicateEntryError` on add |
| `gamdl-ui/app/beets.py` `navidrome.py` `apple.py` `backfill.py` | External adapters |
| `gamdl-ui/app/templates/`       | Three full pages (`index.html`, `library.html`, `runs.html`) + shared partials (`_nav`, `_modals`, `_shared_scripts`) + fragments (`_watchlist`, `_library_rows`, `_watchlist_tracks`, `_runs`, `_stats`, `_beets`, `_status`, `_progress`) |
| `gamdl-ui/ui-data/ui.db`        | SQLite — meta + runs + tracks (gitignored) |
| `gamdl-ui/ui-data/logs/`        | Per-run subprocess logs (gitignored) |

## Public exposure

Only `navidrome` (port 4533) is meant to be reachable from outside the LAN —
e.g. behind a reverse proxy or tunnel at a hostname like `music.example.com`.
`gamdl-ui` deliberately has **no** public route: it can manage cookies, trigger
downloads, and unlink Library files, so keep it on a trusted network (LAN/VPN)
only.
