# CLAUDE.md — aio-neolink handoff

> Read this first. It is the single source of truth for what this project is, why it
> exists, what's done, and what to do next. If you're a fresh Claude (or Claude Code)
> session picking this up: everything you need to continue is here. Update this file as
> the project moves.

---

## 1. Mission

Build **aio-neolink**: a Home Assistant OS add-on that bridges Reolink **Baichuan
(port 9000)** cameras to RTSP for Frigate — and fixes the two things stock Neolink
never solved:

1. **Reliability.** Streams must not silently die. When a feed hangs, the add-on
   recovers it on its own, fast, with no human noticing.
2. **Features + usability.** Full camera control (PTZ, spotlight, siren, IR, eventually
   two-way audio) and a **web GUI** to add cameras instead of hand-editing TOML.

The output RTSP must stay drop-in compatible with the existing setup so **Frigate needs
no reconfiguration**.

The owner's words: *"make my own like aio-neolink… all of the features that work…
cameras don't drop anymore and just work perfect, PTZ, audio, then combine into stream
for Frigate."*

---

## 2. Why this exists (the originating incident)

On a live HA system, the camera `camera.movie_room` (a Frigate camera fed by the
`Neolink-latest` add-on, slug `a14d3924_neolink-latest`) went `unavailable` and stayed
that way for ~29 hours. Root cause: the Neolink **process was still alive** (the add-on
container reported `started`) but its RTSP output had **silently stopped producing
frames** — a hang, not a crash. HA's add-on watchdog only restarts a *fully dead*
process, so it never fired.

A manual add-on restart recovered the stream immediately: Neolink reconnected to the
camera at `192.168.1.236:9000` (Baichuan/TCP discovery) and re-served RTSP on `:8554`,
and Frigate's camera came back to `recording` within a minute.

**The whole project is a generalization of that one manual restart into an automatic,
self-healing system — plus the features and GUI to make it worth replacing Neolink.**

---

## 3. Environment facts (the real deployment this targets)

- **Camera in question:** `movie_room`, wired, at `192.168.1.236`, Baichuan port `9000`.
  Speaks the proprietary Baichuan protocol (no native RTSP/ONVIF on this model).
- **Consumer:** Frigate (HA add-on) pulls `rtsp://<neolink-host>:8554/movie_room` and
  produces `camera.movie_room`. There are ~12 cameras in HA total; several others are
  Frigate/go2rtc cameras unrelated to Neolink.
- **Current add-on being replaced:** `Neolink-latest` (`a14d3924_neolink-latest`),
  running the **QuantumEntangledAndy fork** of Neolink, reporting
  `neolink 0.6.3-rc.3`. RTSP mode, host network, no ingress, no HTTP API.
  Upstream Neolink is **abandoned** — the add-on's own description warns it may stop
  working at any time. That's the motivation to fork + harden.
- **HA platform:** Home Assistant OS (Supervised add-on model, Supervisor present).
- **Other infra available:** an Unraid server (could run Docker builds / host CI).

---

## 4. Architecture (the decision, and why)

Two mature projects each own one half; aio-neolink fuses them and adds the supervisor.

