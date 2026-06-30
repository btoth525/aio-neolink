// aio-neolink GUI logic.
// Talks to the FastAPI surface under the same origin (Ingress-friendly: relative paths).

const $ = (sel, root = document) => root.querySelector(sel);
const api = (path, opts) => fetch(path, opts).then(async r => {
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `${r.status}`);
  return body;
});

let cameras = [];
let health = {};

function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("bad", bad);
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), 3200);
}

function stateFor(name) {
  const h = health[name];
  if (!h) return { cls: "warn", label: "Starting", note: "" };
  if (h.healthy) return { cls: "live", label: "Live", note: "" };
  if (h.consecutive_failures && h.consecutive_failures < 2)
    return { cls: "warn", label: "Recovering", note: h.last_error || "" };
  return { cls: "down", label: "Down", note: h.last_error || "no signal" };
}

function rtspUrl(cam) {
  // RTSP is served on port 8554 of the HA host directly — not through Ingress.
  // homeassistant.local resolves to your HA host on the local network.
  // Replace with your HA host's LAN IP if mDNS isn't working (e.g. 192.168.1.x).
  return `rtsp://homeassistant.local:8554/${cam.name}`;
}

function card(cam) {
  const s = stateFor(cam.name);
  const caps = cam.capabilities || {};
  const el = document.createElement("article");
  el.className = `card ${s.cls}`;
  el.innerHTML = `
    <h3>${cam.name}</h3>
    <div class="addr">${cam.address ? `${cam.address}:${cam.port}` : (cam.uid || "—")}</div>
    <div class="status"><span class="led"></span>${s.label}
      ${s.note ? `<small>· ${s.note}</small>` : ""}</div>
    <div class="rtsp">${rtspUrl(cam)}</div>
    <div class="controls">
      <button data-act="ir"        ${caps.supports_ir ? "" : "disabled"}>IR</button>
      <button data-act="spotlight" ${caps.supports_spotlight ? "" : "disabled"}>Light</button>
      <button data-act="siren"     ${caps.supports_siren ? "" : "disabled"}>Siren</button>
      <button data-act="ptz"       data-op="Left"  ${caps.supports_ptz ? "" : "disabled"}>◀</button>
      <button data-act="ptz"       data-op="Right" ${caps.supports_ptz ? "" : "disabled"}>▶</button>
      <button class="del" data-act="delete">Remove</button>
    </div>`;

  el.querySelectorAll(".controls button").forEach(btn => {
    btn.addEventListener("click", () => onControl(cam, btn.dataset.act, btn.dataset.op));
  });
  return el;
}

function render() {
  const grid = $("#cameras");
  grid.innerHTML = "";
  if (!cameras.length) {
    grid.innerHTML = `<p class="empty">No cameras yet. Add your first one below.</p>`;
  } else {
    cameras.forEach(c => grid.appendChild(card(c)));
  }
  const live = cameras.filter(c => stateFor(c.name).cls === "live").length;
  $("#summary").textContent = `${live}/${cameras.length} live`;
}

async function refresh() {
  try {
    [cameras, health] = await Promise.all([
      api("api/cameras"),
      api("api/health").catch(() => ({})),
    ]);
    render();
  } catch (e) {
    toast(`Couldn't load cameras: ${e.message}`, true);
  }
}

async function onControl(cam, action, op) {
  if (action === "delete") {
    if (!confirm(`Remove ${cam.name}? Frigate will lose this stream.`)) return;
    try {
      await api(`api/cameras/${cam.name}`, { method: "DELETE" });
      toast(`Removed ${cam.name}`);
      refresh();
    } catch (e) { toast(e.message, true); }
    return;
  }
  // Toggle semantics for ir/spotlight/siren are naive on/off; the camera reports back.
  const payload = { action };
  if (action === "ptz") { payload.operation = op; payload.speed = 32; }
  else { payload.on = true; }
  try {
    await api(`api/cameras/${cam.name}/control`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    toast(`${cam.name}: ${action}${op ? " " + op : ""} sent`);
    // PTZ stop after a short nudge
    if (action === "ptz") {
      setTimeout(() => api(`api/cameras/${cam.name}/control`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ action: "ptz", operation: "Stop" }),
      }).catch(() => {}), 600);
    }
  } catch (e) { toast(e.message, true); }
}

// --- add dialog ----------------------------------------------------------------
const dialog = $("#addDialog");
$("#openAdd").addEventListener("click", () => { $("#probeResult").hidden = true; dialog.showModal(); });
$("#cancelBtn").addEventListener("click", () => dialog.close());

$("#testBtn").addEventListener("click", async () => {
  const f = $("#addForm");
  const box = $("#probeResult");
  box.hidden = false; box.classList.remove("bad"); box.textContent = "Probing camera…";
  try {
    const res = await api("api/cameras/probe", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({
        address: f.address.value, port: Number(f.port.value),
        username: f.username.value, password: f.password.value,
      }),
    });
    const c = res.capabilities || {};
    const feats = ["supports_ptz", "supports_spotlight", "supports_siren", "supports_ir"]
      .filter(k => c[k]).map(k => k.replace("supports_", ""));
    box.textContent = `${c.model || "Camera"} reached · ${feats.length ? feats.join(", ") : "video only"}`;
  } catch (e) {
    box.classList.add("bad");
    box.textContent = `Couldn't reach it: ${e.message}`;
  }
});

$("#addForm").addEventListener("submit", async (ev) => {
  const f = ev.target;
  const body = {
    name: f.name.value.trim(),
    address: f.address.value.trim() || null,
    port: Number(f.port.value),
    username: f.username.value,
    password: f.password.value,
    stream: f.stream.value,
    enabled: true,
  };
  try {
    await api("api/cameras", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    toast(`Saved ${body.name}`);
    dialog.close();
    f.reset();
    refresh();
  } catch (e) {
    ev.preventDefault();
    toast(e.message, true);
  }
});

// poll health so cards update as the watchdog reports
refresh();
setInterval(refresh, 8000);
