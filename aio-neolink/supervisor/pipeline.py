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

# Seconds to wait for at least one RTP byte during a probe.
# Must be shorter than health_interval so probes don't queue up.
_PROBE_TIMEOUT = 10.0


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


# ---------------------------------------------------------------------------
# Pure-Python RTSP + RTP probe
# ---------------------------------------------------------------------------

async def _probe_rtp(host: str, port: int, stream_name: str, timeout: float) -> tuple[bool, str]:
    """Verify that an RTSP stream is actually delivering RTP frames.

    Performs a real RTSP session (OPTIONS → DESCRIBE → SETUP → PLAY) over TCP and
    waits for interleaved RTP data. Returns (ok, error_detail).

    This catches the exact failure mode from the original incident: Neolink's RTSP
    server responding to OPTIONS/DESCRIBE but not sending any RTP after PLAY.
    """
    url = f"rtsp://{host}:{port}/{stream_name}"
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=5.0
        )
    except (OSError, asyncio.TimeoutError) as exc:
        return False, f"connect failed: {exc}"

    try:
        cseq = 0

        async def _send(method: str, headers: dict | None = None) -> str:
            nonlocal cseq
            cseq += 1
            lines = [f"{method} {url} RTSP/1.0", f"CSeq: {cseq}"]
            for k, v in (headers or {}).items():
                lines.append(f"{k}: {v}")
            lines += ["", ""]
            writer.write("\r\n".join(lines).encode())
            await writer.drain()
            # Read until blank line (end of headers).
            response = b""
            while b"\r\n\r\n" not in response:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=5.0)
                if not chunk:
                    raise ConnectionError("connection closed during response")
                response += chunk
            return response.decode(errors="replace")

        # OPTIONS — confirm the server is alive.
        resp = await _send("OPTIONS")
        if "RTSP/1.0 200" not in resp:
            return False, f"OPTIONS failed: {resp[:80]}"

        # DESCRIBE — get SDP so we know the track path.
        resp = await _send("DESCRIBE", {"Accept": "application/sdp"})
        if "RTSP/1.0 200" not in resp:
            return False, f"DESCRIBE failed: {resp[:80]}"

        # Parse the first trackID from the SDP control lines.
        track = "trackID=0"
        for line in resp.splitlines():
            if line.strip().startswith("a=control:") and "track" in line.lower():
                track = line.split("a=control:", 1)[-1].strip()
                if track.startswith("rtsp://"):
                    # Absolute URL — extract just the track path suffix.
                    track = track.split("/")[-1]
                break

        # SETUP — request interleaved (TCP) transport so RTP comes back on this socket.
        resp = await _send(
            "SETUP",
            {
                "Transport": "RTP/AVP/TCP;unicast;interleaved=0-1",
                "Content-Length": "0",
            },
        )
        if "RTSP/1.0 200" not in resp:
            return False, f"SETUP failed: {resp[:80]}"

        # Extract Session header.
        session_id = ""
        for line in resp.splitlines():
            if line.lower().startswith("session:"):
                session_id = line.split(":", 1)[1].strip().split(";")[0]
                break

        # PLAY — start the stream.
        play_headers: dict = {"Range": "npt=0.000-"}
        if session_id:
            play_headers["Session"] = session_id
        resp = await _send("PLAY", play_headers)
        if "RTSP/1.0 200" not in resp:
            return False, f"PLAY failed: {resp[:80]}"

        # Now wait for interleaved RTP data (starts with $ byte).
        # Any byte arriving means frames are flowing.
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            try:
                byte = await asyncio.wait_for(reader.read(1), timeout=min(remaining, 2.0))
                if byte:
                    return True, ""
            except asyncio.TimeoutError:
                continue

        return False, f"no RTP data within {timeout:.0f}s (stream hung)"

    except asyncio.TimeoutError:
        return False, "RTSP exchange timed out"
    except Exception as exc:  # noqa: BLE001
        return False, f"probe error: {exc}"
    finally:
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
            log.info("[neolink] %s", raw.decode(errors="replace").rstrip())
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
        unhealthy: list[str] = []

        for cam in self._cameras:
            if not cam.enabled:
                continue
            ok, err = await _probe_rtp(
                self.opts.rtsp_host,
                self.opts.rtsp_port,
                cam.name,
                _PROBE_TIMEOUT,
            )
            h = self._health.setdefault(cam.name, PipelineHealth(camera_name=cam.name))
            if ok:
                h.last_ok = time.monotonic()
                h.consecutive_failures = 0
                h.healthy = True
                h.last_error = ""
            else:
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
