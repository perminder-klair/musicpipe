# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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
          - strips ANY already-held track (by id/ISRC/signature) so beets
            only ever sees genuinely-new tracks (plain unlink, hardlink-safe)
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
| already has a live `library_path` (id / ISRC / signature match) **and that file still exists on disk** | **plain unlink**, any inode | `removed_duplicate` |
| DB claims a Library copy but the file is gone (stale index) | keep — it's the only copy | `stale_library_refs` |

**Always plain-unlink an already-held track — never leave or re-hardlink
it.** beets runs `move: yes`, so ANY file left in Incoming that duplicates
a Library track (a fresh re-download *or* a hardlink to the Library twin)
gets re-imported into a new `%aunique{}` (`[NNNN]`) folder. Same inode =
no extra disk, but Navidrome still indexes it as a distinct `media_file`
with a distinct Subsonic id — the duplicate-song bug (issue #1).

> **History (issue #1):** filter_incoming used to *replace* re-downloads
> with a hardlink to the Library twin (and leave same-inode files alone),
> banking on beets' `incremental` flag to skip the already-imported dir.
> But `incremental: no` was set later to fix the treadmill, silently
> breaking that assumption — so the filter was manufacturing ~1,950
> `[NNNN]` duplicate folders it was meant to prevent. The skip-cache these
> hardlinks provided is obsolete anyway: with `USE_TRACK_EXPANSION=1`
> gamdl is handed only missing tracks (the tracks DB is the skip oracle),
> so nothing already-held reaches Incoming to begin with. One-off cleanup
> lives in `scripts/dedupe_library.py`.

The `/music` unified mount is now vestigial for this path (the removed
`replaced_with_hardlink` branch used `os.link()` across mounts); it's kept
for any future cross-mount op but filter_incoming no longer needs it.

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

### Per-track expansion (flag-gated, `USE_TRACK_EXPANSION=1`)

`expander.py` wraps gamdl's internal `AppleMusicApi` (reusing the same
`cookies.txt`) to enumerate every track an album/artist/playlist URL would
produce *before* gamdl runs. `pre_filter.expand_and_filter()` diffs that list
against the `tracks` table. When the env flag `USE_TRACK_EXPANSION=1` is set,
`runner._run_one()` takes this path instead of the legacy Phase-C check: it
either synthetic-skips (nothing missing) or hands gamdl a **per-track URL list**
covering only the missing tracks — far less re-downloading than re-running the
whole album/artist URL. On any expander error it logs and falls back to the
legacy path, so the flag is safe to leave off (the default). Debug endpoints:
`/expand`, `/expand-filter`, `/resolve-urls`.

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
- `/library` **Library** — Navidrome + Beets service cards on top, then a
  counts header and a two-tab browser:
  - **Browse** — artist → album → track drill-down
    (`/fragments/library/artists` → `/fragments/library/artist` →
    `/fragments/library/album/{id}`). Album rows carry **redownload**
    (`/library/album/{id}/redownload`, re-fetches only missing tracks via the
    runner) and **play** (`/library/album/{id}/play`, 302s into Navidrome).
    Artist/album-level bulk actions go through `/library/artist/action` and
    `/library/album/{id}/action`.
  - **Search** — the original flat track list (`/fragments/library`) with
    search/filter and per-row del / del+block / undelete / blocklist actions
    plus a multi-select `/library/bulk`.
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

# Per-track expansion debug (needs cookies.txt; see USE_TRACK_EXPANSION)
curl 'http://localhost:4150/expand?url=<url-encoded>&kind=album'
curl 'http://localhost:4150/expand-filter?url=<url-encoded>&kind=album'

# Bulk-seed the watchlist from an exported Apple Music library (dry-run first)
docker exec -w /srv gamdl-ui /opt/venv/bin/python /scripts/import-am-library.py \
  --tsv /scripts/am-library.tsv --storefront gb --dry-run

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
- **filter_incoming strips ALL already-held Incoming files, including
  same-inode hardlinks.** Under `move: yes` a leftover hardlink would be
  re-imported by beets into a new `[NNNN]` folder → a distinct Navidrome
  entry (issue #1). Unlinking a hardlink is safe: it only drops that
  directory entry, the Library twin survives. gamdl's skip cache is the
  tracks DB (via `USE_TRACK_EXPANSION`), not Incoming hardlinks.
- **`/mnt/sata/navidrome/{Incoming,Library,NavidromeData}` is shared
  state**, not project-local. Compose references these via
  `${INCOMING_DIR}`, `${LIBRARY_DIR}`, `${NAVIDROME_DATA}`, `${MUSIC_ROOT}`
  from `.env`.
- **`gamdl-ui` is Tailscale-only** by design (manages cookies + can
  trigger downloads + can unlink Library files). Not exposed via the
  Cloudflare Tunnel.
- **Runner parses stdout split on `\r`**, not just `\n`. yt-dlp emits
  progress frames without newlines, so chunks can contain many progress
  frames plus the next `[Track N/TOTAL] Downloading` announcement glued to
  the tail. Not splitting on `\r` caused the track counter to miss every
  actual download (only skipped tracks got clean standalone lines). The
  reader is chunk-based (`stdout.read`, own `[\r\n]` splitting), which also
  makes the inactivity watchdog accurate: `RUNNER_INACTIVITY_TIMEOUT`
  (default 900s) of true silence kills the gamdl **process group**
  (`start_new_session=True`, so yt-dlp/ffmpeg children die too) instead of
  a hung run holding the runner reservation forever.
- **Library wipe-guard**: `index_library()` refuses to mass-clear
  `library_path` when a sweep sees fewer than half the tracks the DB holds
  (min 10 rows). Without it, an unmounted/half-mounted Library looked like
  "everything was deleted", nulled every `library_path`, and the next
  track-expansion run re-downloaded the entire library. Deliberate deletes
  go through the UI (which clears paths in the DB immediately), so a
  legitimate sweep never trips it; the stats dict carries `guard_tripped`.
- **auto-import waits for Incoming to settle** (any audio file with mtime
  <1 min defers the cycle) because gamdl's temp→Incoming move is a
  cross-filesystem copy, not an atomic rename — importing mid-copy would
  tag a truncated m4a forever. `beet import` also runs under
  `timeout ${BEET_TIMEOUT:-3600}` so a hung network plugin (fetchart /
  discogs) can't wedge the loop.

## File reference

| Path                            | Purpose                                                |
|---------------------------------|--------------------------------------------------------|
| `docker-compose.yml`            | All six services on `music-network`; gamdl-ui has `/downloads` + `/library` (rw) + `/music` (unified for hardlinks) |
| `.env`                          | Filesystem paths, `PUID`/`PGID`, ports, Navidrome + Last.fm API creds |
| `scripts/auto-import.sh`        | Beets sidecar loop — curls filter-incoming, runs `beet import`, chowns Library + Incoming |
| `scripts/{setup,fix_permissions,tag-music,setup_symlinks}.sh` | One-shot ops helpers |
| `scripts/export-am-library.applescript` | Run on the Mac with the Apple Music library; dumps `artist<TAB>album` per track to a TSV (bulk-watchlist seeding) |
| `scripts/normalize-am-library.py` | Parses Apple's `Music Library.xml` export → deduped `(album_artist, album)` TSV, filtered to AM catalog tracks |
| `scripts/import-am-library.py`  | Resolves a TSV of (artist, album) to AM album URLs and appends to the watchlist; run inside `gamdl-ui` (imports `watchlist` + `pre_filter`) |
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
| `gamdl-ui/app/pre_filter.py`    | `canonical_id` (dedupe), `should_skip`, `should_skip_for_cron`, `expand_and_filter` (Phase 3) |
| `gamdl-ui/app/expander.py`      | Wraps gamdl's `AppleMusicApi` to enumerate a URL's tracks before download (per-track expansion, `USE_TRACK_EXPANSION=1`) |
| `gamdl-ui/app/runner.py`        | Subprocess manager — CR-aware stdout parser, pre-filter short-circuit, flag-gated per-track expansion (`USE_TRACK_EXPANSION`), post-run indexer call |
| `gamdl-ui/app/watchlist.py`     | Reads/writes `gamdl/config/*.txt`; `DuplicateEntryError` on add |
| `gamdl-ui/app/beets.py` `navidrome.py` `apple.py` `backfill.py` | External adapters |
| `gamdl-ui/app/templates/`       | Three full pages (`index.html`, `library.html`, `runs.html`) + shared partials (`_nav`, `_modals`, `_shared_scripts`) + fragments — flat list (`_library_rows`, `_library_row`), Browse drill-down (`_artists`, `_albums`, `_album_tracks`), plus (`_watchlist`, `_watchlist_tracks`, `_runs`, `_stats`, `_beets`, `_status`, `_progress`) |
| `gamdl-ui/ui-data/ui.db`        | SQLite — meta + runs + tracks (gitignored) |
| `gamdl-ui/ui-data/logs/`        | Per-run subprocess logs (gitignored) |

## Public exposure

Only `navidrome` (port 4533) is meant to be reachable from outside the LAN —
e.g. behind a reverse proxy or tunnel at a hostname like `music.example.com`.
`gamdl-ui` deliberately has **no** public route: it can manage cookies, trigger
downloads, and unlink Library files, so keep it on a trusted network (LAN/VPN)
only.
