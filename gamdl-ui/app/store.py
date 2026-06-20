"""SQLite-backed store for watchlist metadata and gamdl run history.

- `meta` caches Apple Music OG metadata per URL (title, image).
- `runs` records each UI-triggered or log-observed sync.
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DB_PATH = Path(os.environ.get("UI_DATA_DIR", "/ui-data")) / "ui.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    url TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT,
    image TEXT,
    description TEXT,
    total_tracks INTEGER,
    title_custom INTEGER NOT NULL DEFAULT 0,
    fetched_at REAL NOT NULL
);
-- Additive migration: adds total_tracks to an existing meta table without it.
-- The column is idempotent via the PRAGMA check below.

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    kind TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    exit_code INTEGER,
    tracks_new INTEGER DEFAULT 0,
    tracks_skipped INTEGER DEFAULT 0,
    tracks_failed INTEGER DEFAULT 0,
    log_path TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    trigger TEXT NOT NULL DEFAULT 'manual'
);

CREATE INDEX IF NOT EXISTS idx_runs_url_started ON runs (url, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs (started_at DESC);

-- tracks: state of record for every file we've downloaded or imported.
-- Keyed by the Apple Music track ID (atom `plID`), read out of the m4a file
-- by the indexer. Populated by two observers:
--   1. runner post-run + periodic indexer → downloaded_at + incoming_path
--   2. periodic library walk                → imported_at + library_path + mb_*
-- Phase A: observation only. Phase B will consult deleted_at / blocklisted
-- before handing files to beets.
CREATE TABLE IF NOT EXISTS tracks (
    am_track_id    TEXT PRIMARY KEY,
    am_album_id    TEXT,
    source_url     TEXT,
    title          TEXT,
    artist         TEXT,
    album          TEXT,
    album_artist   TEXT,
    isrc           TEXT,
    mb_track_id    TEXT,
    mb_album_id    TEXT,
    genre          TEXT,
    incoming_path  TEXT,
    library_path   TEXT,
    downloaded_at  REAL,
    imported_at    REAL,
    deleted_at     REAL,
    blocklisted    INTEGER NOT NULL DEFAULT 0,
    last_indexed_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tracks_album ON tracks (am_album_id);
CREATE INDEX IF NOT EXISTS idx_tracks_deleted ON tracks (deleted_at) WHERE deleted_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tracks_blocklisted ON tracks (blocklisted) WHERE blocklisted = 1;
"""


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        # Additive migration for existing DBs that predate total_tracks
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(meta)").fetchall()}
        if "total_tracks" not in cols:
            conn.execute("ALTER TABLE meta ADD COLUMN total_tracks INTEGER")
        if "title_custom" not in cols:
            conn.execute("ALTER TABLE meta ADD COLUMN title_custom INTEGER NOT NULL DEFAULT 0")
        tcols = {r["name"] for r in conn.execute("PRAGMA table_info(tracks)").fetchall()}
        if "genre" not in tcols:
            conn.execute("ALTER TABLE tracks ADD COLUMN genre TEXT")


def set_total_tracks(url: str, total: int) -> None:
    if total <= 0:
        return
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO meta (url, kind, total_tracks, fetched_at)
            VALUES (?, 'unknown', ?, ?)
            ON CONFLICT(url) DO UPDATE SET total_tracks=excluded.total_tracks, fetched_at=excluded.fetched_at
            """,
            (url, total, time.time()),
        )


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
    finally:
        conn.close()


# ---------------- meta ----------------

def upsert_meta(url: str, kind: str, title: str | None, image: str | None, description: str | None) -> None:
    """Upsert fetched metadata for a URL.

    A row whose ``title_custom=1`` (set by ``rename_meta``) keeps its existing
    title — Refresh metadata must not clobber a user-supplied name.
    """
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO meta (url, kind, title, image, description, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                kind=excluded.kind,
                title=CASE WHEN meta.title_custom=1 THEN meta.title ELSE excluded.title END,
                image=excluded.image,
                description=excluded.description,
                fetched_at=excluded.fetched_at
            """,
            (url, kind, title, image, description, time.time()),
        )


