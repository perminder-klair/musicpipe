#!/bin/bash
# Iterate watchlist files and invoke gamdl for each URL.
# Errors on individual URLs do not abort the run.
set -u

# Written by entrypoint.sh at container start. Cron jobs run in a minimal
# env that doesn't inherit the container's, so vars like USE_TRACK_EXPANSION
# need to be sourced explicitly.
[ -f /etc/gamdl.env ] && . /etc/gamdl.env

LOG="/var/log/gamdl/run.log"
COOKIES="/config/cookies.txt"
OUTDIR="/downloads"
TEMPDIR="/tmp/gamdl"
ARTISTS="/config/artists.txt"
PLAYLISTS="/config/playlists.txt"
ALBUMS="/config/albums.txt"

mkdir -p "$TEMPDIR"

ts()  { date '+%Y-%m-%d %H:%M:%S %Z'; }
log() { echo "[$(ts)] $*" | tee -a "$LOG" >&2; }

if [ ! -s "$COOKIES" ]; then
    log "ERROR: cookies file missing or empty at $COOKIES"
    exit 1
fi

GAMDL_UI_URL="${GAMDL_UI_URL:-http://gamdl-ui:4150}"

# Ask gamdl-ui whether this URL should be skipped on the cron path.
# Returns 0 to skip, 1 to run. Non-fatal on HTTP error: if the service is
# unreachable we fall through to running gamdl (availability > optimization).
pre_check_skip() {
    local url=$1 kind=$2
    local encoded
    encoded=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$url") || return 1
    local body
    body=$(wget -qO- --timeout=5 --tries=1 \
        "${GAMDL_UI_URL}/pre-check?url=${encoded}&kind=${kind}" 2>/dev/null) || return 1
    # Parse {"skip": true/false, "reason": "…"} without pulling jq in.
    python3 - "$body" <<'PY' 2>/dev/null || return 1
import json, sys
try:
    obj = json.loads(sys.argv[1])
except Exception:
    sys.exit(1)
sys.exit(0 if obj.get("skip") else 1)
PY
}

pre_check_reason() {
    local url=$1 kind=$2
    local encoded
    encoded=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$url") || return
    local body
    body=$(wget -qO- --timeout=5 --tries=1 \
        "${GAMDL_UI_URL}/pre-check?url=${encoded}&kind=${kind}" 2>/dev/null) || return
    python3 - "$body" <<'PY' 2>/dev/null || return
import json, sys
try:
    obj = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)
print(obj.get("reason") or "")
PY
}

# Phase 3 track-level path: expand the URL via gamdl-ui, then either skip or
# invoke gamdl with the per-track URL list. Returns:
#   0   gamdl ran and succeeded (or no-op because all tracks already held)
#   100 skipped entirely — caller increments skipped counter
#   2xx API unreachable / malformed response — caller falls back to legacy
#   other  gamdl's own exit code on failure
resolve_and_run() {
    local url=$1 label=$2
    shift 2
    local extra_args=("$@")
    local encoded
    encoded=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$url") \
        || return 200
    # Higher timeout than pre-check — expansion can run ~1s per artist URL
    # and the first call after container restart re-scrapes the dev token.
    local body
    body=$(wget -qO- --timeout=30 --tries=1 \
        "${GAMDL_UI_URL}/resolve-urls?url=${encoded}&kind=${label}" 2>/dev/null) \
        || return 201

    local parsed
    parsed=$(python3 - "$body" <<'PY' 2>/dev/null
import json, sys
try:
    obj = json.loads(sys.argv[1])
except Exception:
    sys.exit(1)
if obj.get("skip"):
    print("SKIP", obj.get("reason") or "")
else:
    s = obj.get("summary") or {}
    urls = obj.get("urls") or []
    print("RUN", len(urls), s.get("total", 0), s.get("present", 0), s.get("blocked", 0))
    for u in urls:
        print(u)
PY
) || return 202

    local header
    header=$(printf '%s\n' "$parsed" | head -1)
    local action=${header%% *}

    if [ "$action" = "SKIP" ]; then
        local reason=${header#SKIP }
        log "SKIP  $label: $url ($reason)"
        return 100
    fi

    # header = "RUN <count> <total> <present> <blocked>"
    local _ count total present blocked
    read -r _ count total present blocked <<< "$header"

    if [ "${count:-0}" -eq 0 ]; then
        log "SKIP  $label: $url (resolved to 0 missing tracks)"
        return 100
    fi

    # Collect per-track URLs into an array, dropping the header line.
    local -a track_urls=()
    while IFS= read -r u; do
        [ -n "$u" ] && track_urls+=("$u")
    done < <(printf '%s\n' "$parsed" | tail -n +2)

    log "START $label: $url (track-expansion missing=$count total=$total present=$present blocked=$blocked)"

    gamdl \
        --cookies-path "$COOKIES" \
        --output-path "$OUTDIR" \
        --temp-path "$TEMPDIR" \
        --log-level INFO \
        "${extra_args[@]}" \
        "${track_urls[@]}" >> "$LOG" 2>&1
    return $?
}

process_file() {
    local file=$1 label=$2
    shift 2
    local extra_args=("$@")

    if [ ! -f "$file" ]; then
        log "INFO: $file not found, skipping $label list"
        return 0
    fi

    local total=0 ok=0 fail=0 skipped=0
    while IFS= read -r line || [ -n "$line" ]; do
        # strip CR (in case of Windows-edited files) and leading/trailing spaces
        line="${line%$'\r'}"
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        case "$line" in
            ''|'#'*) continue ;;
        esac
        total=$((total + 1))

        if [ "${USE_TRACK_EXPANSION:-0}" = "1" ]; then
            # Phase 3 path: gamdl-ui expands the URL, we hand gamdl only the
            # missing per-track URLs. Falls back to the legacy path on
            # 2xx-range return codes so an unreachable gamdl-ui never
            # silently stalls the nightly cron.
            resolve_and_run "$line" "$label" "${extra_args[@]}"
            rc=$?
            case $rc in
                0)    ok=$((ok + 1)); log "OK    $label: $line" ; continue ;;
                100)  skipped=$((skipped + 1)) ; continue ;;
                200|201|202)
                    log "WARN  $label: $line — resolve-urls unavailable (rc=$rc), falling back to legacy pre-check"
                    ;;
                *)    fail=$((fail + 1)); log "FAIL  $label: $line (exit $rc)"; continue ;;
            esac
        fi

        if pre_check_skip "$line" "$label"; then
            local reason
            reason=$(pre_check_reason "$line" "$label")
            skipped=$((skipped + 1))
            log "SKIP  $label: $line ($reason)"
            continue
        fi
        log "START $label: $line"
        if gamdl \
              --cookies-path "$COOKIES" \
              --output-path "$OUTDIR" \
              --temp-path "$TEMPDIR" \
              --log-level INFO \
              "${extra_args[@]}" \
              "$line" >> "$LOG" 2>&1; then
            ok=$((ok + 1))
            log "OK    $label: $line"
        else
            rc=$?
            fail=$((fail + 1))
            log "FAIL  $label: $line (exit $rc)"
        fi
    done < "$file"

    log "SUMMARY $label: total=$total ok=$ok fail=$fail skipped=$skipped"
}

log "=== gamdl run started ==="
process_file "$ARTISTS"   artist   --artist-auto-select all-albums
process_file "$ALBUMS"    album
process_file "$PLAYLISTS" playlist
log "=== gamdl run completed ==="
