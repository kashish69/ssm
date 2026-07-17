const STATUS_POLL_MS = 10_000;
const CAPTURE_POLL_MS = 1_800;
const CAPTURE_POLL_TIMEOUT_MS = 30_000;
const WIFI_POLL_MS = 1_500;
// Must stay slightly above the backend's wifi_timeout_seconds (50s) so the
// server settles the request first and we surface its verdict rather than
// racing it. 50s covers one full agent rescan+connect retry cycle — see the
// comment on wifi_timeout_seconds in backend/app/config.py.
const WIFI_POLL_TIMEOUT_MS = 52_000;

const loginScreen = document.getElementById("login-screen");
const appScreen = document.getElementById("app-screen");
const loginForm = document.getElementById("login-form");
const passkeyInput = document.getElementById("passkey-input");
const loginError = document.getElementById("login-error");
const logoutBtn = document.getElementById("logout-btn");
const refreshBtn = document.getElementById("refresh-btn");
const deviceGrid = document.getElementById("device-grid");
const emptyState = document.getElementById("empty-state");
const cardTemplate = document.getElementById("device-card-template");

const historyModal = document.getElementById("history-modal");
const historyTitle = document.getElementById("history-title");
const historyMessage = document.getElementById("history-message");
const historyCarousel = document.getElementById("history-carousel");
const carouselImg = document.getElementById("carousel-img");
const carouselThumbs = document.getElementById("carousel-thumbs");
const carouselPrev = document.getElementById("carousel-prev");
const carouselNext = document.getElementById("carousel-next");
const carouselDownload = document.getElementById("carousel-download");
const carouselFullscreen = document.getElementById("carousel-fullscreen");
const detailDevice = document.getElementById("detail-device");
const detailCaptured = document.getElementById("detail-captured");
const detailPosition = document.getElementById("detail-position");

const wifiModal = document.getElementById("wifi-modal");
const wifiForm = document.getElementById("wifi-form");
const wifiSsidInput = document.getElementById("wifi-ssid");
const wifiPasswordInput = document.getElementById("wifi-password");
const wifiFormStatus = document.getElementById("wifi-form-status");

let wifiModalDeviceId = null;
// True while a WiFi change is in flight — locks the modal until it resolves.
let wifiBusy = false;
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
  btn.addEventListener("click", () => {
    // Applying WiFi is a live action: hold the modal open until we know the
    // outcome, so the user can't wander off believing it worked.
    if (btn.dataset.close === "wifi-modal" && wifiBusy) return;
    closeModal(document.getElementById(btn.dataset.close));
  });
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

  // Clear existing per-card status pollers before rebuilding the grid so a
  // reload (e.g. the Refresh button) doesn't leak duplicate intervals.
  for (const id of statusIntervals.values()) clearInterval(id);
  statusIntervals.clear();

  deviceGrid.innerHTML = "";
  emptyState.classList.toggle("hidden", devices.length > 0);

  for (const device of devices) {
    renderDeviceCard(device);
  }
}

refreshBtn.addEventListener("click", async () => {
  refreshBtn.disabled = true;
  refreshBtn.classList.add("refreshing");
  try {
    await loadDevices();
  } finally {
    refreshBtn.disabled = false;
    refreshBtn.classList.remove("refreshing");
  }
});

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
let carouselItems = [];
let carouselIndex = 0;
let carouselDeviceName = "";

function showHistoryMessage(text) {
  historyCarousel.classList.add("hidden");
  historyMessage.textContent = text;
  historyMessage.classList.remove("hidden");
}

function renderCarousel() {
  const item = carouselItems[carouselIndex];
  carouselImg.src = item.image_url;
  detailDevice.textContent = carouselDeviceName;
  detailCaptured.textContent = formatTimestamp(item.captured_at);
  detailPosition.textContent = `${carouselIndex + 1} of ${carouselItems.length}`;
  carouselPrev.disabled = carouselIndex === 0;
  carouselNext.disabled = carouselIndex === carouselItems.length - 1;

  carouselThumbs.querySelectorAll(".carousel-thumb").forEach((thumb, i) => {
    thumb.classList.toggle("active", i === carouselIndex);
  });
  const activeThumb = carouselThumbs.children[carouselIndex];
  if (activeThumb) activeThumb.scrollIntoView({ block: "nearest", inline: "nearest" });
}

function goToCapture(index) {
  carouselIndex = Math.max(0, Math.min(index, carouselItems.length - 1));
  renderCarousel();
}

carouselPrev.addEventListener("click", () => goToCapture(carouselIndex - 1));
carouselNext.addEventListener("click", () => goToCapture(carouselIndex + 1));

document.addEventListener("keydown", (e) => {
  if (historyModal.classList.contains("hidden") || carouselItems.length === 0) return;
  if (e.key === "ArrowLeft") goToCapture(carouselIndex - 1);
  if (e.key === "ArrowRight") goToCapture(carouselIndex + 1);
  if (e.key === "Escape") closeModal(historyModal);
});

