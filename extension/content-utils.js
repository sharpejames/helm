// Pure utility functions extracted from content.js for testability.
// These have no Chrome API dependencies.

"use strict";

/**
 * Clamp a numeric value to [0.5, 2.0].
 */
function clampFps(value) {
  return Math.min(2.0, Math.max(0.5, value));
}

/**
 * Compute the canvas dimensions for a given video, capping the longest side
 * at 1024 px while preserving the aspect ratio.
 * Returns { width, height }.
 */
function computeResizedDimensions(videoWidth, videoHeight) {
  const MAX_DIM = 512;
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

/**
 * Build a frame message object for sending to the background service worker.
 */
function buildFrameMessage(base64Data, timestamp) {
  return {
    type: "frameData",
    frame: base64Data,
    timestamp: timestamp,
  };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { computeResizedDimensions, clampFps, buildFrameMessage };
}
