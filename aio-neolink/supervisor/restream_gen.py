"""Generate go2rtc.yaml — the restream layer between Neolink and everyone else.

Why this layer exists
----------------------
Neolink's RTSP server (this build, rc.3) was found to crash under repeated real-world
testing whenever more than one RTSP client was connected to the same camera at once —
Frigate connecting while this add-on's own health check also held a connection was
enough to trigger a flood of internal GStreamer assertion failures and kill the
process, even after the health check was rewritten to hold a single persistent
connection instead of reconnecting on a cycle. The concurrency issue is in Neolink
itself, not in how anything else connects to it.

go2rtc (the same restream server Frigate already bundles for other camera types) is
purpose-built to pull ONE upstream feed and fan it out to any number of downstream
consumers safely. Putting it between Neolink and the world means Neolink only ever
has exactly one client — go2rtc — for the entire lifetime of the add-on, no matter
how many things (Frigate, VLC, this add-on's own health check) connect downstream.

The external contract is unchanged: go2rtc publishes each camera at
rtsp://<host>:8554/<camera_name>, the exact URL this add-on has always advertised.
Neolink itself moves to a localhost-only, internal port (see config_gen.py) that
nothing outside this container can reach.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

from .store import Camera
from . import config_gen

log = logging.getLogger("aio-neolink.restream_gen")

GO2RTC_CONFIG = Path(os.environ.get("GO2RTC_CONFIG", "/data/go2rtc.yaml"))
# The public, external-facing port — unchanged from what this add-on has always
# exposed. Frigate/VLC/etc. keep using rtsp://<host>:8554/<camera_name> untouched.
PUBLIC_RTSP_PORT = int(os.environ.get("PUBLIC_RTSP_PORT", "8554"))
# go2rtc's own HTTP API/UI — localhost only. Not exposed as an add-on port; it's an
# implementation detail, not a second interface for the user to find and be confused
# by.
GO2RTC_API_ADDR = os.environ.get("GO2RTC_API_ADDR", "127.0.0.1:1984")


def _source_stream(cam: Camera) -> str:
    """Which of Neolink's per-camera mounts go2rtc should pull from.

    Uses the BARE camera name (e.g. "/movie-room"), not a qualified sub-path like
    "/movie-room/mainStream". Neolink registers several aliases per camera
    (main/Main/mainStream/.../ and the bare name), but live trace-level testing
    (go2rtc's rtsp trace log) showed Neolink returning a clean 404 for the
    qualified "/mainStream" path minutes into a stable run — not a startup race,
    a genuine failure to resolve that specific alias for this camera/build. The
    bare alias, by contrast, is the ONLY path ever empirically proven to work
    anywhere in this project's history (VLC, Frigate, and this add-on's own
    watchdog all used it successfully before this restream layer existed).
    Per Neolink's own src/rtsp/mod.rs, the bare "/{name}" alias belongs to
    whichever stream is actually active — Main's setup claims it unconditionally
    when Main is enabled ("both"/"mainStream" configs), and Sub's setup only
    claims it when Main is disabled ("subStream"-only configs) — so the bare
    alias always resolves to the stream this camera is actually configured to
    serve, with no leaf/suffix selection needed here at all.

    #backchannel=0 is required, not optional: go2rtc's RTSP source attempts an
    ONVIF Profile T two-way-audio backchannel negotiation by default, which means
    a SECOND connection/negotiation attempt against the source on top of the main
    pull. Neolink doesn't support that and doesn't handle it gracefully — this is
    almost certainly what let a crash happen even with go2rtc as the "only"
    client, since go2rtc itself was the second connection. Two-way audio isn't
    implemented yet anyway (see ROADMAP.md M4), so there's nothing to lose here.
    """
    return f"rtsp://{config_gen.RTSP_BIND_ADDR}:{config_gen.RTSP_PORT}/{cam.name}#backchannel=0"


def render(cameras: list[Camera]) -> str:
    streams: dict[str, str] = {}
    for cam in cameras:
        if not cam.enabled:
            continue
        streams[cam.name] = _source_stream(cam)

    doc = {
        "api": {"listen": GO2RTC_API_ADDR},
        # No webrtc/other modules needed — this is a pure RTSP restream, not a
        # second GUI or protocol surface.
        "rtsp": {"listen": f":{PUBLIC_RTSP_PORT}"},
        "streams": streams,
        # TEMPORARY diagnostic: go2rtc's source pull is failing DESCRIBE against
        # Neolink with a clean 404 even well after Neolink logs the mount as
        # available, with no further restarts in between (ruled out as a startup
        # race). trace-level rtsp/streams logging exposes the exact request/
        # response bytes so the mismatch can be identified directly instead of
        # guessed at. Revert this once the cause is found — trace logging is noisy.
        "log": {"rtsp": "trace", "streams": "trace"},
    }
    header = (
        "# Generated by aio-neolink — do not edit by hand.\n"
        "# Regenerated every time a camera is added/edited via the GUI.\n"
    )
    return header + yaml.safe_dump(doc, sort_keys=False)


def write(cameras: list[Camera], path: Path = GO2RTC_CONFIG) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(cameras))
    return path
