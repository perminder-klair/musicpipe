"""Run gamdl as a subprocess, stream stdout, record the run in the DB.

Streaming design:
- Every decoded stdout line is assigned a monotonically-increasing ``seq`` and
  appended (seq, line) to ``_buffer``. ``_new_line`` is an asyncio.Event used
  as a fanout signal so the SSE endpoint can push immediately instead of
  polling.
- ``_buffer`` is a bounded deque (last 2000 lines). SSE consumers pass the
  last seq they saw via the standard ``Last-Event-ID`` header; the server
  replays from there. If the gap is larger than the buffer, we emit a single
  "[... N lines dropped ...]" marker so the UI is honest about the skip.
- ``_progress`` carries structured run state (current track index / total /
  name + per-track download %) that the UI renders instead of tailing logs.
"""
from __future__ import annotations

import asyncio
import os
import re
import signal
import sys
import time
import traceback
from collections import deque
from pathlib import Path

from . import indexer, pre_filter, store

CONFIG_DIR = Path(os.environ.get("GAMDL_CONFIG_DIR", "/config"))
LOGS_DIR = Path(os.environ.get("UI_DATA_DIR", "/ui-data")) / "logs"
DOWNLOADS = os.environ.get("GAMDL_DOWNLOADS_DIR", "/downloads")
TEMPDIR = os.environ.get("GAMDL_TEMP_DIR", "/tmp/gamdl")

TRACK_START_RE = re.compile(r"\[Track\s+(\d+)/(\d+)\]\s+Downloading\s+\"([^\"]+)\"")
TRACK_SKIP_RE = re.compile(r"Media file already exists at path", re.IGNORECASE)
TRACK_FAIL_RE = re.compile(r"Failed to download track", re.IGNORECASE)
FINISHED_RE = re.compile(r"Finished with (\d+) error\(s\)")

# yt-dlp progress line — parsed for the structured progress widget, then dropped
# from the UI stream (it would emit 10+ lines/sec per track otherwise).
DOWNLOAD_LINE_RE = re.compile(
    r"\[download\]\s+(?P<pct>\d+\.\d+)%\s+of\s+~?\s*(?P<size>\S+)"
    r"(?:\s+at\s+(?P<speed>\S+))?"
    r"(?:.*?\(frag\s+(?P<fi>\d+)/(?P<ft>\d+)\))?"
)
UI_NOISE_RE = re.compile(r"^\s*\[download\]\s+\d")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

BUFFER_MAX = 2000

# Watchdog: kill gamdl if it produces NO stdout for this long. The reader is
# chunk-based (not line-based), so yt-dlp's newline-less \r progress frames
# count as output — a healthy download, however slow, never trips this; only
# a genuinely wedged subprocess does. Without it a hung gamdl held the runner
# reservation forever and every future sync 409'd until a container restart.
INACTIVITY_TIMEOUT = int(os.environ.get("RUNNER_INACTIVITY_TIMEOUT", "900"))


_seq: int = 0
_buffer: "deque[tuple[int, str]]" = deque(maxlen=BUFFER_MAX)
_new_line: asyncio.Event = asyncio.Event()

_running: dict = {"run_id": None, "url": None, "kind": None}

# The live run task. Module-held for three reasons: (1) ``start``/``start_all``
# do a synchronous check-and-set on it, so two requests in the same event-loop
# tick can't both launch (the old is_running() flag was only set once the task
# body ran — a race window); (2) a bare create_task result is eligible for GC
# mid-run; (3) the done-callback surfaces exceptions that fire-and-forget
# tasks would swallow silently.
_active_task: "asyncio.Task | None" = None

# Cancellation: ``request_cancel()`` sets the flag and signals the live
# gamdl process group. ``start_all``'s sweep checks the flag between entries
# so a cancel stops the whole sweep, not just the current URL.
# ``_current_proc`` holds the active subprocess so the cancel path can signal
# it directly.
_cancel_requested: bool = False
_current_proc: "asyncio.subprocess.Process | None" = None

