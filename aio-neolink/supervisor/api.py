"""HTTP surface — REST API + GUI hosting.

Exposed through HA Ingress (see config.yaml). Endpoints:

  GET    /api/cameras                 list cameras
  POST   /api/cameras                 add/update a camera (regenerates config, restarts pipeline)
  DELETE /api/cameras/{name}          remove a camera
  POST   /api/cameras/probe           probe an IP+creds, return capabilities (used by the GUI)
  GET    /api/health                  per-camera watchdog health
  POST   /api/cameras/{name}/control  PTZ / lights / siren / IR

There is deliberately no live-preview endpoint: an MJPEG preview opens its own RTSP
connection to Neolink, which was observed to land mid-negotiation and trip Neolink's
own RTSP pipeline ("could not create element", then dropped frames). Holding the
actual stream for Frigate matters more than a GUI thumbnail, so the GUI never opens
a second RTSP connection of its own.

The pipeline manager + control registry are injected by main.py via app.state.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .store import Camera, CameraStore
from .control import CameraControl, ControlError

log = logging.getLogger("aio-neolink.api")
WEB_DIR = Path(__file__).resolve().parent.parent / "web" / "static"


class CameraIn(BaseModel):
    name: str
    address: Optional[str] = None
    port: int = 9000
    uid: Optional[str] = None
    username: str = "admin"
    password: str = ""
    stream: str = "both"
    enabled: bool = True


class ProbeIn(BaseModel):
    address: str
    username: str = "admin"
    password: str = ""
    port: int = 9000


class ControlIn(BaseModel):
    action: str                       # "ir" | "spotlight" | "siren" | "ptz"
    on: Optional[bool] = None
    brightness: Optional[int] = None
    seconds: Optional[int] = None
    operation: Optional[str] = None   # for ptz
    speed: int = 32


def create_app(store: CameraStore, pipeline, restreamer, controls) -> FastAPI:
    app = FastAPI(title="aio-neolink", version="0.1.15")
    app.state.store = store
    app.state.pipeline = pipeline
    app.state.restreamer = restreamer
    app.state.controls = controls

    async def _reapply() -> None:
        cameras = store.list()
        # Neolink's config first, then go2rtc's — go2rtc's sources reference the
        # camera names/streams Neolink is about to serve.
        await pipeline.apply(cameras)
        await restreamer.apply(cameras)

    @app.get("/api/cameras")
    async def list_cameras():
        return [c.__dict__ for c in store.list()]

    @app.post("/api/cameras")
    async def upsert_camera(body: CameraIn):
        if not body.address and not body.uid:
            raise HTTPException(400, "Provide an address (wired) or a uid (battery camera).")
        cam = store.get(body.name) or Camera(name=body.name)
        for k, v in body.model_dump().items():
            setattr(cam, k, v)
        store.upsert(cam)
        await _reapply()
        return {"ok": True, "camera": cam.__dict__}

    @app.delete("/api/cameras/{name}")
    async def delete_camera(name: str):
        if not store.delete(name):
            raise HTTPException(404, f"No camera named {name!r}.")
        await controls.forget(name)
        await _reapply()
        return {"ok": True}

    @app.post("/api/cameras/probe")
    async def probe_camera(body: ProbeIn):
        """Used by the GUI's "Test connection" button to auto-fill capabilities."""
        temp = Camera(name="__probe__", address=body.address, port=body.port,
                      username=body.username, password=body.password)
        try:
            caps = await CameraControl(temp).probe()
        except ControlError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"Could not reach camera: {exc}")
        return {"ok": True, "capabilities": caps}

    @app.get("/api/health")
    async def health():
        return pipeline.health_snapshot()

    @app.post("/api/cameras/{name}/control")
    async def control(name: str, body: ControlIn):
        cam = store.get(name)
        if cam is None:
            raise HTTPException(404, f"No camera named {name!r}.")
        ctrl = controls.for_camera(cam)
        try:
            if body.action == "ir":
                await ctrl.set_ir(bool(body.on))
            elif body.action == "spotlight":
                await ctrl.set_spotlight(bool(body.on), body.brightness)
            elif body.action == "siren":
                await ctrl.set_siren(bool(body.on), body.seconds)
            elif body.action == "ptz":
                if not body.operation:
                    raise HTTPException(400, "ptz requires an 'operation'.")
                await ctrl.ptz(body.operation, body.speed)
            else:
                raise HTTPException(400, f"Unknown action {body.action!r}.")
        except ControlError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"Control failed: {exc}")
        return {"ok": True}

    # --- GUI ----------------------------------------------------------------------
    @app.get("/")
    async def index():
        return FileResponse(WEB_DIR / "index.html")

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    return app
