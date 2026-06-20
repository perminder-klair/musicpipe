"""Apple Music URL → per-track expander.

Wraps gamdl's internal ``AppleMusicApi`` so the UI can enumerate the tracks
an album/artist/playlist URL would produce *before* invoking gamdl. Lets the
pre-filter diff against the tracks DB and hand gamdl only URLs for tracks
it doesn't already hold.

Auth: reuses the same ``cookies.txt`` gamdl already consumes. Storefront is
derived from the authenticated account (same one gamdl downloads from), so
per-track URLs returned here match the ones gamdl would otherwise resolve
itself.

Phase 1: raw expansion + debug endpoint. No caching, no diffing, no runner
integration yet.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from gamdl.api.apple_music_api import AppleMusicApi

from . import pre_filter

COOKIES_PATH = Path("/config/cookies.txt")

log = logging.getLogger(__name__)

_api: AppleMusicApi | None = None
_api_lock = asyncio.Lock()


@dataclass
class TrackRef:
    """A single Apple Music song. Fields mirror the columns in ``tracks``
    that the diff stage uses — ``am_track_id`` is the cnID atom, ``isrc``
    is the ISRC gamdl parses from xid."""

    am_track_id: str
    am_album_id: str | None
    title: str
    artist: str
    album: str
    album_artist: str | None
    isrc: str | None
    track_url: str

    def to_dict(self) -> dict:
        return asdict(self)


async def _get_api() -> AppleMusicApi:
    """Lazy-init a single ``AppleMusicApi`` for the process.

    The dev token is scraped from Apple's homepage and good for hours.
    On auth failures callers should ``reset()`` and retry; we don't do
    that automatically yet."""
    global _api
    async with _api_lock:
        if _api is None:
            _api = await AppleMusicApi.create_from_netscape_cookies(str(COOKIES_PATH))
        return _api


async def reset() -> None:
    """Drop the cached client so the next call rebuilds it. Cheap — the
    new instance re-reads cookies and re-scrapes the dev token."""
    global _api
    async with _api_lock:
        _api = None


def _track_from_song(song: dict, album_id: str | None) -> TrackRef | None:
    """Build a ``TrackRef`` from an AMP song object. Returns ``None`` if
    the object isn't a song (e.g. music-video entries in playlists)."""
    if song.get("type") != "songs":
        return None
    a = song.get("attributes") or {}
    return TrackRef(
        am_track_id=str(song["id"]),
        am_album_id=str(album_id) if album_id else None,
        title=a.get("name", "") or "",
        artist=a.get("artistName", "") or "",
        album=a.get("albumName", "") or "",
        album_artist=a.get("artistName") or None,
        isrc=a.get("isrc") or None,
        track_url=a.get("url", "") or "",
    )


async def _expand_album(album_id: str) -> list[TrackRef]:
    """All catalog tracks on an album. Warns if >1 page (shouldn't happen
    for standard albums; compilations could in theory)."""
    api = await _get_api()
    resp = await api.get_album(album_id)
    data = (resp.get("data") or [{}])[0]
    rel = (data.get("relationships") or {}).get("tracks") or {}
    items = rel.get("data") or []
    out: list[TrackRef] = []
    for t in items:
        tr = _track_from_song(t, album_id)
        if tr:
            out.append(tr)
    if rel.get("next"):
        # Rare — log and ignore rather than silently truncate.
        log.warning("album %s has paginated tracks (next=%s); extras skipped", album_id, rel["next"])
    return out


async def _expand_playlist(playlist_id: str) -> list[TrackRef]:
    """All catalog tracks in a playlist, paginated if >300 items. Music
    videos inside playlists are filtered out by ``_track_from_song``."""
    api = await _get_api()
    resp = await api.get_playlist(playlist_id, limit_tracks=300)
    data = (resp.get("data") or [{}])[0]
    rel = (data.get("relationships") or {}).get("tracks") or {}
    items = rel.get("data") or []
    out: list[TrackRef] = []
    for t in items:
        tr = _track_from_song(t, None)
        if tr:
            out.append(tr)
    async for page in api.extend_api_data(rel):
        for t in page.get("data") or []:
            tr = _track_from_song(t, None)
            if tr:
                out.append(tr)
    return out


async def _expand_artist(artist_id: str) -> list[TrackRef]:
    """All tracks across an artist's full-albums, compilation-albums,
    live-albums, and singles views. Each view yields album stubs; we
    fetch each album's tracklist separately (parallelized, bounded)."""
    api = await _get_api()
    resp = await api.get_artist(artist_id)
    data = (resp.get("data") or [{}])[0]
    views = (data.get("views") or {})
    album_ids: list[str] = []
    seen: set[str] = set()
    for view_name in ("full-albums", "compilation-albums", "live-albums", "singles"):
        view = views.get(view_name) or {}
        for item in view.get("data") or []:
            if item.get("type") != "albums":
                continue
            aid = str(item.get("id") or "")
            if aid and aid not in seen:
                seen.add(aid)
                album_ids.append(aid)
    if not album_ids:
        return []
    # Bounded concurrency — Apple's AMP API tolerates a few parallel
    # requests but we'd rather not hammer it for a 50-album artist.
    sem = asyncio.Semaphore(4)

    async def one(aid: str) -> list[TrackRef]:
        async with sem:
            try:
                return await _expand_album(aid)
            except Exception as e:
                log.warning("artist %s: album %s expansion failed: %s", artist_id, aid, e)
                return []

    results = await asyncio.gather(*(one(a) for a in album_ids))
    out: list[TrackRef] = []
    track_seen: set[str] = set()
    for group in results:
        for tr in group:
            if tr.am_track_id in track_seen:
                continue
            track_seen.add(tr.am_track_id)
            out.append(tr)
    return out


async def fetch_tracks(url: str, kind: str) -> list[TrackRef]:
    """Expand a watchlist URL to its constituent catalog tracks.

    Raises on URL-parse failures (caller treats these as unexpandable and
    falls through to passing the URL straight to gamdl). Raises on API
    failures too — we prefer a loud failure that flips back to the
    legacy path over a silent zero-results skip.
    """
    if kind == "album":
        aid = pre_filter.album_id_from_url(url)
        if not aid:
            raise ValueError(f"could not parse album id from {url}")
        return await _expand_album(aid)
    if kind == "playlist":
        pid = pre_filter.playlist_id_from_url(url)
        if not pid:
            raise ValueError(f"could not parse playlist id from {url}")
        return await _expand_playlist(pid)
    if kind == "artist":
        aid = pre_filter.artist_id_from_url(url)
        if not aid:
            raise ValueError(f"could not parse artist id from {url}")
        return await _expand_artist(aid)
    raise ValueError(f"unknown kind: {kind}")
