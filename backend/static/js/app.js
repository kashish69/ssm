const STATUS_POLL_MS = 10_000;
const CAPTURE_POLL_MS = 1_800;
const CAPTURE_POLL_TIMEOUT_MS = 30_000;

const loginScreen = document.getElementById("login-screen");
const appScreen = document.getElementById("app-screen");
const loginForm = document.getElementById("login-form");
const passkeyInput = document.getElementById("passkey-input");
const loginError = document.getElementById("login-error");
const logoutBtn = document.getElementById("logout-btn");
const deviceGrid = document.getElementById("device-grid");
const emptyState = document.getElementById("empty-state");
const cardTemplate = document.getElementById("device-card-template");

const historyModal = document.getElementById("history-modal");
const historyTitle = document.getElementById("history-title");
const historyList = document.getElementById("history-list");

const wifiModal = document.getElementById("wifi-modal");
const wifiForm = document.getElementById("wifi-form");
const wifiSsidInput = document.getElementById("wifi-ssid");
const wifiPasswordInput = document.getElementById("wifi-password");
const wifiFormStatus = document.getElementById("wifi-form-status");

let wifiModalDeviceId = null;
const statusIntervals = new Map();

function showLogin() {
  loginScreen.classList.remove("hidden");
  appScreen.classList.add("hidden");
  for (const id of statusIntervals.values()) clearInterval(id);
  statusIntervals.clear();
}

function showApp() {
  loginScreen.classList.add("hidden");
  appScreen.classList.remove("hidden");
}

async function apiFetch(path, options = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...options });
  if (res.status === 401) {
    showLogin();
    throw new Error("Not authenticated");
  }
  return res;
}

function formatTimestamp(iso) {
  if (!iso) return "";
  const d = new Date(iso.endsWith("Z") ? iso : iso + "Z");
  return d.toLocaleString();
}

function closeModal(modal) {
  modal.classList.add("hidden");
}

document.querySelectorAll("[data-close]").forEach((btn) => {
  btn.addEventListener("click", () => closeModal(document.getElementById(btn.dataset.close)));
});

// --------------------------------------------------------------- login
loginForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  loginError.classList.add("hidden");
  const res = await fetch("/api/auth/login", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ passkey: passkeyInput.value }),
  });
  if (!res.ok) {
    loginError.textContent = "Invalid passkey.";
    loginError.classList.remove("hidden");
    return;
  }
  passkeyInput.value = "";
  showApp();
  loadDevices();
});

logoutBtn.addEventListener("click", async () => {
  await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
  showLogin();
});

// ------------------------------------------------------------- devices
async function loadDevices() {
  const res = await apiFetch("/api/devices");
  const devices = await res.json();

  deviceGrid.innerHTML = "";
  emptyState.classList.toggle("hidden", devices.length > 0);

  for (const device of devices) {
    renderDeviceCard(device);
  }
}

function renderDeviceCard(device) {
  const node = cardTemplate.content.cloneNode(true);
  const card = node.querySelector(".device-card");
  card.dataset.deviceId = device.device_id;
  card.querySelector(".card-name").textContent = device.display_name;

  applyStatus(card, device.online);

  const captureBtn = card.querySelector(".capture-btn");
  const historyBtn = card.querySelector(".history-btn");
  const wifiBtn = card.querySelector(".wifi-btn");
  const captureStatusEl = card.querySelector(".capture-status");

  captureBtn.addEventListener("click", () => onCapture(device.device_id, captureBtn, captureStatusEl, card));
  historyBtn.addEventListener("click", () => onHistory(device.device_id, device.display_name));
  wifiBtn.addEventListener("click", () => onWifiOpen(device.device_id));

  deviceGrid.appendChild(node);

  const cardEl = deviceGrid.querySelector(`[data-device-id="${cssEscape(device.device_id)}"]`);
  refreshLatest(device.device_id, cardEl);

  const intervalId = setInterval(() => refreshStatus(device.device_id, cardEl), STATUS_POLL_MS);
  statusIntervals.set(device.device_id, intervalId);
}

