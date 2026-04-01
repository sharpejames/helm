"""Continuous screenshot capture for video analysis.

Uses mss for region capture (consistent with core/screen.py) and sends each
frame to VisionModule.describe_frame for description.  FPS is self-regulating:
if vision latency exceeds the target interval the capture rate drops gracefully,
never below 0.5 FPS.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from collections import deque
from typing import Awaitable, Callable

import mss
from PIL import Image

from core.vision import VisionModule

logger = logging.getLogger(__name__)

# Hard floor — capture never slower than once every 2 seconds
_MIN_FPS = 0.5
_MAX_INTERVAL = 1.0 / _MIN_FPS  # 2.0 s


class FrameCapturer:
    """Continuous screenshot capture for video analysis."""

    def __init__(self, vision_module: VisionModule):
        self._vision = vision_module
        self._running = False
        self._task: asyncio.Task | None = None
        # Deque of inter-frame durations for the last 10 frames
        self._frame_times: deque[float] = deque(maxlen=10)
        self._actual_fps: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(
        self,
        region: dict,
        fps: float = 1.0,
        on_frame: Callable[[str, float, bytes], Awaitable[None]] | None = None,
    ) -> None:
        """Start capturing.

        Parameters
        ----------
        region : dict
            ``{"x": int, "y": int, "width": int, "height": int}``
        fps : float
            Target frames per second (1–2 recommended).
        on_frame : async callback
            ``async def on_frame(description: str, timestamp: float, frame_png: bytes)``
        """
        if self._running:
            logger.warning("FrameCapturer already running — ignoring start()")
            return

        self._running = True
        self._frame_times.clear()
        self._actual_fps = 0.0
        self._task = asyncio.create_task(
            self._capture_loop(region, fps, on_frame)
        )
        logger.info(
            "FrameCapturer started  region=%s  target_fps=%.1f", region, fps
        )

    async def stop(self) -> None:
        """Stop capture and release resources within 2 seconds."""
        if not self._running:
            return

        self._running = False

        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except asyncio.TimeoutError:
                logger.warning("Capture loop did not finish in 2 s — cancelling")
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            self._task = None

        self._frame_times.clear()
        self._actual_fps = 0.0
        logger.info("FrameCapturer stopped")

    @property
    def running(self) -> bool:
        return self._running

    @property
    def actual_fps(self) -> float:
        """Measured FPS over the last 10 frames."""
        return self._actual_fps

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _grab_region(sct: mss.mss, region: dict) -> bytes:
        """Capture *region* and return PNG bytes."""
        monitor = {
            "left": region["x"],
            "top": region["y"],
            "width": region["width"],
            "height": region["height"],
        }
        shot = sct.grab(monitor)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def _update_fps(self, elapsed: float) -> None:
        """Record a frame duration and recompute actual_fps."""
        self._frame_times.append(elapsed)
        if self._frame_times:
            avg = sum(self._frame_times) / len(self._frame_times)
            self._actual_fps = 1.0 / avg if avg > 0 else 0.0

    async def _capture_loop(
        self,
        region: dict,
        target_fps: float,
        on_frame: Callable[[str, float, bytes], Awaitable[None]] | None,
    ) -> None:
        """Core async loop: grab → describe → callback → sleep."""
        target_interval = 1.0 / max(target_fps, _MIN_FPS)
        loop = asyncio.get_running_loop()

        monitor = {
            "left": region["x"],
            "top": region["y"],
            "width": region["width"],
            "height": region["height"],
        }

        while self._running:
            t_start = time.monotonic()
            timestamp = time.time()

            # --- capture (fast, done in async loop — mss is not thread-safe) ---
            try:
                with mss.mss() as sct:
                    shot = sct.grab(monitor)
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                frame_png = buf.getvalue()
            except Exception:
                logger.exception("Frame grab failed")
                await asyncio.sleep(target_interval)
                continue

            # --- vision description (blocking, run in thread) ---
            try:
                description: str = await loop.run_in_executor(
                    None, self._vision.describe_frame, frame_png
                )
            except Exception:
                logger.exception("Vision describe_frame failed")
                description = ""

            # --- deliver to callback ---
            if on_frame is not None:
                try:
                    await on_frame(description, timestamp, frame_png)
                except Exception:
                    logger.exception("on_frame callback error")

            # --- self-regulating sleep ---
            elapsed = time.monotonic() - t_start
            self._update_fps(elapsed)

            sleep_time = max(0.0, min(target_interval - elapsed, _MAX_INTERVAL))
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
