# Implementation Plan: Helm V2 Rebuild

## Overview

Rebuild Helm's desktop automation with DOM/UIA-based element targeting, batch action planning, replayable training recordings, and add a new real-time video analysis system. Tasks are ordered by dependency: core perception layer first, then agent modifications, then video analysis, then API wiring.

## Tasks

- [x] 1. Core Perception Layer — DOM Inspector and UIA Module
  - [x] 1.1 Create `core/dom_inspector.py` — Chrome DevTools Protocol client
    - Implement `DOMInspector` class with `connect()`, `disconnect()`, `query_selector()`, `query_selector_one()`, `get_page_url()`, `evaluate_js()`, and `connected` property
    - Use `websockets` library for CDP communication on port 9222
    - Return element coordinates in screen-space (adjusted for browser chrome offset)
    - Implement lazy connection — only connect when first web action is requested
    - Log connection errors and expose `connected` property for fallback logic
    - _Requirements: 1.1, 1.2, 1.3, 1.4_

  - [ ]* 1.2 Write unit tests for `DOMInspector`
    - Test `query_selector` returns list of elements with bounding rects and text
    - Test `query_selector_one` returns first match or None
    - Test connection failure handling and `connected` property
    - Mock websocket CDP responses for offline testing
    - _Requirements: 1.1, 1.3, 1.4_

  - [x] 1.3 Create `core/uia_module.py` — Windows UI Automation wrapper
    - Implement `UIAModule` class with `find_element()`, `find_all()`, `get_element_tree()`, `click_element()`, `invalidate_cache()`
    - Use `comtypes` and `UIAutomationCore` for COM interop
    - Cache element tree per window title; invalidate after action batches
    - Implement 5-second timeout on element search before returning None
    - _Requirements: 4.1, 4.2, 4.3, 4.4_

  - [ ]* 1.4 Write unit tests for `UIAModule`
    - Test `find_element` returns bounding rect dict or None on timeout
    - Test element tree caching and `invalidate_cache`
    - Test `click_element` returns True/False based on element discovery
    - _Requirements: 4.1, 4.2, 4.3, 4.4_

- [x] 2. Vision Module Rebuild — Ollama-first with structured output
  - [x] 2.1 Rewrite `core/vision.py` — Replace Gemini-only with Ollama-first `VisionModule`
    - Implement `VisionModule` class with `describe_screen(screenshot, detail)`, `verify_action(before, after, expected)`, `describe_frame(frame)`, `compare_frames(desc_prev, desc_current)`
    - Route `detail="fast"` to Qwen3.5:0.8B, `detail="detailed"` to Qwen3.5:4B via Ollama API
    - `describe_screen` returns structured dict: `{"app", "elements", "state", "description"}`
    - `verify_action` compares before/after screenshots, returns `{"success", "confidence", "changes"}`
    - `describe_frame` uses 0.8B exclusively for <1s latency
    - Remove `estimate_location` and any vision-based coordinate estimation methods
    - Preserve `init_vision()` / `get_vision()` module-level accessors
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 11.1, 11.2_

  - [ ]* 2.2 Write unit tests for `VisionModule`
    - Test `describe_screen` returns structured dict with required keys
    - Test `verify_action` returns success/confidence dict
    - Test model routing: fast → 0.8B, detailed → 4B
    - Mock Ollama HTTP responses
    - _Requirements: 2.1, 2.2, 2.4_

- [x] 3. Model Router — Vision tiers and VRAM management
  - [x] 3.1 Extend `agent/models.py` — Add vision model routing and VRAM monitoring
    - Add `TIER_VISION_FAST` and `TIER_VISION_DETAILED` constants
    - Add `vision_complete(prompt, image_b64, tier)` method to `ModelRouter`
    - Add `check_vram()` method — query Ollama `/api/ps` for loaded models and VRAM usage
    - Add `ensure_vram_budget()` — unload 4B model if VRAM > 7.5GB via Ollama `/api/generate` with `keep_alive=0`
    - Log VRAM usage at startup and on model load/unload events
    - Implement automatic tier escalation: Local → Fast → Smart on failure
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 15.1, 15.2, 15.3, 15.4, 15.5_

  - [ ]* 3.2 Write unit tests for Model Router extensions
    - Test `vision_complete` routes to correct Ollama model based on tier
    - Test `check_vram` parses Ollama response correctly
    - Test `ensure_vram_budget` triggers unload when over 7.5GB
    - Test tier escalation chain: Local → Fast → Smart
    - _Requirements: 5.5, 5.6, 15.4_