_progress: dict = {
    "current_track_idx": None,
    "current_track_total": None,
    "current_track_name": None,
    "current_pct": None,
    "current_frag": None,
    "current_track_retries": 0,
    "tracks_new": 0,
    "tracks_skipped": 0,
    "tracks_failed": 0,
}
# Highest fragment index seen for the current track — used to detect yt-dlp
# retries (fragment counter snapping back to 0).
_current_frag_high: int = -1


def is_running() -> bool:
    return _active_task is not None and not _active_task.done()


def cancel_requested() -> bool:
    return _cancel_requested


def _signal_group(proc: "asyncio.subprocess.Process", sig: int) -> None:
    """Signal gamdl's whole process group — yt-dlp/ffmpeg children included.

    gamdl is started with ``start_new_session=True`` so it leads its own
    group; signalling only the leader could leave a child ffmpeg downloading
    on. Falls back to signalling the leader alone if the group is gone."""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            pass


async def _kill_proc_group(proc: "asyncio.subprocess.Process") -> None:
    """TERM the group, escalate to KILL if it hasn't exited in 10s."""
    _signal_group(proc, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=10.0)
    except asyncio.TimeoutError:
        _signal_group(proc, signal.SIGKILL)
        await proc.wait()


def request_cancel() -> bool:
    """Stop the active run and (if a sweep) prevent further entries.

    Sets ``_cancel_requested`` so the sweep breaks after the current entry,
    and TERMs the live gamdl process group so the current download stops
    promptly (the read loop sees EOF and its kill-escalation path finishes
    the job if TERM is ignored). Returns True if a run was active to cancel.
    """
    global _cancel_requested
    if not is_running():
        return False
    _cancel_requested = True
    proc = _current_proc
    if proc is not None and proc.returncode is None:
        _signal_group(proc, signal.SIGTERM)
    _append_line("[runner] cancellation requested — stopping")
    return True


def current_seq() -> int:
    return _seq


def _reset_buffer() -> None:
    """Clear buffer + seq at the start of a new run.

    Seq resets to 0 per run because the client only holds a Last-Event-ID for
    the currently-open stream; when a new sync starts, the old SSE has closed.
    """
    global _seq
    _seq = 0
    _buffer.clear()


def _append_line(line: str) -> None:
    global _seq, _new_line
    _seq += 1
    _buffer.append((_seq, line))
    # Swap the event so previously-awaiting tasks get woken exactly once.
    # (asyncio.Event has no "set and clear atomically" primitive.)
    old = _new_line
    _new_line = asyncio.Event()
    old.set()


def snapshot_since(resume_from: int) -> list[tuple[int, str]]:
    """Return buffered (seq, line) entries strictly after ``resume_from``.

    If resume_from points before the oldest buffered line, prepend a synthetic
    marker line (seq = oldest-1) so the client knows lines were dropped.
    """
    if not _buffer:
        return []
    oldest_seq = _buffer[0][0]
    if resume_from and resume_from < oldest_seq - 1:
        gap = oldest_seq - 1 - resume_from
        marker = (oldest_seq - 1, f"[… {gap} lines dropped — buffer overrun …]")
        return [marker] + [(s, l) for (s, l) in _buffer if s > resume_from]
    return [(s, l) for (s, l) in _buffer if s > resume_from]


async def wait_for_line() -> None:
    """Await until the next line is appended. Event is swapped on each append."""
    await _new_line.wait()


def current_state() -> dict:
    return {
        "run_id": _running["run_id"],
        "url": _running["url"],
        "kind": _running["kind"],
        "seq": _seq,
        "progress": dict(_progress),
    }


def _reset_progress() -> None:
    global _current_frag_high
    _progress.update(
        current_track_idx=None,
        current_track_total=None,
        current_track_name=None,
        current_pct=None,
        current_frag=None,
        current_track_retries=0,
        tracks_new=0,
        tracks_skipped=0,
        tracks_failed=0,
    )
    _current_frag_high = -1


