"""Video pipeline supervision + the self-healing watchdog.

This is the module that fixes the failure that caused a ~29-hour outage: Neolink's
process stays alive (container reports "started") but its RTSP output silently stops
producing frames, so cameras sit `unavailable` with no automatic recovery.

Why a simple probe would fail
------------------------------
A TCP-connect or RTSP OPTIONS check reports "healthy" even during the original hang
— Neolink's RTSP server keeps listening and responding to session setup, it just
stops sending RTP. So health has to be judged by whether RTP bytes are actually
flowing, not by whether the port answers.

Why the watchdog doesn't connect to Neolink directly at all
-------------------------------------------------------------
Two escalating fixes were tried and both proved insufficient before the real cause
was found. First, the watchdog reconnected to each camera's stream every
`health_interval` seconds (SETUP → PLAY → TEARDOWN, repeated) — that worked with a
single steady client (VLC) but crashed Neolink once a second real client (Frigate)
was also connected continuously, surfacing as a flood of
`gst_poll_write_control: assertion 'set != NULL' failed` followed by the process
dying. Second, the watchdog was rewritten to hold ONE persistent connection instead
of reconnecting on a cycle — but the exact same crash still happened. That proved
the bug isn't about reconnect churn: Neolink's RTSP server (this build) appears
unable to safely serve more than one simultaneous client on the same camera at all,
churn or not.

The real fix is architectural, in restream.py / restream_gen.py: go2rtc (the same
restream server Frigate already bundles) sits between Neolink and everyone else.
Neolink is moved to a localhost-only, internal port and go2rtc is its ONLY client,
ever — a role go2rtc is purpose-built for and Neolink has now proven it can sustain.
go2rtc republishes each camera on the same public port/URL this add-on has always
used, so Frigate needs zero reconfiguration. This module's watchdog then watches
go2rtc's public endpoint instead of Neolink directly, which both keeps the
persistent-connection design (still the right way to detect a silent hang) and
means the watchdog itself is just one more of go2rtc's fan-out clients — exactly
the scenario go2rtc is designed to handle safely, unlike Neolink's own RTSP server.

Recovery
--------
1. Sustained silence on a camera's persistent connection (`watchdog_timeout`
   seconds) → restart Neolink (same action as the manual restart that fixed the
   original incident).
2. Neolink exiting on its own (crash, not a hang) → restart immediately, without
   waiting for the silence timer — a dead process is not a maybe.
3. `restart_backoff` prevents restart storms when a camera is genuinely offline.
4. go2rtc restarts independently of Neolink (see restream.py) — a Neolink restart
   doesn't need to also drop every downstream client's connection to go2rtc.

Why a never-connected camera gets a longer leash than a hung one
------------------------------------------------------------------
Confirmed directly against Neolink's source (src/rtsp/mod.rs, src/rtsp/gst/factory.rs,
src/rtsp/factory.rs at the pinned commit): Neolink registers a dummy RTSP mount for
each camera immediately at startup (the "Available at ..." log line), but that mount
intentionally answers DESCRIBE with 404 until an internal "learning phase" completes
— it buffers frames from the camera until it has >10 frames or knows both the video
and audio codec, and only then can it build the real GStreamer pipeline a DESCRIBE
needs. OPTIONS succeeds immediately (it only needs the mount to exist); DESCRIBE
404s until learning finishes. This is normal Neolink behavior, not a hang.

The watchdog restarting Neolink because a camera has never yet completed a
handshake is actively self-defeating: a restart throws away the learning-phase
progress and starts it over, which can create an infinite loop where the camera
never gets long enough between restarts to finish learning — this was confirmed
live in supervisor logs (`neolink exited with code -15` on a ~120s cycle, i.e.
exactly `watchdog_timeout`) and produced the symptom "Frigate got an initial
stream but it didn't hold" — Frigate happened to catch a brief post-learning
window right before the next forced restart. `first_connect_timeout` gives a
camera that's never connected a much longer window before the watchdog gives up
and restarts it, while `watchdog_timeout` still applies at full strength once a
camera has proven it can stream and then goes silent (the original incident).
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

# Seconds between a monitor's own reconnect attempts after its persistent session
# drops or fails to establish. Deliberately small and independent of
# restart_backoff — a monitor retrying its own connection is normal operation
# (e.g. while Neolink is still negotiating with the camera after a restart), not a
# process-level restart, so it doesn't need the same caution.
_MONITOR_RECONNECT_BACKOFF = 5.0

# How long the initial RTSP handshake (OPTIONS/DESCRIBE/SETUP/PLAY) may take before
# a connection attempt is abandoned and retried. Once PLAY succeeds, the connection
# is held open indefinitely — this bound only covers session setup.
_HANDSHAKE_TIMEOUT = 15.0


@dataclass
class CameraHealth:
    camera_name: str
    connected: bool = False
    last_byte_at: float = 0.0    # monotonic time of the last byte seen on the persistent session
    last_error: str = ""
    healthy: bool = False
    ever_connected: bool = False  # has this camera EVER completed a handshake since it was added?


@dataclass
class SupervisorOptions:
    watchdog_timeout: int = 120      # seconds of *sustained* silence before a restart
    health_interval: int = 30        # seconds between watchdog silence checks
    # The watchdog watches go2rtc's PUBLIC endpoint, not Neolink directly — the same
    # URL Frigate uses. Neolink itself is on an internal, non-public port
    # (config_gen.py) and must only ever have go2rtc as a real client; connecting
    # straight to it here would defeat the entire point of the restream layer.
    #
    # A diagnostic deploy briefly pointed this at Neolink directly (18554) to rule
    # out a go2rtc-specific protocol gap; the DESCRIBE 404 persisted identically,
    # which rules go2rtc out too (see CLAUDE.md §7 for the full elimination list:
    # TOML bind address, base image, go2rtc, camera name, and connection
    # cleanliness have all been individually disproven as the cause — the actual
    # cause inside OUR build is still unknown). Restored to go2rtc's public port;
    # do not point this at Neolink directly again without a fresh reason.
    rtsp_host: str = "127.0.0.1"
    rtsp_port: int = 8554
    restart_backoff: float = 5.0     # min seconds between full process restarts
    startup_grace: float = 45.0      # seconds after (re)start before silence counts
    # Neolink's RTSP mount answers DESCRIBE with a transient 404 until its internal
    # "learning phase" (buffering frames to detect video/audio codecs) completes —
    # this is normal, documented Neolink behavior, not a hang (see CLAUDE.md §7).
    # A camera that has NEVER completed a handshake since being added is still in
    # (or stuck before) that learning phase, not necessarily hung the way the
    # original incident's silent-RTP failure was. Restarting Neolink mid-learning-
    # phase throws away that progress and restarts the phase from zero, so a short
    # watchdog_timeout on top of a never-connected camera can loop forever without
    # ever letting the phase finish. Give first-connection a much longer leash than
    # a genuine "was streaming, went silent" hang.
    first_connect_timeout: int = 600  # seconds of never-connected before restarting


# ---------------------------------------------------------------------------
# Per-camera persistent RTSP session monitor
# ---------------------------------------------------------------------------

class _CameraMonitor:
    """Holds one long-lived RTSP session against a camera's stream and tracks
    whether bytes are still arriving on it.

    Connects once (OPTIONS → DESCRIBE → SETUP → PLAY), then just reads from the
    socket in a loop, updating `last_byte_at` on every chunk. If the connection
    drops, it reconnects after a short backoff. This is the entire probe — no
    periodic reconnect churn, so Neolink sees this as one steady client for as
    long as the stream stays healthy, the same footprint a single VLC connection
    has.
    """

    def __init__(self, host: str, port: int, stream_name: str, health: CameraHealth) -> None:
        self.host = host
        self.port = port
        self.stream_name = stream_name
        self.health = health
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self._connect_and_watch()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.health.last_error = str(exc)
            self.health.connected = False
            await asyncio.sleep(_MONITOR_RECONNECT_BACKOFF)

    async def _connect_and_watch(self) -> None:
        url = f"rtsp://{self.host}:{self.port}/{self.stream_name}"
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=5.0
        )
        try:
            await asyncio.wait_for(
                self._handshake(reader, writer, url), timeout=_HANDSHAKE_TIMEOUT
            )
            # PLAY succeeded — treat this moment as the baseline. A genuinely dead
            # stream will simply never advance last_byte_at again; a healthy one
            # advances it continuously below.
            self.health.connected = True
            self.health.ever_connected = True
            self.health.last_byte_at = time.monotonic()
            self.health.last_error = ""

            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    raise ConnectionError("connection closed")
                self.health.last_byte_at = time.monotonic()
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    async def _handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, url: str) -> None:
        """OPTIONS → DESCRIBE → SETUP → PLAY. Raises on any non-200 or malformed step."""
        _buf = bytearray()
        cseq = 0

        async def _read_response() -> str:
            while b"\r\n\r\n" not in _buf:
                chunk = await reader.read(4096)
                if not chunk:
                    raise ConnectionError("connection closed during handshake")
                _buf.extend(chunk)
            hdr_end = _buf.index(b"\r\n\r\n") + 4
            hdrs = bytes(_buf[:hdr_end])
            del _buf[:hdr_end]
            return hdrs.decode(errors="replace")

        async def _discard_body(headers: str) -> None:
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

        async def _send(method: str, req_url: str = "", extra_headers: dict | None = None) -> str:
            nonlocal cseq
            cseq += 1
            lines = [
                f"{method} {req_url or url} RTSP/1.0",
                f"CSeq: {cseq}",
                "User-Agent: aio-neolink",
            ]
            for k, v in (extra_headers or {}).items():
                lines.append(f"{k}: {v}")
            lines += ["", ""]
            writer.write("\r\n".join(lines).encode())
            await writer.drain()
            return await _read_response()

        # OPTIONS
        resp = await _send("OPTIONS")
        if "RTSP/1.0 200" not in resp:
            raise RuntimeError(f"OPTIONS failed: {resp[:120]}")
        await _discard_body(resp)

        # DESCRIBE — need the SDP to find the track control path.
        resp = await _send("DESCRIBE", extra_headers={"Accept": "application/sdp"})
        if "RTSP/1.0 200" not in resp:
            raise RuntimeError(f"DESCRIBE failed: {resp[:120]}")
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

        # GStreamer RTSP servers emit "a=control:stream=0"; others use "trackID=0".
        # The session-level wildcard "a=control:*" is skipped.
        track_url = url.rstrip("/") + "/stream=0"
        in_media = False
        for line in sdp_text.splitlines():
            line = line.strip()
            if line.startswith("m="):
                in_media = True
            if in_media and line.startswith("a=control:"):
                ctrl = line[len("a=control:"):].strip()
                if ctrl and ctrl != "*":
                    track_url = ctrl if ctrl.startswith("rtsp://") else url.rstrip("/") + "/" + ctrl.lstrip("/")
                    break

        # SETUP must target the track URL, not the session URL.
        resp = await _send("SETUP", track_url, {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"})
        if "RTSP/1.0 200" not in resp:
            raise RuntimeError(f"SETUP failed (track={track_url!r}): {resp[:120]}")
        await _discard_body(resp)

        session_id = ""
        for line in resp.splitlines():
            if line.lower().startswith("session:"):
                session_id = line.split(":", 1)[1].strip().split(";")[0]
                break

        play_headers: dict = {"Range": "npt=0.000-"}
        if session_id:
            play_headers["Session"] = session_id
        resp = await _send("PLAY", extra_headers=play_headers)
        if "RTSP/1.0 200" not in resp:
            raise RuntimeError(f"PLAY failed: {resp[:120]}")
        # Any bytes left in _buf after PLAY's response are the start of the RTP
        # stream; the read loop in _connect_and_watch will see them on its next
        # read() naturally (they're still in the socket's kernel buffer — the
        # small amount already pulled into `_buf` here is simply not double
        # counted, which is harmless since _connect_and_watch sets its own
        # baseline timestamp right after this returns).


# ---------------------------------------------------------------------------
# Pipeline manager
# ---------------------------------------------------------------------------

class PipelineManager:
    """Owns the single Neolink process and the watchdog loop over all cameras."""

    def __init__(self, opts: SupervisorOptions) -> None:
        self.opts = opts
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._cameras: list[Camera] = []
        self._health: dict[str, CameraHealth] = {}
        self._monitors: dict[str, _CameraMonitor] = {}
        self._last_restart: float = 0.0
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        # Processes currently being torn down on purpose (config change, watchdog
        # restart). Tracked by id() so _drain_logs can tell a deliberate stop apart
        # from Neolink dying on its own (segfault, panic, OOM-kill) without racing
        # against a second stop/restart that happens to land while the first one's
        # log-drain task is still unwinding.
        self._intentionally_stopping: set[int] = set()

    # --- process lifecycle --------------------------------------------------------

    async def apply(self, cameras: list[Camera]) -> None:
        """Regenerate config, (re)start Neolink, and (re)build health monitors.

        Monitors are managed here — tied to the camera list changing — rather than
        inside _start_locked/_stop_locked. They watch go2rtc's public endpoint, not
        Neolink directly, and go2rtc is deliberately NOT restarted when Neolink is
        (see restream.py): a Neolink crash-restart shouldn't also tear down and
        rebuild the monitor's connection, since go2rtc absorbs that hiccup on its
        own. Monitors only need to change when the actual camera list does.
        """
        async with self._lock:
            self._cameras = cameras
            self._health = {
                c.name: self._health.get(c.name, CameraHealth(camera_name=c.name))
                for c in cameras if c.enabled
            }
            config_gen.write(cameras)
            await self._stop_monitors()
            await self._restart_locked(reason="config applied")
            self._start_monitors()

    async def _start_locked(self) -> None:
        if not any(c.enabled for c in self._cameras):
            log.info("no enabled cameras; not starting neolink")
            return
        log.info("starting neolink")
        # Neolink is a Rust binary using env_logger — it reads RUST_LOG, not this
        # add-on's own `log_level` option (that only configures the Python
        # supervisor's logging, via AIO_NEOLINK_LOG_LEVEL in main.py/run.sh).
        # Without this, Neolink silently stays at its own default verbosity no
        # matter what the add-on's log_level option is set to, which meant a
        # "trace" log_level never actually produced any more detail from Neolink
        # itself — a real gap while diagnosing why a stream stalls before its
        # first frame.
        env = dict(os.environ)
        aio_level = os.environ.get("AIO_NEOLINK_LOG_LEVEL", "info").lower()
        env["RUST_LOG"] = f"neolink={aio_level},neolink_core={aio_level}"
        self._proc = await asyncio.create_subprocess_exec(
            NEOLINK_BIN, "rtsp", "--config", str(config_gen.NEOLINK_CONFIG),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        asyncio.create_task(self._drain_logs(self._proc))

    def _start_monitors(self) -> None:
        for cam in self._cameras:
            if not cam.enabled:
                continue
            # Watch go2rtc's published name for this camera — the exact URL
            # Frigate uses — not Neolink directly. Neolink must only ever have
            # go2rtc as a client (see restream_gen.py for why).
            health = self._health.setdefault(cam.name, CameraHealth(camera_name=cam.name))
            monitor = _CameraMonitor(self.opts.rtsp_host, self.opts.rtsp_port, cam.name, health)
            monitor.start()
            self._monitors[cam.name] = monitor

    async def _stop_monitors(self) -> None:
        monitors, self._monitors = self._monitors, {}
        for monitor in monitors.values():
            await monitor.stop()

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
            self._intentionally_stopping.add(id(self._proc))
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
            # Neolink polls Reolink's cloud push-notification server; in local-only
            # setups this retries forever and would otherwise spam the log every few
            # seconds (both its "Issue connecting" failures and, at debug/trace, its
            # "Registering new push notification token" retry loop). Drop the whole
            # module's chatter — it's unrelated to streaming.
            if "pushnoti" in line:
                continue
            log.info("[neolink] %s", line)
        rc = await proc.wait()
        log.warning("neolink exited with code %s", rc)

        was_intentional = id(proc) in self._intentionally_stopping
        self._intentionally_stopping.discard(id(proc))
        if was_intentional:
            return

        # Neolink died on its own — a crash (segfault, internal panic, OOM-kill),
        # not the silent-hang failure mode the persistent-connection watchdog
        # exists to catch. The watchdog would eventually notice via a silent
        # monitor, but only after `watchdog_timeout` seconds. A dead process is
        # not a maybe — restart it immediately instead of waiting on that timer.
        if self._proc is proc:
            log.warning(
                "neolink exited unexpectedly (code %s) — restarting immediately, "
                "not waiting for the watchdog silence timer", rc,
            )
            async with self._lock:
                if self._proc is proc:
                    await self._restart_locked(reason=f"neolink exited unexpectedly (code {rc})")

    # --- watchdog -----------------------------------------------------------------

    async def run_watchdog(self) -> None:
        """Check every camera's persistent-connection monitor; restart Neolink on
        sustained silence.

        This converts the ~29-hour silent outage into a ~1-2 minute self-heal, using
        real RTP delivery (not just an open port) to detect the exact hung-stream
        failure mode — without ever opening more than one extra connection per
        camera, so the watchdog itself can't destabilize Neolink under load.
        """
        if self.opts.watchdog_timeout <= 0:
            log.info("watchdog disabled (watchdog_timeout=0)")
            return
        log.info(
            "watchdog active: checking for %ss of stream silence every %ss "
            "(one persistent RTSP connection per camera, no reconnect churn)",
            self.opts.watchdog_timeout, self.opts.health_interval,
        )
        while not self._stop.is_set():
            await asyncio.sleep(self.opts.health_interval)
            await self._check_all()

    async def _check_all(self) -> None:
        if not self._cameras:
            return

        # Don't judge silence during startup — Neolink needs time to negotiate with
        # the camera, and each monitor needs time to complete its own handshake,
        # before either failing to connect or falling silent means anything.
        in_grace = (time.monotonic() - self._last_restart) < self.opts.startup_grace
        if in_grace:
            return

        now = time.monotonic()
        unhealthy: list[str] = []

        for cam in self._cameras:
            if not cam.enabled:
                continue
            h = self._health.get(cam.name)
            if h is None:
                continue

            silent_for = now - h.last_byte_at if h.last_byte_at else now - self._last_restart
            if h.connected and silent_for < self.opts.watchdog_timeout:
                h.healthy = True
                continue

            # Distinguish "never connected yet" (still in Neolink's learning phase,
            # or waiting on it) from "was streaming, then went silent" (the actual
            # hang this watchdog exists to catch) — see first_connect_timeout above.
            threshold = (
                self.opts.watchdog_timeout
                if h.ever_connected
                else self.opts.first_connect_timeout
            )

            h.healthy = False
            if not h.last_error:
                h.last_error = "no RTP data received (stream may be hung)"
            log.warning(
                "camera %s unhealthy: %s — silent %.0fs (restart threshold %ss%s)",
                cam.name, h.last_error, silent_for, threshold,
                "" if h.ever_connected else ", never connected yet",
            )
            if silent_for >= threshold:
                unhealthy.append(cam.name)

        if unhealthy:
            async with self._lock:
                await self._restart_locked(reason=f"unhealthy cameras: {unhealthy}")

    def health_snapshot(self) -> dict[str, dict]:
        now = time.monotonic()
        return {
            name: {
                "healthy": h.healthy,
                "connected": h.connected,
                "seconds_since_ok": (now - h.last_byte_at) if h.last_byte_at else None,
                "last_error": h.last_error,
            }
            for name, h in self._health.items()
        }

    async def shutdown(self) -> None:
        self._stop.set()
        await self._stop_monitors()
        async with self._lock:
            await self._stop_locked()
