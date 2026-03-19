# Helm — AI Desktop Operator
## Product Specification v0.1

> Working name: Helm. An AI agent that operates your computer using a keyboard and mouse, exactly as a human would.

---

## Vision

Helm is an open-source AI agent that controls your desktop. You tell it what to do in plain English — it figures out the steps, executes them visually (mouse + keyboard), and shows you what it did. No APIs, no integrations, no special app support required. If a human can do it on a computer, Helm can do it.

---

## Core Principles

1. **Don't trust, verify.** Every action is preceded and followed by a screenshot. State is never assumed.
2. **Visual-first.** Helm navigates by sight, not by DOM or API. It finds buttons the same way you do.
3. **Strict pipeline.** Every action goes through the same hardcoded verification loop. No shortcuts.
4. **Artifacts over promises.** Every task produces evidence: screenshots, URLs, file paths, output text.
5. **Model-agnostic.** Works with Anthropic Claude (direct or via Kiro proxy), Gemini, or local models.
6. **Cross-platform.** Windows and macOS. Linux best-effort.

---

## Target Users

- Developers who want an AI agent that can actually use their computer
- Power users who want to automate repetitive GUI tasks
- Teams using OpenClaw who want desktop control capabilities
- Anyone who wants a self-hosted, open-source alternative to cloud computer-use agents

---

## Features

### Core
- [x] Natural language task input ("post this on Reddit", "fill out this form", "open Figma and export the logo")
- [x] Strict action pipeline with mandatory pre/post verification
- [x] Iterative coordinate finding (move → screenshot → adjust → repeat)
- [x] Modal/popup detection and automatic handling
- [x] Artifact capture per task (screenshots, URLs, file paths, console output)
- [x] Cross-platform: Windows + macOS

### Web Interface
- [x] Chat interface for manual tasks (real-time streaming)
- [x] Task history (manual + scheduled)
- [x] Artifact viewer per task
- [x] Scheduled task manager (create, edit, delete, enable/disable)
- [x] Settings (model config, Kiro proxy, Gemini key)

### Task Scheduling
- [x] One-shot tasks (run once at a time)
- [x] Repeating tasks (cron expression)
- [x] Interval tasks (every N minutes/hours)
- [x] Task history with full artifact trail

### Model Support
- [x] Anthropic Claude (direct API key)
- [x] Kiro proxy (Anthropic-compatible, local proxy for Kiro subscribers)
- [x] Google Gemini (vision tasks)
- [x] OpenAI-compatible endpoints (any provider)
- [ ] Local models via Ollama (future)

### OpenClaw Integration
- [x] Exposes HTTP API compatible with OpenClaw skill interface
- [x] Can be called as a skill from OpenClaw sessions
- [x] Runs independently without OpenClaw

---

## User Stories

### Manual Tasks
- "Go to reddit.com/r/ScreenDetox and post: [title] [body]" → agent opens browser, navigates, fills form, submits, returns URL
- "Open Spotify and play my Discover Weekly" → agent opens app, finds playlist, clicks play
- "Take a screenshot of my current screen and save it to Desktop" → done, returns file path

### Scheduled Tasks
- "Every Monday at 9am, check my email and summarize unread messages" → creates recurring task, runs on schedule, stores summaries as artifacts
- "Post to Twitter every day at 10am: [rotating content]" → scheduled, runs, returns tweet URLs

### Artifact Examples
- Reddit post → artifact: `{ type: "url", value: "https://reddit.com/r/ScreenDetox/comments/..." }`
- File created → artifact: `{ type: "file", value: "/Users/james/Desktop/screenshot.png" }`
- Form filled → artifact: `{ type: "screenshot", value: "<base64>" }`
- Command run → artifact: `{ type: "text", value: "npm install completed successfully" }`

---

## Non-Goals (v0.1)

- Voice input
- Multi-monitor support (single primary monitor only)
- Mobile device control
- Browser extension (uses visual control only)
- Multi-agent coordination
- Cloud hosting / SaaS

---

## Success Metrics

- Can complete a Reddit post end-to-end without human intervention
- Can fill a web form reliably (>90% success rate)
- Coordinate finding converges within 5 iterations for standard UI elements
- Task artifacts are always captured, even on failure
- Runs on a fresh Windows/Mac install with `pip install` + config

---

## Versioning

- **v0.1** — Core pipeline, web UI, manual tasks, basic scheduling, Windows-first
- **v0.2** — macOS parity, improved vision, local model support
- **v0.3** — Multi-monitor, richer artifact types, task templates
