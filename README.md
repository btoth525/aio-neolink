# aio-neolink

> **The Reolink add-on that actually works.** Self-healing streams, full camera
> control, and a web GUI — all in one Home Assistant add-on.

[![License: GPL-3.0](https://img.shields.io/badge/License-GPL%203.0-blue.svg)](LICENSE)
[![HA add-on](https://img.shields.io/badge/Home%20Assistant-Add--on-41BDF5?logo=home-assistant)](https://github.com/btoth525/aio-neolink)

---

## What this is

**aio-neolink** is a Home Assistant OS add-on that bridges Reolink cameras using the
**Baichuan protocol (port 9000)** to RTSP — and then keeps those streams alive
automatically, forever.

It replaces both **Neolink** (which silently hangs and has no GUI) and the standalone
**reolink-aio** integration for camera control, fusing them into a single, hardened
add-on that does more than either project alone:

| What you get | Stock Neolink | reolink-aio | **aio-neolink** |
|---|:---:|:---:|:---:|
| Reolink Baichuan → RTSP stream | ✅ | ❌ | ✅ |
| Self-healing (auto-restart on silent hang) | ❌ | ❌ | ✅ |
| RTP-level health probe (catches silent hangs) | ❌ | ❌ | ✅ |
| Web GUI — add cameras without editing files | ❌ | ❌ | ✅ |
| PTZ control | ❌ | ✅ | ✅ |
| IR / spotlight / siren | ❌ | ✅ | ✅ |
| Camera capability auto-detection | ❌ | ✅ | ✅ |
| Works with Frigate (no reconfiguration) | ✅ | ❌ | ✅ |
| Drop-in RTSP on `:8554` | ✅ | ❌ | ✅ |

---

## The problem this solves

Stock Neolink hangs silently. The process stays alive — the container reports
`started` — but the RTSP output stops producing frames. Home Assistant's add-on
watchdog only catches a *fully dead* process, so the camera can sit `unavailable`
for hours with no automatic recovery.

**aio-neolink probes at the RTP level.** It doesn't just check if the port is open
or if RTSP OPTIONS returns 200. It completes a full RTSP session (OPTIONS → DESCRIBE →
SETUP → PLAY) and waits for actual RTP data bytes. If frames stop flowing, it knows —
and it restarts the pipeline automatically, typically within 15–30 seconds.

---

## Architecture

```
Reolink camera
(Baichuan :9000)
      │
      ▼
┌─────────────────────────────────────────────────┐
│                aio-neolink add-on                │
│                                                  │
│  ┌───────────────────┐   ┌─────────────────────┐ │
│  │  Neolink (Rust)   │   │ Supervisor (Python)  │ │
│  │  Baichuan → RTSP  │◄──│ • RTP health probe   │ │
│  │  serves :8554     │   │ • auto-restart       │ │
│  └───────────────────┘   │ • config generation  │ │
│                          └──────────┬───────────┘ │
│  ┌───────────────────┐              │              │
│  │  reolink-aio      │◄─────────────┘              │
│  │  PTZ / IR /       │   FastAPI REST + Web GUI    │
│  │  spotlight / siren│   served via HA Ingress     │
│  └───────────────────┘                            │
└──────────────────────┬──────────────────────────┘
                       │ RTSP :8554/<camera_name>
                       ▼
                    Frigate / go2rtc / Blue Iris
```

**Frigate needs zero reconfiguration.** The RTSP URL
(`rtsp://<ha-ip>:8554/<camera_name>`) is identical to what stock Neolink served.

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
2. Click **Install** and wait for the image to download (~1–2 min on first install)

### Step 3 — Start the add-on

1. Go to the **Configuration** tab and review the options (defaults are fine to start)
2. Toggle **Show in sidebar** ON — this adds the GUI panel to your sidebar
3. Click **Start**
4. Open the **Log** tab and confirm you see:
   ```
   aio-neolink up: API on :8099, watchdog running
   ```

### Step 4 — Add your cameras via the GUI

1. Click **aio-neolink** in the HA sidebar (or open the **Web UI** button from the
   add-on page)
2. Click **+ Add a camera**
3. Fill in:
   | Field | Example | Notes |
   |---|---|---|
   | Name | `movie_room` | Becomes the RTSP path — use only letters, numbers, `_`, `-` |
   | Address | `192.168.1.236` | Camera LAN IP |
   | Port | `9000` | Baichuan port — almost always 9000 |
   | Username | `admin` | Camera login |
   | Password | `yourpassword` | Camera password |
   | Streams | `Main + sub` | Serve both streams; pick one if your camera is single-stream |
4. Click **Test connection** — this probes the camera and shows what features it
   supports (PTZ, spotlight, siren, IR)
5. Click **Save camera**

The add-on will regenerate its config and connect immediately. The camera card will
show **Live** (green) once the stream is up.

### Step 5 — Point Frigate at the RTSP stream

If you have Frigate, add this to your `frigate.yml`:

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

Replace `192.168.1.x` with your Home Assistant host IP. The stream will be identical
to what Neolink served — no other changes needed.

---

## Configuration options

Set these in the add-on's **Configuration** tab:

| Option | Default | Description |
|---|---|---|
| `log_level` | `info` | Logging verbosity: `trace` / `debug` / `info` / `warn` / `error` |
| `watchdog_timeout` | `45` | Seconds of stream silence before a restart is forced. Set to `0` to disable. |
| `health_interval` | `15` | Seconds between health probes. Lower = faster detection, more CPU. |

**Recommended for aggressive recovery:**
```
watchdog_timeout: 30
health_interval: 10
```

**To disable the watchdog** (not recommended — this is the whole point):
```
watchdog_timeout: 0
```

---

## How the self-heal works

Every `health_interval` seconds, the supervisor runs a full RTSP health check on each
camera stream:

```
1. TCP connect to localhost:8554
2. Send RTSP OPTIONS  →  expect 200 OK
3. Send RTSP DESCRIBE →  expect SDP with media tracks
4. Send RTSP SETUP    →  request interleaved TCP transport
5. Send RTSP PLAY     →  start the stream
6. Wait up to 10s for any RTP byte
```

If step 6 times out — meaning Neolink's RTSP server is responding but not sending
frames (the exact hang that caused the original silent outage) — it counts as a
failure. After `failures_before_restart` (default: 2) consecutive failures, or after
`watchdog_timeout` seconds of total silence, the supervisor restarts Neolink and the
stream recovers.

**In plain terms:** the camera goes from `unavailable` to `live` in ~30 seconds
instead of ~29 hours.

---

## Testing the self-heal yourself

Once the add-on is running, you can simulate the exact original failure:

```bash
# SSH into your HA host, then enter the add-on container:
docker exec -it $(docker ps --filter name=aio_neolink -q) bash

# Find the Neolink PID and freeze it (mimics the silent hang):
kill -STOP $(pidof neolink)
```

Watch the add-on log. Within one or two `health_interval` cycles (~15–30 seconds)
you'll see:

```
camera movie_room probe failed: no RTP data within 10s (stream hung) — 1 in a row
camera movie_room probe failed: no RTP data within 10s (stream hung) — 2 in a row
neolink (re)started: unhealthy cameras: ['movie_room']
```

The camera card will briefly show **Recovering** (amber) and return to **Live**
(green) within seconds of the restart.

---

## Web GUI

The GUI is served via Home Assistant Ingress — it appears directly in the HA sidebar,
authenticated by HA, no port forwarding needed.

**Camera card states:**

| Color | Meaning |
|---|---|
| 🟢 Cyan — **Live** | Stream is healthy, RTP flowing |
| 🟡 Amber — **Recovering** | First probe failure, restart in progress |
| 🔴 Red — **Down** | Sustained failure, check camera IP/power |

**Controls on each card:**
- **IR** — toggle infrared night vision
- **Light** — toggle spotlight / floodlight
- **Siren** — trigger the alarm siren
- **◀ ▶** — PTZ nudge left / right (sends PTZ command + auto-Stop after 600ms)
- **Remove** — delete the camera and stop its stream

Controls are only shown as active (not greyed out) for features the camera actually
reported supporting during the probe.

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

**Stream shows in the GUI but Frigate can't connect**
- Confirm `host_network: true` is set (it is by default)
- Check that port 8554 is not blocked by a firewall on your HA host
- Try `rtsp://192.168.1.x:8554/movie_room` in VLC from another machine first

**PTZ / lights not working**
- These require the camera to support the feature — click **Test connection** to see
  what your model actually supports; unsupported features are greyed out
- Ensure the camera's HTTP API is reachable on port 80 (same IP as Baichuan)

**Neolink binary fails to start**
- Check the log for `neolink exited with code` messages
- The add-on ships Neolink `0.6.3-rc.3` from the QuantumEntangledAndy fork. If your
  camera needs a newer version, file an issue.

---

## Migrating from Neolink-latest

1. Note your camera names from your existing `neolink.toml`
2. Stop the **Neolink-latest** add-on
3. Install and start **aio-neolink**
4. Re-add each camera via the GUI using the same names
5. Update Frigate's RTSP URLs if the host IP changes (port 8554 stays the same)

Your Frigate entity names (`camera.movie_room`, etc.) will not change.

---

## What's next (Roadmap)

See [`ROADMAP.md`](ROADMAP.md) for the full list. Top priorities:

- **M2** — Verify reolink-aio API signatures against a real camera; wire up any
  missing PTZ/light calls
- **M3** — Fork Neolink and add an in-process frame watchdog (catches hangs *before*
  the external RTP probe notices — sub-second recovery)
- **M4** — Two-way audio via the Neolink backchannel + a talk button in the GUI

---

## License

GPL-3.0. Neolink is GPL-3.0; reolink-aio is MIT. This repo inherits GPL-3.0.

---

## Credits

- **[Neolink](https://github.com/QuantumEntangledAndy/neolink)** — QuantumEntangledAndy fork, originally by George Hilliard. The hard Baichuan reverse-engineering work that makes all of this possible.
- **[reolink-aio](https://github.com/starkillerOG/reolink_aio)** — @starkillerOG's officially Reolink-authorized Python library, the same one powering the native HA Reolink integration.
