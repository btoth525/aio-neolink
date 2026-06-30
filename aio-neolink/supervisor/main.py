"""aio-neolink supervisor entrypoint.

Wires everything together and owns the event loop:
  - load cameras from /data
  - start the Neolink pipeline for them
  - launch the health watchdog (the self-heal)
  - serve the FastAPI GUI/REST on the ingress port
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import uvicorn

from . import api, control, pipeline, store

OPTIONS_FILE = Path("/data/options.json")
INGRESS_PORT = 8099


def load_options() -> dict:
    if OPTIONS_FILE.exists():
        try:
            return json.loads(OPTIONS_FILE.read_text())
        except Exception:  # noqa: BLE001
            pass
    return {}


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def main() -> None:
    opts = load_options()
    setup_logging(os.environ.get("AIO_NEOLINK_LOG_LEVEL", opts.get("log_level", "info")))
    log = logging.getLogger("aio-neolink")

    cam_store = store.CameraStore()
    sup_opts = pipeline.SupervisorOptions(
        watchdog_timeout=int(opts.get("watchdog_timeout", 45)),
        health_interval=int(opts.get("health_interval", 15)),
    )
    pipe = pipeline.PipelineManager(sup_opts)
    controls = control.ControlRegistry()

    # Bring up whatever cameras already exist.
    cameras = cam_store.list()
    log.info("loaded %d camera(s) from store", len(cameras))
    await pipe.apply(cameras)

    # Build the API and run it alongside the watchdog.
    app = api.create_app(cam_store, pipe, controls)
    config = uvicorn.Config(app, host="0.0.0.0", port=INGRESS_PORT, log_level="warning")
    server = uvicorn.Server(config)

    watchdog_task = asyncio.create_task(pipe.run_watchdog(), name="watchdog")
    server_task = asyncio.create_task(server.serve(), name="api")

    log.info("aio-neolink up: API on :%d, watchdog running", INGRESS_PORT)
    try:
        await asyncio.gather(watchdog_task, server_task)
    finally:
        await pipe.shutdown()
        await controls.close_all()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
