// Content script for Helm Video Vision extension
// Handles video element selection, frame capture, and communication with background service worker.
// Kept as lightweight as possible — all heavy processing happens on the backend.

(function () {
  "use strict";

  // ── State ──────────────────────────────────────────────────────────────────
  let selectedVideo = null;
  let captureIntervalId = null;
  let mutationObserver = null;
  let selectorActive = false;
  let captureCanvas = null;
  let captureCtx = null;
  let captureActive = false;
  let captureFps = 1.0;
  let useMssFallback = false; // true if canvas capture fails (DRM/cross-origin)

  const HIGHLIGHT_STYLE = "3px solid #00e1ff";
  let highlightedEl = null;

  // ── Helpers ────────────────────────────────────────────────────────────────

  function clampFps(value) {
    return Math.min(2.0, Math.max(0.1, value));
  }

  // Max 384px longest side — good balance of detail and speed.
  function computeResizedDimensions(videoWidth, videoHeight) {
    const MAX_DIM = 384;
    if (videoWidth <= 0 || videoHeight <= 0) {
      return { width: videoWidth, height: videoHeight };
    }
    if (videoWidth <= MAX_DIM && videoHeight <= MAX_DIM) {
      return { width: videoWidth, height: videoHeight };
    }
    const scale = MAX_DIM / Math.max(videoWidth, videoHeight);
    return {
      width: Math.max(1, Math.round(videoWidth * scale)),
      height: Math.max(1, Math.round(videoHeight * scale)),
    };
  }

  function buildFrameMessage(base64Data, timestamp) {
    return { type: "frameData", frame: base64Data, timestamp: timestamp };
  }

  function safeSend(msg) {
    try {
      if (!chrome.runtime?.id) return; // Extension was reloaded — context dead
      chrome.runtime.sendMessage(msg).catch(() => {});
    } catch (_e) {}
  }

  // ── Video Selector Mode ────────────────────────────────────────────────────

  function activateSelector() {
    const videos = document.querySelectorAll("video");
    console.log("[HelmVision] activateSelector: found", videos.length, "video elements");
    if (videos.length === 0) { safeSend({ type: "noVideo" }); return; }
    selectorActive = true;

    videos.forEach((v, i) => {
      console.log("[HelmVision] video", i, ":", v.videoWidth, "x", v.videoHeight, "src:", v.src || v.currentSrc || "(no src)");

      // Find the container that wraps the video + overlay controls.
      // Ring (and many players) put a div overlay on top of <video> that
      // intercepts clicks. We attach listeners to the parent container
      // so clicks on the overlay still reach us.
      const container = v.parentElement || v;

      container._helmVideo = v; // stash reference to the actual <video>
      container.addEventListener("mouseenter", onVideoMouseEnter);
      container.addEventListener("mouseleave", onVideoMouseLeave);
      container.addEventListener("click", onVideoClick, true); // capture phase
    });
  }

  function onVideoMouseEnter(e) {
    if (!selectorActive) return;
    const container = e.currentTarget;
    const video = container._helmVideo || container;
    highlightedEl = container;
    container._prevOutline = container.style.outline;
    container.style.outline = HIGHLIGHT_STYLE;
    container.style.cursor = "pointer";
  }

  function onVideoMouseLeave(e) {
    if (!selectorActive) return;
    const container = e.currentTarget;
    container.style.outline = container._prevOutline || "";
    container.style.cursor = "";
    if (highlightedEl === container) highlightedEl = null;
  }

  function onVideoClick(e) {
    if (!selectorActive) return;
    e.preventDefault();
    e.stopPropagation();
    const container = e.currentTarget;
    const video = container._helmVideo || container.querySelector("video");
    if (video) {
      console.log("[HelmVision] video clicked via container:", video.videoWidth, "x", video.videoHeight);
      selectVideo(video);
    }
  }

  function selectVideo(video) {
    deactivateSelector();
    selectedVideo = video;

    let thumbnail = "";
    try {
      const c = document.createElement("canvas");
      const d = computeResizedDimensions(video.videoWidth, video.videoHeight);
      c.width = d.width; c.height = d.height;
      c.getContext("2d").drawImage(video, 0, 0, d.width, d.height);
      thumbnail = c.toDataURL("image/jpeg", 0.4).replace(/^data:image\/\w+;base64,/, "");
    } catch (_e) {}

    safeSend({ type: "videoSelected", width: video.videoWidth, height: video.videoHeight, thumbnail });
    startMutationObserver();
    video.addEventListener("pause", onVideoPause);
    video.addEventListener("play", onVideoPlay);
  }

  function deactivateSelector() {
    selectorActive = false;
    document.querySelectorAll("video").forEach((v) => {
      const container = v.parentElement || v;
      container.removeEventListener("mouseenter", onVideoMouseEnter);
      container.removeEventListener("mouseleave", onVideoMouseLeave);
      container.removeEventListener("click", onVideoClick, true);
      container.style.outline = container._prevOutline || "";
      container.style.cursor = "";
      delete container._helmVideo;
    });
    highlightedEl = null;
  }

  // ── Frame Capture (minimal — no motion detection on client) ────────────────

  function startCapture(fps) {
    if (!selectedVideo) {
      console.log("[HelmVision] startCapture: no selectedVideo");
      safeSend({ type: "captureError", reason: "No video selected" });
      return;
    }
    stopCapture();
    captureFps = clampFps(fps);
    captureActive = true;
    console.log("[HelmVision] startCapture: fps=", captureFps);
    captureAndSendFrame();
  }

  function captureAndSendFrame() {
    if (!captureActive || !selectedVideo) return;

    if (selectedVideo.paused) {
      captureIntervalId = setTimeout(captureAndSendFrame, 1000);
      return;
    }

    if (!document.contains(selectedVideo)) {
      stopCapture();
      safeSend({ type: "captureError", reason: "Video element was removed" });
      return;
    }

    // If we already know canvas doesn't work, go straight to mss fallback
    if (useMssFallback) {
      sendRegionCapture();
      return;
    }

    try {
      const vw = selectedVideo.videoWidth;
      const vh = selectedVideo.videoHeight;
      if (vw === 0 || vh === 0) {
        captureIntervalId = setTimeout(captureAndSendFrame, 1000);
        return;
      }

      const dims = computeResizedDimensions(vw, vh);

      if (!captureCanvas || captureCanvas.width !== dims.width || captureCanvas.height !== dims.height) {
        captureCanvas = document.createElement("canvas");
        captureCanvas.width = dims.width;
        captureCanvas.height = dims.height;
        captureCtx = captureCanvas.getContext("2d", { willReadFrequently: true });
      }

      captureCtx.drawImage(selectedVideo, 0, 0, dims.width, dims.height);

      // Test if we got actual pixels or a blank DRM frame
      const testData = captureCtx.getImageData(0, 0, 1, 1).data;
      // DRM videos render as all-black (0,0,0,0) or (0,0,0,255)
      // Check a few pixels to be sure
      let isBlank = true;
      const sampleCtx = captureCtx;
      for (let sx = 0; sx < Math.min(dims.width, 8); sx += 2) {
        const px = sampleCtx.getImageData(sx, Math.floor(dims.height / 2), 1, 1).data;
        if (px[0] > 5 || px[1] > 5 || px[2] > 5) { isBlank = false; break; }
      }

      if (isBlank) {
        console.log("[HelmVision] Canvas returned blank frame (DRM?), switching to mss fallback");
        useMssFallback = true;
        sendRegionCapture();
        return;
      }

      const dataUrl = captureCanvas.toDataURL("image/jpeg", 0.4);
      const base64 = dataUrl.replace(/^data:image\/\w+;base64,/, "");

      console.log("[HelmVision] sending canvas frame:", Math.round(base64.length / 1024), "KB");
      safeSend(buildFrameMessage(base64, Date.now() / 1000));
    } catch (err) {
      if (err.name === "SecurityError") {
        console.log("[HelmVision] SecurityError on canvas, switching to mss fallback");
        useMssFallback = true;
        sendRegionCapture();
      } else {
        captureIntervalId = setTimeout(captureAndSendFrame, 2000);
      }
    }
  }

  /**
   * Fallback: send the video element's screen coordinates to the backend
   * so it can use mss to screenshot that region of the screen.
   */
  function sendRegionCapture() {
    if (!selectedVideo) return;

    const rect = selectedVideo.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    // screenX/screenY = window outer position (physical pixels on Windows)
    // Calculate viewport origin on screen
    const chromeLeft = window.screenX + Math.round((window.outerWidth - window.innerWidth) / 2);
    const chromeTop = window.screenY + (window.outerHeight - window.innerHeight);
    const screenX = Math.round(chromeLeft + rect.left * dpr);
    const screenY = Math.round(chromeTop + rect.top * dpr);
    const width = Math.round(rect.width * dpr);
    const height = Math.round(rect.height * dpr);

    console.log("[HelmVision] sending region capture: screen=", screenX, screenY, "size=", width, height, "dpr:", dpr);
    safeSend({
      type: "regionCapture",
      x: screenX,
      y: screenY,
      width: width,
      height: height,
      timestamp: Date.now() / 1000,
    });
  }

  function onReadyForFrame() {
    if (!captureActive) return;
    console.log("[HelmVision] readyForFrame received, scheduling next capture");
    captureIntervalId = setTimeout(captureAndSendFrame, 1000 / captureFps);
  }

  function stopCapture() {
    captureActive = false;
    useMssFallback = false;
    if (captureIntervalId !== null) {
      clearTimeout(captureIntervalId);
      captureIntervalId = null;
    }
    captureCanvas = null;
    captureCtx = null;
  }

  function onVideoPause() {}
  function onVideoPlay() {}

  // ── MutationObserver ───────────────────────────────────────────────────────

  function startMutationObserver() {
    if (mutationObserver) mutationObserver.disconnect();
    mutationObserver = new MutationObserver(() => {
      if (!selectedVideo || document.contains(selectedVideo)) return;
      stopCapture();
      cleanupVideoListeners();
      safeSend({ type: "captureError", reason: "Video element was removed" });
      selectedVideo = null;
      mutationObserver.disconnect();
      mutationObserver = null;
    });
    mutationObserver.observe(document.body, { childList: true, subtree: true });
  }

  // ── Cleanup ────────────────────────────────────────────────────────────────

  function cleanupVideoListeners() {
    if (selectedVideo) {
      selectedVideo.removeEventListener("pause", onVideoPause);
      selectedVideo.removeEventListener("play", onVideoPlay);
    }
  }

  function cleanup() {
    stopCapture();
    deactivateSelector();
    cleanupVideoListeners();
    if (mutationObserver) { mutationObserver.disconnect(); mutationObserver = null; }
    selectedVideo = null;
  }

  // ── Region Selector ────────────────────────────────────────────────────────

  function activateRegionSelector() {
    // Create full-screen overlay for click-drag region selection
    const overlay = document.createElement("div");
    overlay.id = "helm-region-overlay";
    overlay.style.cssText = "position:fixed;top:0;left:0;width:100vw;height:100vh;z-index:2147483647;cursor:crosshair;background:rgba(0,0,0,0.15);";

    const rect = document.createElement("div");
    rect.style.cssText = "position:absolute;border:2px dashed #00e1ff;background:rgba(0,225,255,0.1);display:none;pointer-events:none;";
    overlay.appendChild(rect);

    let startX = 0, startY = 0, dragging = false;

    overlay.addEventListener("mousedown", (e) => {
      startX = e.clientX;
      startY = e.clientY;
      dragging = true;
      rect.style.left = startX + "px";
      rect.style.top = startY + "px";
      rect.style.width = "0px";
      rect.style.height = "0px";
      rect.style.display = "block";
      e.preventDefault();
    });

    overlay.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      const x = Math.min(e.clientX, startX);
      const y = Math.min(e.clientY, startY);
      const w = Math.abs(e.clientX - startX);
      const h = Math.abs(e.clientY - startY);
      rect.style.left = x + "px";
      rect.style.top = y + "px";
      rect.style.width = w + "px";
      rect.style.height = h + "px";
    });

    overlay.addEventListener("mouseup", (e) => {
      if (!dragging) return;
      dragging = false;

      const x = Math.min(e.clientX, startX);
      const y = Math.min(e.clientY, startY);
      const w = Math.abs(e.clientX - startX);
      const h = Math.abs(e.clientY - startY);

      // Remove overlay
      overlay.remove();

      if (w < 10 || h < 10) return; // Too small, ignore

      // Convert to screen coordinates for mss.
      // screenX/screenY = window outer position (physical pixels on Windows)
      // outerWidth - innerWidth = horizontal chrome (borders)
      // outerHeight - innerHeight = vertical chrome (title bar + tabs + address bar)
      // clientX/clientY are CSS pixels from viewport origin
      const dpr = window.devicePixelRatio || 1;
      const chromeLeft = window.screenX + Math.round((window.outerWidth - window.innerWidth) / 2);
      const chromeTop = window.screenY + (window.outerHeight - window.innerHeight);
      const screenRegionX = Math.round(chromeLeft + x * dpr);
      const screenRegionY = Math.round(chromeTop + y * dpr);
      const screenW = Math.round(w * dpr);
      const screenH = Math.round(h * dpr);

      console.log("[HelmVision] region selected: chrome offset=", chromeLeft, chromeTop,
        "region=", screenRegionX, screenRegionY, screenW, screenH, "dpr:", dpr);

      safeSend({
        type: "regionSelected",
        x: screenRegionX,
        y: screenRegionY,
        width: screenW,
        height: screenH,
      });
    });

    // Allow escape to cancel
    const onKeyDown = (e) => {
      if (e.key === "Escape") {
        overlay.remove();
        document.removeEventListener("keydown", onKeyDown);
      }
    };
    document.addEventListener("keydown", onKeyDown);

    document.body.appendChild(overlay);
  }

  // ── Message Listener ───────────────────────────────────────────────────────

  chrome.runtime.onMessage.addListener((message, _sender, _sendResponse) => {
    // Guard against stale content script after extension reload
    if (!chrome.runtime?.id) return;
    try {
      switch (message.action) {
        case "activateSelector": activateSelector(); break;
        case "activateRegionSelector": activateRegionSelector(); break;
        case "startCapture": startCapture(message.fps); break;
        case "stopCapture": stopCapture(); break;
        case "cleanup": cleanup(); break;
        case "readyForFrame": onReadyForFrame(); break;
      }
    } catch (_e) {}
  });

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { computeResizedDimensions, clampFps, buildFrameMessage };
  }
})();
