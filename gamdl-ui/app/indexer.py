"""Track-state indexer.

Observers for the two filesystem locations a track can occupy:
- ``index_incoming()`` walks the Incoming staging area (gamdl output).
- ``index_library()`` walks the Library (beets output, what Navidrome serves).

Each walker reads m4a atoms via mutagen, keys on the Apple Music track ID
(``cnID`` atom, written by gamdl), and upserts into ``tracks``. Rows are
never deleted here — a track that leaves Incoming after a beets import has
its ``incoming_path`` nulled but its ``downloaded_at`` + IDs preserved.

Phase A: observation only. Nothing consults the table to gate behavior yet.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from mutagen.mp4 import MP4, MP4StreamInfoError

from . import store

INCOMING_DIR = Path(os.environ.get("GAMDL_DOWNLOADS_DIR", "/downloads"))
LIBRARY_DIR = Path(os.environ.get("LIBRARY_DIR", "/library"))

# Unified mount that spans Incoming + Library on a single filesystem, used
# only for cross-mount hardlink operations (os.link fails with EXDEV when
# the source and destination are under the separate bind mounts above).
MUSIC_MOUNT = Path(os.environ.get("MUSIC_MOUNT", "/music"))


def _to_unified(p: Path) -> Path:
    """Translate a /downloads or /library path to its /music equivalent.

    Returns the original path unchanged for anything that doesn't match one
    of the two prefixes — callers treat that as "don't touch this".
    """
    s = str(p)
    if s.startswith("/downloads/"):
        return MUSIC_MOUNT / "Incoming" / s[len("/downloads/"):]
    if s.startswith("/library/"):
        return MUSIC_MOUNT / "Library" / s[len("/library/"):]
    return p

AUDIO_EXTS = {".m4a", ".mp4", ".aac"}

# Feature/credit parenthetical — `(feat. X)`, `[ft. Y, Z]`, `(featuring …)`.
# Stripped from title before the (album, title) fuzzy-dedupe lookup because
# Apple's per-edition tagging often appends the feature list on one side
# but not the other (Library tagged via MusicBrainz vs raw gamdl output).
# NOT applied to album — edition markers like "(Deluxe)" or "(Remastered
# 2019)" legitimately distinguish albums.
_FEATURE_RE = re.compile(
    r"\s*[\[\(]\s*(?:feat\.?|ft\.?|featuring)\b[^)\]]*[\)\]]",
    re.IGNORECASE,
)


def _norm_title(s: str | None) -> str:
    if not s:
        return ""
    return " ".join(_FEATURE_RE.sub("", s).lower().split())


def _norm_album(s: str | None) -> str:
    if not s:
        return ""
    return " ".join(s.lower().split())


@dataclass
class TrackTags:
    am_track_id: str
    am_album_id: str | None
    title: str | None
    artist: str | None
    album: str | None
    album_artist: str | None
    isrc: str | None
    mb_track_id: str | None
    mb_album_id: str | None
    genre: str | None


def _first(tags, key):
    v = tags.get(key)
    if isinstance(v, list) and v:
        return v[0]
    return None


def _first_str(tags, key) -> str | None:
    v = _first(tags, key)
    if v is None:
        return None
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8", errors="replace")
        except Exception:
            return None
    return str(v)


def _freeform(tags, name: str) -> str | None:
    """Read a `----:com.apple.iTunes:<name>` freeform atom, UTF-8 decoded."""
    key = f"----:com.apple.iTunes:{name}"
    v = tags.get(key)
    if isinstance(v, list) and v:
        raw = v[0]
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        return str(raw)
    return None


def _parse_isrc_from_xid(xid: str | None) -> str | None:
    # gamdl stores "BelieveSAS:isrc:INA3S2102160" — provider prefix varies.
    if not xid:
        return None
    parts = xid.split(":")
    for i, p in enumerate(parts):
        if p.lower() == "isrc" and i + 1 < len(parts):
            return parts[i + 1].strip()
    return None


def read_tags(path: Path) -> TrackTags | None:
    """Read a file's Apple Music / MusicBrainz identifiers. Returns None if
    the file isn't a readable m4a or lacks a track ID (``cnID``)."""
    try:
        mp4 = MP4(str(path))
    except (MP4StreamInfoError, Exception):
        return None
    tags = mp4.tags or {}
    cn_id = _first(tags, "cnID")
    if cn_id is None:
        return None
    pl_id = _first(tags, "plID")
    # `xid ` has a trailing space — iTunes four-char atom padding.
    xid = _first_str(tags, "xid ") or _first_str(tags, "xid")
    return TrackTags(
        am_track_id=str(int(cn_id)) if isinstance(cn_id, int) else str(cn_id),
        am_album_id=str(int(pl_id)) if isinstance(pl_id, int) else (str(pl_id) if pl_id else None),
        title=_first_str(tags, "\xa9nam"),
        artist=_first_str(tags, "\xa9ART"),
        album=_first_str(tags, "\xa9alb"),
        album_artist=_first_str(tags, "aART"),
        isrc=_parse_isrc_from_xid(xid),
        mb_track_id=_freeform(tags, "MusicBrainz Track Id"),
        mb_album_id=_freeform(tags, "MusicBrainz Album Id"),
        genre=_first_str(tags, "\xa9gen"),
    )


