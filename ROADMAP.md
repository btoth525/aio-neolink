# Roadmap

This v0.1 is a **scaffold that runs the right shape**, not a finished product. Here's
the honest state of each piece and what to do next.

## Done (works as written, given a real Neolink binary)

- **HAOS add-on packaging** — `config.yaml`, `build.yaml`, `Dockerfile`, Ingress GUI panel.
- **Self-healing watchdog** — `pipeline.py` probes each RTSP path with `gst-discoverer`
  and restarts the pipeline on sustained silence. This is the fix for the exact bug you
  hit (silent hang, container still "started").
- **Camera store + TOML generation** — add a camera in the GUI → `neolink.toml`
  regenerated → pipeline restarted. No hand-editing.
- **REST API + GUI** — list/add/delete cameras, probe capabilities, per-camera health,
  send IR/spotlight/siren/PTZ.

## Needs real wiring before deploy

1. **Neolink binary** — `rootfs/fetch-neolink.sh` currently installs a placeholder.
   Replace it with a real download of the QuantumEntangledAndy release for your arch,
   or add a `cargo build` stage. This is the one blocker to a working build.
2. **Verify the fork's TOML schema** — `config_gen.py` uses the `[[rtsp]]` bind block
   and `[[cameras]]` shape. Confirm against the exact Neolink version you ship; older
   versions used a flatter `bind =` at top level. Adjust `render()` accordingly.
3. **reolink-aio method names** — `control.py` calls `set_ir_lights`, `set_spotlight`,
   `set_siren`, `set_ptz_command`, and `supported(ch, feature)`. Pin a reolink-aio
   version and confirm these signatures; the library evolves. Capability flag names
   (`floodLight` vs `spotlight`, etc.) especially need a real-camera check.
4. **Battery cameras** — store + TOML support `uid`, but the control plane currently
   requires a wired address. Battery/UDP control is a follow-up.

## Reliability iteration (the real prize)

The watchdog restarts the **whole** Neolink process, because stock Neolink has no
per-camera control. Two upgrades, in order of payoff:

- **Per-camera recovery** — once you fork Neolink, add a small control socket (or use
  its MQTT control surface) so the supervisor can restart a single camera's pipeline
  instead of bouncing all of them.
- **In-process frame watchdog** — the most robust fix lives *inside* Neolink: in the
  GStreamer `appsrc` feed loop (the one from George's blog that pushes camera bytes),
  track "time since last buffer pushed." If it exceeds a threshold, force a reconnect
  to the camera rather than waiting for an external probe to notice. This catches the
  hang at the source. This is the single highest-value change to make in the fork.

## "Best of both worlds" milestones

- **M1** — this scaffold builds with a real Neolink binary; cameras stream; watchdog
  recovers a forced hang. (Prove the self-heal with a deliberate `kill -STOP`.)
- **M2** — GUI probe auto-fills capabilities from a real camera; PTZ/lights/siren work.
- **M3** — fork Neolink, add the in-process frame watchdog, point `fetch-neolink.sh` at
  your release. Now recovery is sub-second and per-camera.
- **M4** — two-way audio: reolink-aio + Neolink's audio backchannel, surfaced in the GUI.

## Testing the self-heal without waiting for a real outage

Once running, simulate today's exact failure mode:

```bash
# find the neolink PID inside the addon container, then:
kill -STOP <neolink_pid>     # freeze it — mimics the silent hang
# watch the supervisor log: within health_interval*failures it should restart the pipeline
```

If the camera returns to `live` on its own, the core promise is proven.