def get_meta(url: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM meta WHERE url=?", (url,)).fetchone()
        return dict(row) if row else None


def bulk_meta(urls: list[str]) -> dict[str, dict]:
    if not urls:
        return {}
    with connect() as conn:
        placeholders = ",".join("?" * len(urls))
        rows = conn.execute(
            f"SELECT * FROM meta WHERE url IN ({placeholders})", urls
        ).fetchall()
    return {r["url"]: dict(r) for r in rows}


def delete_meta(url: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM meta WHERE url=?", (url,))


def rename_meta(url: str, title: str) -> bool:
    """Set a user-supplied title for a URL. Creates a row if none exists.

    Flags the row with ``title_custom=1`` so a later ``upsert_meta`` (from
    Refresh metadata) won't overwrite the manual name.
    """
    title = title.strip()
    if not title:
        return False
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO meta (url, kind, title, title_custom, fetched_at)
            VALUES (?, 'unknown', ?, 1, ?)
            ON CONFLICT(url) DO UPDATE SET
                title=excluded.title,
                title_custom=1,
                fetched_at=excluded.fetched_at
            """,
            (url, title, time.time()),
        )
        return cur.rowcount > 0


# ---------------- runs ----------------

def start_run(url: str, kind: str, log_path: str, trigger: str = "manual") -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO runs (url, kind, started_at, log_path, status, trigger)
            VALUES (?, ?, ?, ?, 'running', ?)
            """,
            (url, kind, time.time(), log_path, trigger),
        )
        return cur.lastrowid


def update_run_counts(run_id: int, tracks_new: int, tracks_skipped: int, tracks_failed: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE runs SET tracks_new=?, tracks_skipped=?, tracks_failed=? WHERE id=?",
            (tracks_new, tracks_skipped, tracks_failed, run_id),
        )


def finish_run(
    run_id: int,
    exit_code: int,
    tracks_new: int = 0,
    tracks_skipped: int = 0,
    tracks_failed: int = 0,
) -> None:
    status = "ok" if exit_code == 0 else "failed"
    with connect() as conn:
        conn.execute(
            """
            UPDATE runs SET
                finished_at = ?,
                exit_code   = ?,
                tracks_new  = ?,
                tracks_skipped = ?,
                tracks_failed  = ?,
                status      = ?
            WHERE id = ?
            """,
            (time.time(), exit_code, tracks_new, tracks_skipped, tracks_failed, status, run_id),
        )


def recent_runs(limit: int = 50, offset: int = 0) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def count_runs() -> int:
    """Total run rows — drives the runs-page pager."""
    with connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0])


