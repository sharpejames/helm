# Helm — Technical Design v0.1

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                     Web Interface                        │
│         (Chat UI + Task Manager + Artifact Viewer)       │
└────────────────────────┬────────────────────────────────┘
                         │ HTTP / WebSocket
┌────────────────────────▼────────────────────────────────┐
│                   FastAPI Server                         │
│         /chat  /tasks  /artifacts  /settings             │
└──────┬─────────────────┬───────────────────┬────────────┘
       │                 │                   │
┌──────▼──────┐  ┌───────▼──────┐  ┌────────▼───────┐
│   Agent     │  │ Task Manager │  │   DB (SQLite)  │
│  Planner +  │  │ + Scheduler  │  │  tasks/runs/   │
│  Executor   │  │ (APScheduler)│  │  artifacts     │
└──────┬──────┘  └──────────────┘  └────────────────┘
       │
┌──────▼──────────────────────────────────────────────────┐
│                  Action Pipeline                         │
│  PRE_CAPTURE → STATE_CHECK → BLOCKER_CHECK →            │
│  LOCATE_TARGET → VERIFY_POSITION → EXECUTE →            │
│  POST_CAPTURE → VERIFY_RESULT → ARTIFACT_CAPTURE        │
└──────┬──────────────────────────────────────────────────┘
       │
┌──────▼──────┐  ┌──────────────┐  ┌──────────────────┐
│  core/      │  │  core/       │  │  core/           │
│  screen.py  │  │  input.py    │  │  vision.py       │
│  (mss)      │  │  (pyautogui) │  │  (Gemini Flash)  │
└─────────────┘  └──────────────┘  └──────────────────┘
```

---

## The Action Pipeline (Non-Negotiable)

Every action — click, type, drag, shortcut, scroll — goes through this exact sequence. There are no raw input calls outside this pipeline.

```
Step 1: PRE_CAPTURE
  → Take screenshot
  → Record cursor position

Step 2: STATE_CHECK
  → Vision: "What application/window is currently in focus?"
  → Vision: "What is the current state of the UI?"
  → If wrong window: focus correct window, restart from Step 1

Step 3: BLOCKER_CHECK
  → Vision: "Is there any modal, dialog, popup, or overlay blocking the UI?"
  → If blocker detected: handle it (close/dismiss/accept), restart from Step 1
  → Max 3 blocker-handling attempts before raising BlockerError

Step 4: LOCATE_TARGET (if action has a target element)
  → See Locate Loop below
  → Returns verified (x, y) coordinates

Step 5: PRE_ACTION_VERIFY
  → Vision: "Is the cursor positioned over [target]? Is it safe to proceed?"
  → If no: retry locate loop

Step 6: EXECUTE
  → Perform the raw input action (click/type/drag/key)
  → Record action in run log

Step 7: POST_CAPTURE
  → Take screenshot immediately after action
  → Wait for UI to settle (configurable delay, default 500ms)
  → Take second screenshot

Step 8: VERIFY_RESULT
  → Vision: "Did [expected_result] happen? Compare before/after."
  → If failed: increment retry counter, restart from Step 1
  → Max retries: configurable (default 3)

Step 9: ARTIFACT_CAPTURE
  → Save post-action screenshot
  → Extract any URLs, file paths, or text output
  → Store artifact in DB linked to current task run
```

---

## The Locate Loop

Used in Step 4 to find any UI element by description.

```python
MAX_ATTEMPTS = 8
INITIAL_DAMPING = 1.0
DAMPING_FACTOR = 0.75  # each iteration, adjustments get smaller

async def locate(target_description: str) -> Coords:
    screenshot = capture()
    estimate = vision.estimate_location(screenshot, target_description)
    move_to(estimate)

    for attempt in range(MAX_ATTEMPTS):
        screenshot = capture()
        cursor = get_cursor_pos()
        
        result = vision.check_cursor(
            screenshot=screenshot,
            cursor=cursor,
            target=target_description
        )
        # result: { on_target: bool, dx: int, dy: int, confidence: float }
        
        if result.on_target and result.confidence > 0.8:
            return cursor
        
        damping = INITIAL_DAMPING * (DAMPING_FACTOR ** attempt)
        new_x = cursor.x + int(result.dx * damping)
        new_y = cursor.y + int(result.dy * damping)
        move_to(Coords(new_x, new_y))
    
    raise TargetNotFoundError(target_description)
