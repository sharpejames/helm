"""
kb/analyzer.py — Analyzes recordings to extract reusable knowledge.

Takes a Recording (from observer.py) and uses the LLM to:
  1. Identify what app/website was being used
  2. Extract the action sequence (what the human did)
  3. Identify UI patterns, shortcuts, and tips
  4. Save structured knowledge to the app DB
"""

import json
import logging
import time

from kb.observer import Recording
from kb.apps import AppDB

logger = logging.getLogger(__name__)


def analyze_recording(recording: Recording, app_db: AppDB = None) -> dict:
    """Analyze a recording and extract knowledge using the LLM.
    
    Returns: {"app": "Google Maps", "actions": [...], "tips": [...], "knowledge_saved": True}
    """
    if app_db is None:
        app_db = AppDB()

    from agent.models import get_router, TIER_SMART
    router = get_router()
    if not router:
        return {"error": "No LLM router available"}

    # Build a summary of the recording for the LLM
    events_summary = _summarize_events(recording)
    screenshot_descriptions = _describe_screenshots(recording)
    transcript = recording._build_transcript()

    prompt = f"""Analyze this recording of a human using a computer. Extract reusable knowledge.

RECORDING CONTEXT: {recording.prompt}
DURATION: {recording.duration_secs():.0f} seconds

{f"USER NARRATION (voice transcript — the user described what they were doing):{chr(10)}{transcript}" if transcript else ""}

EVENTS ({len(recording.events)} total):
{events_summary}

SCREENSHOTS (what was visible at key moments):
{screenshot_descriptions}

WINDOWS USED: {', '.join(set(recording.active_windows)) if recording.active_windows else 'unknown'}

Please analyze and return a JSON object:
{{
  "app_name": "Name of the primary app/website used",
  "task_description": "What the human was doing in 1-2 sentences",
  "action_sequence": [
    "Step 1: description of what they did",
    "Step 2: ...",
    ...
  ],
  "shortcuts_observed": {{"action": "keys"}},
  "tips": ["Useful tips for automating this task"],
  "known_issues": ["Any problems or workarounds observed"],
  "ui_elements": ["Important UI elements and their approximate locations"]
}}

Return ONLY the JSON object."""

    try:
        raw = router.complete(
            "You analyze recordings of human computer usage and extract structured knowledge.",
            [{"role": "user", "content": prompt}],
            tier=TIER_SMART,
            max_tokens=2048,
            timeout=60.0,
        )

        # Parse response
        import re
        raw = re.sub(r'```(?:json)?\s*', '', raw).strip().rstrip('`').strip()
        result = json.loads(raw)

        # Save to app DB
        app_name = result.get("app_name", "Unknown")
        if app_name and app_name != "Unknown":
            for tip in result.get("tips", []):
                app_db.add_tip(app_name, tip)
            for issue in result.get("known_issues", []):
                app_db.add_issue(app_name, issue)
            for action, keys in result.get("shortcuts_observed", {}).items():
                app_db.add_shortcut(app_name, action, keys)

            # Save the action sequence as a strategy
            if result.get("action_sequence"):
                sequence_tip = "Learned sequence: " + " → ".join(
                    s.split(": ", 1)[-1][:50] for s in result["action_sequence"][:8])
                app_db.add_tip(app_name, sequence_tip)

            result["knowledge_saved"] = True
            logger.info(f"Analyzed recording: {app_name}, {len(result.get('tips', []))} tips, "
                         f"{len(result.get('action_sequence', []))} steps")
        else:
            result["knowledge_saved"] = False

        return result

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse LLM analysis: {e}")
        return {"error": f"Parse error: {e}", "raw": raw[:500] if 'raw' in dir() else ""}
    except Exception as e:
        logger.error(f"Analysis failed: {e}")
        return {"error": str(e)}