def _iter_audio_files(root: Path):
    if not root.exists():
        return
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
            yield p


def index_incoming(source_url: str | None = None) -> dict:
    """Walk Incoming, upsert download rows. Returns a small stat dict.

    ``source_url`` is attributed to every row upserted in this sweep. Pass the
    watchlist URL when you know it (UI-triggered run); leave None for periodic
    sweeps that can't attribute each file to a single URL.
    """
    scanned = 0
    upserted = 0
    unreadable = 0
    seen_ids: set[str] = set()
    for path in _iter_audio_files(INCOMING_DIR):
        scanned += 1
        tags = read_tags(path)
        if tags is None:
            unreadable += 1
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        store.upsert_track_incoming(
            am_track_id=tags.am_track_id,
            am_album_id=tags.am_album_id,
            title=tags.title,
            artist=tags.artist,
            album=tags.album,
            album_artist=tags.album_artist,
            isrc=tags.isrc,
            mb_track_id=tags.mb_track_id,
            mb_album_id=tags.mb_album_id,
            genre=tags.genre,
            incoming_path=str(path),
            downloaded_at=mtime,
            source_url=source_url,
        )
        seen_ids.add(tags.am_track_id)
        upserted += 1
    cleared = store.clear_incoming_path_if_missing(seen_ids)
    return {
        "location": "incoming",
        "scanned": scanned,
        "upserted": upserted,
        "unreadable": unreadable,
        "stale_cleared": cleared,
    }


def index_library() -> dict:
    """Walk Library, upsert import rows. Returns a small stat dict."""
    scanned = 0
    upserted = 0
    unreadable = 0
    seen_ids: set[str] = set()
    for path in _iter_audio_files(LIBRARY_DIR):
        scanned += 1
        tags = read_tags(path)
        if tags is None:
            unreadable += 1
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        store.upsert_track_library(
            am_track_id=tags.am_track_id,
            am_album_id=tags.am_album_id,
            title=tags.title,
            artist=tags.artist,
            album=tags.album,
            album_artist=tags.album_artist,
            isrc=tags.isrc,
            mb_track_id=tags.mb_track_id,
            mb_album_id=tags.mb_album_id,
            genre=tags.genre,
            library_path=str(path),
            imported_at=mtime,
        )
        seen_ids.add(tags.am_track_id)
        upserted += 1
    cleared = store.clear_library_path_if_missing(seen_ids)
    return {
        "location": "library",
        "scanned": scanned,
        "upserted": upserted,
        "unreadable": unreadable,
        "stale_cleared": cleared,
    }


def index_all() -> dict:
    return {
        "incoming": index_incoming(),
        "library": index_library(),
        "counts": store.track_counts(),
    }


def _same_inode(a: Path, b: Path) -> bool:
    """True iff ``a`` and ``b`` resolve to the same inode (i.e. are
    hardlinks of each other). False on any stat error — callers treat
    that as "not a hardlink" and make their own decision."""
    try:
        sa = a.stat()
        sb = b.stat()
    except OSError:
        return False
    return sa.st_ino == sb.st_ino and sa.st_dev == sb.st_dev


