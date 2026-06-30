#!/usr/bin/env bash
# Install the prebuilt Neolink binary published by THIS repo's CI
# (.github/workflows/build-neolink.yml). The add-on therefore does NOT compile Rust
# on the Home Assistant device — it just downloads a ~15 MB binary.
#
# CI builds Neolink 0.6.3-rc.3 from source (upstream commit 6e05e7844b5b) inside a
# Debian bookworm container so the binary's glibc matches this image's runtime, then
# attaches neolink-linux-amd64 / neolink-linux-arm64 to the 'neolink-rc3' release.
set -euo pipefail

TAG="${NEOLINK_BIN_TAG:-neolink-rc3}"
REPO="${AIO_NEOLINK_REPO:-btoth525/aio-neolink}"
DEST=/usr/local/bin/neolink

# Map HA's BUILD_ARCH (or uname) to the published asset name.
ARCH="${BUILD_ARCH:-$(uname -m)}"
case "${ARCH}" in
    amd64|x86_64)  ASSET="neolink-linux-amd64" ;;
    aarch64|arm64) ASSET="neolink-linux-arm64" ;;
    *)
        echo "[fetch-neolink] unsupported arch: ${ARCH}" >&2
        exit 1
        ;;
esac

URL="https://github.com/${REPO}/releases/download/${TAG}/${ASSET}"
echo "[fetch-neolink] downloading prebuilt Neolink for ${ARCH}"
echo "[fetch-neolink] ${URL}"

curl -fsSL "${URL}" -o "${DEST}"
chmod 0755 "${DEST}"

echo "[fetch-neolink] installed: $("${DEST}" --version 2>&1 | head -1)"
