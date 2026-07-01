#!/usr/bin/env bash
# aio-neolink entrypoint.
#
# HA writes the add-on options to /data/options.json. We read a couple of values for
# convenience/logging here, then hand control to the Python supervisor, which reads
# the same file itself. The supervisor owns the lifecycle from here: it generates the
# Neolink config, launches a video pipeline per camera, watches their health, and
# serves the GUI/API.
set -euo pipefail

OPTIONS=/data/options.json
LOG_LEVEL="$(python3 -c "import json,sys;print(json.load(open('${OPTIONS}')).get('log_level','info'))" 2>/dev/null || echo info)"
# STOPGAP (v0.1.23) — see restream_gen.py: point go2rtc at an external, already-
# working Neolink instance instead of this add-on's own bundled one. Blank/0 by
# default (normal operation, self-contained).
NEOLINK_SOURCE_HOST="$(python3 -c "import json,sys;print(json.load(open('${OPTIONS}')).get('neolink_source_host','') or '')" 2>/dev/null || echo '')"
NEOLINK_SOURCE_PORT="$(python3 -c "import json,sys;print(json.load(open('${OPTIONS}')).get('neolink_source_port',0) or '')" 2>/dev/null || echo '')"

echo "------------------------------------------------------------"
echo " aio-neolink starting"
echo "   log level     : ${LOG_LEVEL}"
echo "   neolink       : $(/usr/local/bin/neolink --version 2>/dev/null | head -n1 || echo 'unknown')"
echo "   data dir      : /data"
if [ -n "${NEOLINK_SOURCE_HOST}" ]; then
    echo "   STOPGAP       : go2rtc sourcing from ${NEOLINK_SOURCE_HOST}:${NEOLINK_SOURCE_PORT:-<bundled port>} instead of bundled Neolink"
fi
echo "------------------------------------------------------------"

export AIO_NEOLINK_LOG_LEVEL="${LOG_LEVEL}"
export NEOLINK_BIN="/usr/local/bin/neolink"
if [ -n "${NEOLINK_SOURCE_HOST}" ]; then
    export NEOLINK_SOURCE_HOST
fi
if [ -n "${NEOLINK_SOURCE_PORT}" ]; then
    export NEOLINK_SOURCE_PORT
fi

exec python3 -m supervisor.main
