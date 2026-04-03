// Service worker for Helm Video Vision extension
"use strict";

const WS_URL = "ws://localhost:8765/api/video/extension-stream";
const MAX_RECONNECT_ATTEMPTS = Infinity; // Keep reconnecting until user presses Stop

// ── State ────────────────────────────────────────────────────────────────────
let ws = null;
let connectionStatus = "disconnected";
let reconnectAttempts = 0;
let reconnectTimeoutId = null;
let activeTabId = null;
let sessionConditions = [];
let sessionFps = 1.0;
let sessionMode = "surveillance";
let wsSendBusy = false;
let wsBusyTimeoutId = null;
let pingIntervalId = null;
let storedRegion = null;
let regionCaptureIntervalId = null;
let isRegionMode = false;
let userRequestedStop = false; // true only when user clicks Stop
let sessionUserContext = "";

// Port-based connection to side panel (reliable, no message loss)
let panelPort = null;

// ── Safe Messaging ───────────────────────────────────────────────────────────

function sendToPanel(message) {
  if (panelPort) {
    try { panelPort.postMessage(message); } catch (_e) { panelPort = null; }
  }
}

function sendToContentScript(message) {
  if (activeTabId == null) return;
  try { chrome.tabs.sendMessage(activeTabId, message).catch(() => {}); } catch (_e) {}
}

function getBackoffDelay(attempt) {
  return Math.pow(2, attempt - 1) * 1000;
}

function setConnectionStatus(status, message) {
  connectionStatus = status;
  const msg = { type: "status", connection: status };
  if (message) msg.message = message;
  sendToPanel(msg);
}

// ── Panel Port Connection ────────────────────────────────────────────────────

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "helm-panel") return;
  console.log("[HelmBG] Panel connected via port");
  panelPort = port;

  // Send current status immediately so panel knows the state
  sendToPanel({ type: "status", connection: connectionStatus });

  port.onMessage.addListener((message) => {
    try { handlePanelMessage(message); } catch (_e) {}
  });

  port.onDisconnect.addListener(() => {
    console.log("[HelmBG] Panel disconnected");
    panelPort = null;
  });
});

// ── WebSocket Management ─────────────────────────────────────────────────────

function openWebSocket() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  userRequestedStop = false; // Starting a new session

  setConnectionStatus("connecting");
  try { ws = new WebSocket(WS_URL); } catch (_e) { setConnectionStatus("error", "Failed to create WebSocket"); return; }

  ws.onopen = () => {
    reconnectAttempts = 0;
    wsSendBusy = false;
    setConnectionStatus("connected");
    try { ws.send(JSON.stringify({ type: "configure", conditions: sessionConditions, mode: sessionMode, userContext: sessionUserContext })); } catch (_e) {}
    // Start keep-alive pings
    clearPingInterval();
    pingIntervalId = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        try { ws.send(JSON.stringify({ type: "ping" })); } catch (_e) {}
      }
    }, 30000);
    if (isRegionMode && storedRegion) {
      startRegionCaptureTimer();
    } else {
      sendToContentScript({ action: "startCapture", fps: sessionFps });
    }
  };

  ws.onmessage = (event) => {
    try { handleWebSocketMessage(JSON.parse(event.data)); } catch (_e) {}
  };

  ws.onclose = () => {
    ws = null;
    wsSendBusy = false;
    clearPingInterval();
    // Only reconnect if user didn't explicitly stop
    if (!userRequestedStop) attemptReconnect();
  };

  ws.onerror = () => {};
}

function clearPingInterval() {
  if (pingIntervalId !== null) { clearInterval(pingIntervalId); pingIntervalId = null; }
}

function startRegionCaptureTimer() {
  stopRegionCaptureTimer();
  if (!storedRegion) return;
  const intervalMs = 1000 / sessionFps;
  sendRegionCaptureFromBackground();
  regionCaptureIntervalId = setInterval(() => {
    if (!wsSendBusy) sendRegionCaptureFromBackground();
  }, intervalMs);
}