- [x] 4. Checkpoint — Core perception and vision layer
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Action Registry — DOM/UIA primitives, remove vision-coordinate clicking
  - [x] 5.1 Modify `agent/actions.py` — Add DOM and UIA action primitives
    - Add `dom_click(css_selector)` action: use `DOMInspector.query_selector_one()` → click center of bounding rect via Clawmetheus
    - Add `dom_type(css_selector, text)` action: focus element via DOM click, then type via Clawmetheus
    - Add `uia_click(name, automation_id, control_type)` action: use `UIAModule.find_element()` → click center
    - Add `uia_type(name, automation_id, text)` action: focus via UIA click, then type
    - Add `keyboard_shortcut(keys)` action: send key combo via Clawmetheus
    - Implement fallback chain in click actions: DOM → UIA → vision-guided (with scene context only, no coordinate estimation)
    - Remove `estimate_location`-based clicking from existing actions
    - Update `ACTION_REGISTRY` dict and `get_action_catalog()` to include new primitives
    - _Requirements: 1.1, 1.2, 1.4, 2.3, 3.1, 3.3, 4.1, 4.2_

  - [ ]* 5.2 Write unit tests for new action primitives
    - Test `dom_click` calls DOMInspector and Clawmetheus with correct coordinates
    - Test `uia_click` calls UIAModule and Clawmetheus with bounding rect center
    - Test fallback chain: DOM failure → UIA → vision-guided
    - Test `keyboard_shortcut` sends correct key combo
    - _Requirements: 1.2, 1.4, 3.3, 4.2_

- [x] 6. Batch Planner — Replace per-step LLM calls
  - [x] 6.1 Modify `agent/step_executor.py` — Implement batch plan execution
    - Add `_plan_batch(task, screen_state, kb_context)` method: use Smart tier to generate `[{"action", "params"}, ...]`
    - Add `_execute_batch(plan)` method: execute actions sequentially WITHOUT LLM calls between steps
    - Add `_verify_batch(before, after, expected)` method: use VisionModule to verify cumulative result
    - Rewrite `run()` main loop: KB context → startup sequence → capture/describe → plan batch → execute → verify → replan if failed
    - Support reactive mode: single-action planning with Fast tier for dynamic environments (games, live web)
    - Read execution mode from Knowledge_Base per-application config
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 7.1, 7.2, 7.3, 7.4, 14.1, 14.2, 14.3, 14.4, 14.5_

  - [ ]* 6.2 Write unit tests for batch planner
    - Test `_plan_batch` returns list of action dicts
    - Test `_execute_batch` calls actions sequentially without LLM calls
    - Test `_verify_batch` triggers replan on failure
    - Test reactive mode uses Fast tier single-action planning
    - _Requirements: 6.1, 6.2, 6.4, 14.2, 14.5_

- [x] 7. Knowledge Base — Replayable recordings and startup sequences
  - [x] 7.1 Modify `kb/observer.py` — Add `to_skill()` method to `Recording` class
    - Add `to_skill(app_db)` method that delegates to `kb/analyzer.py` for conversion
    - Ensure recording captures sufficient context (window titles, active elements) for skill conversion
    - _Requirements: 8.1, 8.2, 8.3_

  - [x] 7.2 Modify `kb/analyzer.py` — Add `recording_to_skill()` function
    - Implement `recording_to_skill(recording, app_db)` that converts raw events + screenshots into Action_Registry primitive sequences
    - Use Smart tier LLM to interpret raw input events into structured action steps
    - Return `{"name", "app", "steps": [{"action", "params"}, ...]}`
    - Store result as a named skill in the Knowledge_Base app JSON
    - _Requirements: 8.2, 8.3, 8.4_

  - [x] 7.3 Add startup sequence support to Knowledge_Base app JSON files
    - Add `startup_sequence` field to app JSON schema: ordered list of `{"action", "params"}` dicts
    - Add `execution_mode` field: `"plan"` or `"reactive"` per app
    - Update `kb/apps/paint.json` and `kb/apps/solitaire.json` with startup sequences and execution modes
    - _Requirements: 7.1, 7.2, 7.5, 14.3_

  - [ ]* 7.4 Write unit tests for recording-to-skill conversion
    - Test `recording_to_skill` produces valid action primitive sequences
    - Test `Recording.to_skill()` delegates correctly
    - Test startup sequence loading from app JSON
    - _Requirements: 8.2, 8.3, 8.4_

- [x] 8. Checkpoint — Desktop automation agent complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Video Analysis — Frame Capturer
  - [x] 9.1 Create `video/__init__.py` and `video/frame_capturer.py`
    - Implement `FrameCapturer` class with `start(region, fps, on_frame)`, `stop()`, `running`, `actual_fps`
    - Use `mss` for region screenshot capture (consistent with existing `core/screen.py`)
    - Send each frame to VisionModule for description within 100ms of capture
    - Self-regulating FPS: if vision takes >1s, capture rate drops gracefully (never below 0.5 FPS)
    - `stop()` releases all resources within 2 seconds
    - Callback-based: `on_frame(description, timestamp, frame_png)`
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5_

  - [ ]* 9.2 Write unit tests for `FrameCapturer`
    - Test start/stop lifecycle and resource cleanup
    - Test `actual_fps` measurement
    - Test callback invocation with correct arguments
    - Mock `mss` and VisionModule for offline testing
    - _Requirements: 10.1, 10.4, 10.5_

