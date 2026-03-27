"""
App Knowledge DB — per-app profiles with version-specific guides, UI layouts,
shortcuts, known issues, and tips. Auto-populated by discover_ui() and refined
by user feedback.

Usage:
    from kb.apps import AppDB
    db = AppDB()
    
    # Get profile for current app
    profile = db.get("Paint")
    
    # Get context string for LLM prompts
    context = db.format_context("Paint")
    
    # Learn from a discover_ui() result
    db.learn_ui("Paint", "11.2409", elements)
    
    # Add a tip or known issue
    db.add_tip("Paint", "Always maximize before drawing for consistent coords")
    db.add_issue("Paint", "Canvas resize handles at edges - stay 15px away from borders")
"""

import json
import os
import time
from pathlib import Path
from typing import Optional

APPS_DIR = Path(__file__).parent / "apps"


class AppDB:
    def __init__(self, apps_dir: Optional[Path] = None):
        self.apps_dir = apps_dir or APPS_DIR
        self.apps_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, app_name: str) -> Path:
        safe = app_name.lower().replace(" ", "_").replace("/", "_")
        return self.apps_dir / f"{safe}.json"

    def _load(self, app_name: str) -> dict:
        p = self._path(app_name)
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {
            "name": app_name,
            "exe": "",
            "versions": {},
            "shortcuts": {},
            "tools": {},
            "known_issues": [],
            "tips": [],
            "ui_elements": [],
            "updated": 0,
        }

    def _save(self, app_name: str, data: dict):
        data["updated"] = time.time()
        self._path(app_name).write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

    def get(self, app_name: str) -> dict:
        """Get the full profile for an app."""
        return self._load(app_name)

    def exists(self, app_name: str) -> bool:
        return self._path(app_name).exists()

    def list_apps(self) -> list[str]:
        """List all known app names."""
        apps = []
        for f in self.apps_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                apps.append(data.get("name", f.stem))
            except Exception:
                apps.append(f.stem)
        return apps

    def set_exe(self, app_name: str, exe: str):
        data = self._load(app_name)
        data["exe"] = exe
        self._save(app_name, data)

    def add_shortcut(self, app_name: str, action: str, keys: str):
        """Add a keyboard shortcut. e.g. add_shortcut("Paint", "save_as", "F12")"""
        data = self._load(app_name)
        data["shortcuts"][action] = keys
        self._save(app_name, data)

    def add_tool(self, app_name: str, tool_name: str, how: str):
        """Document how to activate a tool. e.g. add_tool("Paint", "Pencil", "UIA name 'Pencil'")"""
        data = self._load(app_name)
        data["tools"][tool_name] = how
        self._save(app_name, data)

    def add_tip(self, app_name: str, tip: str):
        """Add a usage tip."""
        data = self._load(app_name)
        if tip not in data["tips"]:
            data["tips"].append(tip)
            self._save(app_name, data)

    def add_issue(self, app_name: str, issue: str):
        """Add a known issue/gotcha."""
        data = self._load(app_name)
        if issue not in data["known_issues"]:
            data["known_issues"].append(issue)
            self._save(app_name, data)

    def learn_ui(self, app_name: str, version: str, elements: list[dict]):
        """
        Store UI element discovery results from discover_ui().
        Keeps the most recent discovery per version.
        """
        data = self._load(app_name)
        if "versions" not in data:
            data["versions"] = {}

        # Store a summary of elements (not the full list — too large)
        summary = []
        for el in elements:
            if el.get("name") and el.get("visible", True):
                summary.append({
                    "name": el["name"],
                    "role": el.get("role", ""),
                    "cx": el.get("cx", 0),
                    "cy": el.get("cy", 0),
                })

        data["versions"][version] = {
            "discovered": time.time(),
            "element_count": len(elements),
            "elements_summary": summary[:100],  # cap at 100 most relevant
        }

        # Also update the top-level tools list from Button/MenuItem elements
        for el in elements:
            name = el.get("name", "")
            role = el.get("role", "")
            if name and role in ("Button", "MenuItem", "ToggleButton") and el.get("visible"):
                if name not in data["tools"]:
                    data["tools"][name] = f"UIA {role} at ({el.get('cx', '?')}, {el.get('cy', '?')})"

        self._save(app_name, data)

    def add_feedback(self, app_name: str, context: str, success: bool):
        """Store user feedback about app interaction."""
        data = self._load(app_name)
        if "feedback" not in data:
            data["feedback"] = []
        data["feedback"].append({
            "context": context,
            "success": success,
            "timestamp": time.time(),
        })
        # Keep last 50 feedback entries
        data["feedback"] = data["feedback"][-50:]
        self._save(app_name, data)

    def format_context(self, app_name: str, max_chars: int = 3000) -> str:
        """
        Format app knowledge as context for LLM prompts.
        Returns a string to inject into the system/user prompt.
        Handles rich profiles with game rules, strategies, launch tips, etc.
        """
        data = self._load(app_name)
        if not data.get("shortcuts") and not data.get("tips") and not data.get("known_issues") and not data.get("launch"):
            return ""

        parts = [f"\n\nAPP KNOWLEDGE — {data['name']}:"]

        if data.get("exe"):
            parts.append(f"  Executable: {data['exe']}")
        if data.get("version_note"):
            parts.append(f"  Note: {data['version_note']}")

        # Launch instructions
        if data.get("launch"):
            launch = data["launch"]
            if launch.get("method"):
                parts.append(f"  Launch: {launch['method']}")
            if launch.get("fullscreen"):
                parts.append(f"  Fullscreen: {launch['fullscreen']}")
            if launch.get("startup_issues"):
                parts.append("  Startup issues to handle:")
                for issue in launch["startup_issues"]:
                    parts.append(f"    - {issue}")

        if data.get("shortcuts"):
            sc = data["shortcuts"]
            if isinstance(sc, dict):
                parts.append("  Shortcuts: " + ", ".join(f"{k}={v}" for k, v in sc.items()))

        # Game-specific knowledge (klondike, spider, etc.)
        for key in ("klondike", "spider", "freecell"):
            if data.get(key):
                game = data[key]
                parts.append(f"  {key.upper()} RULES:")
                if game.get("rules"):
                    parts.append(f"    {game['rules']}")
                if game.get("goal"):
                    parts.append(f"    Goal: {game['goal']}")
                if game.get("new_game"):
                    parts.append(f"    New game: {game['new_game']}")
                if game.get("shortcuts"):
                    parts.append("    Shortcuts: " + ", ".join(f"{k}={v}" for k, v in game["shortcuts"].items()))
                if game.get("tips"):
                    parts.append("    Tips:")
                    for tip in game["tips"][:8]:
                        parts.append(f"      - {tip}")

        # Strategy for agent
        if data.get("strategy_for_agent"):
            parts.append("  STRATEGY:")
            for step in data["strategy_for_agent"]:
                parts.append(f"    {step}")

        # UI layout hints
        if data.get("ui_layout"):
            layout = data["ui_layout"]
            parts.append("  UI Layout:")
            for k, v in layout.items():
                if k != "note":
                    parts.append(f"    {k}: {v}")

        if data.get("known_issues"):
            parts.append("  ⚠ Known issues:")
            for issue in data["known_issues"][:8]:
                parts.append(f"    - {issue}")

        if data.get("tips") and not data.get("strategy_for_agent"):
            parts.append("  Tips:")
            for tip in data["tips"][:8]:
                parts.append(f"    - {tip}")

        result = "\n".join(parts)
        return result[:max_chars]
