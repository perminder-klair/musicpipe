#!/usr/bin/env python3
"""Normalize an Apple Music "Music Library.xml" export into am-library.tsv.

Apple's File → Library → Export Library… writes a plist-XML with every track,
which is richer and usually larger than the AppleScript export: it has
`Album Artist` separate from `Artist`, a `Release Date`, and an
`Apple Music: True` flag that marks catalog items (vs. local uploads that
iTunes Search can never resolve).

This script parses the XML, filters to Apple Music catalog tracks by default,
deduplicates to unique (album_artist, album) pairs, and writes the TSV that
import-am-library.py consumes.

Host-side; pure stdlib (no container, no deps).

Usage:
    python3 scripts/normalize-am-library.py \\
        --xml scripts/Music-Library.xml \\
        --out scripts/am-library.tsv
"""
from __future__ import annotations

import argparse
import plistlib
import sys
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--xml", required=True, type=Path, help="Path to Music-Library.xml")
    p.add_argument("--out", required=True, type=Path, help="TSV output path")
    p.add_argument(
        "--include-non-apple-music",
        action="store_true",
        help="Don't filter by `Apple Music: True` (default: skip local/uploaded tracks)",
    )
    args = p.parse_args()

    if not args.xml.exists():
        print(f"XML not found: {args.xml}", file=sys.stderr)
        return 2

    with args.xml.open("rb") as f:
        lib = plistlib.load(f)

    tracks = lib.get("Tracks", {})
    total = len(tracks)

    non_am = 0
    no_album = 0
    seen: set[tuple[str, str]] = set()
    pairs: list[tuple[str, str]] = []

    for t in tracks.values():
        if not args.include_non_apple_music and not t.get("Apple Music", False):
            non_am += 1
            continue
        album = (t.get("Album") or "").strip()
        if not album:
            no_album += 1
            continue
        artist = (t.get("Album Artist") or t.get("Artist") or "").strip()
        key = (artist.casefold(), album.casefold())
        if key in seen:
            continue
        seen.add(key)
        pairs.append((artist, album))

    pairs.sort(key=lambda p: (p[0].casefold(), p[1].casefold()))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="") as f:
        for artist, album in pairs:
            f.write(f"{artist}\t{album}\n")

    print(f"Total tracks in XML:         {total}")
    print(f"Non-Apple-Music (skipped):   {non_am}")
    print(f"Missing album (skipped):     {no_album}")
    print(f"Unique (album_artist, album): {len(pairs)}")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
