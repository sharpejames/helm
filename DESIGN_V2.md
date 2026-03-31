# Helm V2 — Design Document

## Vision
An AI agent that can do anything on a computer that a human can, using keyboard and mouse, plus real-time video analysis for monitoring and commentary.

## Two Core Systems

### System 1: Desktop Automation Agent
**Goal:** Reliably use any app or website on the machine.

**Architecture:**
- **Vision (Qwen3.5:0.8B via Ollama):** Understanding what's on screen. NOT for clicking coordinates.
- **DevTools/DOM (for websites):** CSS selectors, DOM inspection for precise element interaction. This is how web clicking works — no vision coordinate guessing.
- **Keyboard shortcuts:** Preferred over mouse clicks whenever possible. Faster, more reliable.
- **Mouse clicks:** Only when keyboard shortcuts aren't available. Use DOM coordinates for web, UIA for desktop apps.
- **LLM (Claude via Kiro):** Decision making — what to do next. NOT step-by-step (too slow). Instead: plan a batch of actions, execute them, check result.

**Key Principles:**
1. Vision for UNDERSTANDING, DOM/UIA for CLICKING
2. Keyboard shortcuts first, mouse clicks second
3. Batch actions for speed, check results periodically
4. Training recordings create replayable action sequences (not just tips)
5. Knowledge base drives deterministic startup sequences per app

**Web Interaction Flow:**
1. Navigate to URL (deterministic)
2. Open DevTools, inspect DOM to find elements
3. Get element coordinates from DOM (precise, no vision needed)
4. Click using DOM coordinates
5. Vision to verify the result

**Desktop App Flow:**
1. Launch app (startup_sequence from knowledge base)
2. Use keyboard shortcuts for common actions
3. Vision to understand current state
4. UIA (UI Automation) to find and click elements when needed
5. Vision to verify results

### System 2: Real-Time Video Analysis
**Goal:** Watch streaming video and provide real-time descriptions, alerts, and actions.

**Architecture:**
- **Frame capture:** Continuous screenshot capture at 1-2 FPS from a specific screen region or browser tab
- **Vision model (Qwen3.5:0.8B):** Process each frame, generate description. <1s per frame on GPU.
- **Event detection:** Compare descriptions across frames to detect changes, specific objects, events
- **Alert system:** Trigger notifications, sounds, or actions when specific conditions are met
- **Commentary mode:** Stream continuous text descriptions of what's happening

**Use Cases:**
- Ring camera: "Alert me if you see a coyote/person/package"
- Sports: Real-time play-by-play commentary
- Debates: Fact-checking claims as they're made
- General: "Describe what's happening in this video"

**Architecture:**
```
Screen/Browser → Frame Capture (1-2 FPS) → Qwen3.5:0.8B → Description
                                                          → Event Detector → Alerts/Actions
                                                          → Commentary Stream → Overlay/UI
```

## Model Stack
| Model | Size | Purpose | Speed |
|-------|------|---------|-------|
| Qwen3.5:0.8B | ~1GB | Real-time vision, frame analysis | <1s/frame |
| Qwen3.5:4b | ~3.4GB | Complex vision, detailed analysis | ~5-10s |
| Claude Haiku | Cloud | Fast decisions, routine actions | ~1-2s |
| Claude Opus | Cloud | Complex planning, error recovery | ~6-10s |

Both 0.8B and 4B fit in 8GB VRAM together (~4.4GB total).

## Implementation Priority
1. Fix web interaction (DevTools approach) — immediate
2. Real-time frame capture + Qwen3.5:0.8B analysis — next
3. Event detection and alerting — after that
4. Commentary/streaming mode — after that

## Hardware
- RTX 4070 Laptop (8GB VRAM)
- 32GB RAM
- 2560x1600 screen
- Windows 11

## Current State
- Helm server: FastAPI on port 8765
- Clawmetheus: HTTP server on port 7331 (screenshots, mouse/keyboard, active window)
- Ollama: port 11434 (Qwen3.5:4b and 0.8b)
- Kiro proxy: port 8000 (Claude models)
- Vision model in clawmetheus: DISABLED (freed VRAM)

## Git Repos
- Helm: github.com/sharpejames/helm
- Clawmetheus: github.com/sharpejames/clawmetheus