- [x] 10. Video Analysis — Event Detector
  - [x] 10.1 Create `video/event_detector.py`
    - Implement `DetectedEvent` dataclass: `timestamp`, `frame_png`, `description`, `matched_condition`, `confidence`
    - Implement `EventDetector` class with `set_conditions()`, `process_frame()`, `get_changes()`
    - Maintain sliding window of last 30 seconds of frame descriptions (deque, maxlen based on FPS)
    - Implement cooldown: suppress duplicate events for same condition within configurable period (default 30s)
    - Condition matching: substring match + optional LLM semantic matching via 0.8B model
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5_

  - [ ]* 10.2 Write unit tests for `EventDetector`
    - Test condition matching returns `DetectedEvent` with correct fields
    - Test cooldown suppresses duplicate events within window
    - Test sliding window maintains correct time range
    - Test `get_changes` detects differences between consecutive descriptions
    - _Requirements: 12.1, 12.3, 12.4, 12.5_

- [x] 11. Video Analysis — Alert System
  - [x] 11.1 Create `video/alert_system.py`
    - Implement `AlertRecord` dataclass: `id`, `timestamp`, `condition`, `description`, `frame_b64`, `batched_conditions`
    - Implement `AlertSystem` class with `trigger(event)`, `_flush_batch()`, `_desktop_notify()`, `_play_sound()`, `get_history()`
    - Batch alerts within 10-second window into single notification
    - Desktop notifications via `win10toast` or `plyer`
    - Sound alerts via `winsound`
    - Log each alert to database with timestamp, condition, description, and frame thumbnail
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5_

  - [ ]* 11.2 Write unit tests for `AlertSystem`
    - Test alert batching within 10-second window
    - Test `get_history` returns stored alerts
    - Test single alert triggers immediate notification after batch window
    - Mock desktop notification and sound APIs
    - _Requirements: 13.1, 13.4, 13.5_

- [x] 12. Video Analysis — Commentary Stream
  - [x] 12.1 Create `video/commentary.py`
    - Implement streaming text output for real-time frame descriptions
    - Suppress duplicate consecutive descriptions, report only changes
    - Provide async iterator interface for WebSocket streaming to web UI
    - _Requirements: 11.3, 11.4, 11.5_

- [x] 13. Checkpoint — Video analysis pipeline complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 14. Database Models — Video and Skill tables
  - [x] 14.1 Modify `db/models.py` — Add VideoSession, Alert, and Skill tables
    - Add `VideoSession` model: id, region, fps, conditions, status, started_at, stopped_at
    - Add `Alert` model: id, session_id (FK), timestamp, condition, description, frame_b64, batched_conditions
    - Add `Skill` model: id, name, app, steps (JSON), created_at
    - _Requirements: 8.3, 13.3, 13.5, 16.1_

- [x] 15. API Routes — Video analysis and alert endpoints
  - [x] 15.1 Create `web/routes/video.py` — Video analysis session API
    - `POST /api/video/start` — Start capture session with region, fps, watch conditions
    - `POST /api/video/stop` — Stop active capture session
    - `GET /api/video/status` — Current session status, frame rate, processing latency
    - `GET /api/video/stream` — WebSocket endpoint for real-time commentary
    - Wire FrameCapturer → EventDetector → AlertSystem → Commentary pipeline
    - _Requirements: 16.1, 16.2, 16.6_

  - [x] 15.2 Create `web/routes/alerts.py` — Alert history API
    - `GET /api/alerts` — Alert history with timestamps, descriptions, frame thumbnails
    - `GET /api/alerts/{alert_id}` — Single alert detail with full frame image
    - _Requirements: 13.5, 16.3_

  - [x] 15.3 Modify `web/server.py` — Mount new routes
    - Import and mount `web.routes.video` and `web.routes.alerts` routers
    - Initialize VisionModule, FrameCapturer, EventDetector, AlertSystem in app startup
    - Store video analysis components in `app.state`
    - _Requirements: 16.1, 16.2, 16.3_

- [x] 16. Config — Add new configuration sections
  - [x] 16.1 Modify `config.yaml` — Add video analysis, UIA, and DOM inspector config
    - Add `dom_inspector` section: `port: 9222`
    - Add `uia` section: `timeout: 5`, `cache_ttl: 30`
    - Add `video` section: `default_fps: 1.0`, `cooldown_secs: 30`, `batch_window_secs: 10`
    - Add `vision` section updates: `fast_model: qwen3.5:0.8b`, `detailed_model: qwen3.5:4b`, `provider: ollama`
    - Add `vram` section: `budget_gb: 8.0`, `warning_gb: 7.5`
    - _Requirements: 15.1, 15.4, 15.5_

- [x] 17. Final Checkpoint — Full integration
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation after each major system
- The design uses Python throughout — all implementations target the existing Python codebase
- Desktop automation tasks (1-8) should be completed before video analysis tasks (9-13) due to shared VisionModule dependency