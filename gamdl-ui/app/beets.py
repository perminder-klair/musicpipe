"""Read-only visibility into the beets auto-import sidecar + trigger file.

The auto-import container (see navidrome-slskd/auto-import.sh) writes:
- /config/auto-import.log   — one line per `Found N` / `Import completed …`
- /config/.import-status    — "running" or "idle" (atomic, overwritten each time)
- /config/.trigger-import   — absent normally; touching it wakes the sleep loop

gamdl-ui sees that same directory mounted at /beets-data. We don't talk to the
beets process directly; we just parse the log tail + count the Incoming queue
(already accessible via /downloads).
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from . import store

BEETS_DIR = Path(os.environ.get("BEETS_DATA_DIR", "/beets-data"))
# auto-import.log receives both wrapper "Found/completed" lines and beets'
# per-directory stdout (redirected), so its mtime doubles as a proof-of-life.
LOG_PATH = BEETS_DIR / "auto-import.log"
STATUS_PATH = BEETS_DIR / ".import-status"
TRIGGER_PATH = BEETS_DIR / ".trigger-import"
INCOMING_DIR = Path(os.environ.get("GAMDL_DOWNLOADS_DIR", "/downloads"))

AUDIO_EXTS = {".m4a", ".mp3", ".flac", ".ogg", ".opus", ".wav", ".aac", ".wma"}

# Lines look like:  "2026-04-17 10:10:32 Found 1609 music files, starting import..."
_LOG_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(?P<msg>.+)$"
)
_FOUND_RE = re.compile(r"Found (\d+) music files")


def _read_status() -> str:
    try:
        return STATUS_PATH.read_text().strip() or "unknown"
    except FileNotFoundError:
        return "unknown"


def _tail_log(max_bytes: int = 32_768) -> list[tuple[float, str]]:
    """Return recent (epoch, msg) pairs, newest last. Cheap O(tail)."""
    if not LOG_PATH.exists():
        return []
    size = LOG_PATH.stat().st_size
    with LOG_PATH.open("rb") as f:
        if size > max_bytes:
            f.seek(-max_bytes, os.SEEK_END)
            f.readline()  # discard partial line
        chunk = f.read().decode("utf-8", errors="replace")
    out: list[tuple[float, str]] = []
    for line in chunk.splitlines():
        m = _LOG_RE.match(line)
        if not m:
            continue
        try:
            epoch = time.mktime(time.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
        out.append((epoch, m.group("msg")))
    return out


def _count_queue() -> int:
    """Count audio files currently sitting in Incoming (beets' input)."""
    if not INCOMING_DIR.exists():
        return 0
    n = 0
    # rglob across the whole tree — 1–2 k files is fine; beats shelling out.
    for p in INCOMING_DIR.rglob("*"):
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
            n += 1
    return n


def status() -> dict:
    """Snapshot of beets state for the UI."""
    tail = _tail_log()
    status_file = _read_status()
    queue = _count_queue()

    # Derive last-cycle info from the log tail:
    # - last_started = most recent "Found N" line
    # - last_finished = most recent "Import completed …" or "Import finished …"
    last_started: tuple[float, int] | None = None  # (ts, file_count)
    last_finished: tuple[float, str] | None = None  # (ts, outcome)
    for ts, msg in tail:
        m = _FOUND_RE.search(msg)
        if m:
            last_started = (ts, int(m.group(1)))
        elif "Import completed successfully" in msg:
            last_finished = (ts, "ok")
        elif msg.startswith("Import finished with exit code"):
            code = msg.split()[-1]
            last_finished = (ts, f"fail({code})")
        elif msg.startswith("Trigger received"):
            # Informational; not a "finish" event.
            pass

    running = status_file == "running"
    # Fallback heuristic: if a "Found N" appears in the tail without a matching
    # completion after it, treat as running (in case the status file wasn't
    # written for some reason).
    if not running and last_started and (
        not last_finished or last_finished[0] < last_started[0]
    ):
        running = True

    try:
        last_activity = LOG_PATH.stat().st_mtime
    except FileNotFoundError:
        last_activity = None

    # queue_count = total audio files on disk in Incoming (legacy hardlinks
    # post-Phase-D stay there as gamdl's skip cache — most aren't real work).
    # pending_count = tracks the indexer sees with no library_path yet — the
    # honest "import backlog" number.
    pending = store.pending_count()
    return {
        "running": running,
        "queue_count": queue,
        "pending_count": pending,
        "last_started_at": last_started[0] if last_started else None,
        "last_started_count": last_started[1] if last_started else None,
        "last_finished_at": last_finished[0] if last_finished else None,
        "last_outcome": last_finished[1] if last_finished else None,
        "last_activity_at": last_activity,
        "trigger_pending": TRIGGER_PATH.exists(),
    }


def trigger() -> bool:
    """Ask the auto-import sidecar to run on its next 5-second poll tick."""
    try:
        TRIGGER_PATH.touch(exist_ok=True)
        return True
    except (FileNotFoundError, PermissionError):
        return False
