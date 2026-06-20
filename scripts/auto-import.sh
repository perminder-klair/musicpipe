#!/bin/bash
# Auto-import: watches Incoming/ and runs beet import when new music appears

INCOMING="/music/Incoming"
INTERVAL="${AUTO_IMPORT_INTERVAL:-300}"  # default: 5 minutes
LOG="/config/auto-import.log"
TRIGGER="/config/.trigger-import"
STATUS="/config/.import-status"  # atomic status file gamdl-ui polls: "running"|"idle"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" | tee -a "$LOG"
}

log "Auto-import started (checking every ${INTERVAL}s)"

while true; do
    # Count music files in incoming
    COUNT=$(find -L "$INCOMING" -type f \( -name "*.mp3" -o -name "*.flac" -o -name "*.m4a" -o -name "*.ogg" -o -name "*.wav" -o -name "*.opus" -o -name "*.wma" -o -name "*.aac" \) 2>/dev/null | wc -l)

    if [ "$COUNT" -gt 0 ]; then
        log "Found $COUNT music files, starting import..."
        # Strip deleted/blocklisted tracks from Incoming before beets runs.
        # Non-fatal: if gamdl-ui is down we still import — the point of the
        # filter is durable "never again" enforcement, not a hard gate.
        FILTER_URL="${GAMDL_UI_URL:-http://gamdl-ui:4150}/maintenance/filter-incoming"
        FILTER_OUT=$(curl -fsS -m 120 -X POST "$FILTER_URL" 2>&1) \
            && log "filter-incoming: $FILTER_OUT" \
            || log "filter-incoming FAILED (continuing): $FILTER_OUT"
        echo "running" > "$STATUS"
        beet import -q /music/Incoming >> "$LOG" 2>&1
        EXIT_CODE=$?
        echo "idle" > "$STATUS"
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
            find "$INCOMING" -type f ! \( -name "*.mp3" -o -name "*.flac" -o -name "*.m4a" -o -name "*.ogg" -o -name "*.wav" -o -name "*.opus" -o -name "*.wma" -o -name "*.aac" \) -delete 2>/dev/null
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