function cssEscape(str) {
  return window.CSS && CSS.escape ? CSS.escape(str) : str.replace(/["\\]/g, "\\$&");
}

function applyStatus(card, online) {
  const pill = card.querySelector(".status-pill");
  const captureBtn = card.querySelector(".capture-btn");
  pill.textContent = online ? "online" : "offline";
  pill.classList.toggle("online", online);
  pill.classList.toggle("offline", !online);
  captureBtn.disabled = !online;
}

async function refreshStatus(deviceId, card) {
  try {
    const res = await apiFetch(`/api/devices/${encodeURIComponent(deviceId)}/status`);
    if (!res.ok) return;
    const data = await res.json();
    applyStatus(card, !!data.online);
  } catch (_) {
    // showLogin() already triggered on 401
  }
}

async function refreshLatest(deviceId, card) {
  const img = card.querySelector(".viewfinder-img");
  const placeholder = card.querySelector(".viewfinder-placeholder");
  const tsOverlay = card.querySelector(".timestamp-overlay");

  try {
    const res = await apiFetch(`/api/devices/${encodeURIComponent(deviceId)}/latest`);
    if (res.status === 404) {
      img.classList.remove("loaded");
      placeholder.classList.remove("hidden");
      tsOverlay.textContent = "";
      return;
    }
    if (!res.ok) return;
    const data = await res.json();
    img.src = data.image_url;
    img.classList.add("loaded");
    placeholder.classList.add("hidden");
    tsOverlay.textContent = formatTimestamp(data.captured_at);
  } catch (_) {
    // showLogin() already triggered on 401
  }
}

// ------------------------------------------------------------- capture
async function onCapture(deviceId, btn, statusEl, card) {
  btn.disabled = true;
  statusEl.textContent = "Requesting capture...";

  let requestId;
  try {
    const res = await apiFetch(`/api/devices/${encodeURIComponent(deviceId)}/capture`, { method: "POST" });
    if (res.status === 409) {
      statusEl.textContent = "Device is offline.";
      btn.disabled = false;
      return;
    }
    if (res.status === 429) {
      statusEl.textContent = "Capture already in progress, try again shortly.";
      btn.disabled = false;
      return;
    }
    if (!res.ok) {
      statusEl.textContent = "Failed to request capture.";
      btn.disabled = false;
      return;
    }
    const data = await res.json();
    requestId = data.request_id;
  } catch (_) {
    return;
  }

  const deadline = Date.now() + CAPTURE_POLL_TIMEOUT_MS;
  statusEl.textContent = "Capturing...";

  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, CAPTURE_POLL_MS));
    let data;
    try {
      const res = await apiFetch(`/api/devices/${encodeURIComponent(deviceId)}/capture/${requestId}`);
      if (!res.ok) continue;
      data = await res.json();
    } catch (_) {
      return;
    }

    if (data.status === "completed") {
      statusEl.textContent = "";
      await refreshLatest(deviceId, card);
      btn.disabled = false;
      return;
    }
    if (data.status === "failed") {
      statusEl.textContent = `Capture failed: ${data.error_message || "unknown error"}`;
      btn.disabled = false;
      return;
    }
    if (data.status === "timeout") {
      statusEl.textContent = "No response from device. Check it's online and try again.";
      btn.disabled = false;
      return;
    }
  }

  statusEl.textContent = "No response from device. Check it's online and try again.";
  btn.disabled = false;
}

// ------------------------------------------------------------- history
async function onHistory(deviceId, displayName) {
  historyTitle.textContent = `History - ${displayName}`;
  historyList.innerHTML = "Loading...";
  historyModal.classList.remove("hidden");

  try {
    const res = await apiFetch(`/api/devices/${encodeURIComponent(deviceId)}/history`);
    if (!res.ok) {
      historyList.textContent = "Failed to load history.";
      return;
    }
    const items = await res.json();
    historyList.innerHTML = "";
    if (items.length === 0) {
      historyList.textContent = "No captures yet.";
      return;
    }
    for (const item of items) {
      const div = document.createElement("div");
      div.className = "history-item";
      const img = document.createElement("img");
      img.src = item.image_url;
      img.alt = "Past capture";
      const ts = document.createElement("div");
      ts.className = "history-ts";
      ts.textContent = formatTimestamp(item.captured_at);
      div.appendChild(img);
      div.appendChild(ts);
      historyList.appendChild(div);
    }
  } catch (_) {
    // showLogin() already triggered on 401
  }
}

// ---------------------------------------------------------------- wifi
function onWifiOpen(deviceId) {
  wifiModalDeviceId = deviceId;
  wifiSsidInput.value = "";
  wifiPasswordInput.value = "";
  wifiFormStatus.textContent = "";
  wifiModal.classList.remove("hidden");
}

wifiForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!wifiModalDeviceId) return;
  wifiFormStatus.textContent = "Sending...";
  try {
    const res = await apiFetch(`/api/devices/${encodeURIComponent(wifiModalDeviceId)}/wifi-config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ssid: wifiSsidInput.value, password: wifiPasswordInput.value }),
    });
    wifiFormStatus.textContent = res.ok ? "Sent to device." : "Failed to send.";
  } catch (_) {
    // showLogin() already triggered on 401
  }
});

// ---------------------------------------------------------------- init
(async function init() {
  try {
    const res = await fetch("/api/devices", { credentials: "same-origin" });
    if (res.status === 401) {
      showLogin();
      return;
    }
    showApp();
    const devices = await res.json();
    deviceGrid.innerHTML = "";
    emptyState.classList.toggle("hidden", devices.length > 0);
    for (const device of devices) renderDeviceCard(device);
  } catch (_) {
    showLogin();
  }
})();

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("service-worker.js");
}
