#!/usr/bin/env bash
# Install go2rtc — the restream layer between Neolink and everyone else.
#
# Neolink's RTSP server was found (by direct testing) to be unable to safely serve
# more than one simultaneous client on the same camera, regardless of whether that
# second client reconnects repeatedly or holds a single steady connection. go2rtc
# (AlexxIT/go2rtc — the same restream server Frigate itself bundles for other camera
# types) sits between Neolink and the world: Neolink only ever has go2rtc as a
# client, and go2rtc safely fans that one feed out to Frigate, VLC, this add-on's own
# health check, or anything else. See restream_gen.py for the full rationale.
set -euo pipefail

VERSION="${GO2RTC_VERSION:-v1.9.14}"
REPO="${GO2RTC_REPO:-AlexxIT/go2rtc}"
DEST=/usr/local/bin/go2rtc

ARCH="${BUILD_ARCH:-$(uname -m)}"
case "${ARCH}" in
    amd64|x86_64)  ASSET="go2rtc_linux_amd64" ;;
    aarch64|arm64) ASSET="go2rtc_linux_arm64" ;;
    *)
        echo "[fetch-go2rtc] unsupported arch: ${ARCH}" >&2
        exit 1
        ;;
esac

URL="https://github.com/${REPO}/releases/download/${VERSION}/${ASSET}"
echo "[fetch-go2rtc] downloading go2rtc ${VERSION} for ${ARCH}"
echo "[fetch-go2rtc] ${URL}"

curl -fsSL "${URL}" -o "${DEST}"
chmod 0755 "${DEST}"

# go2rtc's version-flag output isn't stable enough to rely on under `set -e`; just
# confirm the binary is present and executable.
echo "[fetch-go2rtc] installed ${VERSION} for ${ARCH}: $(du -h "${DEST}" | cut -f1)"
