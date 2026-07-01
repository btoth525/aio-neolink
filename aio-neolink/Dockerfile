# syntax=docker/dockerfile:1
#
# aio-neolink image
# -----------------
# Three concerns in one image:
#   1. Neolink (Rust + GStreamer) — speaks Reolink's Baichuan protocol on port 9000
#      and serves RTSP LOCALHOST-ONLY on an internal port. We keep Neolink for the
#      video path and never reinvent it.
#   2. go2rtc — restreams Neolink's internal feed to the public port this add-on has
#      always exposed. Neolink's RTSP server was found to be unable to safely serve
#      more than one simultaneous client at all; go2rtc is purpose-built for exactly
#      that fan-out and is the same restream server Frigate itself bundles. Neolink
#      only ever has go2rtc as a client — see restream_gen.py.
#   3. A Python control plane (reolink-aio + FastAPI): PTZ / IR / spotlight / siren,
#      a self-healing health watchdog, and the web GUI.
#
# Base image: quantumentangledandy/neolink:latest — and we use the Neolink binary
# baked into it AS-IS. We no longer compile/download our own.
#
# v0.1.24 root cause: for many versions this add-on shipped a Neolink binary built
# by its OWN CI from a pinned master commit (6e05e7844b5b). That binary connected,
# logged in, and registered its RTSP mount identically to the working setup — but
# every DESCRIBE against it returned a permanent 404 with zero further log output
# even at RUST_LOG=trace. A live A/B test proved the same camera streamed
# instantly through dm82m's `Neolink-latest` add-on, which is a thin wrapper around
# THIS image and runs the binary baked into it. The trap: the upstream image was
# last published over a year ago, so its baked binary is an OLDER master build,
# while our CI pinned a NEWER commit — and Neolink's Cargo.toml version string
# stayed "0.6.3-rc.3" across many commits, so "same version" masked that we were
# actually shipping different, newer, regressed code. Building/pinning our own
# binary bought us nothing but that regression. Use the image's proven binary.
ARG BUILD_FROM=quantumentangledandy/neolink:latest
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1

# --- Runtime dependencies -----------------------------------------------------------
# GStreamer AND the Neolink binary both come from the base image — we only add the
# packages this add-on's OWN code needs on top: Python for the control plane, curl
# to fetch go2rtc.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
        python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# --- Neolink binary: use the baked-in /usr/local/bin/neolink from the base image ----
# (No fetch/compile step — see the base-image comment above for why our own CI build
# was the bug. run.sh's NEOLINK_BIN already points at /usr/local/bin/neolink.)

# --- go2rtc binary (the restream layer — see restream_gen.py) -----------------------
COPY rootfs/fetch-go2rtc.sh /usr/local/bin/fetch-go2rtc.sh
RUN chmod +x /usr/local/bin/fetch-go2rtc.sh && /usr/local/bin/fetch-go2rtc.sh

# --- Python control plane -----------------------------------------------------------
COPY supervisor/requirements.txt /opt/aio-neolink/requirements.txt
RUN pip3 install --no-cache-dir -r /opt/aio-neolink/requirements.txt

COPY supervisor/ /opt/aio-neolink/supervisor/
COPY web/ /opt/aio-neolink/web/
COPY run.sh /run.sh
RUN chmod +x /run.sh

WORKDIR /opt/aio-neolink
CMD [ "/run.sh" ]
