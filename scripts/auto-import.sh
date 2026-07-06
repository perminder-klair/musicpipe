#!/bin/bash
# Auto-import: watches Incoming/ and runs beet import when new music appears

INCOMING="/music/Incoming"
INTERVAL="${AUTO_IMPORT_INTERVAL:-300}"  # default: 5 minutes
LOG="/config/auto-import.log"
TRIGGER="/config/.trigger-import"
STATUS="/config/.import-status"  # atomic status file gamdl-ui polls: "running"|"idle"
# Hard cap on one beet run — fetchart/discogs are network plugins and can
# hang the loop indefinitely with .import-status stuck at "running".
BEET_TIMEOUT="${BEET_TIMEOUT:-3600}"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" | tee -a "$LOG"
}

# Size-based rotation, keeping N archives (file.1 newest … file.N oldest).
# Called at the top of each idle loop tick — nothing is importing then, so
# neither this script's tee -a nor beets' own log has a writer mid-mv.
rotate_log() {
    local file=$1 max_bytes=${2:-10485760} keep=${3:-3}
    [ -f "$file" ] || return 0
    local size
    size=$(stat -c %s "$file" 2>/dev/null || stat -f %z "$file" 2>/dev/null) || return 0
    [ "$size" -gt "$max_bytes" ] || return 0
    local i=$keep prev
    while [ "$i" -gt 1 ]; do
        prev=$((i - 1))
        [ -f "$file.$prev" ] && mv -f "$file.$prev" "$file.$i"
        i=$prev
    done
    mv -f "$file" "$file.1"
}

# find(1) predicate for audio files, shared by the counters below.
audio_pred=( \( -name "*.mp3" -o -name "*.flac" -o -name "*.m4a" -o -name "*.ogg" -o -name "*.wav" -o -name "*.opus" -o -name "*.wma" -o -name "*.aac" \) )

log "Auto-import started (checking every ${INTERVAL}s)"

while true; do
    rotate_log "$LOG"
    rotate_log /config/beet.log

    # Count music files in incoming
    COUNT=$(find -L "$INCOMING" -type f "${audio_pred[@]}" 2>/dev/null | wc -l)
    # gamdl copies finished files in from its container-local temp dir — not
    # an atomic rename — so a file written in the last minute may still be
    # mid-copy. Importing it would tag a truncated m4a forever. Wait for the
    # directory to go quiet before handing it to beets.
    RECENT=$(find -L "$INCOMING" -type f "${audio_pred[@]}" -mmin -1 2>/dev/null | wc -l)

    if [ "$COUNT" -gt 0 ] && [ "$RECENT" -gt 0 ]; then
        log "Found $COUNT music files but $RECENT written <1m ago — waiting for downloads to settle"
    elif [ "$COUNT" -gt 0 ]; then
        log "Found $COUNT music files, starting import..."
        # Strip deleted/blocklisted tracks from Incoming before beets runs.
        # Non-fatal: if gamdl-ui is down we still import — the point of the
        # filter is durable "never again" enforcement, not a hard gate.
        FILTER_URL="${GAMDL_UI_URL:-http://gamdl-ui:4150}/maintenance/filter-incoming"
        FILTER_OUT=$(curl -fsS -m 120 -X POST "$FILTER_URL" 2>&1) \
            && log "filter-incoming: $FILTER_OUT" \
            || log "filter-incoming FAILED (continuing): $FILTER_OUT"
        echo "running" > "$STATUS"
        timeout "$BEET_TIMEOUT" beet import -q /music/Incoming >> "$LOG" 2>&1
        EXIT_CODE=$?
        echo "idle" > "$STATUS"
        if [ $EXIT_CODE -eq 124 ]; then
            log "Import KILLED after ${BEET_TIMEOUT}s timeout"
        fi
        # beet runs as root here (we override lsio's s6 entrypoint for the
        # auto-loop), so new files in Library land as root:root and gamdl-ui
        # (uid 1000) can't unlink them. Normalize ownership after each import
        # so Phase B's delete flow works and cron-gamdl (also 1000:1001) can
        # still write into Incoming.
        PUID_VAL="${PUID:-1000}"
        PGID_VAL="${PGID:-1001}"
        chown -R "$PUID_VAL:$PGID_VAL" /music/Library /music/Incoming 2>/dev/null || true
        if [ $EXIT_CODE -eq 0 ]; then
            log "Import completed successfully"
            # Clean up leftover non-music files and empty directories
            find "$INCOMING" -type f ! "${audio_pred[@]}" -delete 2>/dev/null
            find "$INCOMING" -mindepth 1 -type d -empty -delete 2>/dev/null
        else
            log "Import finished with exit code $EXIT_CODE"
        fi
    fi

    # Sleep in short bursts so a gamdl-ui "Import now" trigger (a file touch
    # at $TRIGGER) can wake us within a few seconds instead of up to $INTERVAL.
    SLICES=$(( INTERVAL / 5 ))
    [ "$SLICES" -lt 1 ] && SLICES=1
    for _ in $(seq 1 "$SLICES"); do
        if [ -f "$TRIGGER" ]; then
            rm -f "$TRIGGER"
            log "Trigger received — starting import now"
            break
        fi
        sleep 5
    done
done
