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

Three mature projects each own one part; aio-neolink fuses them and adds the supervisor.

| Layer | Project | Role |
|-------|---------|------|
| **Video** | **Neolink** (Rust + GStreamer) | Pull H.264/H.265 Baichuan video, serve RTSP on a **localhost-only internal port** (`18554`). Keep this — it's the hard, reverse-engineered part; don't reinvent it. |
| **Restream** | **go2rtc** (Go, AlexxIT — same restream server Frigate bundles) | The ONLY client Neolink ever has. Pulls Neolink's one stream, republishes on the **public `:8554`** Frigate/VLC/etc. use — Neolink's RTSP server can't safely serve more than one simultaneous client (learned the hard way, §7), go2rtc can. |
| **Control** | **reolink-aio** (Python, *officially authorized by Reolink*, dev @starkillerOG; same lib HA's Reolink integration uses) | Probe model/capabilities, PTZ, spotlight, siren, IR, motion/AI events. |
| **Supervisor + GUI** | **aio-neolink** (this repo, Python + FastAPI) | Health-watchdog the stream (via go2rtc, not Neolink directly) and auto-recover hangs; generate `neolink.toml` + `go2rtc.yaml`; serve REST + web GUI via HA Ingress. |

Data flow:

```
Reolink cam ──Baichuan :9000──► Neolink ──RTSP :18554──► go2rtc ──RTSP :8554/<name>──► Frigate
     ▲                                                       ▲
     │ control (PTZ/lights/siren)                            │ health watch + auto-restart
     └──────── reolink-aio ◄── FastAPI GUI ──────────── supervisor (watchdog)
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
    ├── Dockerfile               ← GStreamer runtime + Neolink binary + go2rtc + Python control plane
    ├── run.sh                   ← entrypoint → launches supervisor.main
    ├── rootfs/
    │   ├── fetch-neolink.sh     ← downloads the CI-built Neolink rc.3 binary for BUILD_ARCH
    │   └── fetch-go2rtc.sh      ← downloads the go2rtc restream binary for BUILD_ARCH
    ├── supervisor/
    │   ├── main.py              ← wires store + pipeline + restream + controls + watchdog + API
    │   ├── pipeline.py          ← ★ the self-heal: runs Neolink, watches go2rtc, restarts on hang
    │   ├── config_gen.py        ← renders neolink.toml (Neolink: localhost-only, internal port)
    │   ├── restream.py          ← ★ owns the go2rtc subprocess (Neolink's only client)
    │   ├── restream_gen.py      ← renders go2rtc.yaml (public RTSP restream, from camera store)
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

- **Neolink TOML schema — ✅ verified against source, and a real bug found.**
  `config_gen.py` wrapped `bind`/`bind_port` in a `[[rtsp]]` table for several
  versions. Checked directly against `src/config.rs` at the pinned commit
  (6e05e7844b5b): the root `Config` struct has `bind`/`bind_port` as **flat
  top-level keys** — there is no `[[rtsp]]` table in this schema at all. Neolink
  silently ignored the unrecognized `[[rtsp]]` block and fell back to its hardcoded
  defaults (`0.0.0.0:8554` — see `default_bind_addr()`/`default_bind_port()` in
  config.rs), which meant the "Neolink is internal-only, go2rtc is its only
  client" architecture (v0.1.11+) was **never actually in effect**: Neolink kept
  publicly binding the same port go2rtc needed, causing go2rtc's own RTSP listener
  to fail with "address already in use". Fixed by emitting `bind`/`bind_port` as
  bare top-level keys before the first `[[cameras]]` block. **Lesson: this section
  was flagged "must verify against the real binary" from the very first version of
  this file and was never actually checked until a live failure forced it** — don't
  let a TODO like that sit unverified through multiple releases again. Whenever
  `NEOLINK_COMMIT` changes, pull `src/config.rs` at that commit and re-check the
  schema directly rather than trusting what worked before.
- **reolink-aio API** (`control.py`) — still unverified, same caution applies: calls `Host.get_host_data()`, `get_states()`,
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
- **Neolink must only ever have ONE RTSP client: go2rtc.** This was learned the hard
  way across three escalating attempts, all traced to the same underlying fact:
  Neolink's RTSP server (this build) cannot safely serve more than one simultaneous
  client on the same camera, period — not a churn/reconnect issue, a hard
  concurrency limit.
  1. v0.1.3–0.1.8 had a live MJPEG preview in the web GUI that opened a second RTSP
     connection whenever the GUI rendered — crashed Neolink
     (`GStreamer-RTSP-Server-CRITICAL: could not create element`). Removed in v0.1.9.
  2. The watchdog's own health probe originally reconnected every `health_interval`
     — also crashed Neolink once Frigate was *also* connected
     (`gst_poll_write_control: assertion 'set != NULL' failed`, then SIGTRAP).
     Rewritten in v0.1.10 to hold one persistent connection instead of reconnecting.
  3. **The v0.1.10 fix was insufficient** — confirmed by direct testing, the exact
     same crash still happened with a single persistent watchdog connection plus
     Frigate. This proved the bug was never about reconnect churn: Neolink can't
     tolerate 2 *simultaneous* clients on one camera, full stop.
  4. **v0.1.11 fixed the architecture, but the crash still happened.** go2rtc (the
     same restream server Frigate bundles for other camera types) was put between
     Neolink and everyone else. `config_gen.py` binds Neolink to `127.0.0.1:18554`
     (localhost-only, internal — see the port comment there for why 18554
     specifically, to avoid go2rtc's own default ports). `restream_gen.py`/
     `restream.py` run go2rtc as a second subprocess that pulls Neolink's one
     stream and republishes it on the public `:8554` Frigate has always used. This
     was the right architecture, but the exact same crash still reproduced —
     because go2rtc itself was silently the second client.
  5. **v0.1.12 found why: go2rtc's own backchannel probe.** go2rtc's RTSP source
     attempts an ONVIF Profile T two-way-audio backchannel negotiation by default
     — a second connection/negotiation on top of the main pull — unless the source
     URL has `#backchannel=0` appended (go2rtc's own docs recommend this suffix
     for other camera types with the same problem). Neolink doesn't support that
     negotiation and doesn't handle it gracefully, so go2rtc's default behavior
     was itself creating the "second simultaneous client" that crashes Neolink —
     with zero external client (Frigate) even needed to trigger it.
     `restream_gen._source_stream()` now appends `#backchannel=0` to every camera
     source URL. Two-way audio isn't implemented yet anyway (§6.6/M4).

  The watchdog (`pipeline.py`, `_CameraMonitor`) watches go2rtc's public endpoint,
  not Neolink directly — Neolink has exactly one client (go2rtc) for its entire
  lifetime, and go2rtc is purpose-built to safely fan that one feed out to any
  number of downstream consumers (Frigate, VLC, the watchdog itself).
  6. **v0.1.19 found the real cause of the persistent DESCRIBE 404 (unrelated to
     Neolink's single-client limit above, but discovered while chasing the same
     stream)** — and a
     second, self-inflicted bug that made it much worse. Confirmed directly
     against Neolink's source at the pinned commit (`src/rtsp/mod.rs`,
     `src/rtsp/gst/factory.rs`, `src/rtsp/factory.rs`): Neolink registers a dummy
     RTSP mount for each camera immediately at startup (the "Available at ..."
     log line fires here), but that mount intentionally answers DESCRIBE with 404
     until an internal **learning phase** completes — it buffers frames from the
     camera until it has >10 frames or knows both the video and audio codec,
     only then building the real pipeline a DESCRIBE needs. OPTIONS succeeds
     immediately (it only needs the mount to exist); DESCRIBE doesn't until
     learning finishes. This is normal Neolink behavior, not a hang, not a TOML
     bug, not a bind-address issue — all ruled out over v0.1.13–v0.1.18 chasing
     the wrong layer. The watchdog in `pipeline.py` made this far worse: it
     treated "never connected yet" identically to "connected then went silent,"
     so it force-restarted Neolink on the same `watchdog_timeout` (120s) even
     though a restart throws away learning-phase progress and starts it over —
     an infinite loop where the camera could never get enough uninterrupted time
     to finish learning. This exactly matched the observed symptom "Frigate got
     an initial stream but it didn't hold" (confirmed in supervisor logs:
     `neolink exited with code -15` on a ~120s cycle) — Frigate briefly caught a
     post-learning window right before the next forced restart. Fixed by adding
     `CameraHealth.ever_connected` and a separate `first_connect_timeout` (600s)
     that only applies before a camera's first successful handshake;
     `watchdog_timeout` still applies at full strength once a camera has proven
     it can stream and then goes silent (the original incident this project
     exists to fix). **Lesson: a "stream not ready yet" 404 and a "stream is
     hung" silence look identical from outside if the watchdog doesn't track
     which one it's looking at — always distinguish first-connection from
     lost-connection before choosing a restart threshold.**

  **Do not connect anything directly to Neolink's internal RTSP port again** —
  not the GUI, not the watchdog, nothing — without first proving, with real logs
  and Frigate connected simultaneously, that Neolink can tolerate it. It has failed
  this test three times. Route everything through go2rtc instead — and if go2rtc's
  own config for a source ever changes, re-check whether backchannel negotiation
  got re-enabled.
