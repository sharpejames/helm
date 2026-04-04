// Side panel JS for Helm Video Vision
// State machine: Idle → Selecting → VideoSelected → Streaming → Error
// Communicates with background.js via chrome.runtime messaging.

"use strict";

// ── State ────────────────────────────────────────────────────────────────────

const STATES = {
  IDLE: "Idle",
  SELECTING: "Selecting",
  VIDEO_SELECTED: "VideoSelected",
  STREAMING: "Streaming",
  ERROR: "Error",
};

let currentState = STATES.IDLE;
let commentaryHistory = [];
let alertConditions = [];
let lastSpokenText = "";
let selectedRegion = null; // {x, y, width, height} if region mode
let isVideoMode = false;   // true if a video element was selected

// ── DOM References ───────────────────────────────────────────────────────────

const btnSelectVideo = document.getElementById("btn-select-video");
const btnSelectRegion = document.getElementById("btn-select-region");
const btnToggle = document.getElementById("btn-toggle");
const modeSelect = document.getElementById("mode-select");
const fpsSlider = document.getElementById("fps-slider");
const fpsValue = document.getElementById("fps-value");
const statusDot = document.getElementById("status-dot");
const statusText = document.getElementById("status-text");
const videoInfo = document.getElementById("video-info");
const thumbnailImg = document.getElementById("thumbnail-img");
const videoDims = document.getElementById("video-dims");
const noVideoMsg = document.getElementById("no-video-msg");
const commentaryFeed = document.getElementById("commentary-feed");
const alertInput = document.getElementById("alert-input");
const btnAddAlert = document.getElementById("btn-add-alert");
const alertList = document.getElementById("alert-list");
const ttsToggle = document.getElementById("tts-toggle");
const voiceSelect = document.getElementById("voice-select");
const rateSlider = document.getElementById("rate-slider");
const rateValue = document.getElementById("rate-value");
const userContext = document.getElementById("user-context");
const summarizerToggle = document.getElementById("summarizer-toggle");
const contextOverlayToggle = document.getElementById("context-overlay-toggle");

// ── Utility ──────────────────────────────────────────────────────────────────

/**
 * Clamp a numeric value to [0.5, 2.0].
 */
function clampRange(value) {
  return Math.min(2.0, Math.max(0.1, Number(value) || 0.5));
}

/**
 * Format a Unix timestamp (seconds) to a readable time string.
 */
function formatTimestamp(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString();
}

// ── State Machine ────────────────────────────────────────────────────────────

/**
 * Transition to a new state and update UI accordingly.
 */
function transitionTo(newState) {
  currentState = newState;
  updateUI();
}

/**
 * Update all UI elements based on current state.
 */
function updateUI() {
  // Select Video button
  btnSelectVideo.disabled = (currentState === STATES.STREAMING || currentState === STATES.SELECTING);
  btnSelectRegion.disabled = (currentState === STATES.STREAMING || currentState === STATES.SELECTING);

  // Start/Stop toggle
  switch (currentState) {
    case STATES.IDLE:
    case STATES.SELECTING:
      btnToggle.disabled = true;
      btnToggle.textContent = "Start";
      btnToggle.classList.remove("streaming");
      break;
    case STATES.VIDEO_SELECTED:
    case STATES.ERROR:
      btnToggle.disabled = false;
      btnToggle.textContent = "Start";
      btnToggle.classList.remove("streaming");
      break;
    case STATES.STREAMING:
      btnToggle.disabled = false;
      btnToggle.textContent = "Stop";
      btnToggle.classList.add("streaming");
      break;
  }

  // No-video message visibility
  noVideoMsg.classList.add("hidden");
}

// ── Message Passing ──────────────────────────────────────────────────────────

/**
 * Send a message to the background service worker.
 */
let bgPort = null;