| Layer | Project | Role |
|-------|---------|------|
| **Video** | **Neolink** (Rust + GStreamer) | Pull H.264/H.265 Baichuan video, serve RTSP on `:8554`. Keep this — it's the hard, reverse-engineered part; don't reinvent it. |
| **Control** | **reolink-aio** (Python, *officially authorized by Reolink*, dev @starkillerOG; same lib HA's Reolink integration uses) | Probe model/capabilities, PTZ, spotlight, siren, IR, motion/AI events. |
| **Supervisor + GUI** | **aio-neolink** (this repo, Python + FastAPI) | Health-watchdog the video pipeline and auto-recover hangs; generate `neolink.toml`; serve REST + web GUI via HA Ingress. |

Data flow:

```
Reolink cam ──Baichuan :9000──► Neolink ──RTSP :8554/<name>──► Frigate
     ▲                                          ▲
     │ control (PTZ/lights/siren)               │ health probes + auto-restart
     └──────── reolink-aio ◄── FastAPI GUI ──── supervisor (watchdog)
```

Stack choice: **Python + FastAPI** (recommended and accepted) because reolink-aio is
Python-native and async-first, so the control plane and supervisor share one runtime.

---

## 5. Repo map (what each file is)

```
aio-neolink/
├── CLAUDE.md                    ← this file (handoff / source of truth)
├── README.md                    ← human-facing overview
├── ROADMAP.md                   ← milestones + done/stubbed status
├── repository.yaml              ← HA add-on store repo descriptor
└── aio-neolink/                 ← the add-on
    ├── config.yaml              ← manifest: ports (8554 RTSP, 8099 ingress GUI), options schema, host_network
    ├── build.yaml               ← per-arch base images (aarch64/amd64 debian bookworm)
    ├── Dockerfile               ← GStreamer runtime + Neolink binary + Python control plane
    ├── run.sh                   ← entrypoint → launches supervisor.main
    ├── rootfs/
    │   └── fetch-neolink.sh     ← downloads the CI-built Neolink rc.3 binary for BUILD_ARCH
    ├── supervisor/
    │   ├── main.py              ← wires store + pipeline + controls + watchdog + API; owns the event loop
    │   ├── pipeline.py          ← ★ the self-heal: runs Neolink, probes RTSP health, restarts on hang
    │   ├── config_gen.py        ← renders neolink.toml from the camera store
    │   ├── control.py           ← reolink-aio wrapper: probe / IR / spotlight / siren / PTZ
    │   ├── store.py             ← camera persistence (JSON in /data, chmod 600)
    │   ├── api.py               ← FastAPI: camera CRUD, probe, health, control; serves GUI
    │   └── requirements.txt
    └── web/static/
        ├── index.html           ← "add a camera" GUI
        ├── app.js               ← fetches cameras + health, renders cards, add/probe/control/delete
        └── style.css            ← "signal room" theme; cyan=live, amber=recovering, red=down
.github/workflows/
└── build-neolink.yml            ← CI: compiles Neolink rc.3 (amd64+arm64) → publishes to the
                                    'neolink-rc3' release; fetch-neolink.sh downloads from there
```

★ = the file that delivers the core promise.

---

## 6. Work order (do these in this order)

### 6.1 ✅ DONE (v0.1.8) — ship Neolink rc.3, built by this repo's own CI
`.github/workflows/build-neolink.yml` compiles Neolink from a **pinned master commit**
(`6e05e7844b5b…`, Cargo.toml version **0.6.3-rc.3**) inside a `rust:slim-bookworm`
container, natively for both amd64 and arm64 (no emulation), and publishes the
binaries to this repo's `neolink-rc3` release. `rootfs/fetch-neolink.sh` downloads
the asset matching `BUILD_ARCH` at image-build time — the **Home Assistant device
never compiles anything**, just downloads a ~12 MB binary.

Why rc.3, not the newest *release* (rc.2, commit `7158943`): on the target E1
(firmware `v3.2.0.4858`, 2025-08) rc.2 connects, logs in, and lists
`/movie-room/mainStream` etc. as available — but then **delivers RTP only
intermittently and hangs after ~30 s**. GStreamer's own `rtspsrc` (the proven client)
fails identically with *"Could not receive message (Timeout while waiting for server
response) … pipeline doesn't want to preroll"*, proving the fault is in Neolink, not
the watchdog probe. The original "Neolink-latest" add-on that worked on this exact
camera reported `0.6.3-rc.3` — i.e. it was a **source build of master**, which is
what CI now reproduces and publishes.

To move Neolink forward: bump `NEOLINK_COMMIT` in `build-neolink.yml`, push to `main`
(the workflow runs on every push to that file, or via `workflow_dispatch`), then
rebuild the add-on once the new release asset is live.

An earlier iteration (v0.1.6–0.1.7) compiled Neolink in a Dockerfile build stage
directly on the HA device — that approach still exists in git history if on-device
compilation is ever preferred over downloading a CI artifact.

### 6.2 Verify the two external contracts against reality
These were written from docs/memory and **must be checked against the actual versions
you ship** — they are the most likely sources of breakage:

- **Neolink TOML schema** (`config_gen.py`): currently emits a top-level `[[rtsp]]`
  bind block + `[[cameras]]` blocks. Older Neolink used a flatter `bind = "0.0.0.0:8554"`.
  Confirm against your exact binary and adjust `render()`.
- **reolink-aio API** (`control.py`): calls `Host.get_host_data()`, `get_states()`,
  `set_ir_lights`, `set_spotlight`, `set_siren`, `set_ptz_command(channel, command=, speed=)`,
  and `supported(channel, feature)`. Pin a reolink-aio version and confirm these
  signatures + the capability flag names (`floodLight` vs `spotlight`, `ir_lights`, `siren`,
  `ptz`) against a real camera. Reolink models differ in what they expose.

### 6.3 Prove the self-heal (acceptance test for the whole project)
Once it streams, simulate the **exact** original failure (a hang, not a crash):
```bash
# inside the add-on container
kill -STOP <neolink_pid>     # freeze the process — RTSP goes silent but PID lives
```
Watch the supervisor log. Within roughly `watchdog_timeout` (default 120s) it should
detect that its persistent per-camera RTSP connection has gone silent and restart the
pipeline, and the camera should return to `live` on its own. **If this passes,
the core mission is met.** Note the watchdog is deliberately tolerant: it restarts only on
*sustained* failure, because bouncing Neolink drops every client (Frigate included) and a
hair-trigger watchdog actively prevents a merely-flaky feed from settling.

### 6.4 Features pass
GUI probe auto-fills capabilities from a real camera; PTZ nudges, spotlight, siren, IR
all actuate. Wire any missing reolink-aio calls surfaced by 6.2.

### 6.5 The real reliability prize — fork Neolink, add an in-process frame watchdog
The supervisor's restart is currently **blunt**: it bounces the whole Neolink process
because stock Neolink exposes no per-camera control. Two upgrades, by payoff:
1. **In-process frame watchdog (highest value):** inside Neolink's GStreamer `appsrc`
   feed loop (the loop from George Hilliard's original blog that pushes camera bytes into
   the pipeline), track time-since-last-buffer. If it exceeds a threshold, force a
   camera reconnect *at the source* — catches the hang before any external probe notices.
