# musicpipe — Web UI for the music stack

Small FastAPI dashboard on port **4150** that manages the `gamdl` watchlist,
kicks off on-demand artist/album/playlist syncs, shows a live log tail, tracks
run + track state, and coordinates deletes + blocklists across the pipeline.

## What it does

- **Dashboard** (`/`): edit the watchlist, sync entries on demand, watch the
  live log stream, drill into each card to see its tracks.
- **Library** (`/library`): browse every tracked m4a (keyed on Apple Music
  track ID), search by title/artist/album, delete or blocklist to stop future
  re-downloads; quick-access Navidrome + Beets cards at the top.
- **Runs** (`/runs`): full sync history — manual, cron, pre-filter skips.
- **Behind the scenes**: periodic indexer keeps the `tracks` table in sync
  with Incoming + Library. Pre-beets filter strips blocklisted/deleted
  tracks. The cron path calls `/pre-check` to skip complete albums and
  fresh-clean runs.

## Architecture

```
musicpipe-ui container (port 4150)
│
├── shares volumes with gamdl + beets:
│     ../gamdl/config   → /config     (rw — watchlist + cookies)
│     ../gamdl/logs     → /gamdl-logs (ro — cron run.log)
│     /mnt/hdd/navidrome/Incoming  → /downloads
│     /mnt/hdd/navidrome/Library   → /library
│     /mnt/hdd/navidrome           → /music  (unified view for hardlink ops)
│
├── own volume:
│     ./ui-data → /ui-data          (SQLite DB + UI-triggered run logs)
│
└── on music-network, talks to:
      http://navidrome:4533  (Subsonic API)
```

The container has gamdl installed so UI-triggered syncs run here directly
(subject to a single-run lock). The nightly cron still runs in the sibling
`gamdl` service — but calls this UI's `/pre-check` and `/maintenance/filter-incoming`
endpoints to share state.

## Run

```
docker compose up -d --build
```

Browse [http://localhost:4150](http://localhost:4150) (or via Tailscale).

## Stack

- **Backend**: FastAPI + uvicorn (Python 3.12 on ubuntu:24.04)
- **Frontend**: server-rendered Jinja2 + HTMX + Tailwind CDN
- **Persistence**: SQLite at `ui-data/ui.db`
- **Tag reader**: mutagen (reads Apple Music `cnID`/`plID` atoms)
- **Apple metadata**: OG-tag scrape via httpx
- **Navidrome**: Subsonic REST API with salted-token auth

## Troubleshooting

| Symptom | Fix |
|---|---|
| "cookies missing" | Re-export `../gamdl/config/cookies.txt`. |
| Watchlist edits don't persist | `../gamdl/config` must be rw-mounted. |
| Navidrome stats blank | Check `NAVIDROME_USER`/`NAVIDROME_PASS` in `.env`. |
| Sync button 409 | Another sync is running; watch the status box. |
| Library delete → "Permission denied" | Run `docker exec auto-import chown -R 1000:1001 /music/Library /music/Incoming`. |
| Live log silent | SSE dropped; refresh to re-arm. |
