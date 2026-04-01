// Service worker for Helm Video Vision extension
"use strict";

const WS_URL = "ws://localhost:8765/api/video/extension-stream";
const MAX_RECONNECT_ATTEMPTS = 5;

// ── State ────────────────────────────────────────────────────────────────────
let ws = null;
let connectionStatus = "disconnected";
let reconnectAttempts = 0;
let reconnectTimeoutId = null;
let activeTabId = null;
let sessionConditions = [];
let sessionFps = 1.0;
let wsSendBusy = false;
let wsBusyTimeoutId = null;

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

  setConnectionStatus("connecting");
  try { ws = new WebSocket(WS_URL); } catch (_e) { setConnectionStatus("error", "Failed to create WebSocket"); return; }

  ws.onopen = () => {
    reconnectAttempts = 0;
    wsSendBusy = false;
    setConnectionStatus("connected");
    try { ws.send(JSON.stringify({ type: "configure", conditions: sessionConditions })); } catch (_e) {}
    sendToContentScript({ action: "startCapture", fps: sessionFps });
  };

  ws.onmessage = (event) => {
    try { handleWebSocketMessage(JSON.parse(event.data)); } catch (_e) {}
  };

  ws.onclose = () => {
    ws = null;
    wsSendBusy = false;
    if (connectionStatus === "connected" || connectionStatus === "connecting") attemptReconnect();
  };

  ws.onerror = () => {};
}

function closeWebSocket() {
  clearReconnectTimeout();
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
  if (reconnectAttempts > MAX_RECONNECT_ATTEMPTS) {
    setConnectionStatus("error", "Cannot reach Helm backend after 5 attempts");
    sendToContentScript({ action: "stopCapture" });
    return;
  }
  const delay = getBackoffDelay(reconnectAttempts);
  setConnectionStatus("connecting", `Reconnecting in ${delay / 1000}s (attempt ${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS})`);
  reconnectTimeoutId = setTimeout(() => { reconnectTimeoutId = null; openWebSocket(); }, delay);
}

function clearReconnectTimeout() {
  if (reconnectTimeoutId !== null) { clearTimeout(reconnectTimeoutId); reconnectTimeoutId = null; }
}

// ── WebSocket Message Handling ───────────────────────────────────────────────

function handleWebSocketMessage(data) {
  if (wsBusyTimeoutId) { clearTimeout(wsBusyTimeoutId); wsBusyTimeoutId = null; }

  if (data.type === "commentary") {
    sendToPanel({
      type: "commentary",
      description: data.description,
      timestamp: data.timestamp,
      ...(data.alert ? { alert: data.alert } : {}),
    });
    if (data.alert) triggerAlertNotification(data.description, data.alert.condition);
    wsSendBusy = false;
    sendToContentScript({ action: "readyForFrame" });
  } else if (data.type === "no_activity") {
    wsSendBusy = false;
    sendToContentScript({ action: "readyForFrame" });
  } else if (data.type === "error") {
    sendToPanel({ type: "status", connection: connectionStatus, message: data.message || "Backend error" });
    wsSendBusy = false;
    sendToContentScript({ action: "readyForFrame" });
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
      sendToPanel({ type: "videoInfo", width: message.width, height: message.height, thumbnail: message.thumbnail });
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
        wsBusyTimeoutId = setTimeout(() => { if (wsSendBusy) { wsSendBusy = false; sendToContentScript({ action: "readyForFrame" }); } }, 120000);
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
        wsBusyTimeoutId = setTimeout(() => { if (wsSendBusy) { wsSendBusy = false; sendToContentScript({ action: "readyForFrame" }); } }, 120000);
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
      wsSendBusy = false;
      if (wsBusyTimeoutId) { clearTimeout(wsBusyTimeoutId); wsBusyTimeoutId = null; }
      openWebSocket();
      break;
    case "stopCapture":
      sendToContentScript({ action: "stopCapture" });
      closeWebSocket();
      break;
    case "requestVideoSelect":
      chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        if (tabs && tabs.length > 0) injectContentScriptAndSelect(tabs[0].id);
      });
      break;
  }
}

// ── Extension Icon Click ─────────────────────────────────────────────────────

chrome.action.onClicked.addListener((tab) => {
  try { chrome.sidePanel.open({ tabId: tab.id }); } catch (_e) {}
});
