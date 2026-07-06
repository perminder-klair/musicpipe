"""Daily maintenance: state-of-record DB backups + per-run log pruning.

Backs up the databases whose loss can't be repaired from disk:
- ``/ui-data/ui.db`` — the tracks table's deleted_at/blocklisted flags are
  the only record of "never re-download this"; losing them silently
  resurrects everything the user deleted.
- ``/beets-data/musiclibrary.blb`` — beets' import history (a SQLite file
  despite the extension).
- ``/beets-data/state.pickle`` — beets' incremental-import state.

Snapshots use the sqlite backup API, which is consistent even with
concurrent writers. Each backup is one timestamped directory under
``/ui-data/backups``; the newest ``BACKUP_KEEP`` are retained. Note the
backups share the disk with the originals — they protect against app bugs
and corruption, not drive failure.

The beets files are skipped (not failed) while an import is running, and
each item is best-effort so e.g. an unreadable beets DB can't stop the
ui.db snapshot.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import time
from pathlib import Path

from . import store

UI_DATA_DIR = Path(os.environ.get("UI_DATA_DIR", "/ui-data"))
BACKUP_DIR = UI_DATA_DIR / "backups"
RUN_LOGS_DIR = UI_DATA_DIR / "logs"

BEETS_DIR = Path(os.environ.get("BEETS_DATA_DIR", "/beets-data"))
BEETS_DB = BEETS_DIR / "musiclibrary.blb"
BEETS_STATE = BEETS_DIR / "state.pickle"
IMPORT_STATUS = BEETS_DIR / ".import-status"

BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "7"))
RUN_LOG_RETENTION_DAYS = int(os.environ.get("RUN_LOG_RETENTION_DAYS", "60"))


def _beets_import_running() -> bool:
    try:
        return IMPORT_STATUS.read_text().strip() == "running"
    except OSError:
        return False


def _snapshot_sqlite(src: Path, dest: Path) -> None:
    """Consistent point-in-time copy via the sqlite backup API. Read-only
    source handle so we can never hurt the original."""
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        d = sqlite3.connect(dest)
        try:
            s.backup(d)
        finally:
            d.close()
    finally:
        s.close()


def _prune_backups() -> int:
    """Drop all but the newest BACKUP_KEEP backup directories."""
    if not BACKUP_DIR.is_dir():
        return 0
    dirs = sorted(p for p in BACKUP_DIR.iterdir() if p.is_dir())
    pruned = 0
    for old in dirs[: max(0, len(dirs) - BACKUP_KEEP)]:
        shutil.rmtree(old, ignore_errors=True)
        pruned += 1
    return pruned


def seconds_since_last_backup() -> float | None:
    """Age of the newest backup dir, or None if there's never been one.
    Lets the scheduler survive container restarts without re-backing-up."""
    if not BACKUP_DIR.is_dir():
        return None
    newest: float | None = None
    for p in BACKUP_DIR.iterdir():
        if p.is_dir():
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if newest is None or mtime > newest:
                newest = mtime
    return None if newest is None else time.time() - newest


def run_backup() -> dict:
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / ts
    dest.mkdir(parents=True, exist_ok=True)
    result: dict = {
        "dir": str(dest),
        "ui_db": False,
        "beets_db": False,
        "beets_state": False,
        "beets_skipped_running": False,
        "errors": [],
        "backups_pruned": 0,
    }

    try:
        _snapshot_sqlite(store.DB_PATH, dest / "ui.db")
        result["ui_db"] = True
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"ui.db: {exc!r}")

    if _beets_import_running():
        # An import rewrites both files; snapshot them next cycle instead.
        result["beets_skipped_running"] = True
    else:
        if BEETS_DB.exists():
            try:
                _snapshot_sqlite(BEETS_DB, dest / "musiclibrary.blb")
                result["beets_db"] = True
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"musiclibrary.blb: {exc!r}")
        if BEETS_STATE.exists():
            try:
                tmp = dest / ".state.pickle.tmp"
                shutil.copy2(BEETS_STATE, tmp)
                tmp.rename(dest / "state.pickle")
                result["beets_state"] = True
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"state.pickle: {exc!r}")

    result["backups_pruned"] = _prune_backups()
    return result


def prune_run_logs() -> int:
    """Delete per-run subprocess logs older than the retention window.

    One file accumulates per sync forever otherwise. The runs table keeps
    its rows; viewing a pruned run's log returns the existing 410
    "log file missing on disk" path in main.py.
    """
    if not RUN_LOGS_DIR.is_dir():
        return 0
    cutoff = time.time() - RUN_LOG_RETENTION_DAYS * 86400
    pruned = 0
    for p in RUN_LOGS_DIR.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                pruned += 1
        except OSError:
            continue
    return pruned


def run_maintenance() -> dict:
    """One daily pass: backup + run-log pruning."""
    result = run_backup()
    result["run_logs_pruned"] = prune_run_logs()
    return result
