#!/bin/bash
# Capture container env into a file cron jobs can source.
# Without this, run.sh running under cron sees a minimal environment and
# the USE_TRACK_EXPANSION flag (set on the container) is invisible.
set -eu

ENV_FILE=/etc/gamdl.env

{
    echo "TZ=${TZ:-Europe/London}"
    echo "USE_TRACK_EXPANSION=${USE_TRACK_EXPANSION:-0}"
    echo "GAMDL_UI_URL=${GAMDL_UI_URL:-http://gamdl-ui:4150}"
} > "$ENV_FILE"
chmod 0644 "$ENV_FILE"

exec cron -f