@dataclass
class Match:
    """Result of looking a track up in ``LibraryIndex``. ``kind`` is one of:

    - ``"none"``: no existing copy — fair game to download/import.
    - ``"blocked"``: soft-deleted or blocklisted; the user's "never again"
      flag takes precedence over any Library presence. ``library_path`` is
      intentionally ``None`` here — callers should act on the flag, not
      the file that may or may not still exist.
    - ``"by_id"``: exact Apple Music track ID match in a non-deleted
      Library row. Strongest dedupe signal.
    - ``"by_isrc"``: same ISRC, different am_track_id — cross-region
      re-release of the same recording.
    - ``"by_signature"``: normalized ``(album, title)`` match — catches
      re-masters and compilation-vs-artist splits where ISRCs don't line
      up but the track is effectively the same from a listener's view.
    """

    kind: str
    library_path: str | None = None


class LibraryIndex:
    """Point-in-time snapshot of the dedupe maps used by both the Incoming
    filter and the pre-expansion track-level pre-filter.

    Construction cost is four SQL round trips and a normalization pass over
    every Library row's ``(album, title)``. Matching against an instance is
    pure dict lookups. Callers that process many tracks should build one
    and reuse it; the snapshot is intentionally not refreshed, since a run
    should see a consistent view of the library.
    """

    def __init__(self) -> None:
        self.blocked_ids: set[str] = store.list_skip_ids()
        self.library_ids: set[str] = store.list_already_library_ids()
        self.library_paths: dict[str, str] = store.library_paths_by_track_id()
        # Cross-region dedupe: Apple Music sometimes assigns a different
        # track ID per storefront for the same recording. ISRC is stable
        # across those variants, so it catches them when ID lookup misses.
        self.library_isrcs: dict[str, str] = store.library_paths_by_isrc()
        # Fuzzy fallback for re-masters and compilation/artist-folder
        # splits: normalized (album, title) both sides, feature credits
        # stripped from title so "Father Time" matches "Father Time
        # (feat. Sampha)". Album intentionally NOT feature-stripped —
        # edition markers like "(Deluxe)" legitimately distinguish albums.
        self.library_by_sig: dict[tuple[str, str], str] = {}
        for row in store.library_rows_for_signature():
            sig = (_norm_album(row["album"]), _norm_title(row["title"]))
            if sig != ("", ""):
                self.library_by_sig[sig] = row["library_path"]

    def match(
        self,
        am_track_id: str,
        isrc: str | None,
        album: str | None,
        title: str | None,
    ) -> Match:
        if am_track_id in self.blocked_ids:
            return Match("blocked")
        if am_track_id in self.library_ids:
            return Match("by_id", self.library_paths.get(am_track_id))
        if isrc and isrc in self.library_isrcs:
            return Match("by_isrc", self.library_isrcs[isrc])
        sig = (_norm_album(album), _norm_title(title))
        if sig != ("", "") and sig in self.library_by_sig:
            return Match("by_signature", self.library_by_sig[sig])
        return Match("none")

    def is_empty(self) -> bool:
        return not (
            self.blocked_ids
            or self.library_ids
            or self.library_isrcs
            or self.library_by_sig
        )