carouselDownload.addEventListener("click", async () => {
  const item = carouselItems[carouselIndex];
  if (!item) return;
  const filename = `capture-${(item.captured_at || "image").replace(/[:.]/g, "-")}.jpg`;
  try {
    const res = await fetch(item.image_url);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  } catch (_) {
    // fallback: open in a new tab if the blob fetch fails (e.g. CORS)
    window.open(item.image_url, "_blank");
  }
});

carouselFullscreen.addEventListener("click", () => {
  if (document.fullscreenElement) {
    document.exitFullscreen();
  } else if (carouselImg.requestFullscreen) {
    carouselImg.requestFullscreen();
  } else if (carouselImg.webkitRequestFullscreen) {
    carouselImg.webkitRequestFullscreen();
  }
});

async function onHistory(deviceId, displayName) {
  historyTitle.textContent = `History - ${displayName}`;
  carouselDeviceName = displayName;
  showHistoryMessage("Loading...");
  historyModal.classList.remove("hidden");

  try {
    const res = await apiFetch(`/api/devices/${encodeURIComponent(deviceId)}/history`);
    if (!res.ok) {
      showHistoryMessage("Failed to load history.");
      return;
    }
    const items = await res.json();
    if (items.length === 0) {
      showHistoryMessage("No captures yet.");
      return;
    }

    carouselItems = items;
    carouselIndex = 0;

    carouselThumbs.innerHTML = "";
    items.forEach((item, i) => {
      const thumb = document.createElement("button");
      thumb.type = "button";
      thumb.className = "carousel-thumb";
      thumb.setAttribute("aria-label", `Go to capture ${i + 1}`);
      const img = document.createElement("img");
      img.src = item.image_url;
      img.alt = "";
      thumb.appendChild(img);
      thumb.addEventListener("click", () => goToCapture(i));
      carouselThumbs.appendChild(thumb);
    });

    historyMessage.classList.add("hidden");
    historyCarousel.classList.remove("hidden");
    renderCarousel();
  } catch (_) {
    // showLogin() already triggered on 401
  }
}

// ---------------------------------------------------------------- wifi
function onWifiOpen(deviceId) {
  if (wifiBusy) return; // a change is still resolving for another device
  wifiModalDeviceId = deviceId;
  wifiSsidInput.value = "";
  wifiPasswordInput.value = "";
  setWifiStatus("");
  setWifiBusy(false);
  wifiModal.classList.remove("hidden");
}

function setWifiStatus(text, state) {
  wifiFormStatus.textContent = text;
  wifiFormStatus.classList.remove("ok", "err", "busy");
  if (state) wifiFormStatus.classList.add(state);
}

// While a WiFi change is in flight the modal is locked: the device drops off
// the network to switch, and the outcome is only knowable here — letting the
// user close and navigate away would silently discard the verdict.
function setWifiBusy(busy) {
  wifiBusy = busy;
  wifiForm.querySelector("button[type=submit]").disabled = busy;
  wifiSsidInput.disabled = busy;
  wifiPasswordInput.disabled = busy;
  const closeBtn = wifiModal.querySelector(".modal-close");
  if (closeBtn) closeBtn.disabled = busy;
  wifiModal.classList.toggle("busy", busy);
}

wifiForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!wifiModalDeviceId || wifiBusy) return;

  const deviceId = wifiModalDeviceId;
  setWifiBusy(true);
  setWifiStatus("Sending...", "busy");

  let requestId;
  try {
    const res = await apiFetch(`/api/devices/${encodeURIComponent(deviceId)}/wifi-config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ssid: wifiSsidInput.value, password: wifiPasswordInput.value }),
    });
    if (!res.ok) {
      setWifiStatus("Failed to send.", "err");
      setWifiBusy(false);
      return;
    }
    requestId = (await res.json()).request_id;
  } catch (_) {
    setWifiBusy(false);
    return; // showLogin() already triggered on 401
  }

  // "Sent" only means the broker took the command. The device now drops off
  // the network to switch, so wait for it to come back and confirm.
  const deadline = Date.now() + WIFI_POLL_TIMEOUT_MS;
  const finish = (text, state) => {
    setWifiStatus(text, state);
    setWifiBusy(false);
  };

  while (Date.now() < deadline) {
    const left = Math.ceil((deadline - Date.now()) / 1000);
    setWifiStatus(`Applying — waiting for device to reconnect... (${left}s)`, "busy");
    await new Promise((r) => setTimeout(r, WIFI_POLL_MS));

    let data;
    try {
      const res = await apiFetch(
        `/api/devices/${encodeURIComponent(deviceId)}/wifi-config/${requestId}`
      );
      if (!res.ok) continue;
      data = await res.json();
    } catch (_) {
      setWifiBusy(false);
      return;
    }

    if (data.status === "connected") {
      return finish(`Connected to ${data.ssid}.`, "ok");
    }
    if (data.status === "failed") {
      return finish(`Failed to connect: ${data.error_message || "unknown error"}`, "err");
    }
    if (data.status === "timeout") {
      return finish(
        "No confirmation from the device in time. If it can't join, it reverts to the previous network on its own.",
        "err"
      );
    }
  }

  finish(
    "No confirmation from the device in time. If it can't join, it reverts to the previous network on its own.",
    "err"
  );
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