function connectToBackground() {
  bgPort = chrome.runtime.connect({ name: "helm-panel" });
  console.log("[HelmPanel] Connected to background via port");

  bgPort.onMessage.addListener((message) => {
    console.log("[HelmPanel] received:", message.type, message);
    switch (message.type) {
      case "videoInfo": handleVideoInfo(message); break;
      case "noVideo": handleNoVideo(); break;
      case "commentary": handleCommentary(message); break;
      case "summary": handleSummary(message); break;
      case "status": handleStatus(message); break;
      case "captureError": handleCaptureError(message); break;
      case "regionSelected": handleRegionSelected(message); break;
      case "thumbnail": handleThumbnailUpdate(message); break;
    }
  });

  bgPort.onDisconnect.addListener(() => {
    console.log("[HelmPanel] Port disconnected, reconnecting...");
    bgPort = null;
    // Reconnect after a short delay (service worker may have restarted)
    setTimeout(connectToBackground, 500);
  });
}

function sendToBackground(message) {
  if (bgPort) {
    try { bgPort.postMessage(message); } catch (_e) {}
  }
}

connectToBackground();

// ── Message Handlers ─────────────────────────────────────────────────────────

function handleVideoInfo(msg) {
  console.log("[HelmPanel] videoInfo received:", msg.width, "x", msg.height, "state:", currentState);
  videoInfo.classList.remove("hidden");
  noVideoMsg.classList.add("hidden");
  thumbnailImg.src = msg.thumbnail ? ("data:image/jpeg;base64," + msg.thumbnail) : "";
  videoDims.textContent = `${msg.width} × ${msg.height}`;
  updateConnectionStatus("disconnected");
  isVideoMode = true;
  selectedRegion = null;
  transitionTo(STATES.VIDEO_SELECTED);
}

function handleNoVideo() {
  videoInfo.classList.add("hidden");
  noVideoMsg.classList.remove("hidden");
  transitionTo(STATES.IDLE);
}

function handleCommentary(msg) {
  const entry = {
    description: msg.description,
    timestamp: msg.timestamp,
    alert: msg.alert || null,
  };

  if (msg.thumbnail) {
    thumbnailImg.src = "data:image/jpeg;base64," + msg.thumbnail;
    videoInfo.classList.remove("hidden");
  }

  commentaryHistory.push(entry);
  const el = appendCommentaryEntry(entry);

  // Queue for TTS (won't interrupt current speech)
  queueTTS(entry.description, el);

  const lastFrameTime = document.getElementById("last-frame-time");
  if (lastFrameTime) {
    lastFrameTime.textContent = `Last update: ${formatTimestamp(entry.timestamp)}`;
    lastFrameTime.classList.remove("hidden");
  }
}

function handleSummary(msg) {
  const summaryEntry = {
    description: "📋 " + msg.summary,
    timestamp: msg.timestamp,
    alert: null,
    isSummary: true,
  };

  commentaryHistory.push(summaryEntry);
  const el = appendSummaryEntry(summaryEntry);

  // Summaries jump the queue — interrupt current speech
  if (ttsToggle.checked && msg.summary) {
    jumpToTTS(msg.summary, el);
  }

  if (msg.key_events) {
    updateKeyEvents(msg.key_events);
  }
}

function handleThumbnailUpdate(msg) {
  if (msg.thumbnail) {
    thumbnailImg.src = "data:image/jpeg;base64," + msg.thumbnail;
    videoInfo.classList.remove("hidden");
  }
}

function handleStatus(msg) {
  updateConnectionStatus(msg.connection, msg.message);

  // Resume streaming state when reconnection succeeds
  if (msg.connection === "connected" && (currentState === STATES.ERROR || currentState === STATES.STREAMING)) {
    transitionTo(STATES.STREAMING);
  }

  // Handle connection failure while streaming — show error but don't stop
  // (background will keep reconnecting)
  if (msg.connection === "error" && currentState === STATES.STREAMING) {
    transitionTo(STATES.ERROR);
  }
}

function handleCaptureError(msg) {
  console.log("[HelmPanel] captureError:", msg.reason);
  updateConnectionStatus("error", "Capture error: " + msg.reason);
  videoInfo.classList.add("hidden");
  transitionTo(STATES.IDLE);
}

function handleRegionSelected(msg) {
  console.log("[HelmPanel] regionSelected:", msg.x, msg.y, msg.width, msg.height);
  videoInfo.classList.remove("hidden");
  noVideoMsg.classList.add("hidden");
  thumbnailImg.src = "";
  videoDims.textContent = `Region: ${msg.width} × ${msg.height}`;
  updateConnectionStatus("disconnected");
  isVideoMode = false;
  selectedRegion = { x: msg.x, y: msg.y, width: msg.width, height: msg.height };
  transitionTo(STATES.VIDEO_SELECTED);
}

