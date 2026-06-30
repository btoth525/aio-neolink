"""Control plane — the reolink-aio wrapper.

This is the "features" half of aio-neolink. Neolink moves the video; reolink-aio does
everything else, using Reolink's officially-authorized API library. Connections are
created on demand and cached per camera.

Capabilities discovered here are also what powers the GUI: when you type an IP +
password, we probe the camera and report back its model, channels, and which features
(PTZ, spotlight, siren, IR) it actually supports — so the UI only shows controls that
work.

reolink-aio is async-first. See https://github.com/starkillerOG/reolink_aio
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .store import Camera

log = logging.getLogger("aio-neolink.control")

try:
    from reolink_aio.api import Host
except Exception:  # pragma: no cover - import guarded so scaffold imports without the dep
    Host = None  # type: ignore


class ControlError(RuntimeError):
    pass


class CameraControl:
    """Lazily-connected reolink-aio Host wrapper for one camera."""

    def __init__(self, cam: Camera) -> None:
        if Host is None:
            raise ControlError("reolink-aio is not installed in this image")
        if not cam.address:
            raise ControlError("control plane requires a wired address (battery cams TBD)")
        self.cam = cam
        self._host: Optional[Host] = None

    async def _connect(self) -> Host:
        if self._host is None:
            self._host = Host(self.cam.address, self.cam.username, self.cam.password)
            await self._host.get_host_data()
        return self._host

    async def probe(self) -> dict[str, Any]:
        """Discover model + capabilities. Cached into Camera.capabilities by the caller."""
        host = await self._connect()
        await host.get_states()
        ch = 0  # single-camera default; NVRs expose multiple channels
        caps = {
            "model": getattr(host, "model", None),
            "is_nvr": getattr(host, "is_nvr", False),
            "channels": getattr(host, "num_channels", 1),
            "mac": getattr(host, "mac_address", None),
            "supports_ptz": self._safe(host, "supported", ch, "ptz"),
            "supports_spotlight": self._safe(host, "supported", ch, "floodLight")
            or self._safe(host, "supported", ch, "spotlight"),
            "supports_siren": self._safe(host, "supported", ch, "siren"),
            "supports_ir": self._safe(host, "supported", ch, "ir_lights"),
        }
        return {k: v for k, v in caps.items() if v is not None}

    @staticmethod
    def _safe(host: Host, method: str, *args) -> Any:
        try:
            return getattr(host, method)(*args)
        except Exception:  # noqa: BLE001
            return None

    # --- controls -----------------------------------------------------------------
    async def set_ir(self, on: bool, channel: int = 0) -> None:
        host = await self._connect()
        await host.set_ir_lights(channel, on)

    async def set_spotlight(self, on: bool, brightness: Optional[int] = None, channel: int = 0) -> None:
        host = await self._connect()
        if brightness is not None:
            await host.set_spotlight(channel, on, brightness)
        else:
            await host.set_spotlight(channel, on)

    async def set_siren(self, on: bool, seconds: Optional[int] = None, channel: int = 0) -> None:
        host = await self._connect()
        if seconds is not None:
            await host.set_siren(channel, on, seconds)
        else:
            await host.set_siren(channel, on)

    async def ptz(self, operation: str, speed: int = 32, channel: int = 0) -> None:
        """operation: one of Left/Right/Up/Down/ZoomInc/ZoomDec/Stop (reolink-aio names)."""
        host = await self._connect()
        await host.set_ptz_command(channel, command=operation, speed=speed)

    async def close(self) -> None:
        if self._host is not None:
            try:
                await self._host.logout()
            finally:
                self._host = None


class ControlRegistry:
    """Caches one CameraControl per camera name."""

    def __init__(self) -> None:
        self._controls: dict[str, CameraControl] = {}

    def for_camera(self, cam: Camera) -> CameraControl:
        ctrl = self._controls.get(cam.name)
        if ctrl is None:
            ctrl = CameraControl(cam)
            self._controls[cam.name] = ctrl
        return ctrl

    async def forget(self, name: str) -> None:
        ctrl = self._controls.pop(name, None)
        if ctrl:
            await ctrl.close()

    async def close_all(self) -> None:
        for ctrl in list(self._controls.values()):
            await ctrl.close()
        self._controls.clear()
