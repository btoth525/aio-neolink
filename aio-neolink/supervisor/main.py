"""aio-neolink supervisor entrypoint.

Wires everything together and owns the event loop:
  - load cameras from /data
  - start the Neolink pipeline for them
  - launch the health watchdog (the self-heal)
  - serve the FastAPI GUI/REST on the ingress port (visible in HA via "Open Web UI")

Logs go to stdout so they appear in the HA add-on Log tab.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

import uvicorn

from . import api, control, pipeline, store

OPTIONS_FILE = Path("/data/options.json")
INGRESS_PORT = int(os.environ.get("INGRESS_PORT", "8099"))


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
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stdout,   # HA log tab reads stdout
        force=True,
    )
    # Quiet down noisy third-party loggers.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


async def main() -> None:
    opts = load_options()
    level = os.environ.get("AIO_NEOLINK_LOG_LEVEL", opts.get("log_level", "info"))
    setup_logging(level)
    log = logging.getLogger("aio-neolink")

    log.info("=" * 60)
    log.info("aio-neolink starting")
    log.info("  log level      : %s", level)
    log.info("  data dir       : /data")
    log.info("  ingress port   : %d", INGRESS_PORT)
    log.info("=" * 60)

    cam_store = store.CameraStore()
    sup_opts = pipeline.SupervisorOptions(
        watchdog_timeout=int(opts.get("watchdog_timeout", 120)),
        health_interval=int(opts.get("health_interval", 30)),
    )
    pipe = pipeline.PipelineManager(sup_opts)
    controls = control.ControlRegistry()

    cameras = cam_store.list()
    log.info("loaded %d camera(s) from store", len(cameras))
    await pipe.apply(cameras)

    app = api.create_app(cam_store, pipe, controls)
    uvi_cfg = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=INGRESS_PORT,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(uvi_cfg)

    # Graceful shutdown on SIGTERM (what HA sends when stopping the add-on).
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _on_signal():
        log.info("shutdown signal received — stopping")
        shutdown_event.set()
        server.should_exit = True

    loop.add_signal_handler(signal.SIGTERM, _on_signal)
    loop.add_signal_handler(signal.SIGINT, _on_signal)

    watchdog_task = asyncio.create_task(pipe.run_watchdog(), name="watchdog")
    server_task   = asyncio.create_task(server.serve(), name="api-server")

    log.info("aio-neolink ready — watchdog active, Web UI on :%d", INGRESS_PORT)
    try:
        await asyncio.gather(watchdog_task, server_task)
    except asyncio.CancelledError:
        pass
    finally:
        log.info("shutting down pipeline and control connections")
        await pipe.shutdown()
        await controls.close_all()
        log.info("aio-neolink stopped cleanly")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
