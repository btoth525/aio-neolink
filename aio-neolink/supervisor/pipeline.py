"""Video pipeline supervision + the self-healing watchdog.

This is the module that fixes the failure that caused a ~29-hour outage: Neolink's
process stays alive (container reports "started") but its RTSP output silently stops
producing frames, so cameras sit `unavailable` with no automatic recovery.

Why the original watchdog would have failed
--------------------------------------------
A simple TCP-connect or RTSP OPTIONS probe would report "healthy" even during the
original hang — because Neolink's RTSP server was still listening and responding to
session setup. The stream was just not sending RTP packets.

How we detect it now
---------------------
We do a full RTSP session: OPTIONS → DESCRIBE → SETUP → PLAY, then wait for actual
RTP data bytes over the interleaved TCP transport. If no RTP arrives within the
timeout, the stream is hung — regardless of whether the port is open or OPTIONS
returns 200. This is the exact failure mode that caused the 29-hour outage.

The probe is pure Python + asyncio: no gst-discoverer, no ffprobe, no extra packages.

Recovery escalation
--------------------
1. First failure → send SIGTERM to Neolink and restart it immediately (same action
   as the manual restart that fixed the original incident).
2. If the restart itself hangs (process won't die) → SIGKILL.
3. `restart_backoff` prevents restart storms when cameras are genuinely offline.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from dataclasses import dataclass
from typing import Optional

from . import config_gen
from .store import Camera

log = logging.getLogger("aio-neolink.pipeline")

NEOLINK_BIN = os.environ.get("NEOLINK_BIN", "/usr/local/bin/neolink")

# Seconds to wait for at least one RTP byte during a probe. This must tolerate a
# *cold* start: when our probe is the only RTSP client, Neolink pulls video from the
# camera only on connect, and the first keyframe on the main stream can take a while.
# A successful probe returns the instant the first byte arrives, so a larger ceiling
# costs nothing in the healthy case and only avoids false "hung" verdicts on slow
# keyframe starts.
_PROBE_TIMEOUT = 20.0


@dataclass
class PipelineHealth:
    camera_name: str
    last_ok: float = 0.0
    consecutive_failures: int = 0
    last_error: str = ""
    healthy: bool = False


@dataclass
class SupervisorOptions:
    watchdog_timeout: int = 45       # seconds of silence → trigger restart regardless of failure count
    health_interval: int = 15        # seconds between probes
    failures_before_restart: int = 2 # consecutive failures before restarting
    rtsp_host: str = "127.0.0.1"
    rtsp_port: int = 8554
    restart_backoff: float = 5.0     # min seconds between full process restarts
    startup_grace: float = 45.0      # seconds after (re)start before probes count as failures


# ---------------------------------------------------------------------------
# Pure-Python RTSP + RTP probe
# ---------------------------------------------------------------------------

async def _probe_rtp(host: str, port: int, stream_name: str, timeout: float) -> tuple[bool, str]:
    """Verify that an RTSP stream is actually delivering RTP frames.

    Performs a real RTSP session (OPTIONS → DESCRIBE → SETUP → PLAY) over TCP and
    waits for interleaved RTP data. Returns (ok, error_detail).

    This catches the exact failure mode from the original incident: Neolink's RTSP
    server responding to OPTIONS/DESCRIBE but not sending any RTP after PLAY.

    The entire exchange is bounded by a single outer asyncio.wait_for so no
    individual step can stall indefinitely.  Individual reads have no per-call
    timeout; the outer cap is the only safety net.
    """
    url = f"rtsp://{host}:{port}/{stream_name}"
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=5.0
        )
    except (OSError, asyncio.TimeoutError) as exc:
        return False, f"connect failed: {exc}"

    # Shared read buffer — never lose bytes between RTSP method calls.
    _buf = bytearray()

    async def _read_response() -> str:
        """Read RTSP response headers (up to blank line); leave leftover bytes in _buf."""
        while b"\r\n\r\n" not in _buf:
            chunk = await reader.read(4096)
            if not chunk:
                raise ConnectionError("connection closed")
            _buf.extend(chunk)
        hdr_end = _buf.index(b"\r\n\r\n") + 4
        hdrs = bytes(_buf[:hdr_end])
        del _buf[:hdr_end]
        return hdrs.decode(errors="replace")

    async def _discard_body(headers: str) -> None:
        """Drain response body (Content-Length bytes) so the buffer stays clean."""
        cl = 0
        for line in headers.splitlines():
            if line.lower().startswith("content-length:"):
                cl = int(line.split(":", 1)[1].strip())
                break
        while len(_buf) < cl:
            chunk = await reader.read(4096)
            if not chunk:
                break
            _buf.extend(chunk)
        del _buf[:cl]

    cseq = 0
    session_id = ""   # populated once SETUP responds; used for TEARDOWN cleanup

    async def _send(method: str, req_url: str = "", extra_headers: dict | None = None) -> str:
        nonlocal cseq
        cseq += 1
        target = req_url or url
        lines = [
            f"{method} {target} RTSP/1.0",
            f"CSeq: {cseq}",
            "User-Agent: aio-neolink",
        ]
        for k, v in (extra_headers or {}).items():
            lines.append(f"{k}: {v}")
        lines += ["", ""]
        writer.write("\r\n".join(lines).encode())
        await writer.drain()
        return await _read_response()

    async def _run_probe() -> tuple[bool, str]:
        nonlocal session_id

        # OPTIONS — confirm the server is alive.
        resp = await _send("OPTIONS")
        if "RTSP/1.0 200" not in resp:
            return False, f"OPTIONS failed: {resp[:120]}"
        await _discard_body(resp)

        # DESCRIBE — get SDP so we know the track control path.
        resp = await _send("DESCRIBE", extra_headers={"Accept": "application/sdp"})
        if "RTSP/1.0 200" not in resp:
            return False, f"DESCRIBE failed: {resp[:120]}"

        # Collect SDP body before parsing so it doesn't bleed into the next read.
        cl = 0
        for line in resp.splitlines():
            if line.lower().startswith("content-length:"):
                cl = int(line.split(":", 1)[1].strip())
                break
        while len(_buf) < cl:
            chunk = await reader.read(4096)
            if not chunk:
                break
            _buf.extend(chunk)
        sdp_text = bytes(_buf[:cl]).decode(errors="replace")
        del _buf[:cl]

        # Parse the first track-level a=control line from the SDP.
        # GStreamer RTSP servers emit "a=control:stream=0"; other servers use
        # "a=control:trackID=0".  The session-level wildcard "a=control:*" is skipped.
        track_url = url.rstrip("/") + "/stream=0"   # GStreamer default
        in_media = False
        for line in sdp_text.splitlines():
            line = line.strip()
            if line.startswith("m="):
                in_media = True
            if in_media and line.startswith("a=control:"):
                ctrl = line[len("a=control:"):].strip()
                if ctrl and ctrl != "*":
                    if ctrl.startswith("rtsp://"):
                        track_url = ctrl
                    else:
                        track_url = url.rstrip("/") + "/" + ctrl.lstrip("/")
                    break

        # SETUP — must target the track URL, not the session URL.
        resp = await _send(
            "SETUP",
            track_url,
            {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"},
        )
        if "RTSP/1.0 200" not in resp:
            return False, f"SETUP failed (track={track_url!r}): {resp[:120]}"
        await _discard_body(resp)

        # Extract Session header.
        for line in resp.splitlines():
            if line.lower().startswith("session:"):
                session_id = line.split(":", 1)[1].strip().split(";")[0]
                break

        # PLAY — start the stream.
        play_headers: dict = {"Range": "npt=0.000-"}
        if session_id:
            play_headers["Session"] = session_id
        resp = await _send("PLAY", extra_headers=play_headers)
        if "RTSP/1.0 200" not in resp:
            return False, f"PLAY failed: {resp[:120]}"

        # Wait for interleaved RTP data.  Any byte arriving means frames are flowing.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            # Check the shared buffer first (might already have bytes from PLAY response).
            if _buf:
                return True, ""
            remaining = deadline - loop.time()
            try:
                byte = await asyncio.wait_for(reader.read(1), timeout=min(remaining, 1.0))
                if byte:
                    return True, ""
            except asyncio.TimeoutError:
                continue

        return False, f"no RTP data within {timeout:.0f}s (stream may be hung)"

    try:
        # Single outer cap: (timeout + 5) s covers the RTSP handshake + RTP wait.
        # No per-call read timeouts needed; this is the only safety net.
        return await asyncio.wait_for(_run_probe(), timeout=timeout + 5)
    except asyncio.TimeoutError:
        return False, f"RTSP probe timed out after {timeout + 5:.0f}s"
    except Exception as exc:  # noqa: BLE001
        return False, f"probe error: {exc}"
    finally:
        # Always TEARDOWN a session we opened. Closing the TCP socket without it
        # leaves the session dangling server-side until its own timeout — cheap
        # to avoid, and removes any chance of session buildup affecting later
        # probes or other RTSP clients (e.g. Frigate) on the same camera.
        if session_id:
            try:
                writer.write(
                    f"TEARDOWN {url} RTSP/1.0\r\nCSeq: {cseq + 1}\r\n"
                    f"Session: {session_id}\r\nUser-Agent: aio-neolink\r\n\r\n".encode()
                )
                await asyncio.wait_for(writer.drain(), timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Pipeline manager
# ---------------------------------------------------------------------------

class PipelineManager:
    """Owns the single Neolink process and the watchdog loop over all cameras."""

    def __init__(self, opts: SupervisorOptions) -> None:
        self.opts = opts
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._cameras: list[Camera] = []
        self._health: dict[str, PipelineHealth] = {}
        self._last_restart: float = 0.0
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()

    # --- process lifecycle --------------------------------------------------------

    async def apply(self, cameras: list[Camera]) -> None:
        """Regenerate config and (re)start Neolink."""
        async with self._lock:
            self._cameras = cameras
            self._health = {
                c.name: self._health.get(c.name, PipelineHealth(camera_name=c.name))
                for c in cameras if c.enabled
            }
            config_gen.write(cameras)
            await self._restart_locked(reason="config applied")

    async def _start_locked(self) -> None:
        if not any(c.enabled for c in self._cameras):
            log.info("no enabled cameras; not starting neolink")
            return
        log.info("starting neolink")
        self._proc = await asyncio.create_subprocess_exec(
            NEOLINK_BIN, "rtsp", "--config", str(config_gen.NEOLINK_CONFIG),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        asyncio.create_task(self._drain_logs(self._proc))

    async def _restart_locked(self, reason: str) -> None:
        now = time.monotonic()
        if now - self._last_restart < self.opts.restart_backoff:
            log.warning("restart requested (%s) but within backoff window; skipping", reason)
            return
        self._last_restart = now
        await self._stop_locked()
        await self._start_locked()
        log.info("neolink (re)started: %s", reason)

    async def _stop_locked(self) -> None:
        if self._proc and self._proc.returncode is None:
            log.info("stopping neolink (SIGTERM)")
            try:
                self._proc.send_signal(signal.SIGTERM)
                await asyncio.wait_for(self._proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                log.warning("neolink did not exit on SIGTERM; sending SIGKILL")
                self._proc.kill()
                await self._proc.wait()
        self._proc = None

    async def _drain_logs(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            # Neolink polls Reolink's cloud push-notification server; this fails in
            # local-only setups and would otherwise spam the log every 4 seconds.
            if "pushnoti" in line and "Issue connecting" in line:
                continue
            log.info("[neolink] %s", line)
        rc = await proc.wait()
        log.warning("neolink exited with code %s", rc)

    # --- watchdog -----------------------------------------------------------------

    async def run_watchdog(self) -> None:
        """Probe every enabled camera; restart the pipeline on sustained failure.

        This converts the ~29-hour silent outage into a ~1-minute self-heal by using
        a real RTP probe (not just a TCP connect) to detect the exact hung-stream
        failure mode.
        """
        if self.opts.watchdog_timeout <= 0:
            log.info("watchdog disabled (watchdog_timeout=0)")
            return
        log.info(
            "watchdog active: RTSP+RTP probe every %ss, restart after %s consecutive "
            "failures or %ss of total silence",
            self.opts.health_interval,
            self.opts.failures_before_restart,
            self.opts.watchdog_timeout,
        )
        while not self._stop.is_set():
            await asyncio.sleep(self.opts.health_interval)
            await self._probe_all()

    async def _probe_all(self) -> None:
        if not self._cameras:
            return

        # Don't count probe failures during startup — Neolink needs time to negotiate
        # with the camera before its RTSP server is fully serving frames.
        in_grace = (time.monotonic() - self._last_restart) < self.opts.startup_grace

        unhealthy: list[str] = []

        for cam in self._cameras:
            if not cam.enabled:
                continue

            # Probe a concrete stream path.  GStreamer RTSP aggregate URLs can behave
            # differently from track-specific paths; mainStream/subStream are always
            # listed explicitly by Neolink and respond reliably.
            if cam.stream == "subStream":
                probe_path = f"{cam.name}/subStream"
            else:
                probe_path = f"{cam.name}/mainStream"

            ok, err = await _probe_rtp(
                self.opts.rtsp_host,
                self.opts.rtsp_port,
                probe_path,
                _PROBE_TIMEOUT,
            )
            h = self._health.setdefault(cam.name, PipelineHealth(camera_name=cam.name))
            if ok:
                h.last_ok = time.monotonic()
                h.consecutive_failures = 0
                h.healthy = True
                h.last_error = ""
            else:
                if in_grace:
                    # Log but don't count as failure during startup window.
                    log.debug(
                        "camera %s probe during startup grace (%s) — not counting",
                        cam.name, err,
                    )
                    continue
                h.consecutive_failures += 1
                h.healthy = False
                h.last_error = err
                silent_for = time.monotonic() - h.last_ok if h.last_ok else 1e9
                log.warning(
                    "camera %s probe failed: %s — %d in a row, silent %.0fs",
                    cam.name, err, h.consecutive_failures, silent_for,
                )
                if (h.consecutive_failures >= self.opts.failures_before_restart
                        or silent_for >= self.opts.watchdog_timeout):
                    unhealthy.append(cam.name)

        if unhealthy:
            async with self._lock:
                await self._restart_locked(reason=f"unhealthy cameras: {unhealthy}")
            # Give each restarted camera a clean failure budget. Without this, a
            # single post-restart hiccup (the new process is still settling) adds
            # to the stale pre-restart count and instantly re-triggers another
            # restart — a self-sustaining restart loop that never recovers, since
            # only a *successful* probe would otherwise clear the counter.
            for name in unhealthy:
                h = self._health.get(name)
                if h:
                    h.consecutive_failures = 0

    def health_snapshot(self) -> dict[str, dict]:
        return {
            name: {
                "healthy": h.healthy,
                "consecutive_failures": h.consecutive_failures,
                "seconds_since_ok": (time.monotonic() - h.last_ok) if h.last_ok else None,
                "last_error": h.last_error,
            }
            for name, h in self._health.items()
        }

    async def shutdown(self) -> None:
        self._stop.set()
        async with self._lock:
            await self._stop_locked()
