# Implementation Plan: Video Vision Extension

## Overview

Build a Chrome extension (Manifest V3) and backend WebSocket endpoint that enable real-time AI-powered video description from any `<video>` element on a web page. Implementation proceeds backend-first (new endpoint + vision helper), then extension scaffolding, then wiring everything together.

## Tasks

- [x] 1. Add `describe_frame_with_context` helper to core/vision.py
  - Add the `describe_frame_with_context(vision, frame, recent_descriptions)` function that wraps `VisionModule.describe_frame` with the last 3 descriptions as context in the prompt
  - The function instructs the model to describe only meaningful changes relative to previous descriptions
  - Returns plain text description string
  - _Requirements: 7.1, 7.2, 7.3_

  - [ ]* 1.1 Write property test for context prompt inclusion (Property 11)
    - **Property 11: Context prompt includes recent descriptions**
    - Generate description sequences of length ≥ 3, verify last 3 appear in prompt
    - **Validates: Requirements 7.2**

  - [ ]* 1.2 Write property test for sliding window bounded size (Property 10)
    - **Property 10: Sliding window bounded size**
    - Generate random description sequences of length > 10, verify window contains exactly last 10
    - **Validates: Requirements 7.1**

- [x] 2. Add `/api/video/extension-stream` WebSocket endpoint to web/routes/video.py
  - Create the bidirectional WebSocket endpoint that accepts JSON messages with types: `frame`, `configure`, `stop`
  - On connect: create per-session `EventDetector`, `CommentaryStream`, and a `deque(maxlen=10)` description history
  - On `frame` message: decode base64 PNG, call `describe_frame_with_context`, push description through per-session `EventDetector` and `CommentaryStream`, send commentary JSON back
  - On `configure` message: set alert conditions on the per-session `EventDetector`
  - On `stop` or disconnect: clean up session resources (stop commentary stream, clear history)
  - Reuse shared `app.state.alert_system` for desktop notifications
  - Handle errors: invalid base64, vision failures, malformed JSON — send error messages back, continue listening
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 7.1, 7.2, 7.3, 7.4_

  - [ ]* 2.1 Write property test for frame encoding round-trip (Property 1)
    - **Property 1: Frame encoding round-trip**
    - Generate random byte sequences, encode to base64, decode, verify equality
    - **Validates: Requirements 2.2, 4.2**

  - [ ]* 2.2 Write property test for commentary pipeline delivery (Property 5)
    - **Property 5: Commentary pipeline delivery**
    - Generate random non-duplicate descriptions, push through pipeline, verify CommentaryStream output matches
    - **Validates: Requirements 4.3, 4.4, 3.3**

  - [ ]* 2.3 Write property test for alert condition detection (Property 8)
    - **Property 8: Alert condition detection flags messages**
    - Generate random descriptions and condition lists, verify alert flags match case-insensitive substring presence
    - **Validates: Requirements 6.4**

  - [ ]* 2.4 Write property test for alert conditions transmitted (Property 9)
    - **Property 9: Alert conditions transmitted to backend**
    - Generate random condition lists, verify configure message sets all conditions on EventDetector
    - **Validates: Requirements 6.3**

  - [ ]* 2.5 Write property test for duplicate description suppression (Property 12)
    - **Property 12: Duplicate description suppression**
    - Generate sequences with consecutive duplicates, verify only first of each run is emitted by CommentaryStream
    - **Validates: Requirements 7.4**

