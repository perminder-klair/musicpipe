"""Read/write the gamdl watchlist files (artists.txt, playlists.txt).

These text files are the source of truth for cron. The UI mirrors every change
back to them so the nightly cron run stays in sync with what the UI shows.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("GAMDL_CONFIG_DIR", "/config"))
ARTISTS_FILE = CONFIG_DIR / "artists.txt"
PLAYLISTS_FILE = CONFIG_DIR / "playlists.txt"
ALBUMS_FILE = CONFIG_DIR / "albums.txt"

KINDS = {"artist", "playlist", "album"}


def _file_for(kind: str) -> tuple[Path, str]:
    if kind == "artist":
        return ARTISTS_FILE, ARTISTS_HEADER
    if kind == "playlist":
        return PLAYLISTS_FILE, PLAYLISTS_HEADER
    if kind == "album":
        return ALBUMS_FILE, ALBUMS_HEADER
    raise ValueError(f"bad kind: {kind}")


@dataclass(frozen=True)
class WatchEntry:
    url: str
    kind: str  # "artist" | "playlist" | "album"


def _read(path: Path) -> list[str]:
    if not path.exists():
        return []
    urls: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    return urls


def _write(path: Path, urls: list[str], header_comment: str) -> None:
    body = "\n".join(urls)
    text = f"{header_comment.rstrip()}\n\n{body}\n" if urls else f"{header_comment.rstrip()}\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


ARTISTS_HEADER = """\
# Apple Music artist URLs — managed by gamdl-ui.
# Lines starting with # are ignored. Blank lines are ignored.
"""
PLAYLISTS_HEADER = """\
# Apple Music playlist URLs — managed by gamdl-ui.
# Lines starting with # are ignored. Blank lines are ignored.
"""
ALBUMS_HEADER = """\
# Apple Music album URLs — managed by gamdl-ui.
# Lines starting with # are ignored. Blank lines are ignored.
"""


def read_artists() -> list[str]:
    return _read(ARTISTS_FILE)


def read_playlists() -> list[str]:
    return _read(PLAYLISTS_FILE)


def read_albums() -> list[str]:
    return _read(ALBUMS_FILE)


def all_entries() -> list[WatchEntry]:
    out: list[WatchEntry] = []
    for u in read_artists():
        out.append(WatchEntry(url=u, kind="artist"))
    for u in read_albums():
        out.append(WatchEntry(url=u, kind="album"))
    for u in read_playlists():
        out.append(WatchEntry(url=u, kind="playlist"))
    return out


class DuplicateEntryError(ValueError):
    """Raised when an add would create a second entry for the same Apple ID.

    The existing URL may differ in country code or slug (e.g. /in/ vs /us/)
    — dedupe is on the canonical (kind, apple_id) tuple.
    """

    def __init__(self, existing_url: str, kind: str):
        super().__init__(f"{kind} already in watchlist: {existing_url}")
        self.existing_url = existing_url
        self.kind = kind


def add(url: str, kind: str) -> None:
    # Deferred import: watchlist is imported early in app startup, pre_filter
    # lives above it in the call graph.
    from . import pre_filter

    url = url.strip()
    if not url:
        raise ValueError("empty url")
    if kind not in KINDS:
        raise ValueError(f"bad kind: {kind}")
    path, header = _file_for(kind)
    urls = _read(path)
    if url in urls:
        return
    # Country-code / slug-agnostic dedupe. A None canonical means we couldn't
    # parse an Apple ID — allow the add so gamdl can reject it with a better
    # error than we can synthesize here.
    canon = pre_filter.canonical_id(url, kind)
    if canon is not None:
        for existing in urls:
            if pre_filter.canonical_id(existing, kind) == canon:
                raise DuplicateEntryError(existing, kind)
    urls.append(url)
    _write(path, urls, header)


def remove(url: str, kind: str) -> bool:
    if kind not in KINDS:
        raise ValueError(f"bad kind: {kind}")
    path, header = _file_for(kind)
    urls = _read(path)
    if url not in urls:
        return False
    urls.remove(url)
    _write(path, urls, header)
    return True


def cookies_present() -> tuple[bool, float | None]:
    cookies = CONFIG_DIR / "cookies.txt"
    if not cookies.exists() or cookies.stat().st_size == 0:
        return False, None
    return True, cookies.stat().st_mtime
