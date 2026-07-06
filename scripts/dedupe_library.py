#!/usr/bin/env python3
"""One-off Library de-duplicator — reverses the hardlink/[NNNN] duplicate bug.

Background (see issue #1): before the filter_incoming fix, gamdl re-downloads
of already-held tracks were hardlinked into Incoming and then re-imported by
beets into new ``%aunique{}`` album folders (``Album [NNNN]``). Same inode, so
zero extra disk, but Navidrome indexes each as a distinct media_file with a
distinct Subsonic id — the duplicate-song bug.

This script groups every Library .m4a by Apple Music track id (``cnID`` atom)
and removes the accidental duplicates. It is intentionally conservative:

  * A ``[NNNN]``-suffixed file is deleted ONLY when a non-suffixed twin with
    the same cnID exists (the keep target is guaranteed present).
  * When every file in a cnID group is suffixed (no clean twin), one is kept
    (the lowest [NNNN]) and the rest deleted — reported distinctly.
  * cnID groups where NO file is suffixed (e.g. a track shared by a "Deluxe"
    and a "Complete Edition" album) are treated as legitimate editions:
    reported, never deleted. Pass --edition-dupes to also collapse these.

Deleting a hardlink only drops that directory entry; the audio survives via
the kept twin's entry, so same-inode deletions free no disk and are safe.

Runs DRY by default: prints the full plan and a summary, deletes nothing.
Pass --apply to actually unlink. After --apply, reindex + Navidrome rescan:

    docker exec gamdl-ui /opt/venv/bin/python /app/scripts/dedupe_library.py --apply
    curl -X POST http://localhost:4150/maintenance/reindex

Run inside the gamdl-ui container (it has mutagen + the /library mount):

    docker cp scripts/dedupe_library.py gamdl-ui:/tmp/dedupe_library.py
    docker exec gamdl-ui /opt/venv/bin/python /tmp/dedupe_library.py            # dry run
    docker exec gamdl-ui /opt/venv/bin/python /tmp/dedupe_library.py --apply     # delete
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict

LIBRARY = os.environ.get("LIBRARY_DIR", "/library")

# A beets %aunique{} disambiguator: a folder name ending in " [NNNN]".
SUFFIX_RE = re.compile(r"\s\[\d+\]$")


def _cn_id(path: str) -> int | None:
    from mutagen.mp4 import MP4
    try:
        tags = MP4(path).tags or {}
    except Exception:
        return None
    cn = tags.get("cnID")
    return int(cn[0]) if cn else None


def _is_suffixed(path: str) -> bool:
    # Any path component (album dir, and defensively parents) carrying [NNNN].
    return any(SUFFIX_RE.search(part) for part in path.split("/"))


def _suffix_num(path: str) -> int:
    m = None
    for part in path.split("/"):
        mm = re.search(r"\[(\d+)\]$", part)
        if mm:
            m = mm
    return int(m.group(1)) if m else 0


def scan() -> dict[int, list[str]]:
    groups: dict[int, list[str]] = defaultdict(list)
    for dp, _, fns in os.walk(LIBRARY):
        for fn in fns:
            if not fn.lower().endswith(".m4a"):
                continue
            p = os.path.join(dp, fn)
            cn = _cn_id(p)
            if cn is not None:
                groups[cn].append(p)
    return groups


def plan(groups: dict[int, list[str]], edition_dupes: bool):
    """Return (deletions, edition_reports). deletions is a list of dicts."""
    deletions: list[dict] = []
    editions: list[tuple[int, list[str]]] = []
    for cn, files in groups.items():
        if len(files) < 2:
            continue
        suffixed = [f for f in files if _is_suffixed(f)]
        clean = [f for f in files if not _is_suffixed(f)]

        if clean and suffixed:
            keep = min(clean, key=len)  # shortest clean path = base album
            for f in suffixed:
                deletions.append({"cn": cn, "path": f, "keep": keep, "why": "suffixed-twin"})
        elif suffixed and not clean:
            # No clean twin: keep the lowest-numbered suffix, drop the rest.
            keep = min(suffixed, key=_suffix_num)
            for f in suffixed:
                if f != keep:
                    deletions.append({"cn": cn, "path": f, "keep": keep, "why": "no-clean-twin"})
        else:
            # All clean, >1 file: distinct edition folders sharing this cnID.
            editions.append((cn, files))
            if edition_dupes:
                keep = min(files, key=len)
                for f in files:
                    if f != keep:
                        deletions.append({"cn": cn, "path": f, "keep": keep, "why": "edition"})
    return deletions, editions


def _ino_size(path: str):
    try:
        st = os.stat(path)
        return st.st_ino, st.st_size
    except OSError:
        return None, 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually unlink (default: dry run)")
    ap.add_argument("--edition-dupes", action="store_true",
                    help="also collapse same-cnID tracks across distinct edition folders")
    ap.add_argument("--limit", type=int, default=40, help="how many deletions to print")
    args = ap.parse_args()

    print(f"Scanning {LIBRARY} ...", file=sys.stderr)
    groups = scan()
    total_files = sum(len(v) for v in groups.values())
    deletions, editions = plan(groups, args.edition_dupes)

    # Compute disk freed: a deletion frees bytes only if its inode is unique
    # among the files that survive (kept target + non-deleted).
    del_paths = {d["path"] for d in deletions}
    surviving_inodes: set[int] = set()
    for files in groups.values():
        for f in files:
            if f in del_paths:
                continue
            ino, _ = _ino_size(f)
            if ino is not None:
                surviving_inodes.add(ino)

    freed = 0
    same_inode = 0
    diff_inode = 0
    for d in deletions:
        ino, sz = _ino_size(d["path"])
        if ino in surviving_inodes:
            same_inode += 1
        else:
            diff_inode += 1
            freed += sz

    print(f"\n=== dedupe plan ({'APPLY' if args.apply else 'DRY RUN'}) ===")
    print(f"library m4a with cnID:   {total_files}")
    print(f"distinct cnIDs:          {len(groups)}")
    print(f"deletion candidates:     {len(deletions)}")
    print(f"  hardlink twins (0 B):  {same_inode}")
    print(f"  real byte copies:      {diff_inode}  (~{freed/1024/1024:.1f} MiB freed)")
    print(f"edition-overlap groups:  {len(editions)}"
          f"{' (INCLUDED via --edition-dupes)' if args.edition_dupes else ' (reported only, not deleted)'}")

    print(f"\n--- first {min(args.limit, len(deletions))} deletions ---")
    for d in deletions[:args.limit]:
        print(f"  [{d['why']}] rm {d['path'].replace(LIBRARY + '/', '')}")
        print(f"            keep {d['keep'].replace(LIBRARY + '/', '')}")

    if editions and not args.edition_dupes:
        print(f"\n--- edition-overlap groups (NOT deleted; --edition-dupes to include) ---")
        for cn, files in editions[:15]:
            print(f"  cnID {cn}:")
            for f in files:
                print(f"      {f.replace(LIBRARY + '/', '')}")

    if not args.apply:
        print(f"\nDRY RUN — nothing deleted. Re-run with --apply to unlink the "
              f"{len(deletions)} candidates above.")
        return 0

    # Apply.
    removed = 0
    errors = 0
    touched: set[str] = set()
    for d in deletions:
        try:
            os.unlink(d["path"])
            removed += 1
            touched.add(os.path.dirname(d["path"]))
        except OSError as e:
            errors += 1
            print(f"  ERROR unlinking {d['path']}: {e}", file=sys.stderr)

    # Prune now-empty directories upward, stopping at LIBRARY.
    pruned = 0
    for start in touched:
        cur = start
        while cur.startswith(LIBRARY) and cur != LIBRARY:
            try:
                if os.path.isdir(cur) and not os.listdir(cur):
                    os.rmdir(cur)
                    pruned += 1
                    cur = os.path.dirname(cur)
                else:
                    break
            except OSError:
                break

    print(f"\nAPPLIED: removed {removed}, errors {errors}, pruned {pruned} empty dirs.")
    print("Next: prune beets DB + reindex + Navidrome rescan:")
    print("  docker exec beets beet update       # drop rows for vanished files")
    print("  curl -X POST http://localhost:4150/maintenance/reindex")
    print("  # then trigger a Navidrome full rescan from the UI (Library page)")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
