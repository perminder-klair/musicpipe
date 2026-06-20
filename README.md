# musicpipe

Self-hosted music pipeline: **Apple Music downloader → MusicBrainz tagger →
Navidrome streamer**, with a web dashboard to drive it. One Docker Compose
project, one network, one `.env`.

You point it at the artists, albums, and playlists you follow on Apple Music.
Every night it downloads new releases, re-tags them against MusicBrainz, files
them into a clean library, and serves them over the Subsonic API so you can
stream from any client.

```
watchlist (artists/albums/playlists)
        │
        ▼  gamdl        (cron 03:00, or on-demand from the UI)
   Incoming/            tagged AAC 256 kbps .m4a
        │
        ▼  auto-import  (beets sidecar — MusicBrainz tag + file)
   Library/             canonical, tagged library
        │
        ▼  navidrome    (Subsonic API + web player on :4533)
   any Subsonic client
```

## Components

| Service       | Port | What it does |
|---------------|------|--------------|
| `gamdl`       | —    | Cron-driven Apple Music downloader. Reads watchlists, writes tagged `.m4a` to `Incoming/`. |
| `auto-import` | —    | beets sidecar. Polls `Incoming/`, MusicBrainz-tags, files into `Library/`. |
| `navidrome`   | 4533 | Streamer / Subsonic API. Serves `Library/`. |
| `gamdl-ui`    | 4150 | FastAPI dashboard: watchlist editor, on-demand sync, live logs, run history, library browser, delete/blocklist. |
| `beets`       | —    | Interactive beets shell for manual imports (`docker exec -it beets bash`). |
| `hdd-check`   | —    | Healthcheck sentinel — gates the stack on the library mount being present. |

> **Note on `gamdl-ui`:** it can manage your Apple Music cookies, trigger
> downloads, and delete files from your library. Keep it on a trusted network
> (LAN / VPN) — **do not** expose it directly to the internet. Navidrome
> (`:4533`) is the only service meant to be public-facing.

## Prerequisites

- Docker + Docker Compose.
- An **Apple Music subscription** and a Netscape-format cookies export from a
  logged-in session (see [gamdl](https://github.com/glomatico/gamdl) for how to
  obtain `cookies.txt`).
- A directory on a mounted disk for the music library.

## Quick start

```bash
git clone https://github.com/perminder-klair/musicpipe.git
cd musicpipe

# 1. Configure
cp .env.example .env                 # edit paths, set NAVIDROME_PASS, etc.

# 2. Apple Music cookies (Netscape format, chmod 600)
cp /path/to/your/cookies.txt gamdl/config/cookies.txt
chmod 600 gamdl/config/cookies.txt

# 3. Watchlists — copy the examples and add your own URLs
cp gamdl/config/albums.example.txt    gamdl/config/albums.txt
cp gamdl/config/artists.example.txt   gamdl/config/artists.txt
cp gamdl/config/playlists.example.txt gamdl/config/playlists.txt
#   (or just add URLs from the gamdl-ui dashboard after launch)

# 4. Launch
docker compose up -d --build
```

Then open:
- `http://<host>:4533` — Navidrome (create your admin user on first visit)
- `http://<host>:4150` — gamdl-ui dashboard

Add watchlist URLs in the dashboard (or in `gamdl/config/*.txt`), then trigger a
sync or wait for the nightly cron.

## Common operations

```bash
docker compose ps                                   # health
docker compose logs -f gamdl-ui                     # tail a service
docker exec -u gamdl gamdl /usr/local/bin/run.sh    # trigger gamdl now
docker exec -it beets bash                          # interactive beets shell
touch beets/.trigger-import                          # wake auto-import within ~5s
```

## Configuration

All configuration is in `.env` (see `.env.example` for the full annotated list):
filesystem paths, runtime uid/gid, ports, Navidrome credentials, and optional
Last.fm API keys for similar-artist/song features.

## Architecture

See [`CLAUDE.md`](CLAUDE.md) for the full architecture deep-dive: the pipeline
internals, the skip/pre-check oracle, the `tracks` state table, the auto-import
filter, and a file-by-file reference.

## Legal

This project is a thin orchestration layer around
[gamdl](https://github.com/glomatico/gamdl), [beets](https://beets.io/), and
[Navidrome](https://www.navidrome.org/). It is intended for **personal use with
content you are entitled to access** through your own Apple Music subscription.
You are responsible for complying with Apple Music's Terms of Service and
applicable copyright law in your jurisdiction. The maintainers provide no
warranty and accept no liability for misuse.

## License

[MIT](LICENSE)
