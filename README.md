# Helm

AI desktop operator that controls any app via mouse and keyboard. Give it a task in plain English, and it writes and executes Python automation scripts using [Clawmetheus](https://github.com/sharpejames/clawmetheus) as its hands and eyes.

## What It Does

- **Controls any desktop app** — Paint, browsers, Office, games, anything with a GUI
- **Writes its own scripts** — LLM generates Python automation code for each task
- **Self-correcting** — verifies results with vision, retries with fixes on failure
- **Web-aware** — inspects DOM elements for precise web interaction
- **Learns from experience** — knowledge base of proven scripts and app-specific tips
- **Web UI** — real-time task monitoring at http://localhost:8765

## Example Tasks

- "Draw a colorful rubber duck in Paint, save it, and upload it to Grok"
- "Open Chrome, go to gmail.com, compose an email to [email] about [topic]"
- "Fill out the contact form on example.com with test data"
- "Open Notepad, write a haiku, save it to Desktop"

## Requirements

- **Python 3.11+**
- **Windows 10/11** (macOS partial support)
- **[Clawmetheus](https://github.com/sharpejames/clawmetheus)** running at `http://127.0.0.1:7331`
- **LLM API access** — one of:
  - [Kiro Gateway](https://github.com/sharpejames/kiro-gateway) (free with Kiro subscription)
  - Anthropic API key (Claude)
  - OpenAI API key
- **Gemini API key** (for vision — get one at [aistudio.google.com](https://aistudio.google.com))

## Quick Start

### 1. Start Clawmetheus first

```bash
git clone https://github.com/sharpejames/clawmetheus.git
cd clawmetheus
pip install -r requirements.txt
# Create .env with GEMINI_API_KEY=your_key
.\start.ps1
```

### 2. Clone and install Helm

```bash
git clone https://github.com/sharpejames/helm.git
cd helm
pip install -r requirements.txt
```

### 3. Configure

Copy the example config and edit it:

```bash
cp config.yaml.example config.yaml
```

Edit `config.yaml`:

```yaml
server:
  host: 0.0.0.0
  port: 8765

llm:
  # Option A: Kiro Gateway (free with Kiro subscription)
  provider: kiro
  base_url: http://localhost:8000
  api_key: your-kiro-proxy-key
  model: claude-sonnet-4.6

  # Option B: Anthropic direct
  # provider: anthropic
  # api_key: sk-ant-...
  # model: claude-sonnet-4-20250514

  # Option C: OpenAI
  # provider: openai
  # api_key: sk-...
  # model: gpt-4o

vision:
  provider: gemini
  api_key: your-gemini-api-key
  model: gemini-2.5-flash

clawmetheus:
  path: /path/to/clawmetheus    # Where you cloned it
  url: http://127.0.0.1:7331    # Clawmetheus server URL
```

### 4. Start Helm

```bash
python main.py
```

Or on Windows:

```batch
start.bat
```

### 5. Open the Web UI

Go to http://localhost:8765 and type a task.

## Using Kiro Gateway (Optional — Free LLM Access)

If you have a [Kiro IDE](https://kiro.dev) subscription, you can use your existing account for LLM access instead of paying for API keys:

1. Clone and set up [kiro-gateway](https://github.com/sharpejames/kiro-gateway)
2. Sign in to Kiro IDE (this creates auth tokens)
3. Start the gateway: `python main.py` (runs at http://localhost:8000)
4. Set `llm.provider: kiro` and `llm.base_url: http://localhost:8000` in config.yaml

Available models via Kiro: `claude-sonnet-4.6`, `claude-opus-4.6`, `claude-haiku-4.5`

## Architecture

```
Web UI (localhost:8765)
    │
    ▼
Helm Server (FastAPI)
    ├── Script Executor — generates + runs Python scripts
    │   ├── LLM (Claude/GPT) — writes automation code
    │   ├── Syntax checker — catches errors before execution
    │   ├── Flood fill guard — strips dangerous Paint operations
    │   └── Retry loop — fixes and re-runs on failure (up to 3 attempts)
    │
    ├── Knowledge Base — proven scripts + app-specific tips
    │
    ├── Output Verification
    │   ├── Code-based checks (file size, output markers)
    │   ├── Vision verification (screenshot → Gemini → PASS/FAIL)
    │   └── validate_image() / verify_result()
    │
    └── Clawmetheus (http://127.0.0.1:7331)
        ├── Mouse & Keyboard
        ├── Screenshots & Vision
        ├── DOM Inspection
        └── UI Discovery
```

## How It Works

1. You describe a task in plain English
2. Helm takes a screenshot to understand the current screen state
3. The LLM writes a complete Python script using `task_runner.py` functions
4. The script is checked for syntax errors and dangerous patterns (e.g., flood fill)
5. The script executes against Clawmetheus (mouse clicks, keyboard input, etc.)
6. After execution, Helm verifies the result with vision
7. If something failed, the LLM writes a fix script and retries (up to 3 attempts)
8. Successful scripts are saved to the knowledge base for future reference

## Project Structure

```
helm/
├── main.py              # Entry point
├── config.yaml          # Configuration
├── agent/
│   ├── script_executor.py  # Core: generates + runs scripts
│   ├── prompts.py          # LLM system prompts
│   ├── models.py           # LLM client abstraction
│   └── executor.py         # Planner-based pipeline (alternative)
├── core/
│   ├── input.py            # Input abstraction
│   ├── screen.py           # Screen capture
│   └── vision.py           # Vision model client
├── kb/
│   ├── __init__.py         # Knowledge base (script search + storage)
│   └── apps/               # App-specific knowledge (paint.json, etc.)
├── web/
│   ├── server.py           # FastAPI web server
│   ├── routes/             # API routes (tasks, chat, runs, settings)
│   └── static/index.html   # Web UI
└── db/
    ├── database.py         # SQLite database
    └── models.py           # Data models
```

## API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Web UI |
| `/api/task` | POST | Submit a task `{"task": "draw a cat in Paint"}` |
| `/api/task/stop` | POST | Stop the current task |
| `/api/runs` | GET | List recent runs |
| `/api/runs/last` | GET | Get the last run |
| `/api/logs` | GET | List log files |
| `/api/logs/last` | GET | Get the last log |
| `/ws` | WebSocket | Real-time task progress stream |

## Troubleshooting

**"Clawmetheus not running"** — Start Clawmetheus first: `cd clawmetheus && .\start.ps1`

**"LLM error"** — Check your config.yaml API keys. If using Kiro Gateway, make sure it's running.

**Scripts timing out** — The default timeout is 300 seconds. Complex tasks (draw + upload) may need simpler drawings.

**Drawing looks wrong** — Helm verifies drawings with vision and retries. If it keeps failing, the canvas bounds detection may be off — try maximizing Paint manually first.

**Web interaction failing** — Helm uses DOM inspection to find elements. If DevTools mode is being used, make sure no other DevTools windows are open.

## License

MIT