2. **Per-camera control socket / MQTT:** let the supervisor restart one camera's pipeline
   instead of all of them. The QuantumEntangledAndy fork already has an MQTT control
   surface worth reusing.
After this, point `fetch-neolink.sh` at your own fork's release.

### 6.6 Two-way audio
Combine reolink-aio + Neolink's audio backchannel; surface a talk button in the GUI.

---

## 7. Constraints & gotchas

- **Host networking is required** (`host_network: true`): Neolink must reach the camera
  on `:9000` and bind RTSP on `:8554` like the original add-on.
- **GUI is served via HA Ingress** (`ingress_port: 8099`) — the front-end uses
  **relative** fetch paths so it works behind the Ingress path prefix. Keep them relative.
- **Credentials** live in `/data/cameras.json` (chmod 600). Treat as secret. A hardening
  follow-up is to move them into HA's secrets store.
- **Battery cameras** use UDP + a `uid` (no `address`). The store/TOML support `uid`, but
  `control.py` currently requires a wired address — battery control is a TODO.
- **Watchdog restart backoff** (`restart_backoff`, default 5s) prevents restart loops —
  don't remove it.
- **Nothing may repeatedly open/close RTSP connections against Neolink.** This has
  caused two separate real regressions, both traced to the same root cause: Neolink
  runs a *shared* per-camera GStreamer pipeline, and repeated attach/detach cycles
  from any client race with other concurrently-connected clients (Frigate) inside
  that shared pipeline, corrupting internal GStreamer state.
  1. v0.1.3–0.1.8 had a live MJPEG preview in the web GUI (ffmpeg, then
     `gst-launch`) that opened a fresh RTSP connection whenever the GUI rendered —
     removed entirely in v0.1.9 (`GStreamer-RTSP-Server-CRITICAL: could not create
     element`, dropped frames).
  2. The watchdog's own health probe originally reconnected every `health_interval`
     (SETUP → PLAY → TEARDOWN, repeated). Harmless alone, but once Frigate was
     *also* connected continuously, the probe's periodic churn crashed Neolink
     outright (`gst_poll_write_control: assertion 'set != NULL' failed`, flooding
     until the process died with SIGTRAP). Fixed by rearchitecting the watchdog
     (`pipeline.py`, `_CameraMonitor`) to hold **one persistent connection per
     camera** for the life of a healthy stream, and just watch it for silence —
     Neolink never sees more churn than a single steady client, matching what a
     lone VLC connection already proved was safe.

  **Do not reintroduce periodic reconnect/probe cycles or a GUI-side RTSP client**
  without first proving — with real logs, cameras connected via both this add-on
  AND Frigate simultaneously — that it can't destabilize the shared stream. Holding
  that stream is the entire point of this project.
- **Licensing:** Neolink is GPL-3.0. Keep this repo GPL-3.0-compatible.
- **Placeholders:** all replaced with the real owner (`btoth525`) across `config.yaml`,
  `repository.yaml`, `build.yaml`, and `README.md`. Nothing left to fill in.

---

## 8. What a Claude session should NOT do

- Don't rewrite the Baichuan video parsing in Python "to drop Neolink" — that's the
  hard reverse-engineered part; the architecture deliberately keeps Neolink for video.
- Don't replace the relative GUI paths with absolute ones (breaks Ingress).
- Don't make the watchdog restart on a brief silence — require `watchdog_timeout`
  seconds of *sustained* silence on the persistent per-camera connection before
  restarting. Restarting Neolink drops every connected client (Frigate included)
  and forces a cold camera re-negotiation, so an eager watchdog actively prevents a
  merely-flaky feed from ever stabilising.
- Don't add anything to the image — GUI feature or watchdog logic — that opens more
  than one RTSP connection per camera, or that reconnects on a cycle instead of
  holding a session open (see §7) — this has already caused two real regressions,
  including a full Neolink crash once a second client was present.
- Don't store secrets anywhere outside `/data`.

---

## 9. Quick reference

- RTSP for Frigate: `rtsp://<addon-host>:8554/<camera_name>` (unchanged from stock).
- Add-on options: `log_level`, `watchdog_timeout` (s, 0=off), `health_interval` (s).
- Key source-of-truth files for behavior: `pipeline.py` (recovery), `config_gen.py`
  (Neolink contract), `control.py` (reolink-aio contract).
- Original protocol background: George Hilliard, "Hacking Reolink cameras for fun and
  profit" (the post that created Neolink) — explains Baichuan, port 9000, the GStreamer
  `appsrc` video loop that §6.5.1 wants to instrument.

---

*Last updated: v0.1.9 — Neolink rc.3 is now built by this repo's own CI and
downloaded at image-build time (no on-device compile); watchdog is sustained-failure
only (120s / 30s interval defaults); the live preview was removed after it was found
to destabilize Neolink's own RTSP pipeline. Confirmed working: VLC holds the stream
served by rc.3. Keep this current — it's the contract between sessions.*
