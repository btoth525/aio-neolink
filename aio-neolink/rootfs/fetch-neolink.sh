#!/usr/bin/env bash
# DEPRECATED / UNUSED as of v0.1.6.
# ---------------------------------------------------------------------------
# The Dockerfile no longer calls this script. We now BUILD Neolink from source
# (commit-pinned, version 0.6.3-rc.3) in a builder stage, because the newest
# *released* binary is only rc.2 and that release stutters/hangs on recent Reolink
# firmware. This script is kept only as a reference for the download-a-release
# approach (e.g. if you later publish your own fork's release assets).
# ---------------------------------------------------------------------------
#
# Download the QuantumEntangledAndy Neolink binary for the target arch.
#
# Release naming (as of v0.6.3.rc.2):
#   tag:    v0.6.3.rc.2           (dots not dashes between rc)
#   assets: neolink_linux_x86_64_bookworm.zip  (amd64 on Debian Bookworm)
#            neolink_linux_arm64.zip
#            neolink_linux_armhf.zip
#
# To use your own fork:
#   docker build --build-arg NEOLINK_VERSION=v0.6.3.rc.2 \
#                --build-arg NEOLINK_REPO=<owner/repo>
set -euo pipefail

VERSION="${NEOLINK_VERSION:-v0.6.3.rc.2}"
REPO="${NEOLINK_REPO:-QuantumEntangledAndy/neolink}"
DEST=/usr/local/bin/neolink

# Map HA BUILD_ARCH / uname -m to release asset names.
ARCH="${BUILD_ARCH:-$(uname -m)}"
case "${ARCH}" in
    amd64|x86_64)
        # Bookworm matches our ghcr.io/home-assistant/amd64-base-debian:bookworm base.
        ASSET="neolink_linux_x86_64_bookworm.zip"
        ;;
    aarch64|arm64)
        ASSET="neolink_linux_arm64.zip"
        ;;
    armv7|armhf)
        ASSET="neolink_linux_armhf.zip"
        ;;
    *)
        echo "[fetch-neolink] unsupported arch: ${ARCH}" >&2
        exit 1
        ;;
esac

URL="https://github.com/${REPO}/releases/download/${VERSION}/${ASSET}"
echo "[fetch-neolink] downloading Neolink ${VERSION} for ${ARCH}"
echo "[fetch-neolink] ${URL}"

curl -fsSL "${URL}" -o /tmp/neolink.zip
unzip -q /tmp/neolink.zip -d /tmp/neolink_extract

# Find the binary wherever it landed in the archive.
BIN=$(find /tmp/neolink_extract -name neolink -type f | head -1)
if [[ -z "${BIN}" ]]; then
    echo "[fetch-neolink] ERROR: neolink binary not found in archive" >&2
    ls -R /tmp/neolink_extract >&2
    exit 1
fi

install -m 0755 "${BIN}" "${DEST}"
rm -rf /tmp/neolink.zip /tmp/neolink_extract

echo "[fetch-neolink] installed: $("${DEST}" --version 2>&1 | head -1)"