def filter_incoming() -> dict:
    """Remove Incoming files that shouldn't reach beets.

    Two reasons a file gets unlinked:

    - **blocked**: am_track_id is soft-deleted or blocklisted. The decision
      is durable — the row keeps its deleted_at/blocklisted flag so any
      future re-download gets caught here again.
    - **duplicate**: am_track_id already has a library_path *and* the
      Incoming copy is a genuinely different inode from the Library file.
      That pattern means gamdl re-downloaded a track we already hold;
      unlinking beats letting beets match + duplicate_action: remove it.

    Legacy hardlinks (Incoming and Library sharing the same inode, a relic
    of the pre-Phase-D ``hardlink: yes`` era) are left alone — they cost no
    disk and serve as gamdl's built-in skip cache.

    Invoked by auto-import.sh before every ``beet import``. Prunes empty
    parent dirs so beets doesn't re-enter them on the next cycle.
    """
    index = LibraryIndex()
    result = {
        "blocked_ids": len(index.blocked_ids),
        "library_ids": len(index.library_ids),
        "library_isrcs": len(index.library_isrcs),
        "library_sigs": len(index.library_by_sig),
        "scanned": 0,
        "removed_blocked": 0,
        "removed_duplicate": 0,
        "replaced_with_hardlink": 0,
        "matched_by_isrc": 0,
        "matched_by_signature": 0,
        "kept_hardlinks": 0,
        "pruned_dirs": 0,
        "errors": 0,
    }
    if index.is_empty():
        return result
    touched_dirs: set[Path] = set()
    for path in _iter_audio_files(INCOMING_DIR):
        result["scanned"] += 1
        tags = read_tags(path)
        if tags is None:
            continue
        m = index.match(tags.am_track_id, tags.isrc, tags.album, tags.title)
        if m.kind == "blocked":
            try:
                path.unlink()
                result["removed_blocked"] += 1
                touched_dirs.add(path.parent)
            except OSError:
                result["errors"] += 1
            continue
        if m.kind == "by_isrc":
            result["matched_by_isrc"] += 1
        elif m.kind == "by_signature":
            result["matched_by_signature"] += 1
        lib_p = m.library_path
        if lib_p is not None:
            if _same_inode(path, Path(lib_p)):
                # Same-inode hardlink → cost-free skip cache, leave alone.
                result["kept_hardlinks"] += 1
                continue
            # Different inode: gamdl re-downloaded a track we already
            # have. Unlinking alone would cause a re-download-and-strip
            # loop on every cron run. Replace with a hardlink to the
            # Library copy so the next gamdl invocation sees the file
            # exists and skips, AND beets' incremental flag treats the
            # dir as already-imported.
            u_inc = _to_unified(path)
            u_lib = _to_unified(Path(lib_p))
            try:
                path.unlink()
                u_inc.parent.mkdir(parents=True, exist_ok=True)
                os.link(str(u_lib), str(u_inc))
                result["replaced_with_hardlink"] += 1
                continue
            except OSError:
                # Either the re-hardlink failed (cross-device, perms)
                # or the Library twin vanished between our DB read and
                # now. Fall through to plain removal — beets will
                # catch it via duplicate_action: remove.
                result["errors"] += 1
            try:
                path.unlink()
                result["removed_duplicate"] += 1
                touched_dirs.add(path.parent)
            except OSError:
                result["errors"] += 1
            continue
    # Prune now-empty album/artist folders so beets doesn't re-enter them.
    # Walk upward from each touched dir, stopping at INCOMING_DIR itself.
    for d in touched_dirs:
        cur = d
        while cur != INCOMING_DIR and cur.is_dir():
            try:
                next(cur.iterdir())
                break  # not empty
            except StopIteration:
                try:
                    cur.rmdir()
                    result["pruned_dirs"] += 1
                except OSError:
                    break
                cur = cur.parent
            except OSError:
                break
    return result


def delete_library_file(am_track_id: str, *, also_blocklist: bool = False) -> dict:
    """Unlink a track's files from Library and Incoming, then flag the row.

    Best-effort: if either file is missing or already gone, that's not an
    error — the goal state is "no files on disk + flag set", and a missing
    file satisfies the first half. The flag change is what makes the
    decision durable across re-downloads.
    """
    row = store.get_track(am_track_id)
    if row is None:
        return {"ok": False, "error": "unknown track"}
    unlinked: list[str] = []
    for key in ("library_path", "incoming_path"):
        p = row.get(key)
        if not p:
            continue
        try:
            Path(p).unlink()
            unlinked.append(p)
        except FileNotFoundError:
            pass
        except OSError as exc:
            return {"ok": False, "error": f"unlink {p}: {exc}"}
    store.mark_deleted(am_track_id, also_blocklist=also_blocklist)
    # Next periodic sweep would null these anyway, but do it now so the UI
    # reflects the state-of-disk immediately after the action returns.
    store.clear_track_paths(am_track_id)
    return {
        "ok": True,
        "am_track_id": am_track_id,
        "unlinked": unlinked,
        "blocklisted": also_blocklist,
    }
