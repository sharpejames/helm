"""
kb/learner.py — Automatic knowledge extraction from task logs.

Reads completed/failed task logs and extracts:
  - Successful app launch commands (exe, window_title)
  - Common failure patterns → known_issues
  - Successful action sequences → tips
  - Popup/dialog patterns → startup_issues

Run after each task, or periodically to update app profiles.
"""

import json
import os
import logging
from pathlib import Path
from collections import Counter, defaultdict

from kb.apps import AppDB

logger = logging.getLogger(__name__)

LOG_DIR = Path(__file__).parent.parent / "logs"

# App name detection from task text
APP_KEYWORDS = {
    "Paint": ["paint", "draw", "sketch", "drawing"],
    "Solitaire": ["solitaire", "klondike", "spider", "freecell", "card game"],
    "Notepad": ["notepad", "text editor"],
    "Chrome": ["chrome", "browser", "website", "web"],
    "Outlook": ["outlook", "email", "mail"],
    "Word": ["word", "document"],
    "Excel": ["excel", "spreadsheet"],
}


def detect_app_from_task(task: str) -> str | None:
    """Detect which app a task is about."""
    task_lower = task.lower()
    for app, keywords in APP_KEYWORDS.items():
        if any(kw in task_lower for kw in keywords):
            return app
    return None


def learn_from_log(log_path: str | Path, app_db: AppDB = None) -> dict:
    """Extract knowledge from a single task log file.
    Only learns from COMPLETED tasks. Skips apps with hand-curated profiles
    (those with startup_sequence or strategy_for_agent).
    """
    if app_db is None:
        app_db = AppDB()

    with open(log_path, encoding="utf-8") as f:
        data = json.load(f)

    task = data.get("task", "")
    status = data.get("status", "")
    events = data.get("events", [])
    app_name = detect_app_from_task(task)

    if not app_name or not events:
        return {"app": None, "learned": []}

    # Don't pollute hand-curated profiles with auto-learned noise
    existing = app_db.get(app_name)
    if existing.get("startup_sequence") or existing.get("strategy_for_agent"):
        return {"app": app_name, "learned": ["skipped: hand-curated profile"]}

    # Only learn from successful completions
    if status != "completed":
        return {"app": app_name, "learned": []}

    learned = []

    # Extract successful action sequence as a tip
    actions = []
    for e in events:
        if e["type"] == "decision":
            dd = json.loads(e["data"]) if isinstance(e["data"], str) else e["data"]
            actions.append(dd.get("action", ""))

    if len(actions) >= 3:
        start_pattern = " → ".join(actions[:5])
        tip = f"Successful sequence: {start_pattern}"
        app_db.add_tip(app_name, tip)
        learned.append(f"tip: {tip}")

    if learned:
        logger.info(f"Learned {len(learned)} facts about {app_name} from {Path(log_path).name}")

    return {"app": app_name, "learned": learned}


def learn_from_all_logs(app_db: AppDB = None) -> dict:
    """Process all task logs and extract knowledge.
    
    Returns summary: {"apps": {"Paint": 5, "Solitaire": 3}, "total_learned": 8}
    """
    if app_db is None:
        app_db = AppDB()

    if not LOG_DIR.exists():
        return {"apps": {}, "total_learned": 0}

    apps_learned = defaultdict(int)
    total = 0

    for log_file in sorted(LOG_DIR.glob("*.json")):
        try:
            result = learn_from_log(log_file, app_db)
            if result["app"] and result["learned"]:
                apps_learned[result["app"]] += len(result["learned"])
                total += len(result["learned"])
        except Exception as e:
            logger.warning(f"Failed to process {log_file.name}: {e}")

    return {"apps": dict(apps_learned), "total_learned": total}


def learn_from_latest_log(app_db: AppDB = None) -> dict:
    """Process only the most recent task log."""
    if not LOG_DIR.exists():
        return {"app": None, "learned": []}

    logs = sorted(LOG_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not logs:
        return {"app": None, "learned": []}

    return learn_from_log(logs[-1], app_db)
