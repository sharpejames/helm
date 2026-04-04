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

from core.vision import describe_frame_with_context, describe_frame_batch, get_vision
from video.frame_capturer import FrameCapturer
from video.event_detector import EventDetector
from video.alert_system import AlertSystem
from video.commentary import CommentaryStream
from video.summarizer import StreamSummarizer

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

    logger.info("mss capturing region: x=%d y=%d w=%d h=%d", x, y, w, h)

    with mss.mss() as sct:
        region = {"left": x, "top": y, "width": w, "height": h}
        shot = sct.grab(region)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

        # Resize to max 512px longest side
        max_dim = 384
        if img.width > max_dim or img.height > max_dim:
            scale = max_dim / max(img.width, img.height)
            new_w = max(1, round(img.width * scale))
            new_h = max(1, round(img.height * scale))
            img = img.resize((new_w, new_h), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


async def _process_frame_batch(
    websocket: WebSocket,
    frame_batch: list[tuple[bytes, float]],
    description_history: deque,
    detector: EventDetector,
    commentary: CommentaryStream,
    alert_system: AlertSystem | None,
    vision_pool: ThreadPoolExecutor,
    summarizer: StreamSummarizer | None,
    summarizer_pool: ThreadPoolExecutor | None,
    session_active: bool,
    mode: str = "surveillance",
    user_context: str = "",
) -> None:
    """Process a batch of frames with multi-image vision call."""
    if not frame_batch:
        return

    frames = [f for f, _ in frame_batch]
    timestamp = frame_batch[-1][1]  # Use latest timestamp

    logger.info("Processing batch of %d frames, mode=%s", len(frames), mode)

    # Generate thumbnail from the latest frame
    thumbnail_b64 = ""
    try:
        from PIL import Image
        import io as _io
        img = Image.open(_io.BytesIO(frames[-1]))
        max_thumb = 160
        if img.width > max_thumb or img.height > max_thumb:
            scale = max_thumb / max(img.width, img.height)
            img = img.resize(
                (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                Image.LANCZOS,
            )
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=50)
        thumbnail_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception:
        pass

    vision = get_vision()
    if vision is None:
        await websocket.send_json({"type": "error", "message": "Vision module not available"})
        return

    try:
        loop = asyncio.get_event_loop()
        import time as _time
        t0 = _time.time()
        if len(frames) == 1:
            description = await loop.run_in_executor(
                vision_pool, describe_frame_with_context,
                vision, frames[0], [], mode, user_context,
            )
        else:
            description = await loop.run_in_executor(
                vision_pool, describe_frame_batch,
                vision, frames, mode, user_context,
            )
        elapsed = _time.time() - t0
        logger.info("Vision returned in %.1fs (%d frames): %s", elapsed, len(frames), (description or "")[:100])
    except Exception:
        logger.exception("Vision analysis failed")
        await websocket.send_json({"type": "error", "message": "Vision analysis failed"})
        return

    if not session_active:
        return

    # Clean up think tags
    import re as _re
    description = _re.sub(r"</?think>", "", description or "").strip()
    description = _re.sub(r"<\|channel>thought.*?<channel\|>", "", description, flags=_re.DOTALL).strip()

    if not description:
        await websocket.send_json(
            {"type": "no_activity", "timestamp": timestamp,
             **({"thumbnail": thumbnail_b64} if thumbnail_b64 else {})}
        )
        return

    # Filter NO_ACTIVITY
    desc_upper = description.upper()
    if "NO_ACTIVITY" in desc_upper or "NO ACTIVITY" in desc_upper or desc_upper.startswith("0 PEOPLE") or "REMAINS UNCHANGED" in desc_upper or "NO CHANGES" in desc_upper:
        await websocket.send_json(
            {"type": "no_activity", "timestamp": timestamp,
             **({"thumbnail": thumbnail_b64} if thumbnail_b64 else {})}
        )
        return

    description_history.append(description)
    commentary.push(description, timestamp)

    events = detector.process_frame(description, timestamp, frames[-1])

    if alert_system and events:
        for event in events:
            await alert_system.trigger(event)

    response: dict = {
        "type": "commentary",
        "description": description,
        "timestamp": timestamp,
    }
    if thumbnail_b64:
        response["thumbnail"] = thumbnail_b64
    if events:
        response["alert"] = {"condition": events[0].matched_condition}

    await websocket.send_json(response)

    # Tier 2: Summarizer (fire-and-forget)
    if summarizer and summarizer_pool:
        should_summarize = summarizer.add_description(timestamp, description)
        if should_summarize:
            async def _run_summarizer():
                try:
                    _loop = asyncio.get_event_loop()
                    import time as _t
                    t0 = _t.time()
                    logger.info("Triggering summarizer...")
                    summary = await _loop.run_in_executor(summarizer_pool, summarizer.summarize)
                    elapsed = _t.time() - t0
                    logger.info("Summarizer returned in %.1fs: %s", elapsed, (summary or "")[:100])
                    if summary:
                        await websocket.send_json({
                            "type": "summary",
                            "summary": summary,
                            "key_events": summarizer.get_key_events_text(),
                            "timestamp": timestamp,
                        })
                except Exception:
                    logger.exception("Summarizer failed")
            asyncio.create_task(_run_summarizer())


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
    session_mode = "surveillance"
    session_user_context = ""

    # Frame buffer for multi-frame batch processing
    BATCH_SIZE = 1  # Single frame for speed — increase for temporal context
    frame_buffer: list[tuple[bytes, float]] = []

    # Thread pool for blocking vision calls — 1 thread since we process
    # one frame at a time anyway
    vision_pool = ThreadPoolExecutor(max_workers=1)

    # Separate thread pool for summarizer (runs concurrently with vision)
    summarizer_pool = ThreadPoolExecutor(max_workers=1)

    # Summarizer will be created on configure
    summarizer: StreamSummarizer | None = None

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
                # Process any remaining buffered frames
                if frame_buffer:
                    batch = frame_buffer[:]
                    frame_buffer.clear()
                    await _process_frame_batch(
                        websocket, batch,
                        description_history, detector, commentary,
                        alert_system, vision_pool,
                        summarizer, summarizer_pool,
                        session_active,
                        mode=session_mode,
                        user_context=session_user_context,
                    )
                session_active = False
                break

            # ----------------------------------------------------------
            # PING message — keep-alive
            # ----------------------------------------------------------
            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

            # ----------------------------------------------------------
            # CONFIGURE message
            # ----------------------------------------------------------
            elif msg_type == "configure":
                conditions = msg.get("conditions", [])
                session_mode = msg.get("mode", "surveillance")
                session_user_context = msg.get("userContext", "")
                enable_summarizer = msg.get("enableSummarizer", False)
                detector.set_conditions(conditions)
                if enable_summarizer:
                    summarizer = StreamSummarizer(
                        ollama_url="http://localhost:11434",
                        summarizer_model="qwen3.5:0.8b",
                        batch_size=5,
                        mode=session_mode,
                        user_context=session_user_context,
                    )
                else:
                    summarizer = None
                logger.info(
                    "Extension stream configured conditions: %s mode: %s summarizer: %s context: %s",
                    conditions, session_mode, enable_summarizer,
                    session_user_context[:80] if session_user_context else "(none)",
                )

            # ----------------------------------------------------------
            # FRAME message
            # ----------------------------------------------------------
            elif msg_type == "frame":
                data_b64 = msg.get("data", "")
                timestamp = msg.get("timestamp", time.time())

                try:
                    frame_bytes = base64.b64decode(data_b64)
                except (binascii.Error, Exception):
                    await websocket.send_json(
                        {"type": "error", "message": "Invalid frame data"}
                    )
                    continue

                frame_buffer.append((frame_bytes, timestamp))
                if len(frame_buffer) < BATCH_SIZE:
                    # Immediately ask for next frame to fill the batch
                    await websocket.send_json({"type": "need_frame"})
                else:
                    batch = frame_buffer[:]
                    frame_buffer.clear()
                    await _process_frame_batch(
                        websocket, batch,
                        description_history, detector, commentary,
                        alert_system, vision_pool,
                        summarizer, summarizer_pool,
                        session_active,
                        mode=session_mode,
                        user_context=session_user_context,
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

                frame_buffer.append((frame_bytes, timestamp))
                if len(frame_buffer) < BATCH_SIZE:
                    # Need more frames — tell extension to send next
                    await websocket.send_json({"type": "need_frame"})
                else:
                    batch = frame_buffer[:]
                    frame_buffer.clear()
                    await _process_frame_batch(
                        websocket, batch,
                        description_history, detector, commentary,
                        alert_system, vision_pool,
                        summarizer, summarizer_pool,
                        session_active,
                        mode=session_mode,
                        user_context=session_user_context,
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
        summarizer_pool.shutdown(wait=False)
        commentary.stop()
        description_history.clear()
        logger.info("Extension stream session cleaned up")
        try:
            await websocket.close()
        except Exception:
            pass
