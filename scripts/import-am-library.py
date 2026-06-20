#!/usr/bin/env python3
"""Resolve a TSV of (artist, album) pairs to Apple Music album URLs and append
them to the musicpipe watchlist.

Intended to be run inside the gamdl-ui container so it can import the
`watchlist` + `pre_filter` modules directly:

    docker exec -w /srv gamdl-ui /opt/venv/bin/python \
        /scripts/import-am-library.py \
        --tsv /scripts/am-library.tsv --storefront gb --dry-run

Input TSV format (one per line, produced by export-am-library.applescript):
    <artist>\\t<album>

Dedupe happens here; duplicates across the TSV collapse before any HTTP call.
Watchlist-level dedupe (country-code / slug agnostic) is handled by
watchlist.add() raising DuplicateEntryError, which we count as "already in
watchlist" — not an error.

Unmatched rows are written to /ui-data/import-unmatched.csv. Lookup results
(both hits and misses) are cached to /ui-data/import-cache.json so dry-run →
real-run doesn't re-query the iTunes Search API, and a 429-interrupted run
picks up from where it left off.

iTunes Search API's undocumented rate ceiling is ~20 requests/minute. This
script paces itself at ~18/min (3.3s between calls) and retries 429s with
exponential backoff.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

# Inside the container, gamdl-ui code lives at /srv/app/. When run with
# `-w /srv` the `app` package is importable.
sys.path.insert(0, "/srv")
from app import watchlist  # noqa: E402

ITUNES_SEARCH = "https://itunes.apple.com/search"
UNMATCHED_PATH = Path("/ui-data/import-unmatched.csv")
CACHE_PATH = Path("/ui-data/import-cache.json")
REQUEST_TIMEOUT = 15.0
SLEEP_BETWEEN = 5.0  # ~12 req/min. Apple's edge escalates to 403 well below
                     # the documented 20/min ceiling; 5s gives headroom.
RETRY_BACKOFFS = (30.0, 90.0, 180.0)  # on 429: sleep these in turn, then give up.


@dataclass
class Row:
    artist: str
    album: str


def normalize(s: str) -> str:
    """Casefold + collapse punctuation/whitespace for fuzzy comparison."""
    s = s.casefold()
    s = re.sub(r"[\(\[].*?[\)\]]", "", s)  # drop "(Deluxe Edition)" etc.
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def read_tsv(path: Path) -> list[Row]:
    rows: list[Row] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        parts = raw.split("\t")
        if len(parts) < 2:
            continue
        artist = parts[0].strip()
        album = parts[1].strip()
        if not album:
            continue
        rows.append(Row(artist=artist, album=album))
    return rows


def dedupe(rows: list[Row]) -> list[Row]:
    seen: set[tuple[str, str]] = set()
    out: list[Row] = []
    for r in rows:
        key = (normalize(r.artist), normalize(r.album))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def search_album(artist: str, album: str, storefront: str) -> list[dict]:
    term = f"{artist} {album}".strip()
    params = {
        "term": term,
        "entity": "album",
        "country": storefront,
        "limit": "10",
    }
    url = f"{ITUNES_SEARCH}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "musicpipe-import/1.0"})
    # Retry once per backoff step on 429; other HTTP errors bubble up.
    attempts = [0.0, *RETRY_BACKOFFS]
    last_err: Exception | None = None
    for delay in attempts:
        if delay > 0:
            print(f"  … 429 backoff: sleeping {delay:.0f}s before retry")
            time.sleep(delay)
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data.get("results", [])
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                continue
            raise
    raise last_err if last_err else RuntimeError("unreachable")


def load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CACHE_PATH)


def cache_key(row: Row) -> str:
    return f"{normalize(row.artist)}\t{normalize(row.album)}"


def score(row: Row, result: dict) -> float:
    album_sim = SequenceMatcher(
        None, normalize(row.album), normalize(result.get("collectionName", ""))
    ).ratio()
    artist_sim = SequenceMatcher(
        None, normalize(row.artist), normalize(result.get("artistName", ""))
    ).ratio()
    # Artist gate: if artist is way off it's almost certainly a bad hit
    # (e.g. a compilation with the same album name by a different artist).
    if artist_sim < 0.5:
        return 0.0
    return 0.5 * album_sim + 0.5 * artist_sim


def best_match(row: Row, results: list[dict], min_score: float) -> tuple[dict | None, float]:
    best: dict | None = None
    best_score = 0.0
    for r in results:
        s = score(row, r)
        if s > best_score:
            best = r
            best_score = s
    if best_score < min_score:
        return None, best_score
    return best, best_score


def canonical_url(result: dict) -> str:
    """iTunes Search returns `collectionViewUrl` with a ?uo=4 tracking suffix.
    Strip the query string — canonical_id in pre_filter ignores it anyway,
    but a cleaner albums.txt is nicer to read."""
    url = result.get("collectionViewUrl", "")
    if "?" in url:
        url = url.split("?", 1)[0]
    return url


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--tsv", required=True, type=Path, help="Path to TSV of (artist, album) lines")
    p.add_argument("--storefront", required=True, help="Apple Music storefront code (gb, us, in, …)")
    p.add_argument("--min-score", type=float, default=0.85, help="Fuzzy match threshold (default 0.85)")
    p.add_argument("--dry-run", action="store_true", help="Resolve + report but don't touch albums.txt")
    p.add_argument("--limit", type=int, default=0, help="Process only the first N rows (debug)")
    p.add_argument("--refresh", action="store_true", help="Ignore cache, re-query iTunes for every row")
    args = p.parse_args()

    if not args.tsv.exists():
        print(f"TSV not found: {args.tsv}", file=sys.stderr)
        return 2

    rows = read_tsv(args.tsv)
    if args.limit > 0:
        rows = rows[: args.limit]
    total_raw = len(rows)
    rows = dedupe(rows)
    total_unique = len(rows)

    cache = {} if args.refresh else load_cache()

    print(f"Read {total_raw} rows, {total_unique} unique (artist, album) pairs.")
    print(f"Storefront: {args.storefront}  min-score: {args.min_score}  dry-run: {args.dry_run}")
    print(f"Cache: {len(cache)} entries loaded from {CACHE_PATH}")
    print()

    matched_added = 0
    already_in_watchlist = 0
    unmatched: list[tuple[Row, float]] = []
    errors = 0
    cache_writes_since_flush = 0

    try:
        for i, row in enumerate(rows, 1):
            prefix = f"[{i}/{total_unique}]"
            key = cache_key(row)
            cached = cache.get(key)
            url: str | None
            s: float

            if cached is not None:
                url = cached.get("url")
                s = float(cached.get("score", 0.0))
                note = "  (cache)"
            else:
                try:
                    results = search_album(row.artist, row.album, args.storefront)
                except Exception as e:
                    print(f"{prefix} SEARCH-ERROR {row.artist!r} / {row.album!r}: {e}")
                    errors += 1
                    time.sleep(SLEEP_BETWEEN)
                    continue

                match, s = best_match(row, results, args.min_score)
                url = canonical_url(match) if match else None
                if not url:
                    url = None
                cache[key] = {
                    "url": url,
                    "score": s,
                    "artist_name": (match or {}).get("artistName", ""),
                    "album_name": (match or {}).get("collectionName", ""),
                }
                cache_writes_since_flush += 1
                if cache_writes_since_flush >= 10:
                    save_cache(cache)
                    cache_writes_since_flush = 0
                note = ""
                time.sleep(SLEEP_BETWEEN)

            if url is None:
                print(f"{prefix} UNMATCHED {row.artist!r} / {row.album!r} (best={s:.2f}){note}")
                unmatched.append((row, s))
                continue

            if args.dry_run:
                entry = cache.get(key, {})
                print(
                    f"{prefix} WOULD-ADD {url}  ({s:.2f}  ← {entry.get('artist_name', '?')}"
                    f" / {entry.get('album_name', '?')}){note}"
                )
                matched_added += 1
            else:
                try:
                    watchlist.add(url, "album")
                    print(f"{prefix} ADDED {url}  ({s:.2f}){note}")
                    matched_added += 1
                except watchlist.DuplicateEntryError as dup:
                    print(f"{prefix} ALREADY-IN-WATCHLIST {dup.existing_url}{note}")
                    already_in_watchlist += 1
                except Exception as e:
                    print(f"{prefix} ADD-ERROR {url}: {e}")
                    errors += 1
    finally:
        save_cache(cache)

    if unmatched:
        UNMATCHED_PATH.parent.mkdir(parents=True, exist_ok=True)
        with UNMATCHED_PATH.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["artist", "album", "best_score"])
            for row, s in unmatched:
                w.writerow([row.artist, row.album, f"{s:.3f}"])
        print()
        print(f"Wrote {len(unmatched)} unmatched rows to {UNMATCHED_PATH}")

    print()
    print("=== Summary ===")
    print(f"Input rows:              {total_raw}")
    print(f"Unique pairs:            {total_unique}")
    print(f"{'Would add' if args.dry_run else 'Added':<24} {matched_added}")
    print(f"Already in watchlist:    {already_in_watchlist}")
    print(f"Unmatched:               {len(unmatched)}")
    print(f"Errors:                  {errors}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