function stopRegionCaptureTimer() {
  if (regionCaptureIntervalId !== null) { clearInterval(regionCaptureIntervalId); regionCaptureIntervalId = null; }
}

function sendRegionCaptureFromBackground() {
  if (!ws || ws.readyState !== WebSocket.OPEN || wsSendBusy || !storedRegion) return;
  wsSendBusy = true;
  try {
    ws.send(JSON.stringify({
      type: "region_capture",
      x: storedRegion.x,
      y: storedRegion.y,
      width: storedRegion.width,
      height: storedRegion.height,
      timestamp: Date.now() / 1000,
    }));
    if (wsBusyTimeoutId) clearTimeout(wsBusyTimeoutId);
    wsBusyTimeoutId = setTimeout(() => { if (wsSendBusy) { wsSendBusy = false; } }, 30000);
  } catch (_e) { wsSendBusy = false; }
}

function closeWebSocket() {
  userRequestedStop = true; // Mark as intentional disconnect
  clearReconnectTimeout();
  clearPingInterval();
  stopRegionCaptureTimer();
  reconnectAttempts = 0;
  wsSendBusy = false;
  if (ws) {
    try { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "stop" })); } catch (_e) {}
    ws.onclose = null; ws.onerror = null; ws.close(); ws = null;
  }
  setConnectionStatus("disconnected");
}

function attemptReconnect() {
  reconnectAttempts++;
  // Cap backoff at 30 seconds so we don't wait forever
  const delay = Math.min(getBackoffDelay(reconnectAttempts), 30000);
  setConnectionStatus("connecting", `Reconnecting in ${delay / 1000}s (attempt ${reconnectAttempts})`);
  reconnectTimeoutId = setTimeout(() => { reconnectTimeoutId = null; openWebSocket(); }, delay);
}

function clearReconnectTimeout() {
  if (reconnectTimeoutId !== null) { clearTimeout(reconnectTimeoutId); reconnectTimeoutId = null; }
}

// ── WebSocket Message Handling ───────────────────────────────────────────────

function handleWebSocketMessage(data) {
  if (wsBusyTimeoutId) { clearTimeout(wsBusyTimeoutId); wsBusyTimeoutId = null; }

  if (data.type === "pong") {
    // Keep-alive response, nothing to do
    return;
  }

  if (data.type === "commentary") {
    sendToPanel({
      type: "commentary",
      description: data.description,
      timestamp: data.timestamp,
      ...(data.thumbnail ? { thumbnail: data.thumbnail } : {}),
      ...(data.alert ? { alert: data.alert } : {}),
    });
    if (data.alert) triggerAlertNotification(data.description, data.alert.condition);
    wsSendBusy = false;
    if (!isRegionMode) sendToContentScript({ action: "readyForFrame" });
  } else if (data.type === "no_activity") {
    if (data.thumbnail) {
      sendToPanel({ type: "thumbnail", thumbnail: data.thumbnail });
    }
    wsSendBusy = false;
    if (!isRegionMode) sendToContentScript({ action: "readyForFrame" });
  } else if (data.type === "error") {
    sendToPanel({ type: "status", connection: connectionStatus, message: data.message || "Backend error" });
    wsSendBusy = false;
    if (!isRegionMode) sendToContentScript({ action: "readyForFrame" });
  }
}

function triggerAlertNotification(description, condition) {
  try { chrome.notifications.create({ type: "basic", iconUrl: "icons/icon128.png", title: `Alert: ${condition}`, message: description }); } catch (_e) {}
}

// ── Content Script Injection ─────────────────────────────────────────────────

async function injectContentScriptAndSelect(tabId) {
  activeTabId = tabId;
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
    sendToContentScript({ action: "activateSelector" });
  } catch (_e) {
    sendToPanel({ type: "status", connection: connectionStatus, message: "Cannot access this page" });
  }
}

// ── Content Script Messages (via chrome.runtime.onMessage) ───────────────────

chrome.runtime.onMessage.addListener((message, sender, _sendResponse) => {
  // Only handle messages from content scripts (have sender.tab)
  if (!sender.tab) return;
  try { handleContentScriptMessage(message); } catch (_e) {}
});