async def _run_one(url: str, kind: str, trigger: str = "manual") -> int:
    global _current_frag_high, _current_proc
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    Path(TEMPDIR).mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H-%M-%S")
    log_name = f"{ts}-{kind}-{url.rstrip('/').rsplit('/', 1)[-1]}.log"
    log_path = LOGS_DIR / log_name
    run_id = store.start_run(url=url, kind=kind, log_path=str(log_path), trigger=trigger)

    _running["run_id"] = run_id
    _running["url"] = url
    _running["kind"] = kind
    _reset_buffer()
    _reset_progress()
    _total_recorded = False

    # Phase 3 (flag-gated): expand the URL first, then either synthetic-skip
    # (nothing missing) or hand gamdl the per-track URL list instead of the
    # original. When the flag is off, we fall through to the legacy Phase C
    # path below, which skips only when the album is provably complete.
    use_track_expansion = os.environ.get("USE_TRACK_EXPANSION") == "1"
    per_track_urls: list[str] | None = None
    if use_track_expansion:
        try:
            ef = await pre_filter.expand_and_filter(url, kind)
        except Exception as exc:  # noqa: BLE001
            _append_line(f"[pre-filter] expand_and_filter failed: {exc!r} — falling back to legacy path")
            ef = None
        if ef is not None:
            _append_line(
                f"[pre-filter] expansion: total={ef.total} present={ef.present} "
                f"blocked={ef.blocked} stale={ef.stale_matches} missing={len(ef.missing)}"
            )
            if not ef.missing:
                reason = ef.reason or "nothing to download"
                _append_line(f"[pre-filter] skipping: {reason}")
                try:
                    log_path.write_text(
                        f"[pre-filter] skipped gamdl invocation (track-expansion)\n"
                        f"reason: {reason}\n"
                        f"url: {url}\nkind: {kind}\n"
                        f"total={ef.total} present={ef.present} blocked={ef.blocked}\n"
                    )
                except OSError:
                    pass
                store.finish_run(run_id=run_id, exit_code=0)
                _running["run_id"] = None
                _running["url"] = None
                _running["kind"] = None
                _append_line("[runner] done: exit=0 (track-expansion skip)")
                return 0
            per_track_urls = [t.track_url for t in ef.missing]

    if per_track_urls is None:
        # Phase C legacy path: skip the subprocess entirely if the album is
        # provably complete. Records a zero-length "ok" run with the reason
        # in place of a real log so the UI still shows what happened.
        decision = pre_filter.should_skip(url, kind)
        if decision.skip:
            _append_line(f"[pre-filter] skipping: {decision.reason}")
            try:
                log_path.write_text(
                    f"[pre-filter] skipped gamdl invocation\n"
                    f"reason: {decision.reason}\n"
                    f"url: {url}\nkind: {kind}\n"
                )
            except OSError:
                pass
            store.finish_run(run_id=run_id, exit_code=0)
            _running["run_id"] = None
            _running["url"] = None
            _running["kind"] = None
            _append_line("[runner] done: exit=0 (pre-filter skip)")
            return 0

    args = [
        "gamdl",
        "--cookies-path", str(CONFIG_DIR / "cookies.txt"),
        "--output-path", DOWNLOADS,
        "--temp-path", TEMPDIR,
        "--log-level", "INFO",
    ]
    if kind == "artist" and per_track_urls is None:
        # --artist-auto-select is a no-op for song URLs, but gamdl warns on
        # it; only pass when we're still handing over an artist URL.
        args += ["--artist-auto-select", "all-albums"]
    if per_track_urls is not None:
        args.extend(per_track_urls)
        _append_line(f"[runner] starting (track-expansion, {len(per_track_urls)} urls): {' '.join(args[:-len(per_track_urls)])} <tracks...>")
    else:
        args.append(url)
        _append_line(f"[runner] starting: {' '.join(args[:-1])} <url>")

    def handle_piece(piece: str) -> None:
        """Process one \\r/\\n-delimited stdout segment: progress parsing,
        SSE fanout, track counters. yt-dlp overwrites its progress line with
        \\r and never emits \\n between frames, so segments — not lines — are
        the real unit; the chunk reader below splits on both delimiters."""
        nonlocal _total_recorded
        global _current_frag_high

        if UI_NOISE_RE.match(piece):
            m = DOWNLOAD_LINE_RE.search(piece)
            if m:
                try:
                    _progress["current_pct"] = float(m.group("pct"))
                except (TypeError, ValueError):
                    pass
                if m.group("fi") and m.group("ft"):
                    fi = int(m.group("fi"))
                    _progress["current_frag"] = f"{fi}/{m.group('ft')}"
                    # yt-dlp starting over on this track — fragment
                    # index snapped back below the highest we've
                    # already seen.
                    if fi < _current_frag_high - 1:
                        _progress["current_track_retries"] += 1
                        _current_frag_high = fi
                    elif fi > _current_frag_high:
                        _current_frag_high = fi
            return

        _append_line(piece)

        counts_changed = False
        tm = TRACK_START_RE.search(piece)
        if tm:
            idx = int(tm.group(1))
            total = int(tm.group(2))
            _progress["current_track_idx"] = idx
            _progress["current_track_total"] = total
            _progress["current_track_name"] = tm.group(3)
            _progress["current_pct"] = 0.0
            _progress["current_frag"] = None
            _progress["current_track_retries"] = 0
            _current_frag_high = -1
            _progress["tracks_new"] += 1
            counts_changed = True
            if not _total_recorded:
                store.set_total_tracks(url, total)
                _total_recorded = True
        if TRACK_SKIP_RE.search(piece):
            _progress["tracks_skipped"] += 1
            _progress["tracks_new"] = max(_progress["tracks_new"] - 1, 0)
            counts_changed = True
        if TRACK_FAIL_RE.search(piece):
            _progress["tracks_failed"] += 1
            counts_changed = True
        if counts_changed:
            store.update_run_counts(
                run_id,
                _progress["tracks_new"],
                _progress["tracks_skipped"],
                _progress["tracks_failed"],
            )

    log_f = log_path.open("wb", buffering=0)
    try:
        # New session → gamdl leads its own process group, so cancel/watchdog
        # can killpg the yt-dlp/ffmpeg children too instead of orphaning them.
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        _current_proc = proc
        assert proc.stdout is not None

        # Chunk reader with our own segment splitting, replacing readline().
        # readline() only returned on \n, so a slow multi-minute download (all
        # \r progress frames, no newline) looked identical to a hung process —
        # it also needed a 16 MiB limit hack to survive the glued frames.
        # read() returns on every frame, which makes the inactivity watchdog
        # accurate: a timeout means gamdl truly went silent.
        buf = b""
        while True:
            try:
                chunk = await asyncio.wait_for(
                    proc.stdout.read(65536), timeout=INACTIVITY_TIMEOUT
                )
            except asyncio.TimeoutError:
                msg = (
                    f"[runner] watchdog: no output for {INACTIVITY_TIMEOUT}s "
                    "— killing gamdl"
                )
                _append_line(msg)
                try:
                    log_f.write(f"\n{msg}\n".encode())
                except OSError:
                    pass
                await _kill_proc_group(proc)
                break
            if not chunk:
                break
            log_f.write(chunk)
            buf += chunk
            # Split on \r AND \n; the last element has no delimiter yet — a
            # partial segment (or partial UTF-8 sequence) kept for next read.
            segments = re.split(rb"[\r\n]", buf)
            buf = segments.pop()
            for seg in segments:
                piece = _ANSI_RE.sub("", seg.decode("utf-8", errors="replace")).strip()
                if piece:
                    handle_piece(piece)
        tail = _ANSI_RE.sub("", buf.decode("utf-8", errors="replace")).strip()
        if tail:
            handle_piece(tail)
        exit_code = await proc.wait()
    except Exception as exc:  # noqa: BLE001
        # Capture the traceback to stderr (→ docker logs) AND the persisted
        # log file AND the SSE buffer. Previously only SSE got a one-line
        # repr, which vanishes when the browser closes — making silent -1
        # runs impossible to diagnose after the fact.
        tb = traceback.format_exc()
        sys.stderr.write(f"[runner] exception in _run_one for {url}:\n{tb}")
        sys.stderr.flush()
        try:
            log_f.write(f"\n[runner] exception: {exc!r}\n{tb}".encode())
        except Exception:
            pass
        _append_line(f"[runner] exception: {exc!r}")
        # Make sure we don't leave gamdl (or its yt-dlp/ffmpeg children)
        # running orphaned in the container. proc may be undefined if
        # create_subprocess_exec itself threw — hence the locals() guard.
        if "proc" in locals() and proc.returncode is None:
            try:
                await _kill_proc_group(proc)
            except Exception:  # noqa: BLE001
                pass
        exit_code = -1
    finally:
        _current_proc = None
        log_f.close()

    store.finish_run(
        run_id=run_id,
        exit_code=exit_code,
        tracks_new=_progress["tracks_new"],
        tracks_skipped=_progress["tracks_skipped"],
        tracks_failed=_progress["tracks_failed"],
    )

    # Observe what landed in Incoming. Runs in a thread so the indexer's
    # synchronous filesystem walk + sqlite writes don't block the event loop.
    # Source URL is attached so a future Phase-C pre-filter knows which
    # watchlist entry produced each track.
    try:
        stats = await asyncio.to_thread(indexer.index_incoming, source_url=url)
        _append_line(
            f"[indexer] incoming scanned={stats['scanned']} "
            f"upserted={stats['upserted']} unreadable={stats['unreadable']} "
            f"stale_cleared={stats['stale_cleared']}"
        )
    except Exception as exc:  # noqa: BLE001
        _append_line(f"[indexer] exception: {exc!r}")

    _running["run_id"] = None
    _running["url"] = None
    _running["kind"] = None
    # _append_line swaps the event + wakes waiters, so this also serves as
    # the "done" signal for any SSE consumer currently in wait_for_line().
    _append_line(f"[runner] done: exit={exit_code}")
    return exit_code


