"""Two-tier video commentary summarizer.

Tier 1 (Vision): Fast frame descriptions from vision model — no context, minimal prompt.
Tier 2 (Summarizer): Periodically condenses raw descriptions into contextualized
summaries and maintains a chronological key events log.

The summarizer runs asynchronously so it never blocks frame processing.
"""

from __future__ import annotations

import logging
import time
import re
from collections import deque
from dataclasses import dataclass, field

import requests

logger = logging.getLogger(__name__)


@dataclass
class KeyEvent:
    """A notable moment worth remembering."""
    timestamp: float
    description: str

    def format(self) -> str:
        t = time.strftime("%H:%M:%S", time.localtime(self.timestamp))
        return f"[{t}] {self.description}"


class StreamSummarizer:
    """Accumulates raw vision descriptions and periodically produces summaries.

    Args:
        ollama_url: Ollama API base URL.
        summarizer_model: Model for text summarization (e.g. qwen3.5:4b).
        batch_size: Number of raw descriptions before triggering a summary.
        mode: Commentary mode (surveillance, audio_description, sports).
        user_context: Optional user-provided context string.
    """

    def __init__(
        self,
        ollama_url: str = "http://localhost:11434",
        summarizer_model: str = "qwen3.5:4b",
        batch_size: int = 5,
        mode: str = "surveillance",
        user_context: str = "",
    ):
        self.ollama_url = ollama_url.rstrip("/")
        self.summarizer_model = summarizer_model
        self.batch_size = batch_size
        self.mode = mode
        self.user_context = user_context

        # Raw descriptions waiting to be summarized
        self._pending: list[tuple[float, str]] = []
        # Latest summary produced by the summarizer
        self.current_summary: str = ""
        # Chronological key events
        self.key_events: list[KeyEvent] = []
        # Cap key events to prevent unbounded growth
        self._max_events = 50

    def add_description(self, timestamp: float, description: str) -> bool:
        """Add a raw description. Returns True if a summary should be triggered."""
        self._pending.append((timestamp, description))
        return len(self._pending) >= self.batch_size

    def needs_summary(self) -> bool:
        return len(self._pending) >= self.batch_size

    def summarize(self) -> str | None:
        """Run the summarizer model on pending descriptions (blocking).

        Returns the new summary text, or None if nothing to summarize.
        Also updates key_events with any notable moments.
        """
        if not self._pending:
            return None

        batch = self._pending[:]
        self._pending.clear()

        # Format the batch
        lines = []
        for ts, desc in batch:
            t = time.strftime("%H:%M:%S", time.localtime(ts))
            lines.append(f"[{t}] {desc}")
        raw_text = "\n".join(lines)

        # Build key events context (last 10)
        events_ctx = ""
        if self.key_events:
            recent = self.key_events[-10:]
            events_ctx = "\nKey events so far:\n" + "\n".join(e.format() for e in recent) + "\n"

        # Build mode-specific prompt
        prompt = self._build_prompt(raw_text, events_ctx)

        # Call summarizer model
        try:
            result = self._chat(prompt)
        except Exception:
            logger.exception("Summarizer failed")
            return None

        if not result:
            return None

        # Clean think tags
        result = re.sub(r"</?think>", "", result).strip()

        # Parse summary and key events from response
        return self._parse_response(result, batch)

    def _build_prompt(self, raw_text: str, events_ctx: str) -> str:
        user_ctx = ""
        if self.user_context:
            user_ctx = f"\nUser context: {self.user_context}\n"

        if self.mode == "surveillance":
            return (
                "You are a security monitoring assistant. "
                "Below are recent observations from a security camera.\n"
                f"{user_ctx}{events_ctx}\n"
                f"Observations:\n{raw_text}\n\n"
                "Tasks:\n"
                "1. SUMMARY: Write a brief 1-2 sentence summary of what's currently happening.\n"
                "2. KEY_EVENTS: List any new notable security events (arrivals, departures, "
                "vehicles, animals, deliveries, unusual activity). One per line, format: "
                "- [time] event description\n"
                "If no new key events, write: - none\n"
                "Respond with:\nSUMMARY: ...\nKEY_EVENTS:\n- ..."
            )
        elif self.mode == "sports":
            return (
                "You are a sports commentary summarizer. "
                "Below are recent play-by-play observations from a live game.\n"
                f"{user_ctx}{events_ctx}\n"
                f"Observations:\n{raw_text}\n\n"
                "Tasks:\n"
                "1. SUMMARY: Summarize the current game action in 1-2 exciting sentences. "
                "Focus on gameplay: passes, shots, goals, fouls, possession. "
                "If a score was reported, include it. Ignore replays, ads, crowd shots.\n"
                "2. KEY_EVENTS: List any confirmed game events (goals, cards, "
                "substitutions, penalties, score changes). One per line, format: "
                "- [time] event description\n"
                "Only include events clearly stated in the observations. Do NOT invent anything.\n"
                "If no new key events, write: - none\n"
                "Respond with:\nSUMMARY: ...\nKEY_EVENTS:\n- ..."
            )
        else:
            # audio_description
            return (
                "You are an audio description narrator for visually impaired viewers. "
                "Below are recent scene observations.\n"
                f"{user_ctx}{events_ctx}\n"
                f"Observations:\n{raw_text}\n\n"
                "Tasks:\n"
                "1. SUMMARY: Write a vivid 2-3 sentence narration of the current scene, "
                "describing setting, characters, actions, and mood.\n"
                "2. KEY_EVENTS: List any new significant scene changes (new characters, "
                "location changes, important actions, dramatic moments). One per line, format: "
                "- [time] event description\n"
                "If no new key events, write: - none\n"
                "Respond with:\nSUMMARY: ...\nKEY_EVENTS:\n- ..."
            )

    def _parse_response(self, result: str, batch: list[tuple[float, str]]) -> str:
        """Extract summary and key events from model response."""
        summary = ""
        new_events = []

        # Extract SUMMARY
        summary_match = re.search(r"SUMMARY:\s*(.+?)(?=\nKEY_EVENTS:|\Z)", result, re.DOTALL)
        if summary_match:
            summary = summary_match.group(1).strip()

        # Extract KEY_EVENTS
        events_match = re.search(r"KEY_EVENTS:\s*(.+)", result, re.DOTALL)
        if events_match:
            events_text = events_match.group(1).strip()
            for line in events_text.split("\n"):
                line = line.strip().lstrip("- ").strip()
                if not line or line.lower() == "none":
                    continue
                # Try to extract timestamp from [HH:MM:SS] format
                ts_match = re.match(r"\[(\d{1,2}:\d{2}:\d{2})\]\s*(.*)", line)
                if ts_match:
                    new_events.append(KeyEvent(
                        timestamp=batch[-1][0],  # Use batch timestamp
                        description=ts_match.group(2).strip(),
                    ))
                else:
                    new_events.append(KeyEvent(
                        timestamp=batch[-1][0],
                        description=line,
                    ))

        # Add new events, cap total
        self.key_events.extend(new_events)
        if len(self.key_events) > self._max_events:
            self.key_events = self.key_events[-self._max_events:]

        self.current_summary = summary or result
        return self.current_summary

    def _chat(self, prompt: str) -> str:
        """Call the summarizer model via Ollama."""
        payload = {
            "model": self.summarizer_model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "think": False,
            "options": {"num_predict": 200},
        }
        resp = requests.post(
            f"{self.ollama_url}/api/chat",
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("message", {}).get("content", "")

    def get_key_events_text(self) -> str:
        """Format all key events as a string for display."""
        if not self.key_events:
            return ""
        return "\n".join(e.format() for e in self.key_events)