- [x] 3. Checkpoint — Backend verification
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Create extension scaffold: manifest.json and project structure
  - Create `extension/` directory with `manifest.json` declaring MV3, permissions (sidePanel, activeTab, scripting, notifications), host_permissions (ws://localhost:8765/*), service worker, side panel, and 128x128 icon placeholder
  - Create `extension/icons/` directory with a placeholder `icon128.png`
  - _Requirements: 9.1, 9.2, 9.3, 9.4, 10.1, 10.2, 10.3_

- [x] 5. Implement content script (extension/content.js)
  - Implement video selector mode: listen for `activateSelector` message, highlight `<video>` elements on hover with visible border, click to select and report dimensions + thumbnail back to background
  - Implement frame capture: on `startCapture` message, draw selected video to offscreen Canvas at configured FPS, resize to max 1024px longest side preserving aspect ratio, encode as base64 PNG, send `frameData` message to background
  - Handle `stopCapture` and `cleanup` messages
  - Handle edge cases: no video elements found (send `noVideo`), video element removed from DOM (MutationObserver), video paused/resumed, cross-origin canvas SecurityError
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_

  - [ ]* 5.1 Write property test for frame resize aspect ratio (Property 2)
    - **Property 2: Frame resize preserves aspect ratio with max dimension**
    - Generate random (width, height) pairs, verify resized dimensions satisfy max(outW, outH) <= 1024 and aspect ratio preserved within rounding tolerance
    - **Validates: Requirements 2.6**

  - [ ]* 5.2 Write property test for frame message JSON structure (Property 3)
    - **Property 3: Frame message JSON structure**
    - Generate random base64 strings and timestamps, verify message has `type: "frameData"`, non-empty `frame`, and positive `timestamp`
    - **Validates: Requirements 3.2**

  - [ ]* 5.3 Write property test for numeric config range clamping (Property 13)
    - **Property 13: Numeric configuration range clamping**
    - Generate random floats, verify clamping to [0.5, 2.0]
    - **Validates: Requirements 2.3, 8.5**

- [x] 6. Implement service worker (extension/background.js)
  - Manage WebSocket connection lifecycle: open on startCapture, close on stopCapture, handle reconnection with exponential backoff (2^(N-1) seconds, up to 5 attempts)
  - Relay messages between content script and side panel
  - Forward `frameData` from content script to WebSocket as `{type: "frame", data, timestamp}`
  - Forward `commentary` from WebSocket to side panel
  - Send `configure` message with alert conditions at session start
  - Send `stop` message on session end
  - Trigger Chrome desktop notifications on alert-flagged messages
  - Track connection status: disconnected, connecting, connected, error
  - Open side panel on extension icon click via `chrome.sidePanel.open`
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 6.3, 6.4, 6.5, 9.2_

  - [ ]* 6.1 Write property test for reconnect exponential backoff (Property 4)
    - **Property 4: Reconnect exponential backoff timing**
    - Generate attempt numbers 1–5, verify delay = 2^(N-1) seconds
    - **Validates: Requirements 3.4**

  - [ ]* 6.2 Write property test for connection status validity (Property 6)
    - **Property 6: Connection status validity**
    - Generate random state transitions, verify status is always one of the four valid values
    - **Validates: Requirements 5.4**

- [x] 7. Implement side panel UI (extension/panel.html, panel.js, panel.css)
  - Build HTML with: "Select Video" button, "Start/Stop" toggle, FPS slider (0.5–2.0, default 1.0), connection status indicator, scrollable commentary feed, alert condition input + add/remove list, TTS toggle + voice dropdown + rate slider (0.5–2.0, default 1.0), video thumbnail preview area
  - Implement panel.js state machine: Idle → Selecting → VideoSelected → Streaming → Error
  - Wire message passing to/from background service worker
  - Implement TTS via Web Speech API (SpeechSynthesis): queue utterances, voice selection, rate control
  - Maintain full commentary history log for the session
  - _Requirements: 1.4, 1.5, 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 6.1, 6.2, 6.5, 6.6, 8.1, 8.2, 8.3, 8.4, 8.5_

  - [ ]* 7.1 Write property test for commentary history completeness (Property 7)
    - **Property 7: Commentary history completeness**
    - Generate random sequences of N distinct commentary entries, verify history contains exactly N entries in order
    - **Validates: Requirements 5.2, 5.3**

- [x] 8. Checkpoint — Extension integration verification
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Wire extension components together and end-to-end validation
  - Verify content script ↔ background ↔ side panel message flow works correctly
  - Verify background ↔ backend WebSocket frame/commentary round-trip
  - Verify alert conditions flow: panel → background → backend configure → detection → alert message → notification
  - Verify TTS speaks commentary entries when enabled
  - Verify extension loads as unpacked extension in Chrome without errors
  - _Requirements: 1.1–1.5, 2.1–2.6, 3.1–3.6, 4.1–4.6, 5.1–5.6, 6.1–6.6, 7.1–7.4, 8.1–8.5, 9.1–9.4, 10.1–10.3_

- [x] 10. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Backend tasks (1–2) come first since the extension depends on the WebSocket endpoint
- Property tests use `hypothesis` for Python (backend) and `fast-check` for JavaScript (extension)
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