def get_run(run_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def last_run_for(url: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE url=? ORDER BY started_at DESC LIMIT 1", (url,)
        ).fetchone()
    return dict(row) if row else None


# ---------------- tracks ----------------
#
# Two entry points — one per location the file can occupy. Each leaves the
# other location's columns untouched so a track that's moved from Incoming to
# Library keeps the downloaded_at timestamp even after the Incoming row is
# dropped. Both upserts refresh last_indexed_at so a periodic sweeper can
# later GC rows whose files have disappeared.

def upsert_track_incoming(
    *,
    am_track_id: str,
    am_album_id: str | None,
    title: str | None,
    artist: str | None,
    album: str | None,
    album_artist: str | None,
    isrc: str | None,
    mb_track_id: str | None,
    mb_album_id: str | None,
    genre: str | None,
    incoming_path: str,
    downloaded_at: float,
    source_url: str | None = None,
) -> None:
    now = time.time()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO tracks (
                am_track_id, am_album_id, source_url, title, artist, album,
                album_artist, isrc, mb_track_id, mb_album_id, genre,
                incoming_path, downloaded_at, last_indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(am_track_id) DO UPDATE SET
                am_album_id    = COALESCE(excluded.am_album_id, tracks.am_album_id),
                source_url     = COALESCE(excluded.source_url, tracks.source_url),
                title          = COALESCE(excluded.title, tracks.title),
                artist         = COALESCE(excluded.artist, tracks.artist),
                album          = COALESCE(excluded.album, tracks.album),
                album_artist   = COALESCE(excluded.album_artist, tracks.album_artist),
                isrc           = COALESCE(excluded.isrc, tracks.isrc),
                mb_track_id    = COALESCE(excluded.mb_track_id, tracks.mb_track_id),
                mb_album_id    = COALESCE(excluded.mb_album_id, tracks.mb_album_id),
                genre          = COALESCE(excluded.genre, tracks.genre),
                incoming_path  = excluded.incoming_path,
                downloaded_at  = COALESCE(tracks.downloaded_at, excluded.downloaded_at),
                last_indexed_at = excluded.last_indexed_at
            """,
            (
                am_track_id, am_album_id, source_url, title, artist, album,
                album_artist, isrc, mb_track_id, mb_album_id, genre,
                incoming_path, downloaded_at, now,
            ),
        )


def upsert_track_library(
    *,
    am_track_id: str,
    am_album_id: str | None,
    title: str | None,
    artist: str | None,
    album: str | None,
    album_artist: str | None,
    isrc: str | None,
    mb_track_id: str | None,
    mb_album_id: str | None,
    genre: str | None,
    library_path: str,
    imported_at: float,
) -> None:
    now = time.time()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO tracks (
                am_track_id, am_album_id, title, artist, album,
                album_artist, isrc, mb_track_id, mb_album_id, genre,
                library_path, imported_at, last_indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(am_track_id) DO UPDATE SET
                am_album_id    = COALESCE(excluded.am_album_id, tracks.am_album_id),
                title          = COALESCE(excluded.title, tracks.title),
                artist         = COALESCE(excluded.artist, tracks.artist),
                album          = COALESCE(excluded.album, tracks.album),
                album_artist   = COALESCE(excluded.album_artist, tracks.album_artist),
                isrc           = COALESCE(excluded.isrc, tracks.isrc),
                mb_track_id    = COALESCE(excluded.mb_track_id, tracks.mb_track_id),
                mb_album_id    = COALESCE(excluded.mb_album_id, tracks.mb_album_id),
                genre          = COALESCE(excluded.genre, tracks.genre),
                library_path   = excluded.library_path,
                imported_at    = COALESCE(tracks.imported_at, excluded.imported_at),
                last_indexed_at = excluded.last_indexed_at
            """,
            (
                am_track_id, am_album_id, title, artist, album,
                album_artist, isrc, mb_track_id, mb_album_id, genre,
                library_path, imported_at, now,
            ),
        )


def clear_incoming_path_if_missing(am_track_ids_present: set[str]) -> int:
    """Null out incoming_path on rows whose file is no longer in Incoming.

    Called once per indexer sweep with the set of IDs we just observed; any
    track whose am_track_id is not in that set has been moved or deleted.
    Returns the number of rows cleared.
    """
    with connect() as conn:
        if am_track_ids_present:
            placeholders = ",".join("?" * len(am_track_ids_present))
            cur = conn.execute(
                f"""
                UPDATE tracks SET incoming_path = NULL
                WHERE incoming_path IS NOT NULL
                  AND am_track_id NOT IN ({placeholders})
                """,
                tuple(am_track_ids_present),
            )
        else:
            cur = conn.execute(
                "UPDATE tracks SET incoming_path = NULL WHERE incoming_path IS NOT NULL"
            )
        return cur.rowcount


def clear_library_path_if_missing(am_track_ids_present: set[str]) -> int:
    with connect() as conn:
        if am_track_ids_present:
            placeholders = ",".join("?" * len(am_track_ids_present))
            cur = conn.execute(
                f"""
                UPDATE tracks SET library_path = NULL
                WHERE library_path IS NOT NULL
                  AND am_track_id NOT IN ({placeholders})
                """,
                tuple(am_track_ids_present),
            )
        else:
            cur = conn.execute(
                "UPDATE tracks SET library_path = NULL WHERE library_path IS NOT NULL"
            )
        return cur.rowcount


def get_track(am_track_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM tracks WHERE am_track_id = ?", (am_track_id,)
        ).fetchone()
    return dict(row) if row else None


def list_skip_ids() -> set[str]:
    """IDs the pre-beets filter should strip because they're forbidden.

    Union of soft-deleted + blocklisted. See ``list_already_library_ids`` for
    the other strip set (already-imported duplicates).
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT am_track_id FROM tracks WHERE deleted_at IS NOT NULL OR blocklisted = 1"
        ).fetchall()
    return {r["am_track_id"] for r in rows}


def library_paths_by_track_id() -> dict[str, str]:
    """am_track_id → library_path, for the inode-aware filter in
    ``indexer.filter_incoming``. Single round trip keeps the filter loop
    from re-entering sqlite per row."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT am_track_id, library_path FROM tracks "
            "WHERE library_path IS NOT NULL AND deleted_at IS NULL"
        ).fetchall()
    return {r["am_track_id"]: r["library_path"] for r in rows}


