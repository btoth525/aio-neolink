#!/usr/bin/env bash
# Download the QuantumEntangledAndy Neolink binary for the target arch.
#
# This fork is the one the original "Neolink-latest" add-on ships and is the last
# actively maintained version with Baichuan, MQTT, multi-stream, and battery support.
#
# To use your own fork:
#   docker build --build-arg NEOLINK_VERSION=<tag> \
#                --build-arg NEOLINK_REPO=<owner/repo>
set -euo pipefail

VERSION="${NEOLINK_VERSION:-0.6.3-rc.3}"
REPO="${NEOLINK_REPO:-QuantumEntangledAndy/neolink}"
DEST=/usr/local/bin/neolink

# Map HA BUILD_ARCH / uname -m to release asset names.
ARCH="${BUILD_ARCH:-$(uname -m)}"
case "${ARCH}" in
    amd64|x86_64)
        ASSET="neolink_amd64_linux.tar.gz"
        ;;
    aarch64|arm64)
        ASSET="neolink_aarch64_linux.tar.gz"
        ;;
    armv7|armhf)
        ASSET="neolink_armv7_linux.tar.gz"
        ;;
    *)
        echo "[fetch-neolink] unsupported arch: ${ARCH}" >&2
        exit 1
        ;;
esac

URL="https://github.com/${REPO}/releases/download/${VERSION}/${ASSET}"
echo "[fetch-neolink] downloading Neolink ${VERSION} for ${ARCH}"
echo "[fetch-neolink] ${URL}"

curl -fsSL "${URL}" -o /tmp/neolink.tar.gz
tar -xzf /tmp/neolink.tar.gz -C /tmp
# The archive puts the binary at neolink or bin/neolink depending on the release.
if [[ -f /tmp/neolink ]]; then
    install -m 0755 /tmp/neolink "${DEST}"
elif [[ -f /tmp/bin/neolink ]]; then
    install -m 0755 /tmp/bin/neolink "${DEST}"
else
    BIN=$(find /tmp -name neolink -type f | head -1)
    [[ -n "${BIN}" ]] || { echo "[fetch-neolink] neolink binary not found in archive" >&2; exit 1; }
    install -m 0755 "${BIN}" "${DEST}"
fi

rm -f /tmp/neolink.tar.gz 2>/dev/null || true
find /tmp -name neolink -type f -delete 2>/dev/null || true
echo "[fetch-neolink] installed: $("${DEST}" --version 2>&1 | head -1)"
