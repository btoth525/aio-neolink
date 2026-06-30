# syntax=docker/dockerfile:1
#
# aio-neolink image
# -----------------
# Two concerns in one image:
#   1. Neolink (Rust + GStreamer) — speaks Reolink's Baichuan protocol on port 9000
#      and serves RTSP on :8554. This is the hard reverse-engineered part; don't
#      reinvent it.
#   2. A Python control plane (reolink-aio + FastAPI) for PTZ, IR, spotlight, siren,
#      health watchdog, and the web GUI.
#
# The health probe is pure-Python RTSP+RTP — no gst-discoverer needed — so the probe
# actually tests whether frames are flowing, not just whether the TCP port is open.

ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1

# --- Runtime dependencies -----------------------------------------------------------
# GStreamer is the media pipeline Neolink links against. We need plugins-bad for the
# RTSP server (gstrtsp) and plugins-ugly for H.264 parsing (x264/avdec_h264).
#
# gstreamer1.0-tools gives us gst-launch-1.0, which the GUI's live preview uses to
# turn the local RTSP feed into a low-rate JPEG stream for the browser. We pointedly
# do NOT use ffmpeg for this: ffmpeg bundles its own libav* media libraries that
# collide with the versions Neolink's GStreamer was built against, which made
# Neolink's own RTSP pipeline fail with "could not create element". gst-launch reuses
# the exact same plugins Neolink already loads, so it can't perturb the video path.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl unzip \
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

# --- Neolink binary -----------------------------------------------------------------
# Downloads the QuantumEntangledAndy fork release for the target arch.
# Override at build time to use your own fork:
#   docker build --build-arg NEOLINK_VERSION=0.6.3-rc.3
ARG NEOLINK_VERSION="v0.6.3.rc.2"
COPY rootfs/fetch-neolink.sh /usr/local/bin/fetch-neolink.sh
RUN chmod +x /usr/local/bin/fetch-neolink.sh && /usr/local/bin/fetch-neolink.sh

# --- Python control plane -----------------------------------------------------------
COPY supervisor/requirements.txt /opt/aio-neolink/requirements.txt
RUN pip3 install --no-cache-dir -r /opt/aio-neolink/requirements.txt

COPY supervisor/ /opt/aio-neolink/supervisor/
COPY web/ /opt/aio-neolink/web/
COPY run.sh /run.sh
RUN chmod +x /run.sh

WORKDIR /opt/aio-neolink
CMD [ "/run.sh" ]
