"""Alert system for real-time video analysis.

Triggers desktop notifications, plays sounds, and logs alerts to the database
when the EventDetector identifies matching conditions.  Batches alerts within
a configurable window (default 10 s) so rapid-fire events produce a single
notification summarising all triggered conditions.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import platform
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from video.event_detector import DetectedEvent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class AlertRecord:
    """A persisted alert entry."""

    id: str
    timestamp: float
    condition: str
    description: str
    frame_b64: str  # base64-encoded PNG thumbnail
    batched_conditions: list[str] | None = None


# ---------------------------------------------------------------------------
# AlertSystem
# ---------------------------------------------------------------------------

class AlertSystem:
    """Desktop notifications, sounds, and alert logging."""

    def __init__(
        self,
        db_session_factory=None,
        batch_window_secs: float = 10.0,
    ):
        self._db_session_factory = db_session_factory
        self._batch_window = batch_window_secs

        # Batching state
        self._pending: list[DetectedEvent] = []
        self._batch_timer: asyncio.Task | None = None

        # In-memory fallback when DB model is not yet available (task 14.1)
        self._history: list[AlertRecord] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def trigger(self, event: DetectedEvent) -> None:
        """Handle a detected event.

        Events arriving within *batch_window_secs* of the first pending event
        are batched into a single notification (Requirement 13.4).
        """
        self._pending.append(event)

        # Start the batch timer on the first event in a new window
        if self._batch_timer is None or self._batch_timer.done():
            self._batch_timer = asyncio.ensure_future(self._wait_and_flush())

    async def get_history(self, limit: int = 50) -> list[AlertRecord]:
        """Return recent alerts, newest first.

        Tries the database first; falls back to the in-memory list when the
        Alert DB model is not yet available.
        """
        if self._db_session_factory is not None:
            try:
                return await self._get_history_from_db(limit)
            except Exception:
                logger.debug("DB history unavailable, using in-memory fallback")

        return list(reversed(self._history[-limit:]))

    # ------------------------------------------------------------------
    # Batching internals
    # ------------------------------------------------------------------

    async def _wait_and_flush(self) -> None:
        """Wait for the batch window then flush."""
        await asyncio.sleep(self._batch_window)
        await self._flush_batch()

    async def _flush_batch(self) -> None:
        """Send a batched notification, play sound, and log to DB."""
        if not self._pending:
            return

        events = list(self._pending)
        self._pending.clear()

        # Build a single AlertRecord that summarises the batch
        conditions = list(dict.fromkeys(e.matched_condition for e in events))
        first = events[0]

        frame_b64 = base64.b64encode(first.frame_png).decode("ascii")

        record = AlertRecord(
            id=str(uuid.uuid4()),
            timestamp=time.time(),
            condition=conditions[0],
            description=first.description,
            frame_b64=frame_b64,
            batched_conditions=conditions if len(conditions) > 1 else None,
        )

        # Persist
        await self._store_alert(record)

        # Desktop notification
        if len(conditions) == 1:
            title = f"Helm Alert: {conditions[0]}"
            body = first.description
        else:
            title = f"Helm Alert: {len(conditions)} conditions"
            body = ", ".join(conditions)

        self._desktop_notify(title, body)
        self._play_sound()

    # ------------------------------------------------------------------
    # Notification helpers
    # ------------------------------------------------------------------

    def _desktop_notify(self, title: str, body: str) -> None:
        """Show a desktop toast notification.

        Uses *plyer* (cross-platform) as the primary backend.  Logs a
        warning and continues silently if no notification backend is
        available.
        """
        try:
            from plyer import notification  # type: ignore[import-untyped]

            notification.notify(
                title=title,
                message=body[:256],
                app_name="Helm",
                timeout=10,
            )
            logger.info("Desktop notification sent: %s", title)
            return
        except Exception:
            logger.debug("plyer notification failed, skipping desktop toast")

        logger.warning("No desktop notification backend available")

    def _play_sound(self) -> None:
        """Play an audible alert beep.

        Uses *winsound* on Windows (standard library).  On other platforms
        the call is silently skipped.
        """
        if platform.system() != "Windows":
            logger.debug("winsound not available on %s", platform.system())
            return

        try:
            import winsound  # type: ignore[import-not-found]

            winsound.Beep(1000, 300)  # 1 kHz for 300 ms
            logger.debug("Alert sound played")
        except Exception:
            logger.debug("winsound.Beep failed, skipping sound alert")

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    async def _store_alert(self, record: AlertRecord) -> None:
        """Persist an alert record.

        Tries the database first (when the Alert model from task 14.1 is
        available).  Always stores in the in-memory list as a fallback.
        """
        # Always keep in-memory copy
        self._history.append(record)

        if self._db_session_factory is None:
            return

        try:
            from db.models import Alert  # type: ignore[attr-defined]

            async with self._db_session_factory() as session:
                db_alert = Alert(
                    id=record.id,
                    timestamp=record.timestamp,
                    condition=record.condition,
                    description=record.description,
                    frame_b64=record.frame_b64,
                    batched_conditions=(
                        ",".join(record.batched_conditions)
                        if record.batched_conditions
                        else None
                    ),
                )
                session.add(db_alert)
                await session.commit()
                logger.info("Alert %s persisted to DB", record.id)
        except Exception:
            logger.debug("DB persistence unavailable, alert stored in-memory only")

    async def _get_history_from_db(self, limit: int) -> list[AlertRecord]:
        """Load recent alerts from the database."""
        from db.models import Alert  # type: ignore[attr-defined]
        from sqlalchemy import select

        async with self._db_session_factory() as session:
            stmt = (
                select(Alert)
                .order_by(Alert.timestamp.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()

        return [
            AlertRecord(
                id=row.id,
                timestamp=row.timestamp,
                condition=row.condition,
                description=row.description,
                frame_b64=row.frame_b64,
                batched_conditions=(
                    row.batched_conditions.split(",")
                    if row.batched_conditions
                    else None
                ),
            )
            for row in rows
        ]
