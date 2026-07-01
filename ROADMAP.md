# Roadmap

Honest state of the project as of **v0.1.9**: the core mission — a Reolink stream
that actually holds, with a self-healing watchdog and a working web GUI — is live
and confirmed working (Neolink rc.3 streaming, VLC holding a clean picture). What's
left from here is hardening and features, not blockers.

## Done

- **HAOS add-on packaging** — `config.yaml`, `Dockerfile`, Ingress GUI panel,
  `host_network: true` so Neolink can reach the camera and bind `:8554`.
- **Real Neolink binary, the version that actually works** — `0.6.3-rc.3`, compiled
  by this repo's own GitHub Actions (`.github/workflows/build-neolink.yml`) from a
  pinned upstream commit, published as a release, and downloaded at image-build
  time. The newest *released* binary (`rc.2`) stutters and hangs on recent Reolink
  firmware; rc.3 (an in-development version one step ahead of any release) is what
  the original working add-on ran, and is what this one ships now. No Rust compiles
  on the Home Assistant device.
- **go2rtc restream layer — the fix that actually solved the crash** — Neolink's
  RTSP server (this build) was proven, by direct testing, unable to safely serve
  more than one simultaneous client on the same camera at all. Two earlier
  mitigations (removing the GUI preview, then rewriting the watchdog to hold one
  persistent connection instead of reconnecting) both reduced but didn't eliminate
  the crash, because neither addressed the real constraint: Neolink + Frigate
  connected together, at all, was enough. `restream_gen.py`/`restream.py` now run
  go2rtc (the same restream server Frigate bundles for other camera types) as a
  second subprocess: Neolink binds `127.0.0.1:18554` (localhost-only, internal —
  `config_gen.py`), go2rtc is its one and only client, and go2rtc republishes on
  the public `:8554` Frigate has always used. Neolink now never has more than one
  client for its entire lifetime, and go2rtc — purpose-built for exactly this
  fan-out — safely serves Frigate, VLC, and the watchdog itself simultaneously.
- **Self-healing watchdog, one persistent connection per camera** — `pipeline.py`
  opens a single RTSP session (`OPTIONS → DESCRIBE → SETUP → PLAY`) against
  go2rtc's public endpoint per camera and holds it open, timestamping every byte
  that arrives. Every `health_interval` seconds the supervisor just checks how long
  that connection has been silent. This catches the exact "port open, but no
  frames" hang that caused the original 29-hour outage — and because it watches
  go2rtc rather than Neolink directly, the watchdog itself can never be the thing
  that destabilizes Neolink, regardless of how many other clients are connected.
- **Tolerant restart policy** — the watchdog only restarts Neolink after
  `watchdog_timeout` seconds of *sustained* silence on that connection, not on the
  first blip. An eager watchdog was found, in practice, to actively prevent a
  merely-flaky feed from ever stabilizing, since every restart drops all clients
  (Frigate included) and forces a cold camera renegotiation.
- **Instant restart on crash** — if Neolink dies on its own (segfault, panic,
  OOM-kill) the supervisor notices from the process exit immediately, rather than
  waiting for the silence timer above to elapse.
- **Camera store + TOML generation** — add a camera in the GUI → `neolink.toml`
  regenerated → pipeline restarted. No hand-editing.
- **REST API + GUI** — list/add/delete cameras, probe capabilities, per-camera
  health, send IR/spotlight/siren/PTZ. Served via HA Ingress with relative paths.
- **Removed a real regression** — v0.1.3–0.1.8 had a live MJPEG preview in the GUI.
  It opened its own RTSP connection to the local Neolink instance, which could land
  mid-negotiation and trip Neolink's own RTSP pipeline
  (`GStreamer-RTSP-Server-CRITICAL: could not create element`), leaving it dropping
  frames afterward. Removed entirely in v0.1.9 rather than patched, since holding
  the stream matters more than a GUI thumbnail.

## Needs real-world verification

1. **reolink-aio capability flags across more models.** `control.py` calls
   `Host.get_host_data()`, `get_states()`, `set_ir_lights`, `set_spotlight`,
   `set_siren`, `set_ptz_command(channel, command=, speed=)`, and
   `supported(channel, feature)`. Confirmed working against an E1; other Reolink
   models expose different capability flag names (`floodLight` vs `spotlight`,
   etc.) and may need adjustment. Pin a reolink-aio version once this is settled.
2. **Battery cameras** — the store/TOML support `uid` (UDP, no fixed address), but
   `control.py` currently requires a wired `address`. Battery/UDP control is a
   follow-up.
3. **Neolink TOML schema drift.** `config_gen.py` emits a top-level `[[rtsp]]` bind
   block + `[[cameras]]` blocks — confirmed against the current rc.3 build. Re-check
   this whenever `NEOLINK_COMMIT` is bumped; the schema has changed between Neolink
   versions before.

## The next reliability prize

The watchdog currently restarts the **whole** Neolink process, because stock
Neolink exposes no per-camera control. In order of payoff:

1. **In-process frame watchdog (highest value).** The real fix lives *inside*
   Neolink's GStreamer `appsrc` feed loop (the loop from George Hilliard's original
   blog post that pushes camera bytes into the pipeline): track time since the last
   buffer was pushed, and if it exceeds a threshold, force a camera reconnect *at
   the source* — catching the hang before any external probe would ever notice it.
   This requires a small patch to the Neolink fork build.
2. **Per-camera recovery.** Add a small control surface (or reuse the
   QuantumEntangledAndy fork's existing MQTT control) so the supervisor can restart
   one camera's pipeline instead of bouncing every camera whenever any one of them
   is unhealthy.

## Milestones

- **M1 — ✅ done.** Real Neolink binary streams and holds (confirmed via VLC);
  watchdog recovers a forced hang (`kill -STOP`); web GUI works end-to-end.
- **M1.5 — ✅ done.** Neolink survives being connected to alongside Frigate without
  crashing — required putting go2rtc in front of it, since Neolink itself can't
  tolerate more than one simultaneous client. This wasn't originally a planned
  milestone; it became one after real-world testing surfaced the crash.
- **M2 — in progress.** reolink-aio capability flags verified across more camera
  models than just the E1; battery camera control.
- **M3 — planned.** Fork Neolink, add the in-process frame watchdog + per-camera
  control surface. Recovery becomes sub-second and per-camera instead of
  minutes-and-whole-process. (Partially softened already: go2rtc absorbs a Neolink
  restart on its own, so downstream clients like Frigate don't necessarily drop
  when Neolink cycles — the remaining gap is Neolink itself still restarting as a
  single whole-process unit rather than per-camera.)
- **M4 — planned.** Two-way audio: reolink-aio + Neolink's audio backchannel,
  surfaced as a talk button in the GUI.

## Testing the self-heal without waiting for a real outage

```bash
# find the neolink PID inside the add-on container, then:
kill -STOP <neolink_pid>     # freeze it — mimics the silent hang, RTSP goes quiet but the PID lives

# watch the supervisor log: within roughly watchdog_timeout seconds (default 120s)
# it should detect sustained silence and restart the pipeline on its own
```

If the camera returns to **Live** on its own, the core promise is proven.
