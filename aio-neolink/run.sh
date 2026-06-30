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

echo "------------------------------------------------------------"
echo " aio-neolink starting"
echo "   log level     : ${LOG_LEVEL}"
echo "   neolink       : $(/usr/local/bin/neolink --version 2>/dev/null | head -n1 || echo 'unknown')"
echo "   data dir      : /data"
echo "------------------------------------------------------------"

export AIO_NEOLINK_LOG_LEVEL="${LOG_LEVEL}"
export NEOLINK_BIN="/usr/local/bin/neolink"

exec python3 -m supervisor.main