def library_rows_for_signature() -> list[dict]:
    """Rows contributing to the (album, title) fuzzy dedupe map.

    Returned as raw dicts so the caller can normalise field-by-field
    (strip ``(feat. X)`` from title, collapse whitespace, etc.) rather
    than doing the munging in SQL. Only non-deleted Library rows qualify.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT album, title, library_path FROM tracks "
            "WHERE library_path IS NOT NULL "
            "  AND deleted_at IS NULL "
            "  AND album IS NOT NULL AND album != '' "
            "  AND title IS NOT NULL AND title != ''"
        ).fetchall()
    return [dict(r) for r in rows]


def library_paths_by_isrc() -> dict[str, str]:
    """isrc → library_path, for the ISRC-aware cross-edition dedupe in
    ``indexer.filter_incoming``. Only non-deleted Library rows with a
    non-empty ISRC contribute. Unlike the am_track_id keyed map, this one
    lets us spot the case where gamdl downloaded an edition-variant of a
    track we already hold (same recording, different Apple Music ID)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT isrc, library_path FROM tracks "
            "WHERE library_path IS NOT NULL "
            "  AND deleted_at IS NULL "
            "  AND isrc IS NOT NULL AND isrc != ''"
        ).fetchall()
    # A single ISRC maps uniquely to one recording; the tags table lets
    # duplicates slip in (e.g. reindex races). Last-writer-wins is fine —
    # both point to equivalent bytes.
    return {r["isrc"]: r["library_path"] for r in rows}


