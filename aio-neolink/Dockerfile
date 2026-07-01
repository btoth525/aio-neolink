# syntax=docker/dockerfile:1
#
# aio-neolink image
# -----------------
# Three concerns in one image:
#   1. Neolink (Rust + GStreamer) — speaks Reolink's Baichuan protocol on port 9000
#      and serves RTSP LOCALHOST-ONLY on an internal port. We DOWNLOAD a prebuilt
#      binary (Neolink 0.6.3-rc.3, built from source by this repo's CI — see
#      .github/workflows/build-neolink.yml) so the Home Assistant device never has
#      to compile Rust. We use rc.3, not the newest *release* (rc.2): rc.2 connects
#      and lists streams but then stutters and hangs on recent Reolink firmware,
#      while rc.3 (what the proven "Neolink-latest" add-on ran) holds the stream.
#      We keep Neolink for the video path and never reinvent it.
#   2. go2rtc — restreams Neolink's internal feed to the public port this add-on has
#      always exposed. Neolink's RTSP server was found to be unable to safely serve
#      more than one simultaneous client at all; go2rtc is purpose-built for exactly
#      that fan-out and is the same restream server Frigate itself bundles. Neolink
#      only ever has go2rtc as a client — see restream_gen.py.
#   3. A Python control plane (reolink-aio + FastAPI): PTZ / IR / spotlight / siren,
#      a self-healing health watchdog, and the web GUI.
#
# To compile Neolink from source on-device instead of downloading, see the
# multi-stage Dockerfile in this repo's git history at tag/commit for v0.1.7.

ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1

# --- Runtime dependencies -----------------------------------------------------------
# GStreamer is the media pipeline Neolink links against. plugins-bad provides the RTSP
# server (gstrtsp); plugins-ugly/libav provide H.264/H.265 parsing + decode.
#
# Deliberately no gstreamer1.0-tools / gst-launch and no ffmpeg: the GUI does not run
# a live preview (see api.py), so nothing in this image opens a second RTSP
# connection to Neolink. Only the runtime libraries Neolink itself links against are
# installed — nothing that could contend with its own pipeline.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
        python3 python3-pip \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-plugins-ugly \
        gstreamer1.0-libav \
        gstreamer1.0-rtsp \
        libgstrtspserver-1.0-0 \
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
