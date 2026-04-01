"""Event detection for real-time video analysis.

Compares frame descriptions against user-defined watch conditions using
substring matching (fast) and optional LLM semantic matching (slower, via
VisionModule 0.8B).  Maintains a sliding window of recent descriptions and
suppresses duplicate events with a configurable cooldown.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.vision import VisionModule

logger = logging.getLogger(__name__)

# Default sliding-window size: 30 seconds at 2 FPS
_DEFAULT_WINDOW_SECS = 30
_DEFAULT_FPS = 2


@dataclass
class DetectedEvent:
    """A single event emitted when a watch condition matches."""

    timestamp: float
    frame_png: bytes
    description: str
    matched_condition: str
    confidence: float


class EventDetector:
    """Compares frame descriptions to detect events and changes."""

    def __init__(
        self,
        cooldown_secs: float = 30.0,
        fps: float = _DEFAULT_FPS,
        vision: VisionModule | None = None,
    ):
        self._conditions: list[str] = []
        maxlen = max(1, int(_DEFAULT_WINDOW_SECS * fps))
        self._history: deque[tuple[float, str]] = deque(maxlen=maxlen)
        self._last_emitted: dict[str, float] = {}  # condition → last emit ts
        self._cooldown = cooldown_secs
        self._vision = vision  # optional, for semantic matching

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_conditions(self, conditions: list[str]) -> None:
        """Set watch conditions (e.g., ["coyote", "person at door", "package"])."""
        self._conditions = [c.strip() for c in conditions if c.strip()]
        # Reset cooldown tracking when conditions change
        self._last_emitted.clear()
        logger.info("EventDetector conditions set: %s", self._conditions)

    def process_frame(
        self,
        description: str,
        timestamp: float,
        frame_png: bytes,
    ) -> list[DetectedEvent]:
        """Check description against conditions.

        Returns detected events (empty list if no match or in cooldown).
        """
        # Store in sliding window
        self._history.append((timestamp, description))

        if not self._conditions or not description:
            return []

        events: list[DetectedEvent] = []
        desc_lower = description.lower()

        for condition in self._conditions:
            # --- cooldown check ---
            last = self._last_emitted.get(condition)
            if last is not None and (timestamp - last) < self._cooldown:
                continue

            # --- fast path: substring match ---
            cond_lower = condition.lower()
            if cond_lower in desc_lower:
                events.append(
                    DetectedEvent(
                        timestamp=timestamp,
                        frame_png=frame_png,
                        description=description,
                        matched_condition=condition,
                        confidence=1.0,
                    )
                )
                self._last_emitted[condition] = timestamp
                continue

            # --- slow path: LLM semantic matching (optional) ---
            if self._vision is not None:
                confidence = self._semantic_match(description, condition)
                if confidence >= 0.6:
                    events.append(
                        DetectedEvent(
                            timestamp=timestamp,
                            frame_png=frame_png,
                            description=description,
                            matched_condition=condition,
                            confidence=confidence,
                        )
                    )
                    self._last_emitted[condition] = timestamp

        return events

    def get_changes(self, description: str) -> list[str]:
        """Compare *description* against the most recent history entry.

        Returns a list of human-readable differences, or an empty list if
        there is no previous description or nothing changed.
        """
        if not self._history:
            return []

        prev_ts, prev_desc = self._history[-1]

        if not prev_desc or prev_desc == description:
            return []

        # Use VisionModule.compare_frames when available for richer diffs
        if self._vision is not None:
            try:
                result = self._vision.compare_frames(prev_desc, description)
                if result.get("changed"):
                    return result.get("differences", [])
                return []
            except Exception:
                logger.debug("compare_frames failed, falling back to simple diff")

        # Simple fallback: report that the scene changed
        return [f"Scene changed from previous description"]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _semantic_match(self, description: str, condition: str) -> float:
        """Use VisionModule (0.8B) to check if *description* semantically
        matches *condition*.  Returns a confidence score 0.0–1.0.
        """
        if self._vision is None:
            return 0.0

        prompt = (
            "Does the following scene description match the condition?\n\n"
            f"DESCRIPTION: {description}\n"
            f"CONDITION: {condition}\n\n"
            "Respond with JSON only:\n"
            '{"match": true/false, "confidence": 0.0-1.0}'
        )
        try:
            raw = self._vision._chat(self._vision.fast_model, prompt)
            result = self._vision._parse_json(raw)
            if result.get("match"):
                return float(result.get("confidence", 0.0))
            return 0.0
        except Exception:
            logger.debug("Semantic match failed for condition=%r", condition)
            return 0.0