def list_already_library_ids() -> set[str]:
    """IDs of tracks that already have a Library file.

    Used by ``filter_incoming`` to strip dupes before beets — matters when
    gamdl re-downloads a track whose Library copy already exists (saves
    beets a round of match + duplicate_action: remove).
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT am_track_id FROM tracks WHERE library_path IS NOT NULL AND deleted_at IS NULL"
        ).fetchall()
    return {r["am_track_id"] for r in rows}


def pending_count() -> int:
    """Tracks genuinely awaiting import: in Incoming, not in Library, not
    soft-deleted. What the beets "Pending" badge should show."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM tracks
            WHERE incoming_path IS NOT NULL
              AND library_path IS NULL
              AND deleted_at IS NULL
            """
        ).fetchone()
    return int(row["n"]) if row else 0


def _tracks_filter(
    q: str | None,
    status: str | None,
    album_id: str | None,
    artist_name: str | None,
    genre: str | None,
) -> tuple[str, list]:
    """Build the shared WHERE clause + params for list_tracks / count_tracks.

    ``status`` is one of {"in_library", "deleted", "blocklisted", "orphaned"}:
    - in_library  → library_path IS NOT NULL AND deleted_at IS NULL
    - deleted     → deleted_at IS NOT NULL
    - blocklisted → blocklisted = 1
    - orphaned    → incoming_path IS NOT NULL AND library_path IS NULL
    """
    where: list[str] = []
    params: list = []
    if q:
        where.append("(title LIKE ? OR artist LIKE ? OR album LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like]
    if album_id:
        where.append("am_album_id = ?")
        params.append(album_id)
    if artist_name:
        # Match either the album_artist (canonical) or the comma-separated
        # artist field (catches features/collabs where the watched artist
        # is billed second).
        where.append("(album_artist LIKE ? OR artist LIKE ?)")
        like = f"%{artist_name}%"
        params += [like, like]
    if genre:
        where.append("genre = ?")
        params.append(genre)
    if status == "in_library":
        where.append("library_path IS NOT NULL AND deleted_at IS NULL")
    elif status == "deleted":
        where.append("deleted_at IS NOT NULL")
    elif status == "blocklisted":
        where.append("blocklisted = 1")
    elif status == "orphaned":
        where.append("incoming_path IS NOT NULL AND library_path IS NULL")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return clause, params


def list_tracks(
    *,
    q: str | None = None,
    status: str | None = None,
    album_id: str | None = None,
    artist_name: str | None = None,
    genre: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    """Paginated track listing for the Library browser."""
    clause, params = _tracks_filter(q, status, album_id, artist_name, genre)
    sql = (
        "SELECT * FROM tracks "
        f"{clause} "
        "ORDER BY album_artist, album, title LIMIT ? OFFSET ?"
    )
    with connect() as conn:
        rows = conn.execute(sql, (*params, limit, offset)).fetchall()
    return [dict(r) for r in rows]


def count_tracks(
    *,
    q: str | None = None,
    status: str | None = None,
    album_id: str | None = None,
    artist_name: str | None = None,
    genre: str | None = None,
) -> int:
    """Total rows matching the same filters as list_tracks — drives the pager."""
    clause, params = _tracks_filter(q, status, album_id, artist_name, genre)
    with connect() as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM tracks {clause}", params).fetchone()[0])


def list_genres() -> list[str]:
    """Distinct non-empty genres, alphabetised — feeds the library filter."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT genre FROM tracks "
            "WHERE genre IS NOT NULL AND genre != '' "
            "ORDER BY genre COLLATE NOCASE"
        ).fetchall()
    return [r["genre"] for r in rows]


# ── Browsable hierarchy (Artists → Albums → Tracks) ─────────────────────────
#
# The Library browser groups the flat tracks table two ways. Both keep a
# single canonical "artist" identity so a value selected in the artists grid
# round-trips exactly to the albums query: prefer the album_artist atom, fall
# back to the per-track artist when album_artist is blank.
_ARTIST_EXPR = "COALESCE(NULLIF(TRIM(album_artist), ''), artist)"

# Per-group rollup columns shared by the artist and album listings. "missing"
# is a track we have a row for but no live Library file and which isn't a
# deliberate delete — i.e. a re-download candidate.
_ROLLUP_COLS = (
    "COUNT(*) AS track_count, "
    "SUM(CASE WHEN library_path IS NOT NULL AND deleted_at IS NULL THEN 1 ELSE 0 END) AS in_library, "
    "SUM(CASE WHEN library_path IS NULL AND deleted_at IS NULL AND blocklisted = 0 THEN 1 ELSE 0 END) AS missing, "
    "SUM(CASE WHEN blocklisted = 1 THEN 1 ELSE 0 END) AS blocked, "
    "SUM(CASE WHEN deleted_at IS NOT NULL THEN 1 ELSE 0 END) AS deleted"
)