```

Vision prompt for cursor check:
```
Screenshot attached. The cursor is at pixel ({x}, {y}).
Target: "{target_description}"

Answer in JSON:
{
  "on_target": true/false,
  "confidence": 0.0-1.0,
  "dx": pixels to move right (negative = left),
  "dy": pixels to move down (negative = up),
  "notes": "brief explanation"
}
```

---

## Agent Planning Loop

When a user sends a task, the planner breaks it into steps, the executor runs each step through the pipeline.

```
User: "Post on Reddit r/ScreenDetox: [title] [body]"

Planner output:
  1. { action: "navigate", target: "browser", url: "https://reddit.com/r/ScreenDetox/submit" }
  2. { action: "click", target: "title input field" }
  3. { action: "type", text: "[title]" }
  4. { action: "click", target: "body text area" }
  5. { action: "type", text: "[body]" }
  6. { action: "click", target: "Post button" }
  7. { action: "extract", target: "current URL", artifact_type: "url" }

Executor: runs each step through pipeline, captures artifacts
Result: { success: true, artifacts: [{ type: "url", value: "https://reddit.com/..." }] }
```

If a step fails, the executor re-queries the planner with current screen state for replanning.

---

## Data Models

### Task
```sql
CREATE TABLE tasks (
  id          TEXT PRIMARY KEY,  -- UUID
  name        TEXT NOT NULL,
  description TEXT,
  type        TEXT NOT NULL,     -- 'manual' | 'scheduled' | 'interval'
  schedule    TEXT,              -- cron expression or interval spec
  enabled     BOOLEAN DEFAULT 1,
  created_at  DATETIME,
  updated_at  DATETIME
);
```

### TaskRun
```sql
CREATE TABLE task_runs (
  id          TEXT PRIMARY KEY,
  task_id     TEXT REFERENCES tasks(id),
  status      TEXT NOT NULL,     -- 'pending' | 'running' | 'completed' | 'failed'
  input       TEXT,              -- JSON: user message or scheduled input
  output      TEXT,              -- agent's final response text
  error       TEXT,              -- error message if failed
  started_at  DATETIME,
  finished_at DATETIME
);
```

### Artifact
```sql
CREATE TABLE artifacts (
  id          TEXT PRIMARY KEY,
  run_id      TEXT REFERENCES task_runs(id),
  type        TEXT NOT NULL,     -- 'screenshot' | 'url' | 'file' | 'text'
  value       TEXT NOT NULL,     -- URL string, file path, text, or base64
  label       TEXT,              -- human-readable label
  step        INTEGER,           -- which pipeline step produced this
  created_at  DATETIME
);
```

---

## API Routes

### Chat
```
POST /api/chat
  Body: { message: string, stream: bool }
  Response: SSE stream of { type: "token"|"artifact"|"done", data: ... }

GET /api/chat/history
  Response: [{ run_id, message, response, artifacts, timestamp }]
```

### Tasks
```
GET    /api/tasks              → list all tasks
POST   /api/tasks              → create task
GET    /api/tasks/:id          → get task + runs
PUT    /api/tasks/:id          → update task
DELETE /api/tasks/:id          → delete task
POST   /api/tasks/:id/run      → trigger manual run
PUT    /api/tasks/:id/toggle   → enable/disable
```

### Runs & Artifacts
```
GET /api/runs/:id              → get run details + artifacts
GET /api/runs/:id/artifacts    → list artifacts for run
GET /api/artifacts/:id         → get artifact (image served as image/png)
```

### Settings
```
GET  /api/settings             → current config (keys redacted)
POST /api/settings             → update config
GET  /api/settings/test        → test model connections
```

---

## Configuration (config.yaml)

```yaml
server:
  host: 0.0.0.0
  port: 8765
  secret_key: ""  # auto-generated on first run

