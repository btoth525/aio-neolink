"""HTTP surface — REST API + GUI hosting.

Exposed through HA Ingress (see config.yaml). Endpoints:

  GET    /api/cameras                 list cameras
  POST   /api/cameras                 add/update a camera (regenerates config, restarts pipeline)
  DELETE /api/cameras/{name}          remove a camera
  POST   /api/cameras/probe           probe an IP+creds, return capabilities (used by the GUI)
  GET    /api/health                  per-camera watchdog health
  POST   /api/cameras/{name}/control  PTZ / lights / siren / IR
  GET    /api/cameras/{name}/preview  live MJPEG preview (browsers can't play RTSP directly)

The pipeline manager + control registry are injected by main.py via app.state.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .store import Camera, CameraStore
from .control import CameraControl, ControlError

log = logging.getLogger("aio-neolink.api")
WEB_DIR = Path(__file__).resolve().parent.parent / "web" / "static"

# Live preview: pulls the sub-stream (lighter to decode than main) at a low frame
# rate, capped width, and modest JPEG quality — this is an "is it alive" thumbnail,
# not a viewing experience, so it should cost as little CPU as possible on whatever
# small box runs Home Assistant.
#
# We deliberately drive this with gst-launch-1.0 (gstreamer1.0-tools) rather than
# ffmpeg: ffmpeg ships its own libav* libraries that clash with the ones Neolink's
# GStreamer is built against, which broke Neolink's RTSP pipeline ("could not create
# element"). gst-launch reuses the exact same plugins, so the preview can't disturb
# the video path it's previewing.
_PREVIEW_FPS = 5
_PREVIEW_MAX_WIDTH = 480
_PREVIEW_JPEG_QUALITY = "65"          # gst jpegenc quality: 0 (worst) .. 100 (best)
_PREVIEW_RTSP_TCP_TIMEOUT_US = "5000000"  # 5s, rtspsrc tcp-timeout in microseconds


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


def create_app(store: CameraStore, pipeline, controls) -> FastAPI:
    app = FastAPI(title="aio-neolink", version="0.1.5")
    app.state.store = store
    app.state.pipeline = pipeline
    app.state.controls = controls

    async def _reapply() -> None:
        await pipeline.apply(store.list())

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

    @app.get("/api/cameras/{name}/preview")
    async def camera_preview(name: str):
        """Live MJPEG preview, transcoded from the local RTSP output.

        Browsers can't play RTSP directly, so gst-launch re-encodes a low-rate JPEG
        stream we frame into multipart/x-mixed-replace — which drops straight into an
        <img> tag with no client-side decoding. jpegenc emits a clean sequence of
        complete JPEGs, which we split on their SOI/EOI markers.
        """
        cam = store.get(name)
        if cam is None:
            raise HTTPException(404, f"No camera named {name!r}.")
        if not cam.enabled:
            raise HTTPException(409, f"{name!r} is disabled.")

        # Mirror the watchdog probe's stream selection: a camera pinned to
        # "mainStream" never has a sub-stream mount (Neolink doesn't request it
        # from the camera), so only prefer the lighter sub-stream when it exists.
        path = f"{name}/mainStream" if cam.stream == "mainStream" else f"{name}/subStream"
        rtsp_url = f"rtsp://{pipeline.opts.rtsp_host}:{pipeline.opts.rtsp_port}/{path}"

        # Select only the video pad (media=video) off rtspsrc so the camera's audio
        # stream is ignored rather than left dangling. The Reolink sub-stream is
        # H.264, so rtph264depay + avdec_h264 is correct; a main-only H.265 camera
        # would fail this pipeline and the preview degrades to "unavailable" (logged
        # below) without touching the actual RTSP feed.
        gst_args = [
            "gst-launch-1.0", "-q",
            "rtspsrc", f"location={rtsp_url}", "protocols=tcp",
            f"tcp-timeout={_PREVIEW_RTSP_TCP_TIMEOUT_US}", "latency=100", "name=src",
            "src.", "!", "application/x-rtp,media=video",
            "!", "rtph264depay", "!", "h264parse", "!", "avdec_h264",
            "!", "videoconvert",
            "!", "videorate", "!", f"video/x-raw,framerate={_PREVIEW_FPS}/1",
            "!", "videoscale", "!", f"video/x-raw,width={_PREVIEW_MAX_WIDTH}",
            "!", "jpegenc", f"quality={_PREVIEW_JPEG_QUALITY}",
            "!", "fdsink", "fd=1",
        ]

        async def generate():
            proc = await asyncio.create_subprocess_exec(
                *gst_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Drain stderr concurrently (so a full pipe never blocks gst) and keep the
            # last few lines for diagnostics if the preview produces nothing.
            stderr_tail: list[str] = []

            async def _drain_stderr():
                assert proc.stderr is not None
                async for raw in proc.stderr:
                    stderr_tail.append(raw.decode(errors="replace").rstrip())
                    del stderr_tail[:-10]

            stderr_task = asyncio.create_task(_drain_stderr())
            frames = 0
            buf = bytearray()
            try:
                assert proc.stdout is not None
                while True:
                    chunk = await proc.stdout.read(4096)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    if len(buf) > 2_000_000:
                        # Non-JPEG bytes accumulating; don't grow unbounded.
                        del buf[:-200_000]
                    while True:
                        start = buf.find(b"\xff\xd8")
                        end = buf.find(b"\xff\xd9", start + 2) if start != -1 else -1
                        if start == -1 or end == -1:
                            break
                        end += 2
                        frame = bytes(buf[start:end])
                        del buf[:end]
                        frames += 1
                        yield (
                            b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                            + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n"
                        )
            finally:
                stderr_task.cancel()
                if proc.returncode is None:
                    try:
                        proc.kill()
                        await proc.wait()
                    except ProcessLookupError:
                        pass
                if frames == 0:
                    log.warning(
                        "preview for %s produced no frames (stream %s); gst said: %s",
                        name, path, " | ".join(stderr_tail[-4:]) or "<no output>",
                    )

        return StreamingResponse(
            generate(), media_type="multipart/x-mixed-replace; boundary=frame"
        )

    # --- GUI ----------------------------------------------------------------------
    @app.get("/")
    async def index():
        return FileResponse(WEB_DIR / "index.html")

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    return app
