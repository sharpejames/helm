"""Streaming commentary for real-time video analysis.

Receives frame descriptions from the FrameCapturer → VisionModule pipeline,
suppresses duplicate consecutive descriptions (Requirement 11.5), and exposes
an async iterator interface that WebSocket endpoints can consume for real-time
streaming to the web UI (Requirements 11.3, 11.4).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class CommentaryEntry:
    """A single commentary item delivered to consumers."""

    description: str
    timestamp: float


class CommentaryStream:
    """Streaming text output for real-time frame descriptions.

    Usage::

        stream = CommentaryStream()

        # Producer side (called from FrameCapturer/EventDetector callback):
        stream.push("A person walks across the yard", time.time())

        # Consumer side (WebSocket endpoint):
        async for entry in stream:
            await ws.send_text(entry.description)
    """

    def __init__(self, maxlen: int = 200):
        # Async queue for delivering entries to consumers
        self._queue: asyncio.Queue[CommentaryEntry | None] = asyncio.Queue()
        # Last description for deduplication (Requirement 11.5)
        self._last_description: str | None = None
        # Whether the stream has been stopped
        self._stopped = False

    # ------------------------------------------------------------------
    # Producer API
    # ------------------------------------------------------------------

    def push(self, description: str, timestamp: float) -> None:
        """Add a new frame description.

        Duplicate consecutive descriptions are silently suppressed
        (Requirement 11.5).  Only changes are forwarded to consumers.
        """
        if not description or not description.strip():
            return

        description = description.strip()

        # Suppress duplicate consecutive descriptions
        if description == self._last_description:
            return

        self._last_description = description

        entry = CommentaryEntry(description=description, timestamp=timestamp)
        try:
            self._queue.put_nowait(entry)
        except asyncio.QueueFull:
            logger.warning("Commentary queue full, dropping oldest entry")

    def stop(self) -> None:
        """Signal consumers that the stream has ended."""
        self._stopped = True
        # Push sentinel so blocked consumers wake up
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    # ------------------------------------------------------------------
    # Consumer API — async iterator
    # ------------------------------------------------------------------

    def __aiter__(self) -> CommentaryStream:
        return self

    async def __anext__(self) -> CommentaryEntry:
        """Yield the next commentary entry.

        Blocks until a new (non-duplicate) description is available.
        Raises ``StopAsyncIteration`` when the stream is stopped.
        """
        entry = await self._queue.get()
        if entry is None:
            raise StopAsyncIteration
        return entry
