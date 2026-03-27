"""
kb/discover.py — Automated app UI discovery.

Opens an app, uses vision to catalog the UI, and builds a knowledge base profile.
Can be triggered as a task: "discover the UI of [app name]"
"""

import json
import logging
import time

from agent.actions import (
    _ask_screen, _screenshot_b64, _get_active_window,
    open_application, focus_application, ActionResult,
)
from kb.apps import AppDB

logger = logging.getLogger(__name__)


def discover_app_ui(app_exe: str, window_title: str, app_db: AppDB = None) -> dict:
    """Open an app and catalog its UI using vision.
    
    Returns a profile dict with discovered elements, shortcuts, and layout.
    """
    if app_db is None:
        app_db = AppDB()

    # Open the app
    result = open_application(app_exe, window_title)
    if not result.ok:
        return {"error": f"Failed to open {window_title}: {result.error}"}

    time.sleep(2)

    # Ask vision to describe the full UI
    ui_description = _ask_screen(
        f"Describe the complete UI layout of {window_title}. "
        "List every visible element: menus, buttons, toolbars, panels, tabs, status bars. "
        "For each element, give its approximate position (top-left, center, bottom-right, etc.) "
        "and what it does. Be thorough."
    )

    # Ask about keyboard shortcuts
    shortcuts_info = _ask_screen(
        f"Looking at {window_title}, what menu items are visible? "
        "List any keyboard shortcuts shown next to menu items (like Ctrl+S, F2, etc.)."
    )

    # Ask about the main interaction area
    main_area = _ask_screen(
        f"What is the main content/interaction area of {window_title}? "
        "Where is it positioned? What can you do in it?"
    )

    # Build profile
    profile = {
        "name": window_title,
        "exe": app_exe,
        "discovered_at": time.time(),
        "ui_description": ui_description[:1000],
        "shortcuts_observed": shortcuts_info[:500],
        "main_area": main_area[:500],
    }

    # Save to app DB
    data = app_db.get(window_title)
    if not data.get("exe"):
        data["exe"] = app_exe
    if not data.get("version_note"):
        data["version_note"] = f"Auto-discovered on {time.strftime('%Y-%m-%d')}"

    # Add discovered info as tips
    app_db.add_tip(window_title, f"UI layout: {ui_description[:200]}")
    app_db.add_tip(window_title, f"Main area: {main_area[:200]}")
    if "ctrl" in shortcuts_info.lower() or "f1" in shortcuts_info.lower():
        app_db.add_tip(window_title, f"Shortcuts: {shortcuts_info[:200]}")

    logger.info(f"Discovered UI for {window_title}: {len(ui_description)} chars")
    return profile