// ── Connection Status ────────────────────────────────────────────────────────

function updateConnectionStatus(connection, message) {
  // Update dot color
  statusDot.className = "status-dot " + connection;

  // Update text
  const labels = {
    connected: "Connected",
    connecting: "Connecting…",
    disconnected: "Disconnected",
    error: "Error",
  };
  statusText.textContent = message || labels[connection] || connection;
}

// ── Commentary Feed ──────────────────────────────────────────────────────────

function appendCommentaryEntry(entry) {
  const div = document.createElement("div");
  div.className = "commentary-entry" + (entry.alert ? " alert" : "") + " raw";

  let html = `<span class="timestamp">${formatTimestamp(entry.timestamp)}</span>`;
  if (entry.alert) {
    html += `<span class="alert-badge">⚠ ${entry.alert.condition}</span>`;
  }
  html += `<span class="description">${escapeHtml(entry.description)}</span>`;

  div.innerHTML = html;
  div.style.cursor = "pointer";
  div.addEventListener("click", () => jumpToTTS(entry.description, div));
  commentaryFeed.appendChild(div);
  commentaryFeed.scrollTop = commentaryFeed.scrollHeight;
  return div;
}

function appendSummaryEntry(entry) {
  const div = document.createElement("div");
  div.className = "commentary-entry summary";

  let html = `<span class="timestamp">${formatTimestamp(entry.timestamp)}</span>`;
  html += `<span class="description">${escapeHtml(entry.description)}</span>`;

  div.innerHTML = html;
  div.style.cursor = "pointer";
  div.addEventListener("click", () => jumpToTTS(entry.description, div));
  commentaryFeed.appendChild(div);
  commentaryFeed.scrollTop = commentaryFeed.scrollHeight;
  return div;
}

function updateKeyEvents(eventsText) {
  let eventsEl = document.getElementById("key-events-content");
  if (!eventsEl) return;
  const section = document.getElementById("key-events-section");
  if (section) section.classList.remove("hidden");
  eventsEl.textContent = eventsText;
}

function escapeHtml(text) {
  const el = document.createElement("span");
  el.textContent = text;
  return el.innerHTML;
}

// ── Alert Conditions ─────────────────────────────────────────────────────────

function addAlertCondition(condition) {
  const trimmed = condition.trim();
  if (!trimmed) return;
  if (alertConditions.includes(trimmed)) return;

  alertConditions.push(trimmed);
  renderAlertList();
}

function removeAlertCondition(index) {
  alertConditions.splice(index, 1);
  renderAlertList();
}

function renderAlertList() {
  alertList.innerHTML = "";
  alertConditions.forEach((cond, i) => {
    const li = document.createElement("li");
    li.innerHTML = `<span>${escapeHtml(cond)}</span>`;
    const removeBtn = document.createElement("button");
    removeBtn.textContent = "×";
    removeBtn.setAttribute("aria-label", `Remove condition: ${cond}`);
    removeBtn.addEventListener("click", () => removeAlertCondition(i));
    li.appendChild(removeBtn);
    alertList.appendChild(li);
  });
}

// ── TTS (Web Speech API) ────────────────────────────────────────────────────

let ttsQueue = [];       // Queue of {text, element} waiting to be spoken
let ttsSpeaking = false; // Is TTS currently speaking?
let ttsCurrentEl = null; // Currently highlighted element

function populateVoices() {
  const voices = window.speechSynthesis.getVoices();
  voiceSelect.innerHTML = "";
  voices.forEach((voice, i) => {
    const opt = document.createElement("option");
    opt.value = i;
    opt.textContent = `${voice.name} (${voice.lang})`;
    if (voice.default) opt.selected = true;
    voiceSelect.appendChild(opt);
  });
}

function queueTTS(text, element) {
  if (!ttsToggle.checked) return;
  // Dedup — skip if same as last queued or currently speaking
  const last = ttsQueue.length > 0 ? ttsQueue[ttsQueue.length - 1].text : lastSpokenText;
  if (last && text.substring(0, 30) === last.substring(0, 30)) return;

  ttsQueue.push({ text, element });
  if (!ttsSpeaking) processNextTTS();
}

