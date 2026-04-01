"""Video analysis session API.

Provides endpoints to start/stop video capture sessions, query status,
and stream real-time commentary over WebSocket.

Wires the pipeline: FrameCapturer → EventDetector → AlertSystem → CommentaryStream

Requirements: 16.1, 16.2, 16.6, 4.1–4.6, 7.1–7.4
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from core.vision import describe_frame_with_context, get_vision
from video.frame_capturer import FrameCapturer
from video.event_detector import EventDetector
from video.alert_system import AlertSystem
from video.commentary import CommentaryStream

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------

class RegionModel(BaseModel):
    x: int = 0
    y: int = 0
    width: int = 1920
    height: int = 1080


class StartRequest(BaseModel):
    region: RegionModel = Field(default_factory=RegionModel)
    fps: float = Field(default=1.0, ge=0.5, le=2.0)
    conditions: list[str] = Field(default_factory=list)


class StartResponse(BaseModel):
    status: str
    message: str


class StopResponse(BaseModel):
    status: str
    message: str


class StatusResponse(BaseModel):
    running: bool
    fps: float
    conditions: list[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_capturer(request: Request) -> FrameCapturer | None:
    return getattr(request.app.state, "frame_capturer", None)


def _get_detector(request: Request) -> EventDetector | None:
    return getattr(request.app.state, "event_detector", None)


def _get_alert_system(request: Request) -> AlertSystem | None:
    return getattr(request.app.state, "alert_system", None)


def _get_commentary(request: Request) -> CommentaryStream | None:
    return getattr(request.app.state, "commentary_stream", None)


# ---------------------------------------------------------------------------
# Pipeline callback — wires FrameCapturer → EventDetector → AlertSystem → Commentary
# ---------------------------------------------------------------------------

async def _on_frame(
    description: str,
    timestamp: float,
    frame_png: bytes,
    detector: EventDetector,
    alert_system: AlertSystem,
    commentary: CommentaryStream,
) -> None:
    """Called by FrameCapturer for every captured frame.

    1. Push description to CommentaryStream (for WebSocket consumers).
    2. Run EventDetector.process_frame to check watch conditions.
    3. For each detected event, trigger the AlertSystem.
    """
    # Stream commentary
    commentary.push(description, timestamp)

    # Detect events
    events = detector.process_frame(description, timestamp, frame_png)
    for event in events:
        await alert_system.trigger(event)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/video/start", response_model=StartResponse)
async def start_video(body: StartRequest, request: Request):
    """Start a video capture session with region, fps, and watch conditions."""
    capturer = _get_capturer(request)
    detector = _get_detector(request)
    alert_system = _get_alert_system(request)
    commentary = _get_commentary(request)

    if capturer is None:
        return StartResponse(status="error", message="Video components not initialised")

    if capturer.running:
        return StartResponse(status="error", message="Session already running")

    # Configure event detector conditions
    if detector is not None:
        detector.set_conditions(body.conditions)

    # Build the on_frame callback that wires the pipeline together
    async def frame_callback(description: str, timestamp: float, frame_png: bytes):
        await _on_frame(
            description, timestamp, frame_png,
            detector=detector,
            alert_system=alert_system,
            commentary=commentary,
        )

    region = body.region.model_dump()
    await capturer.start(region=region, fps=body.fps, on_frame=frame_callback)

    logger.info(
        "Video session started  region=%s  fps=%.1f  conditions=%s",
        region, body.fps, body.conditions,
    )
    return StartResponse(status="ok", message="Capture session started")


@router.post("/video/stop", response_model=StopResponse)
async def stop_video(request: Request):
    """Stop the active video capture session."""
    capturer = _get_capturer(request)
    commentary = _get_commentary(request)

    if capturer is None or not capturer.running:
        return StopResponse(status="error", message="No active session")

    await capturer.stop()

    if commentary is not None:
        commentary.stop()

    logger.info("Video session stopped")
    return StopResponse(status="ok", message="Capture session stopped")


@router.get("/video/status", response_model=StatusResponse)
async def video_status(request: Request):
    """Current session status, frame rate, and watch conditions."""
    capturer = _get_capturer(request)
    detector = _get_detector(request)

    running = capturer.running if capturer else False
    fps = capturer.actual_fps if capturer else 0.0
    conditions = detector._conditions if detector else []

    return StatusResponse(running=running, fps=fps, conditions=conditions)


@router.websocket("/video/stream")
async def video_stream(websocket: WebSocket):
    """WebSocket endpoint for real-time commentary.

    Streams CommentaryEntry objects as JSON to connected clients.
    """
    await websocket.accept()

    commentary: CommentaryStream | None = getattr(
        websocket.app.state, "commentary_stream", None
    )

    if commentary is None:
        await websocket.send_json({"error": "Commentary stream not initialised"})
        await websocket.close()
        return

    try:
        async for entry in commentary:
            await websocket.send_json({
                "description": entry.description,
                "timestamp": entry.timestamp,
            })
    except WebSocketDisconnect:
        logger.debug("WebSocket client disconnected from /video/stream")
    except Exception:
        logger.exception("WebSocket stream error")
    finally:
        await websocket.close()


# ---------------------------------------------------------------------------
# Extension WebSocket helpers
# ---------------------------------------------------------------------------

def _mss_capture_region(x: int, y: int, w: int, h: int) -> bytes:
    """Capture a screen region using mss and return PNG bytes."""
    import mss
    from PIL import Image
    import io

    with mss.mss() as sct:
        region = {"left": x, "top": y, "width": w, "height": h}
        shot = sct.grab(region)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

        # Resize to max 512px longest side (same as canvas capture)
        max_dim = 512
        if img.width > max_dim or img.height > max_dim:
            scale = max_dim / max(img.width, img.height)
            new_w = max(1, round(img.width * scale))
            new_h = max(1, round(img.height * scale))
            img = img.resize((new_w, new_h), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


async def _process_frame(
    websocket: WebSocket,
    frame_bytes: bytes,
    timestamp: float,
    description_history: deque,
    detector: EventDetector,
    commentary: CommentaryStream,
    alert_system: AlertSystem | None,
    vision_pool: ThreadPoolExecutor,
    session_active: bool,
) -> None:
    """Shared frame processing: vision → commentary → alerts → response."""
    vision = get_vision()
    if vision is None:
        await websocket.send_json(
            {"type": "error", "message": "Vision module not available"}
        )
        return

    try:
        loop = asyncio.get_event_loop()
        context = list(description_history)[-3:]
        description = await loop.run_in_executor(
            vision_pool,
            describe_frame_with_context,
            vision, frame_bytes, context,
        )
    except Exception:
        logger.exception("Vision analysis failed")
        await websocket.send_json(
            {"type": "error", "message": "Vision analysis failed"}
        )
        return

    if not session_active:
        return

    if not description or not description.strip():
        await websocket.send_json(
            {"type": "no_activity", "timestamp": timestamp}
        )
        return

    if description.strip().upper() == "NO_ACTIVITY":
        await websocket.send_json(
            {"type": "no_activity", "timestamp": timestamp}
        )
        return

    description_history.append(description)
    commentary.push(description, timestamp)

    events = detector.process_frame(description, timestamp, frame_bytes)

    if alert_system and events:
        for event in events:
            await alert_system.trigger(event)

    response: dict = {
        "type": "commentary",
        "description": description,
        "timestamp": timestamp,
    }
    if events:
        response["alert"] = {"condition": events[0].matched_condition}

    await websocket.send_json(response)


# ---------------------------------------------------------------------------
# Extension WebSocket endpoint — bidirectional frame streaming
# Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 7.1, 7.2, 7.3, 7.4
# ---------------------------------------------------------------------------

@router.websocket("/video/extension-stream")
async def extension_stream(websocket: WebSocket):
    """Bidirectional WebSocket for Chrome extension frame streaming.

    Inbound messages:
      {"type": "frame", "data": "<base64 PNG>", "timestamp": 1234567890.123}
      {"type": "configure", "conditions": ["coyote", "person"]}
      {"type": "stop"}

    Outbound messages:
      {"type": "commentary", "description": "...", "timestamp": 1234567890.123}
      {"type": "commentary", "description": "...", "timestamp": ..., "alert": {"condition": "coyote"}}
      {"type": "error", "message": "..."}
      {"type": "no_activity", "timestamp": ...}
    """
    await websocket.accept()

    # Per-session pipeline instances
    detector = EventDetector()
    commentary = CommentaryStream()
    description_history: deque[str] = deque(maxlen=10)
    session_active = True

    # Thread pool for blocking vision calls — 1 thread since we process
    # one frame at a time anyway
    vision_pool = ThreadPoolExecutor(max_workers=1)

    # Shared alert system for desktop notifications
    alert_system: AlertSystem | None = getattr(
        websocket.app.state, "alert_system", None
    )

    logger.info("Extension stream connected")

    try:
        while session_active:
            try:
                raw = await websocket.receive_text()
            except Exception:
                break

            # Parse JSON message
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json(
                    {"type": "error", "message": "Malformed JSON"}
                )
                continue

            msg_type = msg.get("type")

            # ----------------------------------------------------------
            # STOP message — immediate, no blocking
            # ----------------------------------------------------------
            if msg_type == "stop":
                logger.info("Extension stream received stop message")
                session_active = False
                break

            # ----------------------------------------------------------
            # CONFIGURE message
            # ----------------------------------------------------------
            elif msg_type == "configure":
                conditions = msg.get("conditions", [])
                detector.set_conditions(conditions)
                logger.info(
                    "Extension stream configured conditions: %s", conditions
                )

            # ----------------------------------------------------------
            # FRAME message
            # ----------------------------------------------------------
            elif msg_type == "frame":
                data_b64 = msg.get("data", "")
                timestamp = msg.get("timestamp", time.time())

                # Decode base64
                try:
                    frame_bytes = base64.b64decode(data_b64)
                except (binascii.Error, Exception):
                    await websocket.send_json(
                        {"type": "error", "message": "Invalid frame data"}
                    )
                    continue

                await _process_frame(
                    websocket, frame_bytes, timestamp,
                    description_history, detector, commentary,
                    alert_system, vision_pool, session_active,
                )

            # ----------------------------------------------------------
            # REGION_CAPTURE message — mss fallback for DRM/cross-origin
            # ----------------------------------------------------------
            elif msg_type == "region_capture":
                x = msg.get("x", 0)
                y = msg.get("y", 0)
                w = msg.get("width", 100)
                h = msg.get("height", 100)
                timestamp = msg.get("timestamp", time.time())

                try:
                    loop = asyncio.get_event_loop()
                    frame_bytes = await loop.run_in_executor(
                        vision_pool,
                        _mss_capture_region, x, y, w, h,
                    )
                except Exception:
                    logger.exception("mss region capture failed")
                    await websocket.send_json(
                        {"type": "error", "message": "Screen capture failed"}
                    )
                    continue

                if not frame_bytes:
                    await websocket.send_json(
                        {"type": "no_activity", "timestamp": timestamp}
                    )
                    continue

                await _process_frame(
                    websocket, frame_bytes, timestamp,
                    description_history, detector, commentary,
                    alert_system, vision_pool, session_active,
                )

            else:
                await websocket.send_json(
                    {"type": "error", "message": f"Unknown message type: {msg_type}"}
                )

    except WebSocketDisconnect:
        logger.info("Extension stream client disconnected")
    except Exception:
        logger.exception("Extension stream error")
    finally:
        # Clean up session resources
        session_active = False
        vision_pool.shutdown(wait=False)
        commentary.stop()
        description_history.clear()
        logger.info("Extension stream session cleaned up")
        try:
            await websocket.close()
        except Exception:
            pass
