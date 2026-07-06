"""musicpipe — FastAPI app managing the Apple Music → Navidrome pipeline."""
from __future__ import annotations

import asyncio
import base64
import html
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import apple, backfill, backup, beets, expander, indexer, navidrome, pre_filter, runner, store, watchlist

BASE_DIR = Path(__file__).parent
GAMDL_LOGS_DIR = Path(os.environ.get("GAMDL_LOGS_DIR", "/gamdl-logs"))


def _format_ts(ts: Any) -> str:
    if ts is None:
        return "—"
    try:
        return time.strftime("%b %d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "—"


def _format_ago(ts: Any) -> str:
    """Short humanized 'X ago' — Ns / Nm / Nh / Nd. For freshness badges."""
    if ts is None:
        return "—"
    try:
        delta = max(0, int(time.time() - float(ts)))
    except (TypeError, ValueError):
        return "—"
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _truncate_url(url: Any) -> str:
    if not isinstance(url, str):
        return ""
    # Trim Apple Music URL to the slug portion for display.
    tail = url.rstrip("/").split("/")
    if len(tail) >= 2:
        return "/".join(tail[-2:])
    return url


templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["format_ts"] = _format_ts
templates.env.filters["format_ago"] = _format_ago
templates.env.filters["truncate_url"] = _truncate_url

app = FastAPI(title="musicpipe", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


INDEXER_INTERVAL = int(os.environ.get("INDEXER_INTERVAL", "120"))
BACKUP_INTERVAL = int(os.environ.get("BACKUP_INTERVAL", "86400"))

# Module-held references: a task created without one is eligible for GC
# mid-run (asyncio only keeps weak references to tasks).
_indexer_task: asyncio.Task | None = None
_maintenance_task: asyncio.Task | None = None


async def _indexer_loop() -> None:
    """Periodic sweep of Incoming + Library. First pass on startup doubles as
    the library backfill — every m4a gets a ``tracks`` row without us needing
    a one-shot script. Survives transient failures so a single bad file
    doesn't kill the loop."""
    while True:
        try:
            stats = await asyncio.to_thread(indexer.index_all)
            print(f"[indexer] {stats}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[indexer] sweep failed: {exc!r}", flush=True)
        await asyncio.sleep(INDEXER_INTERVAL)


async def _maintenance_loop() -> None:
    """Daily DB backup + run-log pruning. Checks hourly against the newest
    backup dir's age rather than sleeping BACKUP_INTERVAL flat, so container
    restarts neither pile up extra backups nor reset the schedule."""
    while True:
        try:
            age = backup.seconds_since_last_backup()
            if age is None or age >= BACKUP_INTERVAL:
                stats = await asyncio.to_thread(backup.run_maintenance)
                print(f"[maintenance] {stats}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[maintenance] failed: {exc!r}", flush=True)
        await asyncio.sleep(3600)


@app.on_event("startup")
async def _startup() -> None:
    store.init_db()
    reaped = store.reap_orphan_runs()
    if reaped:
        print(f"[startup] reaped {reaped} orphan running run(s)", flush=True)
    try:
        result = backfill.run()
        print(f"[startup] backfill: {result}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] backfill failed: {exc!r}", flush=True)
    # Kick off the tracks-table indexer + daily maintenance. References kept
    # at module level so the tasks can't be garbage-collected mid-loop;
    # uvicorn cancels them on shutdown via the default task-group teardown.
    global _indexer_task, _maintenance_task
    _indexer_task = asyncio.create_task(_indexer_loop())
    _maintenance_task = asyncio.create_task(_maintenance_loop())


@app.post("/maintenance/backfill")
async def maintenance_backfill() -> JSONResponse:
    return JSONResponse(backfill.run())


@app.post("/maintenance/backup")
async def maintenance_backup() -> JSONResponse:
    """On-demand DB backup + run-log prune (same pass the daily loop runs)."""
    return JSONResponse(await asyncio.to_thread(backup.run_maintenance))


@app.get("/pre-check")
async def pre_check(url: str, kind: str) -> JSONResponse:
    """Cron-path skip oracle.

    ``gamdl/run.sh`` hits this before every URL. Non-skip responses have
    ``{"skip": false}``; skip responses carry a human reason so the cron
    log is legible. The runner's UI path does its own in-process check via
    ``pre_filter.should_skip`` — they share the completeness rule but the
    cron path also applies the fresh-clean-run heuristic.
    """
    if kind not in watchlist.KINDS:
        raise HTTPException(400, f"unknown kind: {kind}")
    decision = pre_filter.should_skip_for_cron(url, kind)
    return JSONResponse({"skip": decision.skip, "reason": decision.reason})


@app.get("/expand")
async def expand(url: str, kind: str) -> JSONResponse:
    """Phase 1 debug endpoint: expand a watchlist URL into its per-track
    list via Apple Music API. Lets us verify expansion output against the
    tracks DB before wiring the runner to consume it."""
    if kind not in watchlist.KINDS:
        raise HTTPException(400, f"unknown kind: {kind}")
    try:
        refs = await expander.fetch_tracks(url, kind)
    except Exception as e:
        raise HTTPException(502, f"expansion failed: {e}")
    return JSONResponse({
        "url": url,
        "kind": kind,
        "count": len(refs),
        "tracks": [r.to_dict() for r in refs],
    })


@app.get("/expand-filter")
async def expand_filter(url: str, kind: str, verify: bool = True) -> JSONResponse:
    """Phase 2 debug endpoint: expand + diff against the tracks DB. Returns
    the full ``ExpandFilterResult`` so we can see what the runner would
    hand to gamdl vs what it would skip."""
    if kind not in watchlist.KINDS:
        raise HTTPException(400, f"unknown kind: {kind}")
    try:
        result = await pre_filter.expand_and_filter(url, kind, verify_exists=verify)
    except Exception as e:
        raise HTTPException(502, f"expand-and-filter failed: {e}")
    return JSONResponse(result.to_dict())


@app.get("/resolve-urls")
async def resolve_urls(url: str, kind: str) -> JSONResponse:
    """Phase 3 runner hook: return either a skip decision or the per-track
    URL list gamdl should download.

    Both the UI runner (via ``pre_filter.expand_and_filter`` directly) and
    cron ``run.sh`` (over HTTP) need this. The shape is optimized for the
    shell caller — flat ``urls`` array and a ``skip`` boolean mean a tiny
    python one-liner in bash can drive the decision.
    """
    if kind not in watchlist.KINDS:
        raise HTTPException(400, f"unknown kind: {kind}")
    try:
        result = await pre_filter.expand_and_filter(url, kind)
    except Exception as e:
        raise HTTPException(502, f"expand-and-filter failed: {e}")
    skip = not result.missing
    return JSONResponse({
        "skip": skip,
        "reason": result.reason,
        "urls": [] if skip else [t.track_url for t in result.missing],
        "summary": {
            "total": result.total,
            "present": result.present,
            "blocked": result.blocked,
            "missing": len(result.missing),
            "stale_matches": result.stale_matches,
        },
    })


@app.post("/maintenance/reindex")
async def maintenance_reindex() -> JSONResponse:
    """Synchronously re-run the full Incoming + Library sweep."""
    stats = await asyncio.to_thread(indexer.index_all)
    return JSONResponse(stats)


@app.post("/maintenance/filter-incoming")
async def maintenance_filter_incoming() -> JSONResponse:
    """Strip deleted + blocklisted tracks from Incoming before beets sees them.

    Invoked by ``auto-import.sh`` immediately before each ``beet import`` —
    non-blocking for the caller (they continue on HTTP error) so a gamdl-ui
    outage can't wedge the import pipeline.
    """
    stats = await asyncio.to_thread(indexer.filter_incoming)
    return JSONResponse(stats)


LIBRARY_PAGE_LIMIT = 100


def _pager_ctx(total: int, offset: int, limit: int) -> dict:
    """Compute numbered-pagination state with a windowed page list.

    ``pages`` is the sequence of buttons to render: page numbers plus None
    sentinels for "…" gaps (always shows first, last, and ±2 around current)."""
    limit = max(1, limit)
    total_pages = max(1, (total + limit - 1) // limit)
    page = min(max(1, offset // limit + 1), total_pages)
    window = {1, total_pages}
    window.update(n for n in range(page - 2, page + 3) if 1 <= n <= total_pages)
    pages: list[int | None] = []
    prev = 0
    for n in sorted(window):
        if n - prev > 1:
            pages.append(None)
        pages.append(n)
        prev = n
    return {"total": total, "page": page, "total_pages": total_pages, "pages": pages}


def _library_ctx(request: Request, q, status, genre, offset) -> dict:
    """Shared context for the flat-list page + fragment, incl. pager state."""
    total = store.count_tracks(q=q, status=status, genre=genre)
    tracks = store.list_tracks(
        q=q, status=status, genre=genre, limit=LIBRARY_PAGE_LIMIT, offset=offset
    )
    return {
        "request": request,
        "tracks": tracks,
        "q": q or "",
        "status": status or "",
        "genre": genre or "",
        "offset": offset,
        "limit": LIBRARY_PAGE_LIMIT,
        **_pager_ctx(total, offset, LIBRARY_PAGE_LIMIT),
    }


@app.get("/library", response_class=HTMLResponse)
async def library_page(
    request: Request,
    q: str | None = None,
    status: str | None = None,
    genre: str | None = None,
    offset: int = 0,
) -> HTMLResponse:
    cookies_ok, _ = watchlist.cookies_present()
    ctx = _library_ctx(request, q, status, genre, offset)
    ctx.update(
        {
            "active_nav": "library",
            "cookies_ok": cookies_ok,
            "counts": store.track_counts(),
            "genres": store.list_genres(),
        }
    )
    return templates.TemplateResponse("library.html", ctx)


@app.get("/fragments/library", response_class=HTMLResponse)
async def frag_library(
    request: Request,
    q: str | None = None,
    status: str | None = None,
    genre: str | None = None,
    offset: int = 0,
) -> HTMLResponse:
    return templates.TemplateResponse(
        "_library_rows.html", _library_ctx(request, q, status, genre, offset)
    )


def _render_library_row(request: Request, am_track_id: str, *, oob: bool) -> HTMLResponse:
    """Render a single `_library_row.html` fragment for the given track id.

    HTMX swaps it in by id; the `oob` flag adds `hx-swap-oob` so the bulk
    endpoint can return several rows in one response."""
    t = store.get_track(am_track_id)
    if t is None:
        raise HTTPException(404, "unknown track")
    return templates.TemplateResponse(
        "_library_row.html",
        {"request": request, "t": t, "oob": oob},
    )


@app.post("/library/delete", response_class=HTMLResponse)
async def library_delete(
    request: Request,
    am_track_id: str = Form(...),
    blocklist: bool = Form(False),
) -> HTMLResponse:
    result = await asyncio.to_thread(
        indexer.delete_library_file, am_track_id, also_blocklist=blocklist
    )
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "delete failed"))
    return _render_library_row(request, am_track_id, oob=False)


@app.post("/library/blocklist", response_class=HTMLResponse)
async def library_blocklist(
    request: Request,
    am_track_id: str = Form(...),
    on: bool = Form(True),
) -> HTMLResponse:
    ok = store.set_blocklisted(am_track_id, on)
    if not ok:
        raise HTTPException(404, "unknown track")
    return _render_library_row(request, am_track_id, oob=False)


@app.post("/library/undelete", response_class=HTMLResponse)
async def library_undelete(
    request: Request,
    am_track_id: str = Form(...),
) -> HTMLResponse:
    ok = store.unmark_deleted(am_track_id)
    if not ok:
        raise HTTPException(404, "unknown track")
    return _render_library_row(request, am_track_id, oob=False)


_BULK_ACTIONS = {"delete", "delete_block", "block"}


@app.post("/library/bulk", response_class=HTMLResponse)
async def library_bulk(
    request: Request,
    action: str = Form(...),
    am_track_ids: list[str] = Form(default=[], alias="am_track_id"),
) -> HTMLResponse:
    """Apply the same action to many tracks in one shot.

    Responds with N concatenated ``_library_row.html`` fragments, each
    marked ``hx-swap-oob`` so HTMX replaces each row in place by id — the
    triggering button uses ``hx-swap="none"`` so the main target is ignored.
    Silently skips unknown ids; per-row failures in `delete` raise, matching
    the single-row endpoint's behaviour.
    """
    if action not in _BULK_ACTIONS:
        raise HTTPException(400, f"unknown action: {action}")
    if not am_track_ids:
        return HTMLResponse("")

    updated: list[str] = []
    for tid in am_track_ids:
        if action == "delete":
            result = await asyncio.to_thread(
                indexer.delete_library_file, tid, also_blocklist=False
            )
            if not result.get("ok"):
                continue
        elif action == "delete_block":
            result = await asyncio.to_thread(
                indexer.delete_library_file, tid, also_blocklist=True
            )
            if not result.get("ok"):
                continue
        else:  # block
            if not store.set_blocklisted(tid, True):
                continue
        updated.append(tid)

    chunks: list[str] = []
    for tid in updated:
        row = store.get_track(tid)
        if row is None:
            continue
        chunks.append(
            templates.get_template("_library_row.html").render(
                request=request, t=row, oob=True
            )
        )
    return HTMLResponse("".join(chunks))


# ---------------- Browsable library: Artists → Albums → Tracks ----------------

_ALBUM_ACTIONS = {"delete", "delete_block", "block", "unblock", "undelete"}


async def _apply_track_action(action: str, ids: list[str]) -> int:
    """Apply a library action to a set of track ids. Returns count affected.

    Reuses the same primitives as the per-row endpoints so album/artist-level
    actions behave identically to clicking each track by hand."""
    n = 0
    for tid in ids:
        if action in ("delete", "delete_block"):
            result = await asyncio.to_thread(
                indexer.delete_library_file, tid, also_blocklist=(action == "delete_block")
            )
            if result.get("ok"):
                n += 1
        elif action == "block":
            if store.set_blocklisted(tid, True):
                n += 1
        elif action == "unblock":
            if store.set_blocklisted(tid, False):
                n += 1
        elif action == "undelete":
            if store.unmark_deleted(tid):
                n += 1
    return n


@app.get("/fragments/library/artists", response_class=HTMLResponse)
async def frag_artists(request: Request, q: str | None = None) -> HTMLResponse:
    return templates.TemplateResponse(
        "_artists.html",
        {"request": request, "artists": store.list_artists(q=q), "q": q or ""},
    )


@app.get("/fragments/library/artist", response_class=HTMLResponse)
async def frag_artist_albums(request: Request, name: str) -> HTMLResponse:
    # Query param (not path) so artist names containing "/" (e.g. AC/DC) work.
    return templates.TemplateResponse(
        "_albums.html",
        {
            "request": request,
            "artist": name,
            "summary": store.get_artist_summary(name),
            "albums": store.list_albums_for_artist(name),
        },
    )


@app.get("/fragments/library/album/{album_id}", response_class=HTMLResponse)
async def frag_album_tracks(request: Request, album_id: str) -> HTMLResponse:
    summary = store.get_album_summary(album_id)
    if summary is None:
        raise HTTPException(404, "unknown album")
    return templates.TemplateResponse(
        "_album_tracks.html",
        {
            "request": request,
            "album": summary,
            "tracks": store.list_tracks(album_id=album_id, limit=500),
            "running": runner.is_running(),
        },
    )


@app.post("/library/album/{album_id}/action", response_class=HTMLResponse)
async def album_action(request: Request, album_id: str, action: str = Form(...)) -> HTMLResponse:
    if action not in _ALBUM_ACTIONS:
        raise HTTPException(400, f"unknown action: {action}")
    await _apply_track_action(action, store.album_track_ids(album_id))
    return await frag_album_tracks(request, album_id)


@app.post("/library/artist/action", response_class=HTMLResponse)
async def artist_action(request: Request, artist: str = Form(...), action: str = Form(...)) -> HTMLResponse:
    if action not in _ALBUM_ACTIONS:
        raise HTTPException(400, f"unknown action: {action}")
    await _apply_track_action(action, store.artist_track_ids(artist))
    return await frag_artist_albums(request, artist)


@app.post("/library/album/{album_id}/redownload")
async def album_redownload(album_id: str) -> JSONResponse:
    """Re-fetch an album. gamdl + track-expansion only pull what's missing,
    so this is safe to hit on a fully-present album (it no-ops)."""
    summary = store.get_album_summary(album_id)
    if summary is None:
        raise HTTPException(404, "unknown album")
    # Slug is cosmetic — gamdl/expander parse the trailing id; storefront comes
    # from the authenticated account, so the country code is irrelevant.
    url = f"https://music.apple.com/us/album/_/{album_id}"
    if not runner.start(url=url, kind="album", trigger="library-redownload"):
        raise HTTPException(409, "A sync is already running")
    await asyncio.sleep(0.1)
    return JSONResponse({"ok": True, "state": runner.current_state()})


@app.get("/library/album/{album_id}/play")
async def album_play(album_id: str) -> RedirectResponse:
    """302 to the album's page in Navidrome, resolving its id by name search.
    Falls back to a name-filtered album view when the lookup misses."""
    summary = store.get_album_summary(album_id)
    if summary is None:
        raise HTTPException(404, "unknown album")
    album = summary.get("album") or ""
    artist = summary.get("artist_name") or ""
    nd_id = await navidrome.find_album_id(album, artist)
    target = navidrome.album_deeplink(nd_id) if nd_id else navidrome.search_deeplink(album)
    return RedirectResponse(target, status_code=302)


# ---------------- helpers ----------------

def encode_url(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def decode_url(tok: str) -> str:
    pad = 4 - (len(tok) % 4)
    if pad != 4:
        tok = tok + ("=" * pad)
    return base64.urlsafe_b64decode(tok).decode()


def _entry_view(
    url: str,
    kind: str,
    meta: dict | None,
    last_run: dict | None,
    library_track_count: int | None,
) -> dict:
    return {
        "url": url,
        "kind": kind,
        "url_token": encode_url(url),
        "title": (meta or {}).get("title") or _fallback_title(url),
        "image": (meta or {}).get("image"),
        "description": (meta or {}).get("description"),
        "total_tracks": (meta or {}).get("total_tracks"),
        "last_run": last_run,
        "library_track_count": library_track_count,
    }


def _fallback_title(url: str) -> str:
    # e.g. https://music.apple.com/in/artist/shubh/1585737475 → "shubh"
    tail = url.rstrip("/").rsplit("/", 2)
    if len(tail) >= 2:
        return tail[-2].replace("-", " ").title()
    return url


async def _collect_entries() -> list[dict]:
    entries = watchlist.all_entries()
    urls = [e.url for e in entries]
    metas = store.bulk_meta(urls)
    last_runs = store.last_runs_for(urls)

    # Album-only: held-track count from the tracks DB, for the "N / total" card line.
    album_urls_to_ids: dict[str, str] = {}
    for e in entries:
        if e.kind == "album":
            aid = pre_filter.album_id_from_url(e.url)
            if aid:
                album_urls_to_ids[e.url] = aid
    album_counts = store.album_progress_by_ids(list(set(album_urls_to_ids.values())))

    out: list[dict] = []
    for e in entries:
        library_count: int | None = None
        if e.kind == "album":
            aid = album_urls_to_ids.get(e.url)
            if aid:
                library_count = album_counts.get(aid, 0)
        out.append(_entry_view(e.url, e.kind, metas.get(e.url), last_runs.get(e.url), library_count))
    return out


# ---------------- page ----------------

_TAB_KINDS = {"artist", "album", "playlist"}


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, kind: str = "artist") -> HTMLResponse:
    if kind not in _TAB_KINDS:
        kind = "artist"
    entries = await _collect_entries()
    counts = {
        "artist": sum(1 for e in entries if e["kind"] == "artist"),
        "album": sum(1 for e in entries if e["kind"] == "album"),
        "playlist": sum(1 for e in entries if e["kind"] == "playlist"),
    }
    entries = [e for e in entries if e["kind"] == kind]
    cookies_ok, cookies_mtime = watchlist.cookies_present()
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "active_nav": "dashboard",
            "entries": entries,
            "counts": counts,
            "active_kind": kind,
            "running": runner.current_state(),
            "cookies_ok": cookies_ok,
            "cookies_mtime": cookies_mtime,
        },
    )


RUNS_PAGE_LIMIT = 50


def _runs_ctx(request: Request, offset: int) -> dict:
    """Shared context for the runs page + fragment, incl. pager state."""
    total = store.count_runs()
    runs = store.recent_runs(limit=RUNS_PAGE_LIMIT, offset=offset)
    return {
        "request": request,
        "runs": runs,
        "offset": offset,
        "limit": RUNS_PAGE_LIMIT,
        **_pager_ctx(total, offset, RUNS_PAGE_LIMIT),
    }


@app.get("/runs", response_class=HTMLResponse)
async def runs_page(request: Request) -> HTMLResponse:
    cookies_ok, _ = watchlist.cookies_present()
    ctx = _runs_ctx(request, 0)
    ctx.update({"active_nav": "runs", "cookies_ok": cookies_ok})
    return templates.TemplateResponse("runs.html", ctx)


# ---------------- fragments ----------------

@app.get("/fragments/watchlist", response_class=HTMLResponse)
async def frag_watchlist(request: Request, kind: str = "artist") -> HTMLResponse:
    if kind not in _TAB_KINDS:
        kind = "artist"
    entries = await _collect_entries()
    entries = [e for e in entries if e["kind"] == kind]
    return templates.TemplateResponse(
        "_watchlist.html", {"request": request, "entries": entries, "active_kind": kind}
    )


@app.get("/fragments/watchlist/tracks", response_class=HTMLResponse)
async def frag_watchlist_tracks(
    request: Request,
    url: str,
    kind: str,
    offset: int = 0,
) -> HTMLResponse:
    """Per-card inline track listing.

    Album → exact match by am_album_id.
    Artist → album_artist / artist LIKE match on the watchlist entry's
             display name (falls back to URL slug if meta is missing).
    Playlist → placeholder until we track playlist membership.
    """
    limit = 100
    tracks: list[dict] = []
    note: str | None = None
    if kind == "album":
        aid = pre_filter.album_id_from_url(url)
        if aid is None:
            note = "Can't parse an album ID from this URL."
        else:
            tracks = store.list_tracks(album_id=aid, limit=limit, offset=offset)
    elif kind == "artist":
        meta = store.get_meta(url) or {}
        name = meta.get("title") or _fallback_title(url)
        tracks = store.list_tracks(artist_name=name, limit=limit, offset=offset)
    elif kind == "playlist":
        note = "Playlist track listings aren't tracked yet — open the playlist on Apple Music."
    else:
        raise HTTPException(400, "unknown kind")
    return templates.TemplateResponse(
        "_watchlist_tracks.html",
        {
            "request": request,
            "tracks": tracks,
            "note": note,
            "url": url,
            "kind": kind,
            "offset": offset,
            "limit": limit,
            "has_more": len(tracks) == limit,
            "scope_token": encode_url(url),
        },
    )


@app.get("/fragments/runs", response_class=HTMLResponse)
async def frag_runs(request: Request, offset: int = 0) -> HTMLResponse:
    return templates.TemplateResponse("_runs.html", _runs_ctx(request, offset))


@app.get("/fragments/status", response_class=HTMLResponse)
async def frag_status(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "_status.html", {"request": request, "running": runner.current_state()}
    )


@app.get("/fragments/stats", response_class=HTMLResponse)
async def frag_stats(request: Request) -> HTMLResponse:
    stats: dict[str, Any] = {"error": None, "artists": None, "scanning": None}
    try:
        lib = await navidrome.library_stats()
        stats.update(lib)
    except Exception as exc:  # noqa: BLE001
        stats["error"] = str(exc)
    try:
        scan = await navidrome.scan_status()
        stats["scanning"] = scan.scanning
        stats["scan_count"] = scan.count
    except Exception:
        pass
    return templates.TemplateResponse("_stats.html", {"request": request, "stats": stats})


# ---------------- watchlist CRUD ----------------

@app.post("/watchlist/add", response_class=HTMLResponse)
async def add_watch(
    request: Request,
    url: str = Form(...),
    kind: str = Form(...),
) -> HTMLResponse:
    url = url.strip()
    if not url.startswith("http"):
        raise HTTPException(400, "url must be an http(s) link")
    if kind not in watchlist.KINDS:
        raise HTTPException(400, f"kind must be one of {sorted(watchlist.KINDS)}")
    try:
        watchlist.add(url, kind)
    except watchlist.DuplicateEntryError as dup:
        # 409 renders HTMX's default error UX (flash); the message carries
        # the existing URL so the user sees what they already have.
        raise HTTPException(409, str(dup))
    try:
        meta = await apple.fetch(url)
        store.upsert_meta(url=url, kind=meta.kind or kind, title=meta.title, image=meta.image, description=meta.description)
    except Exception:  # noqa: BLE001
        pass
    entries = await _collect_entries()
    entries = [e for e in entries if e["kind"] == kind]
    return templates.TemplateResponse(
        "_watchlist.html", {"request": request, "entries": entries, "active_kind": kind}
    )


@app.post("/watchlist/remove", response_class=HTMLResponse)
async def remove_watch(
    request: Request,
    url: str = Form(...),
    kind: str = Form(...),
    active_kind: str = Form("artist"),
) -> HTMLResponse:
    watchlist.remove(url, kind)
    store.delete_meta(url)
    if active_kind not in _TAB_KINDS:
        active_kind = "artist"
    entries = await _collect_entries()
    entries = [e for e in entries if e["kind"] == active_kind]
    return templates.TemplateResponse(
        "_watchlist.html", {"request": request, "entries": entries, "active_kind": active_kind}
    )


@app.post("/meta/rename", response_class=HTMLResponse)
async def rename_meta_route(
    request: Request,
    url: str = Form(...),
    title: str = Form(...),
    active_kind: str = Form("artist"),
) -> HTMLResponse:
    store.rename_meta(url, title)
    if active_kind not in _TAB_KINDS:
        active_kind = "artist"
    entries = await _collect_entries()
    entries = [e for e in entries if e["kind"] == active_kind]
    return templates.TemplateResponse(
        "_watchlist.html", {"request": request, "entries": entries, "active_kind": active_kind}
    )


@app.post("/meta/refresh", response_class=HTMLResponse)
async def refresh_meta(
    request: Request,
    active_kind: str = Form("artist"),
) -> HTMLResponse:
    entries = watchlist.all_entries()
    # Bounded fan-out: serial fetches made a big watchlist take minutes
    # (10s timeout per URL) and time the request out at the proxy.
    sem = asyncio.Semaphore(5)
    async with httpx.AsyncClient(
        timeout=10.0,
        headers={"User-Agent": apple.UA},
        follow_redirects=True,
    ) as client:

        async def refresh_one(e: watchlist.WatchEntry) -> None:
            async with sem:
                try:
                    meta = await apple.fetch(e.url, client=client)
                    store.upsert_meta(
                        url=e.url,
                        kind=meta.kind or e.kind,
                        title=meta.title,
                        image=meta.image,
                        description=meta.description,
                    )
                except Exception:  # noqa: BLE001
                    pass

        await asyncio.gather(*(refresh_one(e) for e in entries))
    if active_kind not in _TAB_KINDS:
        active_kind = "artist"
    entries_view = await _collect_entries()
    entries_view = [e for e in entries_view if e["kind"] == active_kind]
    return templates.TemplateResponse(
        "_watchlist.html", {"request": request, "entries": entries_view, "active_kind": active_kind}
    )


# ---------------- sync ----------------

@app.post("/sync/one")
async def sync_one(url: str = Form(...), kind: str = Form(...)) -> JSONResponse:
    # runner.start reserves synchronously, so concurrent requests can't
    # double-launch — one wins, the rest 409.
    if not runner.start(url=url, kind=kind, trigger="manual"):
        raise HTTPException(409, "A sync is already running")
    # Give the runner a moment to flip state so the UI shows "running"
    await asyncio.sleep(0.1)
    return JSONResponse({"ok": True, "state": runner.current_state()})


@app.post("/sync/all")
async def sync_all() -> JSONResponse:
    entries = [(e.url, e.kind) for e in watchlist.all_entries()]
    if not entries:
        raise HTTPException(400, "watchlist is empty")
    if not runner.start_all(entries=entries, trigger="manual-all"):
        raise HTTPException(409, "A sync is already running")
    await asyncio.sleep(0.1)
    return JSONResponse({"ok": True, "count": len(entries), "state": runner.current_state()})


@app.post("/sync/cancel")
async def sync_cancel() -> JSONResponse:
    cancelled = runner.request_cancel()
    if not cancelled:
        raise HTTPException(409, "No sync is running")
    return JSONResponse({"ok": True, "state": runner.current_state()})


def _sse_format(seq: int, line: str) -> str:
    # SSE requires "data:" prefix on each wrapped line within a message.
    data = "\n".join(f"data: {p}" for p in line.split("\n"))
    return f"id: {seq}\n{data}\n\n"


@app.get("/sync/stream")
async def sync_stream(request: Request) -> StreamingResponse:
    """Resumable SSE log stream.

    Honours the standard ``Last-Event-ID`` header (set automatically by
    EventSource on reconnect) so we replay only the lines the client missed.
    Idle connections get a `: keepalive` comment every 15 s to keep proxies
    from closing us mid-sync.
    """
    hdr = request.headers.get("last-event-id")
    resume_from = int(hdr) if hdr and hdr.isdigit() else 0

    async def gen():
        yield "retry: 2000\n\n"
        last_sent = resume_from
        # Drain-then-wait loop: always flush any buffered lines before sleeping
        # on the next-line event. This closes the race where a line arrives
        # between the previous drain and the next await (the event gets
        # swapped on append, so a set() on the old event is lost if nobody is
        # waiting on it yet).
        while True:
            for seq, line in runner.snapshot_since(last_sent):
                yield _sse_format(seq, line)
                last_sent = seq
            if not runner.is_running() and last_sent >= runner.current_seq():
                yield "event: done\ndata: {}\n\n"
                return
            try:
                await asyncio.wait_for(runner.wait_for_line(), timeout=15.0)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                # Loop back to drain+check again.

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------- progress fragment + historical log ----------------

@app.get("/fragments/progress", response_class=HTMLResponse)
async def frag_progress(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "_progress.html", {"request": request, "running": runner.current_state()}
    )


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_NOISE_RE = re.compile(r"^\s*\[download\]\s+\d")
_LOG_LINE_CAP = 5000


def _render_historical_log(path: Path) -> str:
    """Read a raw gamdl log, collapse `\r` overwrites, strip ANSI + noise."""
    out: list[str] = []
    with path.open("rb") as f:
        for raw in f:
            # yt-dlp uses \r to overwrite progress on a single line; keep only
            # the last segment between newlines so the 1MB raw log collapses
            # down to a few hundred meaningful lines.
            decoded = raw.decode("utf-8", errors="replace")
            pieces = decoded.split("\r")
            for piece in pieces:
                clean = _ANSI_RE.sub("", piece).rstrip("\n")
                if clean and not _NOISE_RE.match(clean):
                    out.append(clean)
    truncated = False
    if len(out) > _LOG_LINE_CAP:
        out = out[-_LOG_LINE_CAP:]
        truncated = True
    body = html.escape("\n".join(out))
    prefix = (
        f"<div class='text-xs text-amber-400 mb-2'>"
        f"… truncated to last {_LOG_LINE_CAP:,} lines …</div>"
        if truncated
        else ""
    )
    return (
        prefix
        + "<pre class='text-xs font-mono whitespace-pre-wrap break-words "
        "text-zinc-300 leading-relaxed'>" + body + "</pre>"
    )


@app.get("/runs/{run_id}/log", response_class=HTMLResponse)
async def run_log(run_id: int) -> HTMLResponse:
    row = store.get_run(run_id)
    if not row or not row.get("log_path"):
        raise HTTPException(404, "run not found")
    path = Path(row["log_path"])
    if not path.exists():
        raise HTTPException(410, "log file missing on disk")
    return HTMLResponse(_render_historical_log(path))


# ---------------- beets ----------------

@app.get("/fragments/beets", response_class=HTMLResponse)
async def frag_beets(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "_beets.html", {"request": request, "beets": beets.status()}
    )


@app.post("/beets/trigger")
async def beets_trigger() -> JSONResponse:
    ok = beets.trigger()
    if not ok:
        raise HTTPException(500, "failed to write trigger file")
    return JSONResponse({"ok": True, "status": beets.status()})


# ---------------- navidrome ----------------

@app.post("/navidrome/scan")
async def trigger_scan() -> JSONResponse:
    try:
        status = await navidrome.trigger_scan(full=False)
        return JSONResponse({"ok": True, "scanning": status.scanning, "count": status.count})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"navidrome error: {exc}")


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "time": time.time()}
