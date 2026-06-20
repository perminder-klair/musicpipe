"""URL-level skip decisions for the runner.

Phase C: before handing a watchlist URL to gamdl, check whether we've already
got everything it would produce. If so, short-circuit the subprocess and
record a synthetic "skipped" run.

Current scope is album URLs only:
- Apple Music album URLs embed the album ID (same value that ends up in the
  ``plID`` atom beets reads), so we can map URL → ``am_album_id`` directly.
- We know the expected track count from ``meta.total_tracks``, populated by
  the runner the first time it observes a ``[Track N/TOTAL]`` log line.
- If the count of non-deleted rows for this album reaches the total, we skip.

Artist URLs expand to many albums inside gamdl and would need a replica of
that expansion to pre-filter; playlists are dynamic. Both bypass the filter.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field

from . import store

_ALBUM_ID_RE = re.compile(r"/album/[^/]+/(?P<id>\d+)")
_ARTIST_ID_RE = re.compile(r"/artist/[^/]+/(?P<id>\d+)")
# Apple Music playlist URLs use an alphanumeric token like "pl.XXXX" or "p.XXXX".
_PLAYLIST_ID_RE = re.compile(r"/playlist/(?:[^/]+/)?(?P<id>p[l]?\.[A-Za-z0-9]+)")

# Clean-run skip window: if a URL was last synced this recently with zero
# new tracks, the cron-side pre-check declines to re-run it. Chosen so the
# nightly 24 h cron always re-enters, but a manual sync within the same day
# doesn't trigger a redundant second pass.
FRESH_RUN_WINDOW_SEC = int(20 * 3600)


@dataclass
class SkipDecision:
    skip: bool
    reason: str | None = None


def album_id_from_url(url: str) -> str | None:
    m = _ALBUM_ID_RE.search(url)
    return m.group("id") if m else None


def artist_id_from_url(url: str) -> str | None:
    m = _ARTIST_ID_RE.search(url)
    return m.group("id") if m else None


def playlist_id_from_url(url: str) -> str | None:
    m = _PLAYLIST_ID_RE.search(url)
    return m.group("id") if m else None


def canonical_id(url: str, kind: str) -> str | None:
    """Kind-qualified Apple Music identifier, country-code agnostic.

    Lets the watchlist dedupe `/in/artist/foo/123` and `/us/artist/foo/123`
    as the same entry. Returns None for URLs we can't parse — callers treat
    that as "allow" so malformed URLs still hit gamdl's own validator.
    """
    if kind == "album":
        aid = album_id_from_url(url)
    elif kind == "artist":
        aid = artist_id_from_url(url)
    elif kind == "playlist":
        aid = playlist_id_from_url(url)
    else:
        return None
    if aid is None:
        return None
    return f"{kind}:{aid}"


def should_skip(url: str, kind: str) -> SkipDecision:
    """UI-path pre-filter: currently only the Phase C album-completeness rule."""
    if kind != "album":
        return SkipDecision(False)
    album_id = album_id_from_url(url)
    if album_id is None:
        return SkipDecision(False)
    meta = store.get_meta(url) or {}
    total = meta.get("total_tracks")
    if not total:
        return SkipDecision(False)
    have = store.count_album_tracks(album_id, exclude_deleted=True)
    if have >= int(total):
        return SkipDecision(True, f"album {album_id}: {have}/{total} present")
    return SkipDecision(False)


@dataclass
class ExpandFilterResult:
    """Outcome of ``expand_and_filter`` — what the runner needs to decide
    whether to invoke gamdl and, if so, with which URLs.

    ``missing`` is the list the caller hands gamdl one-by-one (each entry's
    ``track_url`` is the per-track ``.../album/<slug>/<id>?i=<track_id>``
    form Apple emits). If ``missing`` is empty, the run is a no-op and
    ``reason`` carries a human string for the skip-run log entry.
    """

    url: str
    kind: str
    total: int
    present: int
    blocked: int
    matched_by_id: int
    matched_by_isrc: int
    matched_by_signature: int
    stale_matches: int
    missing: list = field(default_factory=list)
    reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "kind": self.kind,
            "total": self.total,
            "present": self.present,
            "blocked": self.blocked,
            "matched_by_id": self.matched_by_id,
            "matched_by_isrc": self.matched_by_isrc,
            "matched_by_signature": self.matched_by_signature,
            "stale_matches": self.stale_matches,
            "missing_count": len(self.missing),
            "missing": [t.to_dict() for t in self.missing],
            "reason": self.reason,
        }


async def expand_and_filter(url: str, kind: str, *, verify_exists: bool = True) -> ExpandFilterResult:
    """Expand a watchlist URL and diff against the tracks DB.

    For each expanded track we consult the shared ``LibraryIndex``:
      1. blocked (deleted_at or blocklisted) → never download
      2. same am_track_id in Library → already have
      3. same ISRC in Library → cross-region variant already held
      4. same normalized (album, title) → re-master/compilation variant held
      5. nothing matches → add to ``missing``

    With ``verify_exists=True`` (the default) a match in categories 2–4
    is downgraded to "missing" when the ``library_path`` no longer points
    at an actual file — the DB is an index of filesystem state and can
    briefly lag a manual delete. ``filter_incoming`` keeps the old
    DB-only behavior because it independently stats each file via the
    inode check.
    """
    # Local imports avoid a circular import: expander → pre_filter (for
    # URL parsing) and pre_filter → expander here.
    from . import expander, indexer

    refs = await expander.fetch_tracks(url, kind)
    index = indexer.LibraryIndex()
    present = 0
    blocked = 0
    by_id = 0
    by_isrc = 0
    by_sig = 0
    stale = 0
    missing: list[expander.TrackRef] = []
    for tr in refs:
        m = index.match(tr.am_track_id, tr.isrc, tr.album, tr.title)
        if m.kind == "blocked":
            blocked += 1
            continue
        if m.kind == "none":
            missing.append(tr)
            continue
        # Any by_* match. Optionally verify the file is still on disk —
        # belt-and-suspenders against a stale tracks DB.
        if verify_exists and (not m.library_path or not os.path.exists(m.library_path)):
            stale += 1
            missing.append(tr)
            continue
        present += 1
        if m.kind == "by_id":
            by_id += 1
        elif m.kind == "by_isrc":
            by_isrc += 1
        elif m.kind == "by_signature":
            by_sig += 1

    total = len(refs)
    reason = None
    if not missing:
        if total == 0:
            # Empty expansion — either the URL is valid but has no tracks
            # yet (new artist, empty playlist) or the API hid them from us.
            # Either way, nothing to download this pass.
            reason = "expansion returned 0 tracks"
        elif blocked == total:
            reason = f"all {total} tracks are blocked/deleted"
        else:
            reason = f"all {total} tracks already in Library"
    return ExpandFilterResult(
        url=url,
        kind=kind,
        total=total,
        present=present,
        blocked=blocked,
        matched_by_id=by_id,
        matched_by_isrc=by_isrc,
        matched_by_signature=by_sig,
        stale_matches=stale,
        missing=missing,
        reason=reason,
    )


def should_skip_for_cron(url: str, kind: str) -> SkipDecision:
    """Cron-path pre-filter: UI rule + fresh-clean-run rule.

    The fresh-clean-run rule skips an URL that was last synced within
    ``FRESH_RUN_WINDOW_SEC`` with exit_code=0 and tracks_new=0 — i.e. the
    previous run was quiet and recent. Runs within the window that added
    tracks still allow re-entry, in case Apple published something since.
    """
    # 1. Completeness rule inherits the UI path.
    base = should_skip(url, kind)
    if base.skip:
        return base

    # 2. Fresh-clean-run rule.
    last = store.last_run_for(url)
    if not last:
        return SkipDecision(False)
    if last.get("status") != "ok":
        return SkipDecision(False)
    if (last.get("tracks_new") or 0) > 0:
        return SkipDecision(False)
    started = last.get("started_at")
    if not started:
        return SkipDecision(False)
    age = time.time() - float(started)
    if age < FRESH_RUN_WINDOW_SEC:
        hours = int(age // 3600)
        return SkipDecision(
            True,
            f"last clean run was {hours}h ago with 0 new tracks",
        )
    return SkipDecision(False)