llm:
  provider: kiro          # kiro | anthropic | openai-compatible
  base_url: http://localhost:8000   # for kiro proxy
  api_key: ""
  model: kiro/claude-sonnet-4.6
  max_tokens: 4096

vision:
  provider: gemini
  api_key: ""
  model: gemini-2.5-flash

pipeline:
  max_retries: 3
  locate_max_attempts: 8
  locate_damping: 0.75
  settle_delay_ms: 500
  blocker_max_attempts: 3

scheduler:
  timezone: America/Los_Angeles

db:
  path: ./helm.db
```

---

## Directory Structure

```
helm/
├── SPEC.md
├── DESIGN.md
├── README.md
├── requirements.txt
├── config.yaml              ← default config (user copies + edits)
├── start.sh                 ← Mac/Linux startup
├── start.bat                ← Windows startup
├── main.py                  ← entry point
│
├── core/
│   ├── screen.py            ← screenshot capture, cursor position (mss)
│   ├── input.py             ← mouse + keyboard (pyautogui)
│   └── vision.py            ← vision model interface (Gemini)
│
├── pipeline/
│   ├── action.py            ← BaseAction + ActionPipeline (THE STRICT LOOP)
│   ├── locate.py            ← iterative coordinate finding loop
│   └── blockers.py          ← modal/popup detection + handling
│
├── actions/
│   ├── click.py             ← ClickAction
│   ├── type_text.py         ← TypeAction
│   ├── drag.py              ← DragAction
│   ├── shortcut.py          ← ShortcutAction
│   ├── scroll.py            ← ScrollAction
│   └── navigate.py          ← NavigateAction (open URL in browser)
│
├── agent/
│   ├── models.py            ← LLM client (Kiro/Anthropic/OpenAI-compat)
│   ├── planner.py           ← task → steps (Claude)
│   ├── executor.py          ← runs steps through pipeline
│   └── prompts.py           ← system prompts
│
├── tasks/
│   ├── manager.py           ← CRUD for tasks + runs
│   ├── scheduler.py         ← APScheduler wrapper
│   └── artifacts.py         ← artifact storage + retrieval
│
├── db/
│   ├── database.py          ← SQLite connection + init
│   └── models.py            ← SQLAlchemy models
│
└── web/
    ├── server.py            ← FastAPI app
    ├── routes/
    │   ├── chat.py
    │   ├── tasks.py
    │   └── settings.py
    └── static/
        ├── index.html       ← single-page app
        ├── app.js
        └── style.css
```

---

## Model Integration

### Kiro Proxy (James's setup)
```python
import anthropic
client = anthropic.Anthropic(
    base_url="http://localhost:8000",
    api_key="skippy-kiro-local-2026"
)
```

### Direct Anthropic
```python
client = anthropic.Anthropic(api_key="sk-ant-...")
```

### OpenAI-compatible (any provider)
```python
from openai import OpenAI
client = OpenAI(base_url="...", api_key="...")
```

### Gemini Vision
```python
import google.generativeai as genai
genai.configure(api_key="...")
model = genai.GenerativeModel("gemini-2.5-flash")
```

---

## OpenClaw Integration

Helm exposes a skill-compatible HTTP endpoint:

```
POST /openclaw/execute
  Body: { task: string, context: {} }
  Response: { success: bool, output: string, artifacts: [...] }
```

OpenClaw SKILL.md points to this endpoint. Helm runs as a sidecar service.

---

## Cross-Platform Notes

| Feature | Windows | macOS |
|---------|---------|-------|
| Screenshots | mss | mss |
| Mouse/keyboard | pyautogui | pyautogui |
| Window focus | win32gui | AppKit/subprocess |
| Autostart | Task Scheduler | launchd plist |
| Config path | %APPDATA%\helm | ~/Library/Application Support/helm |

---

## Security

- Web UI binds to localhost by default (not exposed to network)
- API keys stored in config.yaml (user-managed, gitignored)
- No telemetry, no cloud sync
- Screenshots stored locally in SQLite (base64) or filesystem
- Optional: password-protect the web UI (config: `server.password`)
