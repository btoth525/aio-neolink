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
    │   └── fetch-neolink.sh     ← ⚠ installs a PLACEHOLDER binary — must be replaced (see §6.1)
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
```

★ = the file that delivers the core promise. ⚠ = the one blocker to a real build.

---

## 6. Work order (do these in this order)

### 6.1 ✅ DONE (v0.1.6) — build a real Neolink binary from source
The `Dockerfile` now compiles Neolink in a multi-stage builder from a **pinned master
commit** (`6e05e7844b5b…`, Cargo.toml version **0.6.3-rc.3**) and copies the binary into
the runtime image. `rootfs/fetch-neolink.sh` is retained but **no longer used**.

Why source, not a release: the newest *released* asset is `v0.6.3.rc.2` (commit
`7158943`). On the target E1 (firmware `v3.2.0.4858`, 2025-08) rc.2 connects, logs in,
and lists `/movie-room/mainStream` etc. as available — but then **delivers RTP only
intermittently and hangs after ~30 s**. GStreamer's own `rtspsrc` (the proven client)
fails identically with *"Could not receive message (Timeout while waiting for server
response) … pipeline doesn't want to preroll"*, proving the fault is in Neolink, not the
watchdog probe. The original "Neolink-latest" add-on that worked on this exact camera
reported `0.6.3-rc.3` — i.e. it was a **source build of master**, which is what we now
reproduce. To move Neolink forward, bump `NEOLINK_COMMIT` in the Dockerfile.

Trade-off: the first image build compiles Rust (several minutes natively, longer under
arch emulation) and wants ~2-4 GB RAM. Docker layer caching means it only recompiles
when `NEOLINK_COMMIT` or the build deps change; Python-only edits rebuild fast.

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
Watch the supervisor log. Within roughly `watchdog_timeout` (default 120s, reached as
`health_interval 30s × failures_before_restart 4`) it should detect sustained silence and
restart the pipeline, and the camera should return to `live` on its own. **If this passes,
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
- **Licensing:** Neolink is GPL-3.0. Keep this repo GPL-3.0-compatible.
- **Placeholders:** all replaced with the real owner (`btoth525`) across `config.yaml`,
  `repository.yaml`, `build.yaml`, and `README.md`. Nothing left to fill in.

---

## 8. What a Claude session should NOT do

- Don't rewrite the Baichuan video parsing in Python "to drop Neolink" — that's the
  hard reverse-engineered part; the architecture deliberately keeps Neolink for video.
- Don't replace the relative GUI paths with absolute ones (breaks Ingress).
- Don't make the watchdog restart on a single probe failure — require sustained failure
  (`failures_before_restart`) to avoid bouncing on transient blips.
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

*Last updated: v0.1.6 — Neolink now builds from source (rc.3) to fix intermittent
stream hangs; watchdog made tolerant (sustained-failure only); web GUI gained a
gst-launch live preview. Keep this current — it's the contract between sessions.*