function jumpToTTS(text, element) {
  // User clicked — cancel current, clear queue, speak this immediately
  window.speechSynthesis.cancel();
  ttsQueue = [];
  ttsSpeaking = false;
  clearTTSHighlight();
  speakNow(text, element);
}

function processNextTTS() {
  if (ttsQueue.length === 0) { ttsSpeaking = false; return; }
  const next = ttsQueue.shift();
  speakNow(next.text, next.element);
}

function speakNow(text, element) {
  ttsSpeaking = true;
  clearTTSHighlight();

  // Highlight current
  if (element) {
    element.classList.add("speaking");
    ttsCurrentEl = element;
  }

  const utterance = new SpeechSynthesisUtterance(text);
  const voices = window.speechSynthesis.getVoices();
  const selectedIndex = parseInt(voiceSelect.value, 10);
  if (voices[selectedIndex]) utterance.voice = voices[selectedIndex];
  utterance.rate = clampRange(rateSlider.value);

  utterance.onend = () => {
    lastSpokenText = text;
    clearTTSHighlight();
    processNextTTS();
  };
  utterance.onerror = () => {
    clearTTSHighlight();
    processNextTTS();
  };

  window.speechSynthesis.speak(utterance);
}

function clearTTSHighlight() {
  if (ttsCurrentEl) {
    ttsCurrentEl.classList.remove("speaking");
    ttsCurrentEl = null;
  }
}

// Populate voices on load and when they change
if (window.speechSynthesis) {
  populateVoices();
  window.speechSynthesis.onvoiceschanged = populateVoices;
}

// ── Event Listeners ──────────────────────────────────────────────────────────

btnSelectVideo.addEventListener("click", () => {
  if (currentState === STATES.STREAMING) return;
  noVideoMsg.classList.add("hidden");
  commentaryFeed.innerHTML = "";
  commentaryHistory = [];
  lastSpokenText = "";
  window.speechSynthesis.cancel();
  updateConnectionStatus("disconnected"); // Clear any error status
  transitionTo(STATES.SELECTING);
  sendToBackground({ type: "requestVideoSelect" });
});

btnSelectRegion.addEventListener("click", () => {
  if (currentState === STATES.STREAMING) return;
  noVideoMsg.classList.add("hidden");
  commentaryFeed.innerHTML = "";
  commentaryHistory = [];
  lastSpokenText = "";
  window.speechSynthesis.cancel();
  updateConnectionStatus("disconnected");
  transitionTo(STATES.SELECTING);
  sendToBackground({ type: "requestRegionSelect" });
});

btnToggle.addEventListener("click", () => {
  if (currentState === STATES.VIDEO_SELECTED || currentState === STATES.ERROR) {
    // Start capture
    const fps = clampRange(fpsSlider.value);
    const mode = modeSelect.value;
    const context = (userContext.value || "").trim();
    const msg = {
      type: "startCapture",
      fps: fps,
      mode: mode,
      conditions: [...alertConditions],
      userContext: context,
      enableSummarizer: summarizerToggle.checked,
      enableContextOverlay: contextOverlayToggle.checked,
    };
    // Include region info so background can restore it after service worker restart
    if (selectedRegion) {
      msg.region = selectedRegion;
    }
    sendToBackground(msg);
    transitionTo(STATES.STREAMING);
  } else if (currentState === STATES.STREAMING) {
    // Stop capture
    sendToBackground({ type: "stopCapture" });
    window.speechSynthesis.cancel();
    ttsQueue = [];
    ttsSpeaking = false;
    clearTTSHighlight();
    transitionTo(STATES.IDLE);
  }
});

fpsSlider.addEventListener("input", () => {
  fpsValue.textContent = parseFloat(fpsSlider.value).toFixed(1);
});

rateSlider.addEventListener("input", () => {
  rateValue.textContent = parseFloat(rateSlider.value).toFixed(1);
});

btnAddAlert.addEventListener("click", () => {
  addAlertCondition(alertInput.value);
  alertInput.value = "";
});

alertInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    addAlertCondition(alertInput.value);
    alertInput.value = "";
  }
});

// ── Init ─────────────────────────────────────────────────────────────────────

updateUI();
