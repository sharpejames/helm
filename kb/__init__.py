"""
Knowledge Base for Helm — stores successful scripts and enables few-shot learning.

Usage:
    from kb import KnowledgeBase
    kb = KnowledgeBase()
    
    # Save a successful script
    kb.save(task="draw a dog in Paint", script="...", tags=["paint", "drawing"], app="Paint")
    
    # Find similar scripts for few-shot prompting
    examples = kb.search("draw a cat in Paint", limit=3)
    
    # Rate a script (thumbs up/down from UI)
    kb.rate(entry_id, thumbs_up=True)
"""

import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional

KB_DIR = Path(__file__).parent
KB_FILE = KB_DIR / "scripts.json"


class KnowledgeBase:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or KB_FILE
        self._entries = []
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self._entries = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self._entries = []
        else:
            self._entries = []

    def _save(self):
        self.path.write_text(
            json.dumps(self._entries, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

    def save(self, task: str, script: str, tags: list[str] = None,
             app: str = None, run_id: str = None) -> str:
        """Save a successful script to the KB. Returns the entry ID."""
        entry_id = str(uuid.uuid4())[:8]
        entry = {
            "id": entry_id,
            "task": task,
            "script": script,
            "tags": tags or [],
            "app": app or "",
            "timestamp": time.time(),
            "rating": 0,        # net score: +1 for thumbs up, -1 for thumbs down
            "thumbs_up": 0,
            "thumbs_down": 0,
            "run_id": run_id,
        }
        self._entries.append(entry)
        self._save()
        return entry_id

    def rate(self, entry_id: str, thumbs_up: bool, context: str = "") -> bool:
        """Rate a KB entry with optional context. Returns True if found."""
        for entry in self._entries:
            if entry["id"] == entry_id:
                if thumbs_up:
                    entry["thumbs_up"] = entry.get("thumbs_up", 0) + 1
                else:
                    entry["thumbs_down"] = entry.get("thumbs_down", 0) + 1
                entry["rating"] = entry.get("thumbs_up", 0) - entry.get("thumbs_down", 0)
                if context:
                    if "feedback" not in entry:
                        entry["feedback"] = []
                    entry["feedback"].append({
                        "thumbs_up": thumbs_up,
                        "context": context,
                        "timestamp": time.time(),
                    })
                self._save()
                return True
        return False

    def remove(self, entry_id: str) -> bool:
        """Remove a KB entry (e.g. after thumbs down). Returns True if found."""
        before = len(self._entries)
        self._entries = [e for e in self._entries if e["id"] != entry_id]
        if len(self._entries) < before:
            self._save()
            return True
        return False

    def search(self, query: str, limit: int = 3, min_rating: int = 0) -> list[dict]:
        """
        Find similar scripts by keyword matching.
        Returns top matches sorted by relevance score * rating.
        """
        query_words = set(query.lower().split())
        scored = []

        for entry in self._entries:
            if entry.get("rating", 0) < min_rating:
                continue

            # Score: keyword overlap between query and task + tags
            entry_words = set(entry["task"].lower().split())
            entry_words.update(t.lower() for t in entry.get("tags", []))
            if entry.get("app"):
                entry_words.add(entry["app"].lower())

            overlap = len(query_words & entry_words)
            if overlap == 0:
                continue

            # Boost by rating (minimum 1 to avoid zeroing out)
            rating_boost = max(1, entry.get("rating", 0) + 1)
            score = overlap * rating_boost

            scored.append((score, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in scored[:limit]]

    def get(self, entry_id: str) -> Optional[dict]:
        """Get a single KB entry by ID."""
        for entry in self._entries:
            if entry["id"] == entry_id:
                return entry
        return None

    def list_all(self, limit: int = 50) -> list[dict]:
        """List all KB entries, newest first."""
        return sorted(self._entries, key=lambda e: e.get("timestamp", 0), reverse=True)[:limit]

    def format_examples(self, query: str, limit: int = 2) -> str:
        """
        Format matching KB entries as few-shot examples for the LLM prompt.
        Returns a string to inject into the system/user prompt.
        """
        examples = self.search(query, limit=limit, min_rating=0)
        if not examples:
            return ""

        parts = ["\n\nPROVEN SCRIPTS — these worked for similar tasks. Adapt, don't copy blindly:\n"]
        for i, ex in enumerate(examples, 1):
            parts.append(f"--- Example {i}: \"{ex['task']}\" (rating: {ex.get('rating', 0)}) ---")
            parts.append(f"```python\n{ex['script']}\n```\n")

        return "\n".join(parts)
