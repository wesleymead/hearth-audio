const CLIENT_SETTINGS_KEY = "hearth-display-settings-v1";
const BROWSER_NODE_NAME_KEY = "hearth-temp-node-name-v1";
const BROWSER_NODE_SESSION_KEY = "hearth-temp-node-session-v1";
const CLIENT_SETTINGS_DEFAULTS = {
  screensaverSeconds: 120,
  quietHoursEnabled: false,
  quietHoursStart: "23:00",
  quietHoursEnd: "06:00",
};

const app = {
  state: null,
  activeNodeId: null,
  draftActions: [],
  activeInput: null,
  keyboardShift: false,
  keyboardSymbols: false,
  lastInteraction: Date.now(),
  screensaverActive: false,
  screensaverDimTimer: null,
  displayOffTimer: null,
  displayOff: false,
  clientSettings: null,
  cursorTimer: null,
  volumeDragNodeId: null,
  pairingKnownIds: null,
  browserNodeId: null,
  browserNodeName: "",
  browserHeartbeatTimer: null,
  soundboard: null,
  selectedSounds: new Set(),
  selectedSoundTargets: new Set(["hearth-hub"]),
  displayOnly: new URLSearchParams(location.search).get("display") === "node",
  displayNodeId: new URLSearchParams(location.search).get("node") || "",
  hubDisplay: new URLSearchParams(location.search).get("display") === "hub"
    || ["127.0.0.1", "localhost", "::1"].includes(location.hostname),
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  let data = {};
  try { data = await response.json(); } catch (_) { /* no JSON response */ }
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function loadClientSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem(CLIENT_SETTINGS_KEY) || "{}");
    const allowedTimers = new Set([0, 30, 60, 120, 300, 600, 1800]);
    const timer = Number(saved.screensaverSeconds);
    const timePattern = /^([01]\d|2[0-3]):[0-5]\d$/;
    return {
      screensaverSeconds: allowedTimers.has(timer) ? timer : CLIENT_SETTINGS_DEFAULTS.screensaverSeconds,
      quietHoursEnabled: Boolean(saved.quietHoursEnabled),
      quietHoursStart: timePattern.test(saved.quietHoursStart || "") ? saved.quietHoursStart : CLIENT_SETTINGS_DEFAULTS.quietHoursStart,
      quietHoursEnd: timePattern.test(saved.quietHoursEnd || "") ? saved.quietHoursEnd : CLIENT_SETTINGS_DEFAULTS.quietHoursEnd,
    };
  } catch (_) {
    return { ...CLIENT_SETTINGS_DEFAULTS };
  }
}

function saveClientSettings() {
  localStorage.setItem(CLIENT_SETTINGS_KEY, JSON.stringify(app.clientSettings));
  app.lastInteraction = Date.now();
  renderClientDisplaySettings();
}

function renderClientDisplaySettings() {
  if (!app.clientSettings) return;
  $("#screensaver-timer").value = String(app.clientSettings.screensaverSeconds);
  $("#quiet-hours-enabled").checked = app.clientSettings.quietHoursEnabled;
  $("#quiet-hours-start").value = app.clientSettings.quietHoursStart;
  $("#quiet-hours-end").value = app.clientSettings.quietHoursEnd;
  $(".quiet-hours-card").hidden = !app.hubDisplay;
}

function minutesSinceMidnight(value) {
  const [hour, minute] = value.split(":").map(Number);
  return hour * 60 + minute;
}

function isQuietHours(now = new Date()) {
  if (!app.clientSettings?.quietHoursEnabled) return false;
  const current = now.getHours() * 60 + now.getMinutes();
  const start = minutesSinceMidnight(app.clientSettings.quietHoursStart);
  const end = minutesSinceMidnight(app.clientSettings.quietHoursEnd);
  if (start === end) return true;
  return start < end ? current >= start && current < end : current >= start || current < end;
}

async function setHardwareDisplayPower(state) {
  if (!app.hubDisplay) return;
  try {
    await api("/api/display/power", { method: "POST", body: JSON.stringify({ state }) });
  } catch (_) { /* A remote browser still uses the black sleep overlay. */ }
}

function turnDisplayOff() {
  if (app.displayOff) return;
  app.displayOff = true;
  const overlay = $("#display-off");
  overlay.classList.add("open");
  overlay.setAttribute("aria-hidden", "false");
  setTimeout(() => { if (app.displayOff) setHardwareDisplayPower("off"); }, 350);
}

function wakeDisplay() {
  if (!app.displayOff) return;
  app.displayOff = false;
  setHardwareDisplayPower("on");
  const overlay = $("#display-off");
  overlay.classList.remove("open");
  overlay.setAttribute("aria-hidden", "true");
  hideScreensaver();
}