- **RESOLVED in v0.1.25 — the DESCRIBE-404 saga's ending, and the final
  architecture.** The permanent DESCRIBE 404 was fixed by two changes landed
  together after every single-variable test failed:
  1. **v0.1.24: use the Neolink binary BAKED INTO `quantumentangledandy/neolink:
     latest`** instead of our own CI build. Research found that image was last
     published over a year ago — its binary is an older, proven master build,
     while our CI pinned a NEWER master commit that Cargo.toml still labeled
     "0.6.3-rc.3". The unchanged version string masked that we were shipping
     different, newer, regressed code the whole time. (v0.1.24 alone still
     404'd, but via go2rtc/port 18554.)
  2. **v0.1.25: Neolink serves Frigate DIRECTLY on the public :8554; go2rtc
     disabled by default** (still in the code behind `AIO_ENABLE_RESTREAM=1` +
     `NEOLINK_RTSP_PORT=18554`). This exactly replicates the proven-working
     `Neolink-latest` add-on's shape: same baked binary, same default port, no
     restream layer.
  **Confirmed live:** watchdog healthy/connected, `camera.movie_room` =
  `recording` in Frigate, and Neolink's own logs showed TWO simultaneous RTSP
  sessions (Frigate + the watchdog's persistent connection) streaming without
  any crash — so the "Neolink can't serve 2 clients" limit that justified
  go2rtc (§7 above) was a symptom of the regressed newer binary + the old
  churning watchdog, not a real limit of the proven build. The go2rtc layer and
  the whole CI-build pipeline (build-neolink.yml, fetch-neolink.sh) are now
  dead weight kept only as fallback options; a future cleanup can delete them.
  **Lessons:** (a) a version string is not a build identity — pin by what
  actually runs in the proven environment, not by what the label says matches;
  (b) when replicating a working system, replicate its *shape* first (binary,
  port, topology) before theorizing about deeper causes.
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
- Don't connect anything (GUI feature, watchdog, ad-hoc debugging) directly to
  Neolink's internal RTSP port (`127.0.0.1:18554`) — only go2rtc may. Route new
  consumers through go2rtc's public port instead (see §7) — connecting straight to
  Neolink has failed three separate times, including a full process crash.
- Don't store secrets anywhere outside `/data`.

---

## 9. Quick reference

- RTSP for Frigate: `rtsp://<addon-host>:8554/<camera_name>` (unchanged from stock;
  this is go2rtc now, not Neolink directly).
- Neolink's internal-only RTSP: `127.0.0.1:18554` — go2rtc's only client, nothing
  else may connect here (§7).
- Add-on options: `log_level`, `watchdog_timeout` (s, 0=off), `health_interval` (s).
- Key source-of-truth files for behavior: `pipeline.py` (recovery, watches go2rtc),
  `config_gen.py` (Neolink contract), `restream_gen.py`/`restream.py` (go2rtc
  contract + process), `control.py` (reolink-aio contract).
- Original protocol background: George Hilliard, "Hacking Reolink cameras for fun and
  profit" (the post that created Neolink) — explains Baichuan, port 9000, the GStreamer
  `appsrc` video loop that §6.5.1 wants to instrument.

---

*Last updated: v0.1.25 — WORKING END TO END, confirmed live: camera →
baked-in Neolink binary (from `quantumentangledandy/neolink:latest`, used
as-is — our own CI-built binary was a silently newer, regressed commit and is
no longer used) → RTSP directly on :8554 → Frigate `recording`, with the
supervisor's persistent-connection watchdog simultaneously connected and
healthy (two concurrent clients, no crash — the go2rtc restream layer is now
DISABLED by default and kept only as an option, see §7's "RESOLVED in
v0.1.25" entry for the full story and lessons). The watchdog remains the core
deliverable: 120s of sustained stream silence → automatic Neolink restart,
which is the self-heal for the original "stream falls off after a while and
never comes back" incident this project exists to fix. Next steps: §6.3
acceptance test (kill -STOP the neolink pid, watch it self-heal), §6.4
features pass (PTZ/lights/siren via GUI), then cleanup (delete
build-neolink.yml / fetch-neolink.sh / restream code if the direct
architecture holds).*

*Previous history (v0.1.21): the persistent DESCRIBE 404 was ultimately traced past the
watchdog fix (v0.1.19) to the container image itself: our own CI-built Neolink
binary connected, logged in, and registered its RTSP mount identically on our
generic `ghcr.io/home-assistant/*-base-debian:bookworm` image, but its GStreamer
pipeline-construction callback silently never succeeded (permanent 404, zero
error output even at RUST_LOG=trace). Proven via a live side-by-side test: the
exact same pinned commit, run through the upstream `Neolink-latest` HA add-on
(a thin wrapper around `quantumentangledandy/neolink:latest`), streamed to
Frigate immediately on the same camera. Fixed by rebasing this add-on's
Dockerfile/build.yaml onto that same upstream image instead of hand-picking
GStreamer apt packages on a generic Debian base — we still overwrite the binary
with our own CI-pinned build on top of it for reproducibility. Lesson: when a
Rust+GStreamer binary works in one container and not another with identical
logs up to the point of silent failure, suspect the *runtime's plugin set*
before the application code — a mount can register successfully (needs
nothing) while the actual pipeline-build callback silently fails for a
plugin/version mismatch invisible to the app's own logging.

*Previously: v0.1.19 — the persistent RTSP DESCRIBE 404 that blocked the stream
for several versions is understood and fixed. Root cause, confirmed against
Neolink's pinned-commit source: Neolink's RTSP mounts intentionally 404 on
DESCRIBE until an internal per-camera learning phase (buffering frames until it
knows both codecs, or has >10 frames) completes — normal behavior, not a hang,
not a TOML/bind-address bug. The watchdog in `pipeline.py` was restarting Neolink
before that phase could ever finish (treating "never connected" the same as "went
silent after connecting"), which explained the "Frigate got a stream but it
didn't hold" symptom exactly. Fixed with `CameraHealth.ever_connected` +
`first_connect_timeout` (600s) so first-connection gets a much longer leash than
a genuine post-connection hang, which still restarts at `watchdog_timeout` (120s)
as before. Underneath all of this, go2rtc still sits between Neolink and the
world (Neolink localhost-only on internal port 18554; go2rtc republishes on the
public :8554 Frigate has always used) because Neolink was proven, by direct
testing, unable to safely serve more than one simultaneous RTSP client on the
same camera (§7) — that architecture is unrelated to and unaffected by the
DESCRIBE-404 fix above. Neolink rc.3 is still built by this repo's own CI and
downloaded at image-build time (no on-device compile). Keep this current — it's
the contract between sessions.*
