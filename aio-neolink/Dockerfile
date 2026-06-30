# syntax=docker/dockerfile:1
#
# aio-neolink image
# -----------------
# Two concerns in one image:
#   1. Neolink (Rust + GStreamer) — speaks Reolink's Baichuan protocol on port 9000
#      and serves RTSP on :8554. We BUILD IT FROM SOURCE (stage 1) at a pinned master
#      commit whose Cargo.toml version is 0.6.3-rc.3 — the same in-development version
#      the proven "Neolink-latest" add-on ran. The newest *released* artifact is only
#      rc.2 (commit 7158943), which connects and lists streams but then stutters and
#      hangs on recent Reolink firmware; the rc.3 source resolves that. We keep
#      Neolink for the video path — it's the hard reverse-engineered part — and never
#      reinvent it.
#   2. A Python control plane (reolink-aio + FastAPI): PTZ / IR / spotlight / siren,
#      a self-healing health watchdog, and the web GUI.

# ---------------------------------------------------------------------------
# Stage 1 — compile Neolink from source (Rust + GStreamer)
# ---------------------------------------------------------------------------
# The builder is bookworm so it matches the bookworm runtime base below: the
# dynamically linked binary then finds the same glibc and GStreamer 1.22 at runtime.
FROM rust:slim-bookworm AS neolink-builder

# Pin the exact upstream commit for reproducible builds. This is master HEAD as of
# the last upstream change (2025-01-30); its Cargo.toml version is 0.6.3-rc.3.
# Bump this single value to move Neolink forward.
ARG NEOLINK_REPO=QuantumEntangledAndy/neolink
ARG NEOLINK_COMMIT=6e05e7844b5b50f89787d30bffcbbd3471bfcfde

ENV DEBIAN_FRONTEND=noninteractive

# Build natively for the image's own architecture. Upstream's .cargo/config.toml pins
# a cross-linker (aarch64-linux-gnu-gcc) for the aarch64 target; when we build natively
# on aarch64 (emulated by buildx) that prefixed binary may be absent, so force the
# stock `cc` from build-essential. Harmless on amd64 (a different target triple, with
# no forced linker in upstream's config). The global tokio_unstable rustflag in that
# same config file is left intact — we don't set RUSTFLAGS, so it still applies.
ENV CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=cc

# Build dependencies, taken verbatim from upstream Neolink's own Dockerfile.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl \
        openssl libssl-dev \
        libgstrtspserver-1.0-dev libgstreamer1.0-dev \
        libgtk2.0-dev protobuf-compiler libglib2.0-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
# Fetch just the pinned commit as a tarball — no git history needed.
RUN curl -fsSL "https://github.com/${NEOLINK_REPO}/archive/${NEOLINK_COMMIT}.tar.gz" \
        -o /tmp/neolink.tar.gz \
    && tar xzf /tmp/neolink.tar.gz --strip-components=1 \
    && rm /tmp/neolink.tar.gz

# The slow step (several minutes natively; longer under arch emulation). Docker layer
# caching means it only re-runs when NEOLINK_COMMIT or the build deps above change —
# editing the Python control plane never triggers a Neolink recompile.
RUN cargo build --release \
    && strip target/release/neolink

# ---------------------------------------------------------------------------
# Stage 2 — runtime image (HA base + GStreamer runtime + Python control plane)
# ---------------------------------------------------------------------------
ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1

# --- Runtime dependencies -----------------------------------------------------------
# GStreamer is the media pipeline Neolink links against. plugins-bad provides the RTSP
# server (gstrtsp); plugins-ugly/libav provide H.264/H.265 parsing + decode.
#
# gstreamer1.0-tools gives us gst-launch-1.0, which the GUI's live preview uses to
# turn the local RTSP feed into a low-rate JPEG stream for the browser. We pointedly
# do NOT use ffmpeg for this: ffmpeg bundles its own libav* libraries that collide
# with the versions Neolink's GStreamer uses, which broke Neolink's RTSP pipeline
# ("could not create element"). gst-launch reuses the exact same plugins Neolink
# already loads, so it can't perturb the video path it's previewing.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        python3 python3-pip \
        gstreamer1.0-tools \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-plugins-ugly \
        gstreamer1.0-libav \
        gstreamer1.0-rtsp \
        libgstrtspserver-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

# --- Neolink binary (compiled in stage 1) -------------------------------------------
COPY --from=neolink-builder /src/target/release/neolink /usr/local/bin/neolink
RUN chmod 0755 /usr/local/bin/neolink

# --- Python control plane -----------------------------------------------------------
COPY supervisor/requirements.txt /opt/aio-neolink/requirements.txt
RUN pip3 install --no-cache-dir -r /opt/aio-neolink/requirements.txt

COPY supervisor/ /opt/aio-neolink/supervisor/
COPY web/ /opt/aio-neolink/web/
COPY run.sh /run.sh
RUN chmod +x /run.sh

WORKDIR /opt/aio-neolink
CMD [ "/run.sh" ]