function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.className = `toast show${error ? " error" : ""}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { element.className = "toast"; }, 2800);
}

function formatProfile(node) {
  const id = node.audio.activeProfile;
  return node.audio.profiles.find(profile => profile.id === id)?.label.replace(" · ", " · ") || id;
}

function updateClock() {
  const now = new Date();
  const time = now.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", hour12: true });
  const date = now.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", year: "numeric" });
  $("#clock-time").textContent = time;
  $("#clock-date").textContent = now.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
  $("#screensaver-time").textContent = time;
  $("#screensaver-date").textContent = date;
  renderProgress();
  checkScreensaver();
  if (!isQuietHours() && app.displayOff) wakeDisplay();
}

function isPlaying(playback) { return playback?.state === "playing"; }

function effectivePosition(playback) {
  let position = Number(playback.positionSeconds || 0);
  if (isPlaying(playback) && playback.positionUpdatedAt) {
    position += Math.max(0, Date.now() / 1000 - Number(playback.positionUpdatedAt));
  }
  return Math.min(position, Number(playback.durationSeconds || position));
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const whole = Math.floor(seconds);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

function artworkUrl(playback) {
  return playback.artworkRevision ? `/api/artwork?v=${playback.artworkRevision}` : "";
}

function setArtwork(containerImage, playback) {
  const image = $(containerImage);
  const url = artworkUrl(playback);
  if (url && image.dataset.url !== url) {
    image.src = url;
    image.dataset.url = url;
  } else if (!url) {
    image.removeAttribute("src");
    image.dataset.url = "";
  }
  image.parentElement.classList.toggle("has-art", Boolean(url));
}

function renderProgress() {
  if (!app.state) return;
  const playback = app.state.system.playback;
  const position = effectivePosition(playback);
  const duration = Number(playback.durationSeconds || 0);
  $("#screensaver-progress").style.width = `${duration ? Math.min(100, position / duration * 100) : 0}%`;
  $("#screensaver-elapsed").textContent = formatDuration(position);
  $("#screensaver-duration").textContent = formatDuration(duration);
}

function renderSystem() {
  const { system } = app.state;
  const localHub = app.state.nodes?.find((node) => node.local);
  $("#system-hardware").textContent = (localHub?.hardware || "Raspberry Pi").replace(/ · Local playback$/, "");
  const soundboardEnabled = system.settings?.soundboardEnabled !== false;
  $("#soundboard-enabled").checked = soundboardEnabled;
  $("#soundboard-nav").hidden = !soundboardEnabled;
  if (!soundboardEnabled && $("#view-soundboard").classList.contains("active")) {
    $("#view-soundboard").classList.remove("active");
    $("#view-home").classList.add("active");
    $("#soundboard-nav").classList.remove("active");
    $('[data-view="home"]').classList.add("active");
  }
  $("#wifi-label").textContent = `${system.wifi.ssid} · ${system.wifi.signal}%`;
  $("#system-network").textContent = `${system.wifi.ssid} · ${system.wifi.signal}%`;
  const playback = system.playback;
  const playing = isPlaying(playback);
  $("#source-label").textContent = playing ? `${playback.source} · ${playback.state}`.toUpperCase() : "NOT PLAYING";
  $("#track-title").textContent = playing ? playback.title || "AirPlay audio" : "Nothing playing";
  $("#track-artist").textContent = playing ? [playback.artist, playback.album].filter(Boolean).join(" — ") || playback.client || "AirPlay 2" : "—";
  setArtwork("#player-artwork", playback);
  const idleScene = $("#screensaver-idle");
  const playingScene = $("#screensaver-playing");
  idleScene.classList.toggle("active", !playing);
  playingScene.classList.toggle("active", playing);
  idleScene.setAttribute("aria-hidden", String(playing));
  playingScene.setAttribute("aria-hidden", String(!playing));
  $("#screensaver-title").textContent = playback.title || "AirPlay audio";
  $("#screensaver-artist").textContent = playback.artist || playback.client || "Unknown artist";
  $("#screensaver-album").textContent = playback.album || "";
  setArtwork("#screensaver-artwork", playback);
  const canControl = Boolean(playback.transportControl);
  $("#transport-controls").hidden = !canControl;
  $$("#previous-track, #play-pause, #next-track").forEach(button => {
    button.disabled = !canControl;
    button.title = canControl ? "" : "AirPlay 2 transport is controlled from the sender";
  });
  $("#play-pause").textContent = playback.state === "playing" ? "Ⅱ" : "▶";
  renderClientDisplaySettings();
  const playbackMode = system.settings?.playbackMode || "stereo";
  $("#playback-mode").value = playbackMode;
  const channels = Number(system.settings?.playbackChannels || 2);
  $("#playback-mode-status").textContent = channels === 2
    ? "2-channel PCM · all locations"
    : `${channels === 6 ? "5.1" : "7.1"} PCM · HDMI locations only · reconnect AirPlay`;
  renderProgress();
}

function showScreensaver() {
  closeOverlays();
  app.screensaverActive = true;
  $("#screensaver").classList.add("open");
  $("#screensaver").setAttribute("aria-hidden", "false");
  armScreensaverDimmer();
  renderSystem();
}

function hideScreensaver() {
  app.screensaverActive = false;
  app.lastInteraction = Date.now();
  clearTimeout(app.screensaverDimTimer);
  clearTimeout(app.displayOffTimer);
  app.screensaverDimTimer = null;
  app.displayOffTimer = null;
  $("#screensaver").classList.remove("dimmed");
  $("#screensaver").classList.remove("open");
  $("#screensaver").setAttribute("aria-hidden", "true");
}

function armScreensaverDimmer() {
  clearTimeout(app.screensaverDimTimer);
  clearTimeout(app.displayOffTimer);
  $("#screensaver").classList.remove("dimmed");
  if (!app.hubDisplay) return;
  app.screensaverDimTimer = setTimeout(() => {
    if (!app.screensaverActive) return;
    $("#screensaver").classList.add("dimmed");
    if (isQuietHours()) {
      app.displayOffTimer = setTimeout(() => {
        if (app.screensaverActive && isQuietHours()) turnDisplayOff();
      }, 10000);
    }
  }, 30000);
}

function checkScreensaver() {
  if (!app.state || app.screensaverActive || app.displayOff) return;
  const seconds = Number(app.clientSettings?.screensaverSeconds || 0);
  if (seconds && Date.now() - app.lastInteraction >= seconds * 1000) showScreensaver();
}

function renderNodes() {
  const grid = $("#node-grid");
  grid.innerHTML = "";
  if (!app.state.nodes.length) {
    grid.innerHTML = '<div class="empty-panel rooms-empty"><span class="orb">⌁</span><h2>No rooms paired yet</h2><p>Select Add node, then hold the button on a node for five seconds. It will roam for this hub automatically.</p></div>';
    return;
  }
  const template = $("#node-card-template");
  app.state.nodes.forEach(node => {
    const card = template.content.firstElementChild.cloneNode(true);
    card.dataset.nodeId = node.id;
    card.classList.toggle("local-node", Boolean(node.local));
    card.classList.toggle("browser-node", Boolean(node.browserTemporary));
    const ownsBrowserNode = Boolean(node.browserTemporary && node.id === app.browserNodeId);
    card.classList.toggle("owned-browser-node", ownsBrowserNode);
    card.classList.toggle("offline", !node.online);
    $(".online-dot", card).hidden = Boolean(node.local);
    $(".node-name", card).textContent = node.name;
    $(".node-hardware", card).textContent = node.local ? node.hardware : `${node.hardware} · ${node.online ? "Online" : "Offline"}`;
    const audioReady = !node.browserTemporary || Boolean(node.audioReady);
    $(".output-label", card).textContent = !node.online
      ? "No Output"
      : node.browserTemporary && !audioReady
        ? "Browser stereo · Tap Play"
        : formatProfile(node);
    const power = $(".power-button", card);
    power.hidden = Boolean(node.local || (node.browserTemporary && !ownsBrowserNode));
    if (ownsBrowserNode) {
      power.classList.add("remove-temp-button");
      power.title = "Remove temporary node";
      power.setAttribute("aria-label", "Remove temporary node");
      power.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>';
      power.addEventListener("click", removeBrowserNode);
    } else {
      power.classList.toggle("on", node.power.state === "on");
      power.title = node.power.controlType === "button" ? "Run power action" : `Power ${node.power.state}`;
      power.addEventListener("click", () => controlPower(node));
    }
    const mute = $(".mute-button", card);
    mute.disabled = !node.online || !audioReady;
    mute.classList.toggle("active", node.muted);
    mute.addEventListener("click", async () => {
      await updateNode(node.id, { muted: !node.muted });
    });
    const volume = $(".volume", card);
    volume.disabled = !node.online || !audioReady;
    const output = $(".volume-value", card);
    volume.value = node.volume;
    volume.title = "Software volume · does not control an HDMI receiver";
    volume.style.setProperty("--value", `${node.volume}%`);
    output.textContent = `${node.volume}%`;
    volume.addEventListener("input", () => {
      output.textContent = `${volume.value}%`;
      volume.style.setProperty("--value", `${volume.value}%`);
    });
    volume.addEventListener("pointerdown", () => { app.volumeDragNodeId = node.id; });
    volume.addEventListener("change", async () => {
      const value = Number(volume.value);
      app.volumeDragNodeId = null;
      await updateNode(node.id, { volume: value });
    });
    volume.addEventListener("pointercancel", () => { app.volumeDragNodeId = null; });
    const settings = $(".settings-button", card);
    settings.hidden = Boolean(node.browserTemporary && !ownsBrowserNode);
    settings.addEventListener("click", () => ownsBrowserNode ? openBrowserNodeDialog() : openSettings(node.id));
    grid.append(card);
  });
  renderBrowserNodeStatus();
}

async function updateNode(nodeId, changes) {
  try {
    const result = await api(`/api/nodes/${encodeURIComponent(nodeId)}/settings`, { method: "POST", body: JSON.stringify(changes) });
    const index = app.state.nodes.findIndex(node => node.id === nodeId);
    app.state.nodes[index] = result.node;
    renderNodes();
  } catch (error) { toast(error.message, true); }
}

async function controlPower(node) {
  let state = "press";
  if (node.power.controlType === "switch") state = node.power.state === "on" ? "off" : "on";
  try {
    const result = await api(`/api/nodes/${encodeURIComponent(node.id)}/power`, { method: "POST", body: JSON.stringify({ state }) });
    if (node.power.controlType === "switch") node.power.state = result.state;
    if (result.piPowerMode === "soft_sleep") node.systemPowerState = result.state === "off" ? "sleeping" : "awake";
    if (result.piPowerMode === "halt" && result.state === "off") node.systemPowerState = "halting";
    renderNodes();
    const piMessage = result.piPowerMode === "soft_sleep"
      ? (result.state === "off" ? "soft sleep scheduled" : "waking audio and display")
      : result.piPowerMode === "halt" && result.state === "off"
        ? "safe shutdown scheduled"
        : result.deviceControlled ? "connected-device command sent" : "power command accepted";
    toast(`${node.name}: ${piMessage}`);
  } catch (error) { toast(error.message, true); }
}

async function controlPlayback(operation) {
  try {
    await api(`/api/playback/${operation}`, { method: "POST", body: "{}" });
  } catch (error) { toast(error.message, true); }
}

async function sendDisplayHeartbeat() {
  if (!app.displayOnly || !app.displayNodeId) return;
  try {
    await api(`/api/nodes/${encodeURIComponent(app.displayNodeId)}/display/heartbeat`, {
      method: "POST",
      body: "{}",
    });
  } catch (_) { /* The next heartbeat will retry after a brief hub outage. */ }
}

function showCursorTemporarily(event) {
  if (app.displayOnly || event?.pointerType === "touch") {
    document.body.classList.add("cursor-hidden");
    clearTimeout(app.cursorTimer);
    return;
  }
  document.body.classList.remove("cursor-hidden");
  clearTimeout(app.cursorTimer);
  if (app.hubDisplay) {
    app.cursorTimer = setTimeout(() => document.body.classList.add("cursor-hidden"), 1500);
  }
}

function openSettings(nodeId) {
  app.activeNodeId = nodeId;
  const node = app.state.nodes.find(item => item.id === nodeId);
  $$("#settings-tabs button").forEach(button => {
    button.hidden = Boolean(node.local) && ["power", "network", "ir-actions"].includes(button.dataset.tab);
  });
  $("#rescan-audio").hidden = Boolean(node.local);
  app.draftActions = structuredClone(node.actions || []);
  $("#drawer-title").textContent = node.name;
  $("#drawer-hardware").textContent = `${node.hardware} · ${node.connection.type}`;
  renderButtonHardware(node);
  $("#node-name").value = node.name;
  $("#node-room").value = node.room;
  renderPowerSettings(node);
  renderAudioSettings(node);
  renderNetworkSettings(node);
  renderIrSettings(node);
  switchTab("general");
  $("#settings-drawer").classList.add("open");
  $("#settings-drawer").setAttribute("aria-hidden", "false");
  $("#scrim").classList.add("open");
}

function renderButtonHardware(node) {
  const button = node.button;
  $("#button-hardware").hidden = !button?.enabled;
  if (!button?.enabled) return;
  $("#button-gpio").textContent = button.gpio;
  $("#button-wiring").textContent = `Physical pin ${button.physicalPin} → switch → Ground pin ${button.groundPin}`;
  const last = button.lastEvent ? ` · last ${button.lastEvent.type.replace("_", " ")}` : "";
  $("#button-status").textContent = `${button.status}${last}`;
}

function populateActionSelect(selector, actions, selectedId) {
  const select = $(selector);
  select.innerHTML = "";
  select.add(new Option("No action assigned", "", false, !selectedId));
  actions.forEach(action => select.add(new Option(action.name, action.id, false, action.id === selectedId)));
}

function renderPowerSettings(node) {
  const power = node.power || {};
  setSegment($("#power-transport"), power.transport || "auto");
  setSegment($("#power-type"), power.controlType || "button");
  $("#state-mode").value = power.stateMode || "stateless";
  $("#pi-power-mode").value = power.piPowerMode || "always_on";
  $("#shutdown-delay").value = String(power.shutdownDelaySeconds || 10);
  populateActionSelect("#power-toggle-action", node.actions || [], power.pressActionId || "");
  populateActionSelect("#power-on-action", node.actions || [], power.onActionId || "");
  populateActionSelect("#power-off-action", node.actions || [], power.offActionId || "");
  const cec = $('button[data-value="hdmi_cec"]', $("#power-transport"));
  cec.disabled = !node.audio.detected.hdmiConnected;
  cec.title = cec.disabled ? "Connect and rescan HDMI before selecting HDMI-CEC" : "HDMI receiver detected";
  updatePowerForm();
}

function updatePowerForm() {
  const node = app.state.nodes.find(item => item.id === app.activeNodeId);
  if (!node) return;
  const type = segmentValue($("#power-type")) || "button";
  const transport = segmentValue($("#power-transport")) || "auto";
  $("#power-toggle-field").hidden = type !== "button";
  $("#power-on-field").hidden = type === "button";
  $("#power-off-field").hidden = type === "button";
  $("#power-actions").hidden = transport === "hdmi_cec";
  $("#state-mode").disabled = type === "button";
  if (type === "button") $("#state-mode").value = "stateless";
  const hdmiDetected = Boolean(node.audio.detected.hdmiConnected);
  const transportMessages = {
    auto: hdmiDetected
      ? "Auto runs an assigned IR action first, then falls back to the detected HDMI-CEC receiver."
      : "Auto runs an assigned IR action. HDMI-CEC will become available after an HDMI receiver is detected.",
    hdmi_cec: "HDMI-CEC sends standards-based on/standby commands to the connected receiver or soundbar.",
    ir: "IR runs the actions selected below. Build or import those commands in IR & Actions first.",
  };
  $("#power-capability-status").textContent = transportMessages[transport];
  $("#state-notice").textContent = transport === "hdmi_cec"
    ? "HDMI-CEC sends discrete on/standby commands, but receiver state is not polled continuously in this prototype."
    : "IR is one-way. Assumed state can become inaccurate when the original remote is used.";

  const piMode = $("#pi-power-mode").value;
  $("#shutdown-delay-field").hidden = piMode === "always_on";
  const detail = $("#pi-power-detail");
  const guidance = $("#wake-guidance");
  if (piMode === "soft_sleep") {
    detail.textContent = "Stops Snapclient and the HDMI display while keeping the node agent and network available.";
    guidance.innerHTML = "<strong>Wake from Hub</strong><span>The room will appear offline, but its Power button remains available and restarts audio and display services.</span>";
  } else if (piMode === "halt") {
    detail.textContent = "Gracefully halts Linux. This is not suspend; the node performs a normal boot when woken.";
    const gpio = Number(node.button?.gpio);
    guidance.innerHTML = gpio === 3
      ? "<strong>GPIO3 wake ready</strong><span>The configured multi-use button on GPIO3 can wake this Pi after halt.</span>"
      : `<strong>Physical wake wiring required</strong><span>The current multi-use button is GPIO${gpio || "?"} and cannot wake a halted Pi. Rewire it to GPIO3 / physical pin 5 with Ground pin 6, or add an always-powered external controller. A halted Pi cannot wake from the Hub over Wi-Fi or Ethernet.</span>`;
  } else {
    detail.textContent = "Keeps the node fully available for immediate synchronized playback.";
    guidance.innerHTML = "<strong>Instantly reachable</strong><span>The Pi, Snapclient, display, and node agent remain running.</span>";
  }
}

function closeOverlays() {
  $("#settings-drawer").classList.remove("open");
  $("#settings-drawer").setAttribute("aria-hidden", "true");
  $("#pairing-modal").classList.remove("open");
  $("#pairing-modal").setAttribute("aria-hidden", "true");
  $("#browser-node-modal").classList.remove("open");
  $("#browser-node-modal").setAttribute("aria-hidden", "true");
  $("#node-type-modal").classList.remove("open");
  $("#node-type-modal").setAttribute("aria-hidden", "true");
  $("#scrim").classList.remove("open");
  app.pairingKnownIds = null;
  hideKeyboard();
}

function openAddNodeDialog() {
  setBrowserNodeButtonState();
  $("#node-type-modal").classList.add("open");
  $("#node-type-modal").setAttribute("aria-hidden", "false");
  $("#scrim").classList.add("open");
}

async function startPairing() {
  try {
    closeOverlays();
    await api("/api/pairing/start", { method: "POST", body: "{}" });
    app.pairingKnownIds = new Set(app.state.nodes.map(node => node.id));
    $("#pairing-status").textContent = "Waiting for a node on this network…";
    $("#pairing-modal").classList.add("open");
    $("#pairing-modal").setAttribute("aria-hidden", "false");
    $("#scrim").classList.add("open");
  } catch (error) { toast(error.message, true); }
}

function browserSessionId() {
  try {
    let value = sessionStorage.getItem(BROWSER_NODE_SESSION_KEY);
    if (!value) {
      value = typeof crypto.randomUUID === "function"
        ? crypto.randomUUID()
        : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
      sessionStorage.setItem(BROWSER_NODE_SESSION_KEY, value);
    }
    return value;
  } catch (_) {
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  }
}

function rememberedBrowserNodeName() {
  try { return localStorage.getItem(BROWSER_NODE_NAME_KEY) || ""; } catch (_) { return ""; }
}

function rememberBrowserNodeName(name) {
  try { localStorage.setItem(BROWSER_NODE_NAME_KEY, name); } catch (_) { /* Private browsing may block storage. */ }
}

function snapwebUrl() {
  const url = new URL(location.href);
  url.port = "1780";
  url.pathname = "/";
  url.search = "";
  url.hash = "";
  return url.href;
}

function openBrowserNodeDialog() {
  $("#node-type-modal").classList.remove("open");
  $("#node-type-modal").setAttribute("aria-hidden", "true");
  const active = Boolean(app.browserNodeId);
  $("#browser-node-name-step").hidden = active;
  $("#browser-node-player-step").hidden = !active;
  if (!active) {
    $("#browser-node-name").value = rememberedBrowserNodeName();
  } else {
    $("#browser-node-player-title").textContent = app.browserNodeName || "Browser audio";
  }
  $("#browser-node-modal").classList.add("open");
  $("#browser-node-modal").setAttribute("aria-hidden", "false");
  $("#scrim").classList.add("open");
  if (!active) setTimeout(() => $("#browser-node-name").focus(), 120);
}

function setBrowserNodeButtonState() {
  const detail = $("#temporary-node-choice-detail");
  if (detail) detail.textContent = app.browserNodeId
    ? `Open setup for ${app.browserNodeName || "this temporary node"}.`
    : "Play synchronized audio through this browser.";
}

function renderBrowserNodeStatus() {
  const status = $("#browser-node-status");
  if (!status || !app.browserNodeId) return;
  const node = app.state?.nodes.find(item => item.id === app.browserNodeId);
  status.classList.toggle("connected", Boolean(node?.audioReady));
  $("span", status).textContent = node?.audioReady
    ? "Connected and synchronized. You can hide this setup panel."
    : "Tap Play in the player below to start synchronized audio.";
}

async function createBrowserNode() {
  const name = $("#browser-node-name").value.replace(/\s+/g, " ").trim();
  const button = $("#create-browser-node");
  if (!name) {
    toast("Enter a name for this temporary node", true);
    return;
  }
  button.disabled = true;
  try {
    const result = await api("/api/browser-nodes/register", {
      method: "POST",
      body: JSON.stringify({ sessionId: browserSessionId(), name }),
    });
    app.browserNodeId = result.node.id;
    app.browserNodeName = name;
    rememberBrowserNodeName(name);
    $("#browser-node-name-step").hidden = true;
    $("#browser-node-player-step").hidden = false;
    $("#browser-node-player-title").textContent = name;
    const player = $("#snapweb-player");
    if (player.src !== snapwebUrl()) player.src = snapwebUrl();
    setBrowserNodeButtonState();
    startBrowserNodeHeartbeat();
    await refreshState();
    toast(`${name} added — tap Play to connect audio`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function sendBrowserNodeHeartbeat() {
  if (!app.browserNodeId) return;
  try {
    await api(`/api/browser-nodes/${encodeURIComponent(app.browserNodeId)}/heartbeat`, {
      method: "POST",
      body: "{}",
    });
  } catch (_) {
    // If the Hub restarted while this tab stayed open, recreate the runtime
    // node without asking the listener to press Play again.
    try {
      const result = await api("/api/browser-nodes/register", {
        method: "POST",
        body: JSON.stringify({ sessionId: browserSessionId(), name: app.browserNodeName }),
      });
      app.browserNodeId = result.node.id;
    } catch (_) { /* The next heartbeat will retry. */ }
  }
}

function startBrowserNodeHeartbeat() {
  clearInterval(app.browserHeartbeatTimer);
  app.browserHeartbeatTimer = setInterval(sendBrowserNodeHeartbeat, 5000);
}

async function removeBrowserNode() {
  const nodeId = app.browserNodeId;
  if (!nodeId) return;
  $("#snapweb-player").src = "about:blank";
  clearInterval(app.browserHeartbeatTimer);
  app.browserHeartbeatTimer = null;
  app.browserNodeId = null;
  try {
    await api(`/api/browser-nodes/${encodeURIComponent(nodeId)}/unregister`, {
      method: "POST",
      body: "{}",
    });
  } catch (_) { /* Heartbeat expiry is the cleanup fallback. */ }
  setBrowserNodeButtonState();
  closeOverlays();
  await refreshState();
  toast("Temporary node removed");
}

function unregisterBrowserNodeOnClose() {
  if (!app.browserNodeId) return;
  const path = `/api/browser-nodes/${encodeURIComponent(app.browserNodeId)}/unregister`;
  navigator.sendBeacon(path, new Blob(["{}"], { type: "application/json" }));
}

function switchTab(name) {
  $$("#settings-tabs button").forEach(button => button.classList.toggle("active", button.dataset.tab === name));
  $$(".tab-panel").forEach(panel => panel.classList.toggle("active", panel.dataset.panel === name));
}

function setSegment(group, value) {
  $$('button[data-value]', group).forEach(button => button.classList.toggle("active", button.dataset.value === value));
}

function segmentValue(group) {
  return $("button.active", group)?.dataset.value;
}

function renderAudioSettings(node) {
  const detected = node.audio.detected;
  $("#hdmi-sink").textContent = detected.hdmiConnected ? detected.sink : "No HDMI receiver detected";
  $("#hdmi-detail").textContent = detected.hdmiConnected
    ? `EDID reports up to ${detected.maxPcmChannels} PCM channels · synchronized source is ${detected.streamChannels || 2}-channel · ${detected.activeDevice || "device pending"}`
    : "Analog output remains available";
  const select = $("#audio-profile");
  select.innerHTML = "";
  node.audio.profiles.forEach(profile => {
    const option = new Option(profile.label, profile.id, false, profile.id === node.audio.requestedProfile);
    option.disabled = !profile.available;
    option.title = profile.detail;
    select.add(option);
  });
  updateProfileDetail();
  setSegment($("#output-gain"), String(node.audio.outputGainDb ?? 9));
  $("#format-chips").innerHTML = detected.formats.map(format => `<span>${escapeHtml(format)}</span>`).join("");
}

function updateProfileDetail() {
  const node = app.state.nodes.find(item => item.id === app.activeNodeId);
  const profile = node?.audio.profiles.find(item => item.id === $("#audio-profile").value);
  $("#profile-detail").textContent = profile?.detail || "";
}

function renderNetworkSettings(node) {
  const wifi = node.wifi;
  const connection = node.connection;
  $("#signal-value").textContent = `${connection.quality}%`;
  $("#network-title").textContent = connection.type === "ethernet" ? "Ethernet connected" : wifi.ssid || "Wi-Fi";
  $("#network-connection").textContent = connection.type === "ethernet" ? "Wired network" : "Wireless network";
  $("#network-address").textContent = connection.address || "Unavailable";
  $("#network-mode").textContent = connection.type === "ethernet" ? "Wired · recommended" : `${wifi.status} · ${wifi.signal}% signal`;
  $("#signal-ring").style.borderColor = connection.quality > 65 ? "var(--green)" : "var(--accent)";
  $("#wifi-form").hidden = !wifi.supported;
  $("#ethernet-only").hidden = wifi.supported;
  $("#wifi-ssid").value = wifi.ssid || "";
  $("#wifi-password").value = "";
}

function renderIrSettings(node) {
  $("#ir-summary").textContent = node.ir.signals.length
    ? `${node.ir.fileName} · ${node.ir.signals.length} commands`
    : "No .ir file imported";
  $("#signal-chips").innerHTML = node.ir.signals.map(signal => `<span>${escapeHtml(signal.name)}</span>`).join("");
  renderActions();
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[character]));
}

const actionTypes = [
  ["ir.send", "Send IR"], ["delay", "Delay"], ["node.mute", "Mute / unmute"],
  ["node.volume", "Set volume"], ["audio.profile", "Audio profile"], ["action.call", "Run action"],
];

function renderActions() {
  const root = $("#action-list");
  root.innerHTML = "";
  const node = app.state.nodes.find(item => item.id === app.activeNodeId);
  app.draftActions.forEach((action, actionIndex) => {
    const card = document.createElement("article");
    card.className = "action-card";
    card.innerHTML = `<div class="action-head"><span>⚡</span><input value="${escapeHtml(action.name)}" aria-label="Action name"><button class="run">Run</button><button class="delete">Delete</button></div><div class="action-steps"></div>`;
    $(".action-head input", card).addEventListener("input", event => { action.name = event.target.value; });
    $(".run", card).addEventListener("click", () => runAction(action.id));
    $(".delete", card).addEventListener("click", () => { app.draftActions.splice(actionIndex, 1); renderActions(); });
    const steps = $(".action-steps", card);
    action.steps.forEach((step, stepIndex) => steps.append(makeStep(step, action, stepIndex, node)));
    const add = document.createElement("button");
    add.className = "add-step";
    add.textContent = "＋ Add command";
    add.addEventListener("click", () => { action.steps.push({ type: "delay", milliseconds: 500 }); renderActions(); });
    steps.append(add);
    root.append(card);
  });
  if (!app.draftActions.length) root.innerHTML = '<div class="notice amber">No actions yet. Add one to build a command sequence.</div>';
}

function makeStep(step, action, stepIndex, node) {
  const row = document.createElement("div");
  row.className = "action-step";
  const type = document.createElement("select");
  actionTypes.forEach(([value, label]) => type.add(new Option(label, value, false, value === step.type)));
  type.addEventListener("change", () => {
    action.steps[stepIndex] = defaultStep(type.value);
    renderActions();
  });
  row.append(type, makeStepValue(step, node), Object.assign(document.createElement("button"), { textContent: "×", title: "Remove command" }));
  row.lastElementChild.addEventListener("click", () => { action.steps.splice(stepIndex, 1); renderActions(); });
  return row;
}

function defaultStep(type) {
  if (type === "ir.send") return { type, signal: "", repeats: 1 };
  if (type === "delay") return { type, milliseconds: 500 };
  if (type === "node.mute") return { type, value: true };
  if (type === "node.volume") return { type, value: 50 };
  if (type === "audio.profile") return { type, value: "auto" };
  return { type, actionId: "" };
}

function makeStepValue(step, node) {
  let control;
  if (step.type === "ir.send") {
    control = document.createElement("select");
    control.add(new Option("Choose IR command…", ""));
    node.ir.signals.forEach(signal => control.add(new Option(signal.name, signal.name, false, signal.name === step.signal)));
    control.addEventListener("change", () => { step.signal = control.value; });
  } else if (step.type === "node.mute") {
    control = document.createElement("select");
    control.add(new Option("Mute", "true", false, step.value === true));
    control.add(new Option("Unmute", "false", false, step.value === false));
    control.addEventListener("change", () => { step.value = control.value === "true"; });
  } else if (step.type === "audio.profile") {
    control = document.createElement("select");
    node.audio.profiles.filter(profile => profile.available).forEach(profile => control.add(new Option(profile.label, profile.id, false, profile.id === step.value)));
    control.addEventListener("change", () => { step.value = control.value; });
  } else if (step.type === "action.call") {
    control = document.createElement("select");
    control.add(new Option("Choose action…", ""));
    app.draftActions.forEach(item => control.add(new Option(item.name, item.id, false, item.id === step.actionId)));
    control.addEventListener("change", () => { step.actionId = control.value; });
  } else {
    control = document.createElement("input");
    control.type = "number";
    control.min = "0";
    control.max = step.type === "delay" ? "30000" : "100";
    control.value = step.type === "delay" ? step.milliseconds : step.value;
    control.placeholder = step.type === "delay" ? "Milliseconds" : "0–100";
    control.addEventListener("input", () => {
      if (step.type === "delay") step.milliseconds = Number(control.value);
      else step.value = Number(control.value);
    });
  }
  return control;
}

async function runAction(actionId) {
  try {
    await saveActions(false);
    await api(`/api/nodes/${encodeURIComponent(app.activeNodeId)}/actions/${encodeURIComponent(actionId)}/run`, { method: "POST", body: "{}" });
    toast("Action queued on the node");
  } catch (error) { toast(error.message, true); }
}

async function saveActions(showToast = true) {
  const result = await api(`/api/nodes/${encodeURIComponent(app.activeNodeId)}/actions`, { method: "POST", body: JSON.stringify({ actions: app.draftActions }) });
  const node = app.state.nodes.find(item => item.id === app.activeNodeId);
  node.actions = result.actions;
  if (showToast) toast("Action list saved");
}

function slug(value) {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 32) || `action-${Date.now()}`;
}

function addAction() {
  let name = `New action ${app.draftActions.length + 1}`;
  let id = slug(name);
  while (app.draftActions.some(action => action.id === id)) id += "-2";
  app.draftActions.push({ id, name, steps: [] });
  renderActions();
}

async function saveSettings() {
  const powerType = segmentValue($("#power-type"));
  const node = app.state.nodes.find(item => item.id === app.activeNodeId);
  const power = node.local
    ? { ...node.power, controlType: "switch", stateMode: "assumed" }
    : {
        ...node.power,
        controlType: powerType,
        stateMode: $("#state-mode").value,
        transport: segmentValue($("#power-transport")),
        piPowerMode: $("#pi-power-mode").value,
        shutdownDelaySeconds: Number($("#shutdown-delay").value),
        pressActionId: $("#power-toggle-action").value,
        onActionId: $("#power-on-action").value,
        offActionId: $("#power-off-action").value,
      };
  if (powerType === "button") power.stateMode = "stateless";
  const changes = {
    name: $("#node-name").value.trim(),
    room: $("#node-room").value.trim(),
    audioProfile: $("#audio-profile").value,
    outputGainDb: Number(segmentValue($("#output-gain"))),
    power,
  };
  if (!changes.name || !changes.room) return toast("Name and room are required", true);
  try {
    const result = await api(`/api/nodes/${encodeURIComponent(app.activeNodeId)}/settings`, { method: "POST", body: JSON.stringify(changes) });
    const index = app.state.nodes.findIndex(item => item.id === app.activeNodeId);
    app.state.nodes[index] = result.node;
    renderNodes();
    $("#drawer-title").textContent = result.node.name;
    $("#save-status").textContent = "Saved just now.";
    toast("Node settings saved");
  } catch (error) { toast(error.message, true); }
}

async function importIr(file) {
  if (!file) return;
  try {
    const content = await file.text();
    const result = await api(`/api/nodes/${encodeURIComponent(app.activeNodeId)}/ir/import`, { method: "POST", body: JSON.stringify({ fileName: file.name, content }) });
    const node = app.state.nodes.find(item => item.id === app.activeNodeId);
    node.ir = { fileName: file.name, signals: result.signals };
    renderIrSettings(node);
    toast(`Imported ${result.imported} IR commands`);
  } catch (error) { toast(error.message, true); }
}

async function applyWifi() {
  const ssid = $("#wifi-ssid").value.trim();
  const password = $("#wifi-password").value;
  try {
    const result = await api(`/api/nodes/${encodeURIComponent(app.activeNodeId)}/wifi`, { method: "POST", body: JSON.stringify({ ssid, password }) });
    const node = app.state.nodes.find(item => item.id === app.activeNodeId);
    node.wifi = result.node.wifi;
    $("#wifi-password").value = "";
    renderNetworkSettings(node);
    toast(result.message);
  } catch (error) { toast(error.message, true); }
}

function initKeyboard() {
  renderKeyboard();
  document.addEventListener("focusin", event => {
    if (!event.target.matches("[data-osk]")) return;
    const shouldUse = window.innerWidth <= 900 || matchMedia("(pointer: coarse)").matches;
    if (!shouldUse) return;
    app.activeInput = event.target;
    $("#keyboard").classList.add("open");
    $("#keyboard").setAttribute("aria-hidden", "false");
  });
  $("#keyboard-close").addEventListener("click", hideKeyboard);
}

function hideKeyboard() {
  $("#keyboard").classList.remove("open");
  $("#keyboard").setAttribute("aria-hidden", "true");
  app.activeInput = null;
}

function renderKeyboard() {
  const root = $("#keyboard-keys");
  root.innerHTML = "";
  const letters = app.keyboardSymbols
    ? [["1","2","3","4","5","6","7","8","9","0"], ["!","@","#","$","%","&","*","(",")"], ["-","_","+","=","/","?"]]
    : [["q","w","e","r","t","y","u","i","o","p"], ["a","s","d","f","g","h","j","k","l"], ["z","x","c","v","b","n","m"]];
  letters[0].forEach(key => addKey(root, displayKey(key), () => insertKey(displayKey(key))));
  root.append(spacer(1));
  letters[1].forEach(key => addKey(root, displayKey(key), () => insertKey(displayKey(key))));
  root.append(spacer(1));
  addKey(root, "⇧", () => { app.keyboardShift = !app.keyboardShift; renderKeyboard(); }, "wide accent");
  letters[2].forEach(key => addKey(root, displayKey(key), () => insertKey(displayKey(key))));
  addKey(root, "⌫", () => insertKey("Backspace"), "wide");
  addKey(root, app.keyboardSymbols ? "ABC" : "123", () => { app.keyboardSymbols = !app.keyboardSymbols; renderKeyboard(); }, "wide");
  addKey(root, "space", () => insertKey(" "), "space");
  addKey(root, "@", () => insertKey("@"));
  addKey(root, ".", () => insertKey("."));
  addKey(root, "Enter", () => insertKey("Enter"), "enter accent");
}

function displayKey(key) { return app.keyboardShift && !app.keyboardSymbols ? key.toUpperCase() : key; }
function spacer(columns) { const item = document.createElement("span"); item.style.gridColumn = `span ${columns}`; return item; }
function addKey(root, label, handler, classes = "") { const button = document.createElement("button"); button.type = "button"; button.className = `key ${classes}`; button.textContent = label; button.addEventListener("pointerdown", event => { event.preventDefault(); handler(); }); root.append(button); }
function insertKey(key) {
  const input = app.activeInput;
  if (!input) return;
  const start = input.selectionStart ?? input.value.length;
  const end = input.selectionEnd ?? input.value.length;
  if (key === "Enter") { hideKeyboard(); input.blur(); return; }
  if (key === "Backspace") {
    const from = start === end ? Math.max(0, start - 1) : start;
    input.setRangeText("", from, end, "end");
  } else input.setRangeText(key, start, end, "end");
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

function renderSoundboard() {
  if (!app.soundboard || !app.state) return;
  const sounds = app.soundboard.sounds || [];
  const soundIds = new Set(sounds.map(sound => sound.id));
  for (const id of app.selectedSounds) if (!soundIds.has(id)) app.selectedSounds.delete(id);
  const list = $("#sound-list");
  list.replaceChildren();
  if (!sounds.length) list.innerHTML = '<p class="sound-empty">No sounds uploaded yet. Add an audio file to get started.</p>';
  for (const sound of sounds) {
    const row = document.createElement("div");
    row.className = "sound-row";
    const label = document.createElement("label");
    const check = document.createElement("input");
    check.type = "checkbox";
    check.checked = app.selectedSounds.has(sound.id);
    check.addEventListener("change", () => check.checked ? app.selectedSounds.add(sound.id) : app.selectedSounds.delete(sound.id));
    const title = document.createElement("span");
    title.textContent = sound.name;
    label.append(check, title);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "sound-remove";
    remove.textContent = "×";
    remove.title = `Delete ${sound.name}`;
    remove.addEventListener("click", async () => {
      if (!confirm(`Delete ${sound.name} and any schedules using it?`)) return;
      try {
        await api(`/api/soundboard/sounds/${sound.id}`, { method: "DELETE" });
        await refreshSoundboard();
      } catch (error) { toast(error.message, true); }
    });
    row.append(label, remove);
    list.append(row);
  }
  const targets = $("#sound-targets");
  targets.replaceChildren();
  const eligible = app.state.nodes.filter(node => node.local || node.browserTemporary);
  if (!eligible.length) targets.innerHTML = '<p class="sound-empty">No Hub or temporary playback locations found.</p>';
  for (const node of eligible) {
    const label = document.createElement("label");
    label.className = "sound-row sound-target";
    const check = document.createElement("input");
    check.type = "checkbox";
    check.checked = app.selectedSoundTargets.has(node.id);
    check.disabled = !node.snapcastConnected || !node.modeCompatible;
    check.addEventListener("change", () => check.checked ? app.selectedSoundTargets.add(node.id) : app.selectedSoundTargets.delete(node.id));
    const name = document.createElement("span");
    name.textContent = node.name;
    const detail = document.createElement("small");
    detail.textContent = check.disabled ? "Audio not ready" : "Ready";
    label.append(check, name, detail);
    targets.append(label);
  }
  const active = app.soundboard.active;
  $("#sound-status").textContent = app.soundboard.error || (active ? "Playing selected sound…" : "Ready");
  const dayNames = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const schedules = $("#schedule-list");
  schedules.replaceChildren();
  if (!(app.soundboard.schedules || []).length) schedules.innerHTML = '<p class="sound-empty">No schedules yet.</p>';
  for (const item of app.soundboard.schedules || []) {
    const row = document.createElement("div");
    row.className = "sound-row schedule-row";
    const description = document.createElement("div");
    const soundNames = item.soundIds.map(id => sounds.find(sound => sound.id === id)?.name || "Deleted sound").join(", ");
    description.innerHTML = `<strong>${escapeHtml(soundNames)}</strong><small>${escapeHtml(item.time)} · ${item.days.map(day => dayNames[day]).join(", ")} · ${item.targetNodeIds.map(id => app.state.nodes.find(node => node.id === id)?.name || id).map(escapeHtml).join(", ")}${item.expiresOn ? ` · Ends ${escapeHtml(item.expiresOn)}` : ""}</small>`;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "sound-remove";
    remove.textContent = "×";
    remove.title = "Delete schedule";
    remove.addEventListener("click", async () => {
      try {
        await api(`/api/soundboard/schedules/${item.id}`, { method: "DELETE" });
        await refreshSoundboard();
      } catch (error) { toast(error.message, true); }
    });
    row.append(description, remove);
    schedules.append(row);
  }
}

async function refreshSoundboard() {
  app.soundboard = await api("/api/soundboard");
  renderSoundboard();
}

async function uploadSounds(files) {
  for (const file of files) {
    try {
      const response = await fetch("/api/soundboard/upload", {
        method: "POST", headers: { "Content-Type": "application/octet-stream", "X-Filename": encodeURIComponent(file.name) }, body: file,
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `Upload failed (${response.status})`);
      app.selectedSounds.add(result.sound.id);
      toast(`${file.name} uploaded`);
    } catch (error) { toast(error.message, true); }
  }
  await refreshSoundboard();
}

function soundSelection() {
  const soundIds = [...app.selectedSounds];
  const targetNodeIds = [...app.selectedSoundTargets];
  if (!soundIds.length || !targetNodeIds.length) throw new Error("Select sounds and playback locations in the Sounds tab.");
  const unavailable = targetNodeIds.find(id => {
    const node = app.state.nodes.find(item => item.id === id);
    return !node?.snapcastConnected || !node?.modeCompatible;
  });
  if (unavailable) throw new Error("A selected playback location is not ready. Deselect it or reconnect its player.");
  return { soundIds, targetNodeIds };
}

function bindEvents() {
  $("#soundboard-enabled").addEventListener("change", async event => {
    const control = event.target;
    control.disabled = true;
    try {
      const result = await api("/api/settings", { method: "POST", body: JSON.stringify({ soundboardEnabled: control.checked }) });
      app.state.system.settings = result.settings;
      renderSystem();
      refreshSoundboard().catch(() => {});
      toast(control.checked ? "Soundboard enabled" : "Soundboard disabled; schedules paused");
    } catch (error) {
      control.checked = !control.checked;
      toast(error.message, true);
    } finally { control.disabled = false; }
  });
  $("#sound-upload").addEventListener("change", event => {
    uploadSounds([...event.target.files]).catch(error => toast(error.message, true));
    event.target.value = "";
  });
  $$("[data-sound-tab]").forEach(button => button.addEventListener("click", () => {
    $$("[data-sound-tab]").forEach(item => item.classList.toggle("active", item === button));
    $$(".sound-panel").forEach(panel => panel.classList.toggle("active", panel.id === `sound-panel-${button.dataset.soundTab}`));
  }));
  $("#sound-play").addEventListener("click", async () => {
    try {
      await api("/api/soundboard/play", { method: "POST", body: JSON.stringify(soundSelection()) });
      toast("Sound queued for playback");
      await refreshSoundboard();
    } catch (error) { toast(error.message, true); }
  });
  $("#sound-stop").addEventListener("click", async () => {
    try { await api("/api/soundboard/stop", { method: "POST", body: "{}" }); toast("Stopping soundboard playback"); }
    catch (error) { toast(error.message, true); }
  });
  $("#schedule-add").addEventListener("click", async () => {
    try {
      const days = $$("#schedule-days input:checked").map(input => Number(input.value));
      await api("/api/soundboard/schedules", { method: "POST", body: JSON.stringify({ ...soundSelection(), time: $("#schedule-time").value, days, expiresOn: $("#schedule-expires").value }) });
      toast("Weekly schedule added");
      await refreshSoundboard();
    } catch (error) { toast(error.message, true); }
  });
  document.addEventListener("pointerdown", () => {
    if (!app.screensaverActive && !app.displayOff) app.lastInteraction = Date.now();
  }, { capture: true });
  document.addEventListener("keydown", () => {
    if (app.displayOff) wakeDisplay();
    else if (!app.screensaverActive) app.lastInteraction = Date.now();
  }, { capture: true });
  $$('[data-view]').forEach(button => button.addEventListener("click", () => {
    const view = button.dataset.view;
    $$(".view").forEach(panel => panel.classList.toggle("active", panel.id === `view-${view}`));
    $$(".rail-button").forEach(item => item.classList.toggle("active", item.dataset.view === view));
  }));
  $("#close-settings").addEventListener("click", event => {
    event.preventDefault();
    event.stopPropagation();
    closeOverlays();
  });
  $("#scrim").addEventListener("click", closeOverlays);
  $("#settings-tabs").addEventListener("click", event => { if (event.target.dataset.tab) switchTab(event.target.dataset.tab); });
  $("#power-type").addEventListener("click", event => {
    if (!event.target.dataset.value) return;
    setSegment($("#power-type"), event.target.dataset.value);
    if (event.target.dataset.value === "button") $("#state-mode").value = "stateless";
    updatePowerForm();
  });
  $("#power-transport").addEventListener("click", event => {
    if (!event.target.dataset.value || event.target.disabled) return;
    setSegment($("#power-transport"), event.target.dataset.value);
    updatePowerForm();
  });
  $("#pi-power-mode").addEventListener("change", updatePowerForm);
  $("#audio-profile").addEventListener("change", updateProfileDetail);
  $("#output-gain").addEventListener("click", event => {
    if (event.target.dataset.value !== undefined) setSegment($("#output-gain"), event.target.dataset.value);
  });
  $("#rescan-audio").addEventListener("click", async () => {
    try {
      const result = await api(`/api/nodes/${encodeURIComponent(app.activeNodeId)}/audio/rescan`, { method: "POST", body: "{}" });
      const index = app.state.nodes.findIndex(node => node.id === app.activeNodeId);
      app.state.nodes[index] = result.node;
      renderAudioSettings(result.node);
      toast("HDMI capabilities updated");
    } catch (error) { toast(error.message, true); }
  });
  $("#save-settings").addEventListener("click", saveSettings);
  $("#show-password").addEventListener("click", () => {
    const input = $("#wifi-password"); input.type = input.type === "password" ? "text" : "password"; $("#show-password").textContent = input.type === "password" ? "Show" : "Hide";
  });
  $("#apply-wifi").addEventListener("click", applyWifi);
  $("#ir-file").addEventListener("change", event => importIr(event.target.files[0]));
  $("#add-action").addEventListener("click", addAction);
  $("#save-actions").addEventListener("click", () => saveActions().catch(error => toast(error.message, true)));
  $("#add-node").addEventListener("click", openAddNodeDialog);
  $("#add-permanent-node-choice").addEventListener("click", startPairing);
  $("#add-temporary-node-choice").addEventListener("click", openBrowserNodeDialog);
  $("#create-browser-node").addEventListener("click", createBrowserNode);
  $("#browser-node-name").addEventListener("keydown", event => {
    if (event.key === "Enter") createBrowserNode();
  });
  $("#hide-browser-node-setup").addEventListener("click", closeOverlays);
  $("#pairing-done").addEventListener("click", closeOverlays);
  $("#show-screensaver").addEventListener("click", showScreensaver);
  $("#open-player-screensaver").addEventListener("click", showScreensaver);
  $("#previous-track").addEventListener("click", () => controlPlayback("previous"));
  $("#play-pause").addEventListener("click", () => controlPlayback("play-pause"));
  $("#next-track").addEventListener("click", () => controlPlayback("next"));
  $("#screensaver").addEventListener("pointerdown", () => { if (!app.displayOnly) hideScreensaver(); });
  $("#display-off").addEventListener("pointerdown", event => {
    event.preventDefault();
    event.stopPropagation();
    wakeDisplay();
  });
  $("#screensaver-timer").addEventListener("change", event => {
    app.clientSettings.screensaverSeconds = Number(event.target.value);
    saveClientSettings();
    toast("Screensaver timer saved on this device");
  });
  $("#quiet-hours-enabled").addEventListener("change", event => {
    app.clientSettings.quietHoursEnabled = event.target.checked;
    saveClientSettings();
    if (!event.target.checked && app.displayOff) wakeDisplay();
    toast("Quiet hours saved on this device");
  });
  [["#quiet-hours-start", "quietHoursStart"], ["#quiet-hours-end", "quietHoursEnd"]].forEach(([selector, key]) => {
    $(selector).addEventListener("change", event => {
      app.clientSettings[key] = event.target.value;
      saveClientSettings();
      toast("Quiet hours saved on this device");
    });
  });
  $("#playback-mode").addEventListener("change", async event => {
    const control = event.target;
    const previous = app.state.system.settings?.playbackMode || "stereo";
    control.disabled = true;
    try {
      const result = await api("/api/settings", {
        method: "POST",
        body: JSON.stringify({ playbackMode: control.value }),
      });
      app.state.system.settings = result.settings;
      renderSystem();
      renderNodes();
      toast(`Playback switched to ${control.options[control.selectedIndex].text}`);
    } catch (error) {
      control.value = previous;
      toast(error.message, true);
    } finally {
      control.disabled = false;
    }
  });
  $$(".modal-close").forEach(button => button.addEventListener("click", closeOverlays));
  document.addEventListener("keydown", event => { if (event.key === "Escape") closeOverlays(); });
  document.addEventListener("pointermove", showCursorTemporarily, { passive: true });
  document.addEventListener("pointerdown", showCursorTemporarily, { passive: true });
}

async function init() {
  app.clientSettings = loadClientSettings();
  if (app.displayOnly) document.body.classList.add("display-only", "cursor-hidden");
  if (app.hubDisplay) document.body.classList.add("hub-display");
  bindEvents();
  setBrowserNodeButtonState();
  showCursorTemporarily();
  initKeyboard();
  updateClock();
  setInterval(updateClock, 1000);
  try {
    app.state = await api("/api/state");
    renderSystem();
    renderNodes();
    refreshSoundboard().catch(error => { $("#sound-status").textContent = error.message; });
    setInterval(() => refreshSoundboard().catch(() => {}), 5000);
    if (app.displayOnly) {
      showScreensaver();
      await sendDisplayHeartbeat();
      setInterval(sendDisplayHeartbeat, 5000);
    }
    setInterval(refreshState, 2000);
  } catch (error) {
    $("#node-grid").innerHTML = `<div class="notice amber">Could not reach the hub: ${escapeHtml(error.message)}</div>`;
  }
}

async function refreshState() {
  try {
    const state = await api("/api/state");
    app.state.system = state.system;
    app.state.nodes = state.nodes;
    if (app.pairingKnownIds) {
      const added = state.nodes.find(node => !app.pairingKnownIds.has(node.id));
      if (added) {
        $("#pairing-status").textContent = `${added.name} paired successfully.`;
        app.pairingKnownIds.add(added.id);
        toast(`${added.name} added`);
      } else if (!state.system.pairing?.active) {
        $("#pairing-status").textContent = "Pairing window expired. Close and try again.";
      }
    }
    renderSystem();
    if (!$("#settings-drawer").classList.contains("open") && !app.volumeDragNodeId) renderNodes();
    if ($("#view-soundboard").classList.contains("active")) renderSoundboard();
  } catch (_) { /* Keep the last known state during a brief network interruption. */ }
}

init();
window.addEventListener("pagehide", unregisterBrowserNodeOnClose);
