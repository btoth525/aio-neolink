# aio-neolink

> **The Reolink add-on that actually holds the stream.** Self-healing RTSP, full
> camera control, and a web GUI — one Home Assistant add-on, zero hand-edited TOML.

[![License: GPL-3.0](https://img.shields.io/badge/License-GPL%203.0-blue.svg)](LICENSE)
[![HA add-on](https://img.shields.io/badge/Home%20Assistant-Add--on-41BDF5?logo=home-assistant)](https://github.com/btoth525/aio-neolink)
[![Neolink](https://img.shields.io/badge/Neolink-0.6.3--rc.3-orange)](https://github.com/QuantumEntangledAndy/neolink)

---

## What this is

**aio-neolink** is a Home Assistant OS add-on that bridges Reolink cameras speaking
the **Baichuan protocol (port 9000)** to standard RTSP — and then keeps that stream
alive automatically, instead of quietly going dark for hours.

It fuses two proven open-source projects with a hardened supervisor on top, so it
does more than any one of them alone:

| What you get | Stock Neolink | reolink-aio | **aio-neolink** |
|---|:---:|:---:|:---:|
| Reolink Baichuan → RTSP stream | ✅ | ❌ | ✅ |
| Self-healing (auto-restart on silent hang) | ❌ | ❌ | ✅ |
| RTP-level health probe (catches silent hangs OPTIONS can't) | ❌ | ❌ | ✅ |
| Tolerant watchdog (won't bounce a merely-flaky feed) | ❌ | ❌ | ✅ |
| Web GUI — add cameras without editing files | ❌ | ❌ | ✅ |
| PTZ control | ❌ | ✅ | ✅ |
| IR / spotlight / siren | ❌ | ✅ | ✅ |
| Camera capability auto-detection | ❌ | ✅ | ✅ |
| Works with Frigate (no reconfiguration) | ✅ | ❌ | ✅ |
| Drop-in RTSP on `:8554` | ✅ | ❌ | ✅ |
| Binary built by CI, not compiled on your HA box | ❌ | n/a | ✅ |

---

## The problem this solves

Stock Neolink hangs silently. The process stays alive — the add-on container still
reports `started` — but the RTSP output stops producing frames. Home Assistant's
own add-on watchdog only catches a *fully dead* process, so the camera can sit
`unavailable` for hours (in the incident that started this project: **29 hours**)
with zero automatic recovery.

**aio-neolink probes at the RTP level, not just the TCP/HTTP level.** It doesn't
just check whether the port is open or whether RTSP `OPTIONS` returns 200 — both of
those stay healthy during the exact hang that caused the outage. It runs a complete
RTSP session (`OPTIONS → DESCRIBE → SETUP → PLAY`) and waits for actual RTP bytes
to arrive. No bytes, no health — and after a few minutes of *sustained* silence, it
restarts the pipeline on its own.

That "sustained" qualifier matters: restarting Neolink drops every connected client
(Frigate included) and forces a cold camera re-negotiation, so a hair-trigger
watchdog would actively prevent a merely-flaky feed from ever settling. aio-neolink
only restarts after the stream has been silent for a stretch — gentle by default,
still a massive improvement over a 29-hour outage with no recovery at all.

---

## Architecture

```
Reolink camera
(Baichuan :9000)
      │
      ▼
┌──────────────────────────────────────────────────────────────┐
│                       aio-neolink add-on                      │
│                                                                │
│  ┌────────────────────┐                                       │
│  │  Neolink (Rust)     │  RTSP, LOCALHOST-ONLY (internal port) │
│  │  Baichuan → RTSP    │  go2rtc is its ONLY client — see      │
│  │  (rc.3, CI-built)   │  "Why go2rtc" below                   │
│  └──────────┬──────────┘                                      │
│             │ single persistent pull                          │
│             ▼                                                 │
│  ┌────────────────────┐   ┌──────────────────────┐            │
│  │  go2rtc (restream)  │   │ Supervisor (Python)  │            │
│  │  fans out to any    │◄──│ • watches go2rtc for │            │
│  │  number of clients  │   │   silence, restarts  │            │
│  │  serves :8554       │   │   Neolink on hangs   │            │
│  └──────────┬──────────┘   │ • config generation   │           │
│             │               └──────────┬───────────┘           │
│  ┌────────────────────┐                │                       │
│  │  reolink-aio        │◄────────────────┘                     │
│  │  PTZ / IR /         │   FastAPI REST + Web GUI              │
│  │  spotlight / siren  │   served via HA Ingress               │
│  └────────────────────┘                                       │
└──────────────────────┬─────────────────────────────────────────┘
                        │ RTSP :8554/<camera_name>  (unchanged)
                        ▼
                 Frigate / VLC / Blue Iris / anything else
```

**Frigate needs zero reconfiguration.** The RTSP URL
(`rtsp://<ha-ip>:8554/<camera_name>`) is identical to what stock Neolink served —
go2rtc republishes on the exact same port and path this add-on has always used.

**Why go2rtc sits in front of Neolink:** direct testing found that Neolink's RTSP
server (this build) can't safely serve more than one simultaneous client on the same
camera — even this add-on's own health check, holding just one steady connection,
was enough to crash it once Frigate was *also* connected. go2rtc (the same restream
server Frigate itself bundles for other camera types) is purpose-built for exactly
this: pull one upstream feed, fan it out to any number of downstream clients safely.
With go2rtc in the middle, Neolink only ever has one client — go2rtc — for the
entire lifetime of the add-on, regardless of how many things connect downstream.

Neolink stays exactly what it's always been — the hard, reverse-engineered Baichuan
client — this project never tries to replace it. aio-neolink adds go2rtc, the
supervisor, the control plane, and the GUI around it.

---

## How Neolink gets onto your device

The newest *released* Neolink binary (`v0.6.3.rc.2`) connects to recent Reolink
firmware fine but then delivers video only intermittently and hangs — that's the
GStreamer-level fault, not a watchdog bug, confirmed by reproducing it with
GStreamer's own `rtspsrc` client outside this add-on entirely. The fix is
**`0.6.3-rc.3`**, an in-development version one step ahead of any release.

Rather than compile Rust on your Home Assistant box, **this repo's own GitHub
Actions workflow** (`.github/workflows/build-neolink.yml`) compiles Neolink rc.3
from a pinned upstream commit — natively for both amd64 and arm64 — and publishes
the binaries to this repo's releases. The add-on's Dockerfile just downloads the
~12 MB binary for your architecture at build time. Installing or updating the
add-on is fast; nothing on your HA device compiles anything.

---

## Installation

### Step 1 — Add this repository to Home Assistant

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**
2. Click the **⋮ menu** (top right) → **Repositories**
3. Paste this URL and click **Add**:
   ```
   https://github.com/btoth525/aio-neolink
   ```
4. Close the dialog. The **aio-neolink** add-on will appear in the store — scroll
   down or search for it.

> **Can't see it?** Try refreshing the page or clicking the ⋮ menu →
> **Check for updates**.

### Step 2 — Install the add-on

1. Click **aio-neolink** in the store
2. Click **Install** — this downloads the prebuilt image; it's a fast install,
   not a Rust compile.

### Step 3 — Configure and start

**Configuration tab** — all options are optional to change:

| Option | Default | What it does |
|---|---|---|
| `log_level` | `info` | `debug` for more detail, `warn` for quiet |
| `watchdog_timeout` | `120` | Seconds of *sustained* stream silence before a forced restart. `0` disables the watchdog. |
| `health_interval` | `30` | Seconds between health probes. |

1. Toggle **Show in sidebar** ON — adds the GUI panel to your HA sidebar
2. Click **Start**
3. Open the **Log** tab and confirm you see:
   ```
   ------------------------------------------------------------
    aio-neolink starting
      neolink       : neolink 0.6.3-rc.3
   ------------------------------------------------------------
   aio-neolink ready — watchdog active, Web UI on :8099
   ```
   The `neolink 0.6.3-rc.3` line is the confirmation you're running the fixed
   binary, not the stuttering `rc.2` release.

### Step 4 — Add your cameras via the Web UI

Open the GUI in one of two ways:
- Click **aio-neolink** in the HA sidebar (if you enabled "Show in sidebar"), OR
- Click the **Open Web UI** button on the add-on Info tab

1. Click **+ Add a camera**
2. Fill in:

   | Field | Example | Notes |
   |---|---|---|
   | Name | `movie_room` | Becomes the RTSP path — use only letters, numbers, `_`, `-` |
   | Address | `192.168.1.236` | Camera LAN IP |
   | Port | `9000` | Baichuan port — almost always 9000 |
   | Username | `admin` | Camera login |
   | Password | `yourpassword` | Camera password |
   | Streams | `Main + sub` | Serve both streams; pick one if your camera is single-stream |

3. Click **Test connection** — probes the camera and shows what features it
   supports (PTZ, spotlight, siren, IR)
4. Click **Save camera**

The add-on regenerates its Neolink config and connects immediately. The camera card
shows **Live** (cyan) once the stream is up.

### Step 5 — Point Frigate at the RTSP stream

```yaml
cameras:
  movie_room:
    ffmpeg:
      inputs:
        - path: rtsp://192.168.1.x:8554/movie_room
          roles:
            - detect
            - record
```

Replace `192.168.1.x` with your Home Assistant host IP. The stream is identical to
what Neolink served before — nothing else in Frigate's config needs to change.

> **Sanity check before wiring up Frigate:** point VLC at
> `rtsp://192.168.1.x:8554/movie_room` from another machine on your network. If VLC
> holds a clean picture, Neolink is healthy and Frigate will connect the same way.

---

## Configuration

All options live in the add-on's **Configuration tab** in HA:

| Option | Default | Description |
|---|---|---|
| `log_level` | `info` | `trace` / `debug` / `info` / `warn` / `error` |
| `watchdog_timeout` | `120` | Seconds of *sustained* stream silence — no RTP bytes on the watchdog's persistent connection — before a forced restart. `0` disables the watchdog entirely. |
| `health_interval` | `30` | Seconds between silence checks against that connection. Lower means a hang is noticed sooner; it does **not** mean more RTSP connections, since only one is ever held open per camera. |

Changes take effect after clicking **Save** and then **Restart** on the add-on Info tab.

**Why the defaults are this gentle:** a too-eager watchdog is worse than no
watchdog. Restarting Neolink drops every connected client (Frigate included) and
forces the camera to renegotiate from scratch, so bouncing on the first hiccup
actively prevents a merely-flaky feed from ever settling. The defaults wait for
roughly two minutes of real, sustained silence — still an enormous improvement over
the 29-hour outage with zero recovery that this project exists to fix.

---

## How the self-heal works

The supervisor holds **one persistent RTSP session per camera against go2rtc's
public endpoint** — the same URL Frigate uses — for as long as the stream is
healthy:

```
1. TCP connect to localhost:8554 (go2rtc)
2. Send RTSP OPTIONS  →  expect 200 OK
3. Send RTSP DESCRIBE →  expect SDP with media tracks
4. Send RTSP SETUP    →  request interleaved TCP transport on the actual track URL
5. Send RTSP PLAY     →  start the stream
6. Read continuously, timestamping every byte that arrives
```

No reconnecting every cycle — connect once, then just watch. Every `health_interval`
seconds, the supervisor checks how long it's been since that one connection last saw
a byte. If it's been silent for `watchdog_timeout` seconds — meaning frames have
stopped flowing all the way through the chain, the exact hang that caused the
original outage — the supervisor restarts Neolink specifically (go2rtc itself
usually doesn't need restarting; it just reconnects to Neolink once Neolink is back).
A fresh Neolink restart gets a 45-second grace period before silence is judged at
all, since it needs time to renegotiate with the camera first.

**Why the watchdog watches go2rtc instead of Neolink directly:** direct testing
found Neolink's RTSP server (this build) can't safely serve more than one
simultaneous client on the same camera — even a single steady health-check
connection was enough to crash it once Frigate was *also* connected, regardless of
whether that connection reconnected periodically or was held open the whole time.
Watching go2rtc's public endpoint instead means the watchdog is just one more of
go2rtc's fan-out clients, which is exactly the scenario go2rtc is built to handle
safely — while Neolink itself never has more than one client (go2rtc) at all. See
[Architecture](#architecture) for the full reasoning.

**A crash is handled separately, and faster.** If the Neolink or go2rtc process
dies on its own (a segfault, an OOM-kill — anything besides hanging while still
alive) the supervisor notices immediately from the process exit, not from the next
silence check, and restarts that process right away. The silence-based path exists
specifically for the *hang* failure mode (process alive, stream dead); an actual
crash doesn't need to wait, since there's no flaky-feed risk in restarting
something that's already gone.

---

## Testing the self-heal yourself

Once the add-on is running, you can simulate the exact original failure:

```bash
# SSH into your HA host, then enter the add-on container:
docker exec -it $(docker ps --filter name=aio_neolink -q) bash

# Find the Neolink PID and freeze it (mimics the silent hang):
kill -STOP $(pidof neolink)
```

Watch the add-on log. Within roughly `watchdog_timeout` seconds you'll see:

```
camera movie_room unhealthy: no RTP data received (stream may be hung) — silent 120s (restart threshold 120s)
stopping neolink (SIGTERM)
neolink (re)started: unhealthy cameras: ['movie_room']
```

The camera card briefly shows **Recovering** (amber) and returns to **Live** (cyan)
once the new Neolink process reconnects to the camera.

---

## Web GUI

The GUI is served via Home Assistant Ingress — it appears directly in the HA
sidebar, authenticated by HA, no port forwarding needed.

**Camera card states:**

| Color | Meaning |
|---|---|
| 🔵 Cyan — **Live** | Stream is healthy, RTP flowing |
| 🟡 Amber — **Recovering** | A probe failure or two, watching to see if it self-resolves |
| 🔴 Red — **Down** | Sustained failure — check camera power/network, or wait for auto-recovery |

**Controls on each card:**
- **IR** — toggle infrared night vision
- **Light** — toggle spotlight / floodlight
- **Siren** — trigger the alarm siren
- **◀ ▶** — PTZ nudge left / right (sends PTZ command + auto-Stop after 600ms)
- **Remove** — delete the camera and stop its stream

Controls only appear active (not greyed out) for features the camera actually
reported supporting during the probe.

There's deliberately **no live video preview** in the GUI. An MJPEG preview was
tried and removed: it opened its own RTSP connection to the local Neolink instance,
which could land mid-negotiation and destabilize the very stream Frigate depends
on. Holding that stream is the entire point of this add-on, so nothing in the GUI
opens a second connection to it. Use VLC (`rtsp://<ha-ip>:8554/<camera_name>`) to
eyeball a feed instead — that's exactly what Frigate is doing under the hood.

---

## REST API

The full API is available at `http://<ha-ip>:8099/api/` (or via Ingress at the same
path relative to the GUI URL).

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/cameras` | List all cameras |
| `POST` | `/api/cameras` | Add or update a camera |
| `DELETE` | `/api/cameras/{name}` | Remove a camera |
| `POST` | `/api/cameras/probe` | Probe an IP+creds, return capabilities |
| `GET` | `/api/health` | Per-camera watchdog health snapshot |
| `POST` | `/api/cameras/{name}/control` | Send IR / spotlight / siren / PTZ command |

**Example — add a camera:**
```bash
curl -X POST http://homeassistant.local:8099/api/cameras \
  -H "Content-Type: application/json" \
  -d '{
    "name": "movie_room",
    "address": "192.168.1.236",
    "port": 9000,
    "username": "admin",
    "password": "yourpassword",
    "stream": "both"
  }'
```

**Example — PTZ nudge:**
```bash
curl -X POST http://homeassistant.local:8099/api/cameras/movie_room/control \
  -H "Content-Type: application/json" \
  -d '{"action": "ptz", "operation": "Left", "speed": 32}'
```

---

## Troubleshooting

**Camera stays "Down" after adding**
- Verify the IP and port with `ping 192.168.1.x` from your HA host
- Use **Test connection** in the GUI before saving — it shows exactly what the
  camera reported
- Check the add-on log (Settings → Add-ons → aio-neolink → Log) for the Neolink
  output

**Stream shows healthy in the GUI but Frigate can't connect**
- Confirm `host_network: true` is set (it is by default)
- Check that port 8554 isn't blocked by a firewall on your HA host
- Try `rtsp://192.168.1.x:8554/movie_room` in VLC from another machine first — if
  VLC holds it, Frigate's config (not the stream) is the problem

**Stream connects then drops repeatedly**
- Check the **Log** tab for `neolink : neolink 0.6.3-rc.3` at startup — if it shows
  `0.6.3-rc.2` instead, the add-on downloaded the wrong release; reinstall to pick
  up the latest version
- A camera that's also being hit by another client (an old add-on, a phone app
  still connected) can starve the RTSP session; make sure nothing else is pulling
  the Baichuan connection on port 9000
- `neolink exited with code -5` in the log means Neolink crashed (not hung); the
  supervisor restarts it immediately when this happens, no need to wait. Versions
  before go2rtc was put in front of Neolink (see [Architecture](#architecture))
  could trigger this crash themselves as soon as Frigate connected — Neolink's RTSP
  server can't safely handle more than one simultaneous client at all. If you're on
  a version with go2rtc and still see this, check the **Log** tab for `[go2rtc]`
  lines: Neolink should now only ever have go2rtc as a client.
- **Frigate has a camera pointed at this add-on that isn't actually configured
  here.** That shows up in Frigate's own log as `method DESCRIBE failed: 404 Not
  Found` repeating every ~20 seconds for a camera name you didn't add via this
  add-on's GUI — either add that camera here too, or point Frigate's entry for it
  somewhere else.
- If Frigate logs `Error during demuxing: Connection timed out` for a camera that
  *is* configured here, try forcing TCP transport in `frigate.yml` (UDP is more
  prone to timeouts on this kind of bridged stream):
  ```yaml
  cameras:
    movie_room:
      ffmpeg:
        inputs:
          - path: rtsp://192.168.1.x:8554/movie_room
            input_args: preset-rtsp-restream
            roles: [detect, record]
  ```
  (`preset-rtsp-restream` is Frigate's built-in TCP-transport ffmpeg preset.)

**PTZ / lights not working**
- These require the camera to support the feature — click **Test connection** to see
  what your model actually supports; unsupported features are greyed out
- Ensure the camera's HTTP API is reachable on port 80 (same IP as Baichuan)

**Neolink binary fails to start / fetch-neolink errors in the build log**
- Check the **Log** tab for `neolink exited with code` or `fetch-neolink` messages
- The add-on downloads a prebuilt binary from this repo's `neolink-rc3` release
  (built by `.github/workflows/build-neolink.yml`). If that release is missing an
  asset for your architecture, file an issue.

**The Log tab is empty**
- Make sure the add-on is Started (green dot on the Info tab)
- Try clicking **Refresh** at the top of the Log tab
- Switch `log_level` to `debug` in the Configuration tab and restart

---

## Migrating from Neolink-latest

1. Note your camera names from your existing `neolink.toml`
2. Stop the **Neolink-latest** add-on
3. Install and start **aio-neolink**
4. Re-add each camera via the GUI using the same names
5. Update Frigate's RTSP URLs if the host IP changes (port 8554 stays the same)

Your Frigate entity names (`camera.movie_room`, etc.) won't change.

---

## What's next (Roadmap)

See [`ROADMAP.md`](ROADMAP.md) for the full list. Top priorities:

- **Verify reolink-aio capability flags** across more camera models (PTZ/light/siren
  naming differs by model — only confirmed against an E1 so far)
- **Fork Neolink and add an in-process frame watchdog** — catches a hang *inside*
  Neolink's own GStreamer feed loop, before the external RTP probe would ever
  notice. Sub-second recovery instead of minutes.
- **Per-camera restart** instead of bouncing the whole Neolink process when only
  one camera is unhealthy
- **Two-way audio** via the Neolink backchannel + a talk button in the GUI

---

## License

GPL-3.0. Neolink is GPL-3.0; reolink-aio is MIT. This repo inherits GPL-3.0.

---

## Credits

- **[Neolink](https://github.com/QuantumEntangledAndy/neolink)** — QuantumEntangledAndy fork, originally by George Hilliard. The hard Baichuan reverse-engineering work that makes all of this possible.
- **[reolink-aio](https://github.com/starkillerOG/reolink_aio)** — @starkillerOG's officially Reolink-authorized Python library, the same one powering the native HA Reolink integration.
- **[go2rtc](https://github.com/AlexxIT/go2rtc)** — AlexxIT's restream server, the same one Frigate itself bundles. Sits between Neolink and everything else so Neolink only ever has one client.