def list_artists(q: str | None = None, *, limit: int = 2000, offset: int = 0) -> list[dict]:
    """Artists grid: one row per canonical artist with album/track rollups."""
    where: list[str] = []
    params: list = []
    if q:
        where.append(f"({_ARTIST_EXPR} LIKE ? OR artist LIKE ?)")
        like = f"%{q}%"
        params += [like, like]
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    sql = (
        f"SELECT {_ARTIST_EXPR} AS artist_name, "
        "COUNT(DISTINCT am_album_id) AS album_count, "
        f"{_ROLLUP_COLS} "
        f"FROM tracks {clause} "
        f"GROUP BY {_ARTIST_EXPR} "
        f"HAVING artist_name IS NOT NULL AND artist_name != '' "
        "ORDER BY artist_name COLLATE NOCASE "
        "LIMIT ? OFFSET ?"
    )
    with connect() as conn:
        rows = conn.execute(sql, (*params, limit, offset)).fetchall()
    return [dict(r) for r in rows]


def get_artist_summary(artist_name: str) -> dict | None:
    """Single-artist rollup, for re-rendering after an artist-level action."""
    sql = (
        f"SELECT {_ARTIST_EXPR} AS artist_name, "
        "COUNT(DISTINCT am_album_id) AS album_count, "
        f"{_ROLLUP_COLS} "
        f"FROM tracks WHERE {_ARTIST_EXPR} = ? "
        f"GROUP BY {_ARTIST_EXPR}"
    )
    with connect() as conn:
        row = conn.execute(sql, (artist_name,)).fetchone()
    return dict(row) if row else None


def list_albums_for_artist(artist_name: str) -> list[dict]:
    """Albums belonging to one canonical artist, with per-album rollups."""
    sql = (
        "SELECT am_album_id, "
        "MAX(album) AS album, "
        f"MAX({_ARTIST_EXPR}) AS artist_name, "
        f"{_ROLLUP_COLS} "
        f"FROM tracks WHERE {_ARTIST_EXPR} = ? "
        "GROUP BY am_album_id "
        "ORDER BY album COLLATE NOCASE"
    )
    with connect() as conn:
        rows = conn.execute(sql, (artist_name,)).fetchall()
    return [dict(r) for r in rows]


def get_album_summary(album_id: str) -> dict | None:
    """Single-album rollup + display fields, for the album header."""
    sql = (
        "SELECT am_album_id, MAX(album) AS album, "
        f"MAX({_ARTIST_EXPR}) AS artist_name, MAX(genre) AS genre, "
        f"{_ROLLUP_COLS} "
        "FROM tracks WHERE am_album_id = ? "
        "GROUP BY am_album_id"
    )
    with connect() as conn:
        row = conn.execute(sql, (album_id,)).fetchone()
    return dict(row) if row else None


def album_track_ids(album_id: str, *, missing_only: bool = False) -> list[str]:
    """Track ids in an album — all, or just re-download candidates."""
    sql = "SELECT am_track_id FROM tracks WHERE am_album_id = ?"
    if missing_only:
        sql += " AND library_path IS NULL AND deleted_at IS NULL AND blocklisted = 0"
    with connect() as conn:
        rows = conn.execute(sql, (album_id,)).fetchall()
    return [r["am_track_id"] for r in rows]


def artist_track_ids(artist_name: str) -> list[str]:
    """All track ids for one canonical artist — for artist-level bulk actions."""
    with connect() as conn:
        rows = conn.execute(
            f"SELECT am_track_id FROM tracks WHERE {_ARTIST_EXPR} = ?",
            (artist_name,),
        ).fetchall()
    return [r["am_track_id"] for r in rows]


def mark_deleted(am_track_id: str, *, also_blocklist: bool = False) -> bool:
    """Flag a track as deleted (and optionally blocklisted). Idempotent."""
    now = time.time()
    with connect() as conn:
        if also_blocklist:
            cur = conn.execute(
                "UPDATE tracks SET deleted_at = ?, blocklisted = 1 WHERE am_track_id = ?",
                (now, am_track_id),
            )
        else:
            cur = conn.execute(
                "UPDATE tracks SET deleted_at = ? WHERE am_track_id = ?",
                (now, am_track_id),
            )
        return cur.rowcount > 0


