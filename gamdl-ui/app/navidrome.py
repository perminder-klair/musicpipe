"""Minimal Navidrome/Subsonic API client for scan trigger + library stats."""
from __future__ import annotations

import hashlib
import os
import secrets
import time
from dataclasses import dataclass

import httpx

NAVIDROME_URL = os.environ.get("NAVIDROME_URL", "http://navidrome:4533").rstrip("/")
# Browser-facing base used to build "open in Navidrome" deep links. Set this to
# your public Navidrome URL (e.g. https://music.example.com) via env.
NAVIDROME_PUBLIC_URL = os.environ.get("NAVIDROME_PUBLIC_URL", "http://localhost:4533").rstrip("/")
NAVIDROME_USER = os.environ.get("NAVIDROME_USER", "")
NAVIDROME_PASS = os.environ.get("NAVIDROME_PASS", "")
CLIENT = "gamdl-ui"
API_VERSION = "1.16.1"


def _auth_params() -> dict[str, str]:
    salt = secrets.token_hex(6)
    token = hashlib.md5((NAVIDROME_PASS + salt).encode()).hexdigest()
    return {
        "u": NAVIDROME_USER,
        "t": token,
        "s": salt,
        "v": API_VERSION,
        "c": CLIENT,
        "f": "json",
    }


@dataclass
class ScanStatus:
    scanning: bool
    count: int


# One client for the process — connection reuse instead of a TCP + client
# setup per Subsonic call (the stats fragment polls these endpoints).
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=10.0)
    return _client


async def _get(path: str, params: dict | None = None) -> dict:
    r = await _get_client().get(
        f"{NAVIDROME_URL}/rest/{path}",
        params={**_auth_params(), **(params or {})},
    )
    r.raise_for_status()
    return r.json().get("subsonic-response", {})


async def trigger_scan(full: bool = False) -> ScanStatus:
    resp = await _get("startScan", params={"fullScan": "true" if full else "false"})
    info = resp.get("scanStatus", {})
    return ScanStatus(scanning=bool(info.get("scanning")), count=int(info.get("count", 0)))


async def scan_status() -> ScanStatus:
    resp = await _get("getScanStatus")
    info = resp.get("scanStatus", {})
    return ScanStatus(scanning=bool(info.get("scanning")), count=int(info.get("count", 0)))


# library_stats paginates the entire album list; the stats fragment polls it,
# so cache the totals briefly rather than re-walking every album per poll.
_STATS_TTL_SEC = 120
_stats_cache: tuple[float, dict] | None = None


async def library_stats() -> dict:
    """Return Artists/Albums/Songs totals for the Navidrome card.

    Subsonic caps ``getAlbumList2`` at size=500 per call, so we paginate
    until a short page comes back — otherwise a library > 500 albums
    silently reports 500. Song total is free from ``getScanStatus.count``.
    """
    global _stats_cache
    if _stats_cache is not None and time.time() - _stats_cache[0] < _STATS_TTL_SEC:
        return dict(_stats_cache[1])

    resp = await _get("getArtists")
    artists_index = resp.get("artists", {}).get("index", [])
    artist_count = sum(len(bucket.get("artist", [])) for bucket in artists_index)

    album_count = 0
    offset = 0
    page_size = 500
    while True:
        album_resp = await _get(
            "getAlbumList2",
            params={"type": "alphabeticalByArtist", "size": page_size, "offset": offset},
        )
        page = album_resp.get("albumList2", {}).get("album", [])
        album_count += len(page)
        if len(page) < page_size:
            break
        offset += page_size

    stats = {
        "artists": artist_count,
        "albums": album_count,
    }
    _stats_cache = (time.time(), stats)
    return dict(stats)


async def artist_tracks(artist_name: str) -> int:
    """Search by artist name and return total track count."""
    resp = await _get("search3", params={"query": artist_name, "songCount": 1000, "artistCount": 1, "albumCount": 50})
    result = resp.get("searchResult3", {})
    return len(result.get("song", []))


async def find_album_id(album: str, artist: str | None = None) -> str | None:
    """Resolve a Navidrome album id by name (+ artist) via search3.

    We index by Apple Music ids, which Navidrome doesn't know, so the only
    bridge to a playback deep link is a name search. Prefer an album whose
    artist also matches; otherwise fall back to the first hit. Returns None
    when nothing matches (caller then links to a name search instead).
    """
    query = f"{album} {artist}".strip() if artist else album
    try:
        resp = await _get(
            "search3",
            params={"query": query, "albumCount": 10, "artistCount": 0, "songCount": 0},
        )
    except Exception:
        return None
    albums = resp.get("searchResult3", {}).get("album", []) or []
    if not albums:
        return None
    if artist:
        a_low = artist.lower()
        for al in albums:
            if (al.get("artist") or "").lower() == a_low and (al.get("name") or "").lower() == album.lower():
                return al.get("id")
        for al in albums:
            if (al.get("artist") or "").lower() == a_low:
                return al.get("id")
    return albums[0].get("id")


def album_deeplink(nd_album_id: str) -> str:
    """Public Navidrome web URL that opens an album's detail/play page."""
    return f"{NAVIDROME_PUBLIC_URL}/app/#/album/{nd_album_id}/show"


def search_deeplink(term: str) -> str:
    """Fallback: open Navidrome's UI focused on an album/artist name search."""
    from urllib.parse import quote
    return f"{NAVIDROME_PUBLIC_URL}/app/#/album?filter=%7B%22name%22%3A%22{quote(term)}%22%7D"
