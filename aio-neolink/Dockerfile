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
# Base image: quantumentangledandy/neolink:latest, NOT the generic HA Debian base.
# v0.1.20 diagnosis: our own Neolink binary (built by this repo's CI from the exact
# same pinned commit, 6e05e7844b5b) connected to the camera, logged in, and
# registered its RTSP mount identically — but every DESCRIBE against it returned a
# permanent 404 with zero further Neolink log output even at RUST_LOG=trace,
# meaning its GStreamer pipeline-construction callback was silently never
# succeeding. A controlled side-by-side test proved the SAME commit, SAME camera,
# run via the upstream `Neolink-latest` HA add-on (which is a thin wrapper around
# this exact upstream image) streamed to Frigate immediately. The only variable
# that changed was the container: that add-on doesn't hand-pick GStreamer apt
# packages on a generic Debian base like this Dockerfile used to — it runs inside
# the image the Neolink maintainers themselves build and test the binary against.
# Building FROM that image instead removes the guesswork of matching its exact
# GStreamer plugin set/versions by hand. We still overwrite the binary below with
# our own CI-pinned build for reproducibility (this image's own commit happened to
# match ours, but pinning explicitly means a future upstream :latest move can't
# silently change what we ship).
ARG BUILD_FROM=quantumentangledandy/neolink:latest
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1

# --- Runtime dependencies -----------------------------------------------------------
# GStreamer itself comes from the base image (see above) — we only need to add the
# packages this add-on's OWN code needs on top of it: Python for the control plane,
# curl to fetch the Neolink/go2rtc binaries below.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
        python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# --- Neolink binary (prebuilt by CI, downloaded here) -------------------------------
# BUILD_ARCH is provided by the HA builder; fetch-neolink.sh maps it to the right
# published asset and installs /usr/local/bin/neolink.
ARG BUILD_ARCH
ENV BUILD_ARCH=${BUILD_ARCH}
COPY rootfs/fetch-neolink.sh /usr/local/bin/fetch-neolink.sh
RUN chmod +x /usr/local/bin/fetch-neolink.sh && /usr/local/bin/fetch-neolink.sh

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