def _summarize_events(recording: Recording, max_events: int = 100) -> str:
    """Create a compact summary of input events for the LLM."""
    if not recording.events:
        return "(no events recorded)"

    lines = []
    t0 = recording.started
    prev_ts = t0

    for e in recording.events[:max_events]:
        elapsed = e.ts - t0
        gap = e.ts - prev_ts

        if e.type == "click":
            lines.append(f"  [{elapsed:.1f}s] CLICK ({e.data.get('x')}, {e.data.get('y')}) {e.data.get('button', '')}")
        elif e.type == "key":
            key = e.data.get("key", "?")
            # Group rapid keystrokes into typed text
            lines.append(f"  [{elapsed:.1f}s] KEY {key}")
        elif e.type == "window_change":
            lines.append(f"  [{elapsed:.1f}s] WINDOW → {e.data.get('window', '?')}")
        elif e.type == "scroll":
            lines.append(f"  [{elapsed:.1f}s] SCROLL {e.data.get('direction', '?')}")
        elif e.type == "speech":
            lines.append(f"  [{elapsed:.1f}s] 🎤 \"{e.data.get('text', '')}\"")

        prev_ts = e.ts

    if len(recording.events) > max_events:
        lines.append(f"  ... ({len(recording.events) - max_events} more events)")

    return "\n".join(lines)


def _describe_screenshots(recording: Recording, max_screenshots: int = 5) -> str:
    """Use vision to describe key screenshots from the recording."""
    if not recording.screenshots:
        return "(no screenshots)"

    # Pick evenly spaced screenshots
    total = len(recording.screenshots)
    if total <= max_screenshots:
        selected = recording.screenshots
    else:
        indices = [int(i * (total - 1) / (max_screenshots - 1)) for i in range(max_screenshots)]
        selected = [recording.screenshots[i] for i in indices]

    lines = []
    t0 = recording.started

    for ss in selected:
        elapsed = ss.ts - t0
        # Use vision model to describe the screenshot
        try:
            from agent.actions import _ask_screen
            import requests
            # We can't use _ask_screen directly since it takes a live screenshot
            # Instead, send the recorded screenshot to the vision API
            CLAWMETHEUS_URL = "http://127.0.0.1:7331"
            r = requests.post(f"{CLAWMETHEUS_URL}/vision/ask", json={
                "image": ss.image_b64,
                "question": "Briefly describe what's on screen. What app/website is visible? What is the user doing?"
            }, timeout=10)
            desc = r.json().get("answer", "unknown")
        except Exception:
            desc = f"Window: {ss.window}"

        lines.append(f"  [{elapsed:.1f}s] {desc[:150]}")

    return "\n".join(lines)


# ── Action Registry primitives (for LLM prompt context) ─────────────────────

_ACTION_PRIMITIVES = [
    "open_app(app_exe, window_title, wait_secs)",
    "focus_app(window_title)",
    "close_app(window_title)",
    "open_website(url)",
    "click_web_element(css_selector, description)",
    "type_in_web(text, css_selector, description)",
    "upload_file(filepath, attach_description)",
    "scroll_page(direction, amount)",
    "setup_paint()",
    "paint_color(color_name)",
    "paint_pencil()",
    "paint_fill_tool()",
    "paint_fill_at(x, y)",
    "paint_draw(points)",
    "paint_shape_tool(shape_name)",
    "paint_draw_shape(x1, y1, x2, y2)",
    "paint_fill_style(style)",
    "paint_outline_style(style)",
    "save_file()",
    "click(x, y)",
    "double_click(x, y)",
    "drag(x1, y1, x2, y2)",
    "type_text(text)",
    "type_keys(text)",
    "press_key(keys)",
    "keyboard_shortcut(keys)",
    "dom_click(css_selector)",
    "dom_type(css_selector, text)",
    "uia_click(name, automation_id, control_type)",
    "uia_type(name, automation_id, text)",
    "wait(seconds)",
    "dismiss_popup()",
]


