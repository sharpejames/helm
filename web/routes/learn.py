"""
web/routes/learn.py — API routes for Learn Mode (watch & learn, auto-discover).
"""

import logging
import asyncio
from fastapi import APIRouter

from kb.observer import get_observer
from kb.analyzer import analyze_recording
from kb.discover import discover_app_ui
from kb.learner import learn_from_all_logs
from kb.apps import AppDB

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/learn/status")
async def learn_status():
    """Get current learn mode status."""
    obs = get_observer()
    return {
        "recording": obs.is_recording,
        "prompt": obs.current_prompt,
    }


@router.post("/learn/watch/start")
async def watch_start(body: dict):
    """Start watch & learn mode. Records human activity."""
    prompt = body.get("prompt", "")
    interval = body.get("screenshot_interval", 3.0)

    obs = get_observer()
    result = obs.start(prompt=prompt, screenshot_interval=interval)
    return result


@router.post("/learn/watch/stop")
async def watch_stop():
    """Stop recording and analyze the session."""
    obs = get_observer()
    recording = obs.stop()

    if not recording:
        return {"error": "No active recording"}

    # Save the raw recording
    filepath = recording.save()

    # Analyze in background (don't block the response)
    analysis = {"status": "analyzing"}
    try:
        app_db = AppDB()
        analysis = await asyncio.to_thread(analyze_recording, recording, app_db)
    except Exception as e:
        logger.error(f"Analysis failed: {e}")
        analysis = {"error": str(e)}

    return {
        "recording": {
            "duration_secs": recording.duration_secs(),
            "events": len(recording.events),
            "screenshots": len(recording.screenshots),
            "filepath": filepath,
        },
        "analysis": analysis,
    }


@router.post("/learn/discover")
async def auto_discover(body: dict):
    """Auto-discover an app's UI. Helm opens the app and explores it."""
    app_exe = body.get("app_exe", "")
    window_title = body.get("window_title", "")

    if not app_exe and not window_title:
        return {"error": "Provide app_exe or window_title"}

    app_db = AppDB()
    result = await asyncio.to_thread(
        discover_app_ui, app_exe or window_title, window_title or app_exe, app_db)
    return result


@router.post("/learn/from-logs")
async def learn_from_logs():
    """Process all task logs and extract knowledge."""
    app_db = AppDB()
    result = await asyncio.to_thread(learn_from_all_logs, app_db)
    return result


@router.get("/learn/knowledge")
async def get_knowledge():
    """Get all app knowledge profiles."""
    app_db = AppDB()
    apps = app_db.list_apps()
    profiles = {}
    for app in apps:
        data = app_db.get(app)
        profiles[app] = {
            "name": data.get("name", app),
            "exe": data.get("exe", ""),
            "tips_count": len(data.get("tips", [])),
            "issues_count": len(data.get("known_issues", [])),
            "shortcuts": data.get("shortcuts", {}),
        }
    return {"apps": profiles}
