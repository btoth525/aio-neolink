"""Owns the go2rtc subprocess — the restream layer between Neolink and the world.

See restream_gen.py for why this exists. This manager mirrors PipelineManager's
process-supervision pattern (start/stop/instant-restart-on-crash) but deliberately
does NOT restart go2rtc whenever Neolink restarts: go2rtc already reconnects to a
dropped upstream source on its own, so decoupling the two means Frigate's
connection to go2rtc can stay up even while Neolink cycles underneath it — a
meaningful reliability win on top of fixing the crash this layer was added for.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from pathlib import Path
from typing import Optional

from . import restream_gen
from .store import Camera

log = logging.getLogger("aio-neolink.restream")

GO2RTC_BIN = os.environ.get("GO2RTC_BIN", "/usr/local/bin/go2rtc")


class RestreamManager:
    """Starts/restarts go2rtc and regenerates its config when cameras change."""

    def __init__(self, restart_backoff: float = 5.0) -> None:
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()
        self._last_restart: float = 0.0
        self._restart_backoff = restart_backoff
        self._stop = asyncio.Event()
        self._intentionally_stopping: set[int] = set()

    async def apply(self, cameras: list[Camera]) -> None:
        """Regenerate go2rtc.yaml and (re)start go2rtc."""
        async with self._lock:
            restream_gen.write(cameras)
            await self._restart_locked(reason="config applied")

    async def _start_locked(self) -> None:
        if not restream_gen.GO2RTC_CONFIG.exists():
            log.info("no go2rtc config yet; not starting go2rtc")
            return
        log.info("starting go2rtc")
        self._proc = await asyncio.create_subprocess_exec(
            GO2RTC_BIN, "-config", str(restream_gen.GO2RTC_CONFIG),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        asyncio.create_task(self._drain_logs(self._proc))

    async def _restart_locked(self, reason: str) -> None:
        now = time.monotonic()
        if now - self._last_restart < self._restart_backoff:
            log.warning("go2rtc restart requested (%s) but within backoff window; skipping", reason)
            return
        self._last_restart = now
        await self._stop_locked()
        await self._start_locked()
        log.info("go2rtc (re)started: %s", reason)

    async def _stop_locked(self) -> None:
        if self._proc and self._proc.returncode is None:
            log.info("stopping go2rtc (SIGTERM)")
            self._intentionally_stopping.add(id(self._proc))
            try:
                self._proc.send_signal(signal.SIGTERM)
                await asyncio.wait_for(self._proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                log.warning("go2rtc did not exit on SIGTERM; sending SIGKILL")
                self._proc.kill()
                await self._proc.wait()
        self._proc = None

    async def _drain_logs(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            log.info("[go2rtc] %s", raw.decode(errors="replace").rstrip())
        rc = await proc.wait()
        log.warning("go2rtc exited with code %s", rc)

        was_intentional = id(proc) in self._intentionally_stopping
        self._intentionally_stopping.discard(id(proc))
        if was_intentional:
            return

        if self._proc is proc:
            log.warning("go2rtc exited unexpectedly (code %s) — restarting immediately", rc)
            async with self._lock:
                if self._proc is proc:
                    await self._restart_locked(reason=f"go2rtc exited unexpectedly (code {rc})")

    async def shutdown(self) -> None:
        self._stop.set()
        async with self._lock:
            await self._stop_locked()