def _on_task_done(task: asyncio.Task) -> None:
    """Surface exceptions from the background run task — a fire-and-forget
    task swallows them silently otherwise."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        sys.stderr.write(f"[runner] run task crashed: {exc!r}\n")
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
        sys.stderr.flush()


async def _run_entries(entries: list[tuple[str, str]], trigger: str) -> list[int]:
    results: list[int] = []
    for url, kind in entries:
        if _cancel_requested:
            _append_line("[runner] sweep cancelled — remaining entries skipped")
            break
        results.append(await _run_one(url=url, kind=kind, trigger=trigger))
    return results


def start(url: str, kind: str, trigger: str = "manual") -> bool:
    """Reserve the runner and launch a single-URL run in the background.

    The check-and-set on ``_active_task`` happens synchronously on the event
    loop, so two requests in the same tick can't both start. Returns False
    when a run is already active — callers turn that into a 409.
    """
    global _active_task, _cancel_requested
    if is_running():
        return False
    _cancel_requested = False
    _active_task = asyncio.create_task(_run_one(url=url, kind=kind, trigger=trigger))
    _active_task.add_done_callback(_on_task_done)
    return True


def start_all(entries: list[tuple[str, str]], trigger: str = "manual-all") -> bool:
    """Reserve the runner and launch a whole-watchlist sweep. See ``start``."""
    global _active_task, _cancel_requested
    if is_running():
        return False
    _cancel_requested = False
    _active_task = asyncio.create_task(_run_entries(entries, trigger))
    _active_task.add_done_callback(_on_task_done)
    return True
