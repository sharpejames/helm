"""Local TTS using Kokoro-ONNX (CPU-only, no GPU needed)."""

from __future__ import annotations

import base64
import io
import logging
import time

logger = logging.getLogger(__name__)

_kokoro_instance = None
_kokoro_voices = None


def _get_kokoro():
    """Lazy-load Kokoro model (downloads on first use)."""
    global _kokoro_instance
    if _kokoro_instance is not None:
        return _kokoro_instance
    try:
        import kokoro_onnx
        logger.info("Loading Kokoro TTS model...")
        t0 = time.time()
        _kokoro_instance = kokoro_onnx.Kokoro("kokoro-v1.0.onnx", "voices-v1.0.bin")
        logger.info("Kokoro TTS loaded in %.1fs", time.time() - t0)
        return _kokoro_instance
    except Exception:
        logger.exception("Failed to load Kokoro TTS")
        return None


def get_kokoro_voices() -> list[dict]:
    """Return available Kokoro voice names."""
    kokoro = _get_kokoro()
    if kokoro is None:
        return []
    try:
        voices = kokoro.get_voices()
        return [{"id": f"kokoro:{v}", "name": f"Kokoro - {v}"} for v in voices]
    except Exception:
        logger.exception("Failed to get Kokoro voices")
        return []


def synthesize(text: str, voice: str = "af_heart", speed: float = 1.0) -> str | None:
    """Generate speech audio and return as base64-encoded WAV.

    Args:
        text: Text to speak.
        voice: Kokoro voice name (without 'kokoro:' prefix).
        speed: Speech rate multiplier.

    Returns:
        Base64-encoded WAV string, or None on failure.
    """
    kokoro = _get_kokoro()
    if kokoro is None:
        return None

    try:
        import soundfile as sf
        t0 = time.time()
        audio, sample_rate = kokoro.create(text, voice=voice, speed=speed)
        elapsed = time.time() - t0
        logger.info("Kokoro TTS generated in %.2fs for %d chars", elapsed, len(text))

        buf = io.BytesIO()
        sf.write(buf, audio, sample_rate, format="WAV")
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception:
        logger.exception("Kokoro TTS synthesis failed")
        return None