function handleContentScriptMessage(message) {
  switch (message.type) {
    case "videoSelected":
      console.log("[HelmBG] videoSelected:", message.width, "x", message.height);
      isRegionMode = false;
      storedRegion = null;
      sendToPanel({ type: "videoInfo", width: message.width, height: message.height, thumbnail: message.thumbnail });
      break;
    case "regionSelected":
      console.log("[HelmBG] regionSelected:", message.x, message.y, message.width, message.height);
      isRegionMode = true;
      storedRegion = { x: message.x, y: message.y, width: message.width, height: message.height };
      sendToPanel({ type: "regionSelected", x: message.x, y: message.y, width: message.width, height: message.height });
      break;
    case "frameData":
      if (!ws || ws.readyState !== WebSocket.OPEN || wsSendBusy) {
        sendToContentScript({ action: "readyForFrame" });
        return;
      }
      wsSendBusy = true;
      try {
        ws.send(JSON.stringify({ type: "frame", data: message.frame, timestamp: message.timestamp }));
        if (wsBusyTimeoutId) clearTimeout(wsBusyTimeoutId);
        wsBusyTimeoutId = setTimeout(() => { if (wsSendBusy) { wsSendBusy = false; sendToContentScript({ action: "readyForFrame" }); } }, 30000);
      } catch (_e) { wsSendBusy = false; sendToContentScript({ action: "readyForFrame" }); }
      break;
    case "regionCapture":
      // Fallback: send screen coordinates to backend for mss screenshot
      if (!ws || ws.readyState !== WebSocket.OPEN || wsSendBusy) {
        sendToContentScript({ action: "readyForFrame" });
        return;
      }
      wsSendBusy = true;
      try {
        ws.send(JSON.stringify({ type: "region_capture", x: message.x, y: message.y, width: message.width, height: message.height, timestamp: message.timestamp }));
        if (wsBusyTimeoutId) clearTimeout(wsBusyTimeoutId);
        wsBusyTimeoutId = setTimeout(() => { if (wsSendBusy) { wsSendBusy = false; sendToContentScript({ action: "readyForFrame" }); } }, 30000);
      } catch (_e) { wsSendBusy = false; sendToContentScript({ action: "readyForFrame" }); }
      break;
    case "captureError":
      sendToPanel({ type: "captureError", reason: message.reason });
      break;
    case "noVideo":
      sendToPanel({ type: "noVideo" });
      break;
  }
}

// ── Panel Messages (via port) ────────────────────────────────────────────────

function handlePanelMessage(message) {
  switch (message.type) {
    case "startCapture":
      sessionFps = message.fps || 1.0;
      sessionConditions = message.conditions || [];
      sessionMode = message.mode || "surveillance";
      sessionUserContext = message.userContext || "";
      wsSendBusy = false;
      if (wsBusyTimeoutId) { clearTimeout(wsBusyTimeoutId); wsBusyTimeoutId = null; }
      openWebSocket();
      break;
    case "stopCapture":
      stopRegionCaptureTimer();
      sendToContentScript({ action: "stopCapture" });
      closeWebSocket();
      break;
    case "requestVideoSelect":
      chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        if (tabs && tabs.length > 0) injectContentScriptAndSelect(tabs[0].id);
      });
      break;
    case "requestRegionSelect":
      chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        if (tabs && tabs.length > 0) injectContentScriptAndActivateRegion(tabs[0].id);
      });
      break;
  }
}

async function injectContentScriptAndActivateRegion(tabId) {
  activeTabId = tabId;
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
    sendToContentScript({ action: "activateRegionSelector" });
  } catch (_e) {
    sendToPanel({ type: "status", connection: connectionStatus, message: "Cannot access this page" });
  }
}

// ── Extension Icon Click ─────────────────────────────────────────────────────

chrome.action.onClicked.addListener((tab) => {
  try { chrome.sidePanel.open({ tabId: tab.id }); } catch (_e) {}
});
