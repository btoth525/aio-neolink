# aio-neolink

A Home Assistant OS add-on that bridges **Reolink Baichuan (port 9000) cameras** to
RTSP for Frigate / go2rtc / Blue Iris — and adds the things stock Neolink never had:
a **self-healing supervisor** so streams stop silently dying, a **REST + web GUI**
for adding cameras without hand-editing TOML, and **full camera control** (PTZ,
spotlight, siren, IR, audio) through Reolink's officially-authorized `reolink-aio`
library.

It fuses two projects that are each good at one half of the problem:

| Layer | Project | Job |
|-------|---------|-----|
| Video pipeline | **Neolink** (Rust + GStreamer) | Pull the H.264/H.265 Baichuan video stream, serve it as RTSP on `:8554`. |
| Control plane | **reolink-aio** (Python, Reolink-authorized) | Probe camera capabilities, PTZ, lights, siren, IR, motion/AI events. |
| Supervisor + GUI | **aio-neolink** (this repo) | Health-watchdog the video pipeline, generate Neolink config, expose a web UI + API. |

---

## Why this exists

Stock Neolink works until its process silently wedges: the container still reports
`started`, but the RTSP server stops handing out frames. Home Assistant's add-on
watchdog only catches a *fully dead* process, not a hung one — so the camera can sit
`unavailable` for many hours before anyone notices.

aio-neolink's supervisor closes that gap. It actively probes the RTSP output and, when
a stream goes quiet past a threshold, restarts **just the video pipeline** for that
camera and reconnects — typically within a minute, with no human in the loop.

---

## Architecture

```
                    ┌─────────────────────────────────────────────┐
                    │              aio-neolink add-on              │
                    │                                              │
  Reolink camera    │   ┌────────────┐        ┌────────────────┐   │
  (Baichuan :9000)  │   │  Neolink   │ RTSP   │   Supervisor    │  │
  ───────────────────►  │  (video)   ├──:8554─►│  + health probe │  │
        ▲           │   └────────────┘        └───────┬────────┘   │
        │           │                                 │            │
        │ control   │   ┌────────────┐        ┌───────▼────────┐   │
        └───────────┼───┤ reolink-aio│◄───────┤  FastAPI + GUI  │  │
          (PTZ,     │   │  (control) │  calls │  (add cameras)  │  │
           lights,  │   └────────────┘        └────────────────┘   │
           siren)   │                                              │
                    └──────────────────┬───────────────────────────┘
                                       │ RTSP :8554/<camera>
                                       ▼
                                    Frigate
```

The RTSP output is unchanged from stock Neolink, so **Frigate needs no
reconfiguration** — point it at `rtsp://<addon-host>:8554/<camera_name>` exactly as
before.

---

## Status

This is a **v0.1 scaffold** — a working skeleton with the architecture in place and
the reliability fix implemented. It is meant to be cloned, built, and iterated on.
See `ROADMAP.md` for what's stubbed vs. done.

## Repo layout

```
aio-neolink/
├── README.md
├── ROADMAP.md
├── repository.yaml              # HA add-on store repository descriptor
└── aio-neolink/                 # the add-on itself
    ├── config.yaml              # add-on manifest (ports, options, schema)
    ├── Dockerfile               # multi-stage: neolink binary + python control plane
    ├── build.yaml               # base images per arch
    ├── run.sh                   # entrypoint: starts supervisor
    ├── supervisor/
    │   ├── __init__.py
    │   ├── main.py              # orchestrates pipelines + API, owns the event loop
    │   ├── pipeline.py          # wraps a Neolink process per camera + health watchdog
    │   ├── config_gen.py        # turns the camera DB into neolink.toml
    │   ├── control.py           # reolink-aio wrapper: probe, PTZ, lights, siren
    │   ├── store.py             # camera persistence (JSON in /data)
    │   └── api.py               # FastAPI app: REST + serves the GUI
    └── web/
        └── static/
            ├── index.html       # the "add a camera" GUI
            ├── app.js
            └── style.css
```

## License

Neolink is GPL-3.0; reolink-aio is its own license. Keep this repo GPL-3.0-compatible.
