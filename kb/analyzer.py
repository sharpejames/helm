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

    prompt = f"""Analyze this recording of a human using a computer. Extract reusable knowledge.

RECORDING CONTEXT: {recording.prompt}
DURATION: {recording.duration_secs():.0f} seconds
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