def recording_to_skill(recording: Recording, app_db: AppDB) -> dict:
    """Convert a recording into a replayable skill (sequence of Action_Registry primitives).

    Uses Smart tier LLM to interpret raw input events + screenshots into
    structured action steps that can be replayed via the Action_Registry.

    Args:
        recording: A Recording from kb/observer.py with events and screenshots.
        app_db: AppDB instance for storing the resulting skill.

    Returns:
        {"name": str, "app": str, "steps": [{"action": str, "params": dict}, ...]}
    """
    from agent.models import get_router, TIER_SMART
    import re

    router = get_router()
    if not router:
        return {"error": "No LLM router available", "name": "", "app": "", "steps": []}

    # 1. Build event summary (reuse existing helper)
    events_summary = _summarize_events(recording)
    screenshot_descriptions = _describe_screenshots(recording)
    transcript = recording._build_transcript()

    # Determine windows used
    windows_used = ", ".join(set(recording.active_windows)) if recording.active_windows else "unknown"

    # Build the action primitives list for the prompt
    primitives_list = "\n".join(f"  - {p}" for p in _ACTION_PRIMITIVES)

    # 2. Send to Smart tier LLM
    prompt = f"""Convert this recording of human desktop activity into a replayable skill — a sequence of Action_Registry primitives.

RECORDING CONTEXT: {recording.prompt}
DURATION: {recording.duration_secs():.0f} seconds
WINDOWS USED: {windows_used}

{f"USER NARRATION:{chr(10)}{transcript}" if transcript else ""}

RAW EVENTS ({len(recording.events)} total):
{events_summary}

SCREENSHOTS (what was visible at key moments):
{screenshot_descriptions}

AVAILABLE ACTION PRIMITIVES:
{primitives_list}

Convert the raw events into a sequence of these primitives. Group related low-level events into single high-level actions. For example:
- Multiple rapid key presses → type_text("hello world") or press_key(["ctrl", "s"])
- Click at a known UI element location → uia_click(name="Pencil") or dom_click(css_selector)
- Mouse drag sequences → drag(x1, y1, x2, y2) or paint_draw(points)

Return a JSON object:
{{
  "name": "short_skill_name (snake_case, e.g. draw_red_circle)",
  "app": "Primary application name (e.g. Paint, Chrome, Solitaire)",
  "steps": [
    {{"action": "action_name", "params": {{"param1": "value1"}}}},
    {{"action": "action_name", "params": {{"param1": "value1"}}}},
    ...
  ]
}}

Rules:
- Use the EXACT action names from the primitives list above.
- Each step must have "action" (string) and "params" (dict).
- Merge consecutive keystrokes into type_text or press_key where appropriate.
- Use uia_click/dom_click when you can identify the UI element from context.
- Use click(x, y) only as a last resort when the element can't be identified.
- Omit redundant or accidental events (e.g. stray clicks, repeated keys).
- Keep the sequence minimal — only include intentional user actions.

Return ONLY the JSON object."""

    try:
        raw = router.complete(
            "You convert recordings of human computer usage into replayable action sequences. "
            "You output precise, minimal JSON using only the provided action primitives.",
            [{"role": "user", "content": prompt}],
            tier=TIER_SMART,
            max_tokens=4096,
            timeout=90.0,
        )

        # 3. Parse the JSON response
        raw = re.sub(r'```(?:json)?\s*', '', raw).strip().rstrip('`').strip()
        skill = json.loads(raw)

        # Validate structure
        if not isinstance(skill.get("steps"), list):
            skill["steps"] = []
        if not skill.get("name"):
            # Generate a name from the prompt
            slug = recording.prompt[:40].lower().replace(" ", "_")
            slug = re.sub(r'[^a-z0-9_]', '', slug)
            skill["name"] = slug or "unnamed_skill"
        if not skill.get("app"):
            skill["app"] = windows_used.split(",")[0].strip() if windows_used != "unknown" else "Unknown"

        # Validate each step has the required fields
        valid_steps = []
        for step in skill["steps"]:
            if isinstance(step, dict) and "action" in step:
                if "params" not in step or not isinstance(step["params"], dict):
                    step["params"] = {}
                valid_steps.append({"action": step["action"], "params": step["params"]})
        skill["steps"] = valid_steps

        # 4. Store the skill in the app DB
        app_name = skill["app"]
        if app_name and app_name != "Unknown":
            app_data = app_db.get(app_name)
            if "skills" not in app_data:
                app_data["skills"] = {}
            app_data["skills"][skill["name"]] = {
                "steps": skill["steps"],
                "created": time.time(),
                "source_prompt": recording.prompt[:200],
            }
            app_db._save(app_name, app_data)
            logger.info(f"Skill saved: '{skill['name']}' for {app_name} "
                        f"({len(skill['steps'])} steps)")

        return {
            "name": skill["name"],
            "app": skill["app"],
            "steps": skill["steps"],
        }

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse skill JSON from LLM: {e}")
        return {
            "error": f"Parse error: {e}",
            "raw": raw[:500] if 'raw' in dir() else "",
            "name": "",
            "app": "",
            "steps": [],
        }
    except Exception as e:
        logger.error(f"recording_to_skill failed: {e}")
        return {"error": str(e), "name": "", "app": "", "steps": []}