def unmark_deleted(am_track_id: str) -> bool:
    """Undo a soft-delete. Leaves blocklisted flag alone."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE tracks SET deleted_at = NULL WHERE am_track_id = ?",
            (am_track_id,),
        )
        return cur.rowcount > 0


def clear_track_paths(am_track_id: str) -> None:
    """Null both location paths on a row. Use after a manual unlink so the UI
    reflects the state-of-disk before the next periodic reindex."""
    with connect() as conn:
        conn.execute(
            "UPDATE tracks SET incoming_path = NULL, library_path = NULL WHERE am_track_id = ?",
            (am_track_id,),
        )


def set_blocklisted(am_track_id: str, on: bool) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE tracks SET blocklisted = ? WHERE am_track_id = ?",
            (1 if on else 0, am_track_id),
        )
        return cur.rowcount > 0


def count_album_tracks(
    am_album_id: str,
    *,
    exclude_deleted: bool = True,
    require_library: bool = False,
) -> int:
    """How many tracks we hold for this album.

    - ``exclude_deleted`` drops rows with ``deleted_at`` set (the default).
    - ``require_library`` additionally drops rows whose file only exists in
      Incoming (set True when deciding whether the album is truly "done").
    """
    clauses = ["am_album_id = ?"]
    if exclude_deleted:
        clauses.append("deleted_at IS NULL")
    if require_library:
        clauses.append("library_path IS NOT NULL")
    sql = f"SELECT COUNT(*) AS n FROM tracks WHERE {' AND '.join(clauses)}"
    with connect() as conn:
        row = conn.execute(sql, (am_album_id,)).fetchone()
    return int(row["n"]) if row else 0


def album_progress_by_ids(album_ids: list[str]) -> dict[str, int]:
    """Bulk variant: map album_id → count for the watchlist reconcile view."""
    if not album_ids:
        return {}
    placeholders = ",".join("?" * len(album_ids))
    sql = (
        f"SELECT am_album_id, COUNT(*) AS n FROM tracks "
        f"WHERE am_album_id IN ({placeholders}) AND deleted_at IS NULL "
        "GROUP BY am_album_id"
    )
    with connect() as conn:
        rows = conn.execute(sql, album_ids).fetchall()
    return {r["am_album_id"]: int(r["n"]) for r in rows}


def track_counts() -> dict:
    """Headline counts for the library page.

    ``pending`` is the actionable "awaiting import" number — in Incoming,
    not yet in Library, not soft-deleted. Prefer it over a raw
    ``incoming_path IS NOT NULL`` count, which is dominated by cost-free
    hardlinks that share an inode with their Library twin.
    """
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN library_path IS NOT NULL THEN 1 ELSE 0 END) AS in_library,
                SUM(CASE WHEN incoming_path IS NOT NULL
                          AND library_path IS NULL
                          AND deleted_at IS NULL THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN deleted_at IS NOT NULL THEN 1 ELSE 0 END) AS deleted,
                SUM(CASE WHEN blocklisted = 1 THEN 1 ELSE 0 END) AS blocklisted
            FROM tracks
            """
        ).fetchone()
    return dict(row) if row else {}


def reap_orphan_runs() -> int:
    """Mark any rows still in 'running' status as interrupted.

    Called on startup — if the container was killed mid-sync the subprocess
    is gone, so there cannot be a live run. Marked as 'interrupted' to
    distinguish a container-restart kill from a genuine gamdl failure.
    """
    with connect() as conn:
        cur = conn.execute(
            "UPDATE runs SET status='interrupted', exit_code=-1, finished_at=strftime('%s','now') WHERE status='running'"
        )
        return cur.rowcount


def running_run() -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE status='running' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None
