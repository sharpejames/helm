"""
kb/observer.py — Watch & Learn: records human desktop activity.

Records mouse clicks, keyboard presses, active window changes, and periodic
screenshots. Produces a timestamped event log that can be analyzed by the LLM
to extract reusable knowledge (action sequences, UI patterns, shortcuts).

Modes:
  - "watch": Record human activity passively
  - "discover": Helm explores an app autonomously

Usage:
    observer = Observer()
    observer.start(prompt="Watch me use Google Maps to get directions")
    # ... user does stuff ...
    recording = observer.stop()  # returns Recording with events + screenshots
"""

import time
import json
import threading
import logging
import base64
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

RECORDINGS_DIR = Path(__file__).parent.parent / "recordings"


@dataclass
class InputEvent:
    ts: float
    type: str          # "click", "key", "scroll", "window_change", "speech"
    data: dict = field(default_factory=dict)


@dataclass
class Screenshot:
    ts: float
    image_b64: str     # base64 PNG
    window: str        # active window title


@dataclass
class Recording:
    prompt: str
    started: float
    stopped: float = 0
    events: list = field(default_factory=list)
    screenshots: list = field(default_factory=list)
    active_windows: list = field(default_factory=list)

    def duration_secs(self) -> float:
        return (self.stopped or time.time()) - self.started

    def to_dict(self) -> dict:
        return {
            "prompt": self.prompt,
            "started": self.started,
            "stopped": self.stopped,
            "duration_secs": self.duration_secs(),
            "event_count": len(self.events),
            "screenshot_count": len(self.screenshots),
            "speech_count": sum(1 for e in self.events if e.type == "speech"),
            "events": [{"ts": e.ts, "type": e.type, "data": e.data} for e in self.events],
            "screenshots": [{"ts": s.ts, "window": s.window} for s in self.screenshots],
            "transcript": self._build_transcript(),
        }

    def _build_transcript(self) -> str:
        """Build a timestamped transcript from speech events."""
        speech_events = [e for e in self.events if e.type == "speech"]
        if not speech_events:
            return ""
        t0 = self.started
        lines = []
        for e in speech_events:
            elapsed = e.ts - t0
            text = e.data.get("text", "")
            if text:
                lines.append(f"[{elapsed:.0f}s] {text}")
        return "\n".join(lines)

    def save(self) -> str:
        """Save recording to disk. Returns filepath."""
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.fromtimestamp(self.started).strftime("%Y%m%d_%H%M%S")
        slug = self.prompt[:30].lower().replace(" ", "_").replace("/", "_")
        slug = "".join(c for c in slug if c.isalnum() or c == "_")
        base = RECORDINGS_DIR / f"{ts}_{slug}"

        # Save events + metadata
        meta = self.to_dict()
        with open(f"{base}.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

        # Save screenshots as separate files
        for i, ss in enumerate(self.screenshots):
            img_path = f"{base}_ss{i:03d}.png"
            with open(img_path, "wb") as f:
                f.write(base64.b64decode(ss.image_b64))

        logger.info(f"Recording saved: {base}.json ({len(self.events)} events, {len(self.screenshots)} screenshots)")
        return f"{base}.json"


class Observer:
    """Records human desktop activity: mouse, keyboard, screenshots, window changes."""

    def __init__(self):
        self._recording: Recording | None = None
        self._running = False
        self._audio_enabled = False
        self._threads: list[threading.Thread] = []
        self._mouse_listener = None
        self._key_listener = None

    @property
    def is_recording(self) -> bool:
        return self._running

    @property
    def current_prompt(self) -> str:
        return self._recording.prompt if self._recording else ""

    def start(self, prompt: str = "", screenshot_interval: float = 3.0, audio: bool = False):
        """Start recording. Takes periodic screenshots and logs input events.
        If audio=True, also records microphone and transcribes speech."""
        if self._running:
            return {"error": "Already recording"}

        self._recording = Recording(prompt=prompt, started=time.time())
        self._running = True
        self._audio_enabled = audio

        # Start screenshot thread
        ss_thread = threading.Thread(target=self._screenshot_loop,
                                      args=(screenshot_interval,), daemon=True)
        ss_thread.start()
        self._threads.append(ss_thread)

        # Start input listener thread
        input_thread = threading.Thread(target=self._input_listener, daemon=True)
        input_thread.start()
        self._threads.append(input_thread)

        # Start window tracker thread
        win_thread = threading.Thread(target=self._window_tracker, daemon=True)
        win_thread.start()
        self._threads.append(win_thread)

        # Start audio listener thread (if enabled)
        if audio:
            audio_thread = threading.Thread(target=self._audio_listener, daemon=True)
            audio_thread.start()
            self._threads.append(audio_thread)

        logger.info(f"Observer started: '{prompt}' (audio={'on' if audio else 'off'})")
        return {"status": "recording", "prompt": prompt, "audio": audio}

    def stop(self) -> Recording | None:
        """Stop recording and return the Recording."""
        if not self._running:
            return None

        self._running = False
        self._recording.stopped = time.time()

        # Stop pynput listeners
        if self._mouse_listener:
            try:
                self._mouse_listener.stop()
            except Exception:
                pass
        if self._key_listener:
            try:
                self._key_listener.stop()
            except Exception:
                pass

        # Wait for threads to finish
        for t in self._threads:
            t.join(timeout=2)
        self._threads.clear()

        recording = self._recording
        self._recording = None
        logger.info(f"Observer stopped: {len(recording.events)} events, "
                     f"{len(recording.screenshots)} screenshots, "
                     f"{recording.duration_secs():.0f}s")
        return recording

    def _screenshot_loop(self, interval: float):
        """Take periodic screenshots while recording."""
        import requests
        CLAWMETHEUS_URL = "http://127.0.0.1:7331"

        while self._running:
            try:
                r = requests.get(f"{CLAWMETHEUS_URL}/screenshot/base64?scale=0.5", timeout=5)
                data = r.json()
                img = data.get("image", "")
                if img:
                    # Get active window
                    try:
                        win = requests.get(f"{CLAWMETHEUS_URL}/active-window", timeout=3).json()
                        window_title = win.get("title", "unknown")
                    except Exception:
                        window_title = "unknown"

                    self._recording.screenshots.append(
                        Screenshot(ts=time.time(), image_b64=img, window=window_title))
            except Exception as e:
                logger.debug(f"Screenshot failed: {e}")

            time.sleep(interval)

    def _input_listener(self):
        """Listen for mouse and keyboard events using pynput."""
        try:
            from pynput import mouse, keyboard

            def on_click(x, y, button, pressed):
                if not self._running:
                    return False
                if pressed:
                    self._recording.events.append(InputEvent(
                        ts=time.time(), type="click",
                        data={"x": int(x), "y": int(y), "button": str(button)}
                    ))

            def on_key(key):
                if not self._running:
                    return False
                try:
                    key_str = key.char if hasattr(key, 'char') and key.char else str(key)
                except Exception:
                    key_str = str(key)
                self._recording.events.append(InputEvent(
                    ts=time.time(), type="key",
                    data={"key": key_str}
                ))

            self._mouse_listener = mouse.Listener(on_click=on_click)
            self._key_listener = keyboard.Listener(on_press=on_key)
            self._mouse_listener.start()
            self._key_listener.start()
            self._mouse_listener.join()

        except ImportError:
            logger.warning("pynput not installed — input recording disabled. Install with: pip install pynput")
            # Fall back to no input recording — screenshots still work
            while self._running:
                time.sleep(1)

    def _window_tracker(self):
        """Track active window changes."""
        import requests
        CLAWMETHEUS_URL = "http://127.0.0.1:7331"
        last_window = ""

        while self._running:
            try:
                win = requests.get(f"{CLAWMETHEUS_URL}/active-window", timeout=3).json()
                title = win.get("title", "")
                if title and title != last_window:
                    self._recording.events.append(InputEvent(
                        ts=time.time(), type="window_change",
                        data={"window": title, "previous": last_window}
                    ))
                    self._recording.active_windows.append(title)
                    last_window = title
            except Exception:
                pass
            time.sleep(0.5)

    def _audio_listener(self):
        """Record microphone audio in chunks and transcribe to text.
        Uses speech_recognition with Google's free API for transcription.
        Each recognized phrase becomes a timestamped 'speech' event."""
        try:
            import speech_recognition as sr

            recognizer = sr.Recognizer()
            recognizer.energy_threshold = 300
            recognizer.dynamic_energy_threshold = True
            recognizer.pause_threshold = 1.5

            mic = sr.Microphone()

            with mic as source:
                logger.info("Audio: calibrating for ambient noise (2s)...")
                recognizer.adjust_for_ambient_noise(source, duration=2)
                logger.info(f"Audio: calibrated, energy_threshold={recognizer.energy_threshold:.0f}")

            # Add a startup event so we know audio is working
            self._recording.events.append(InputEvent(
                ts=time.time(), type="speech",
                data={"text": "[Audio recording started — speak to narrate your actions]"}
            ))

            def _transcribe_callback(recognizer, audio):
                if not self._running:
                    return
                speech_ts = time.time()
                try:
                    text = recognizer.recognize_google(audio)
                    if text and text.strip():
                        self._recording.events.append(InputEvent(
                            ts=speech_ts, type="speech",
                            data={"text": text.strip()}
                        ))
                        logger.info(f"🎤 Speech: '{text.strip()}'")
                except sr.UnknownValueError:
                    pass
                except sr.RequestError as e:
                    logger.warning(f"Speech API error: {e}")
                except Exception as e:
                    logger.warning(f"Transcription error: {e}")

            stop_listening = recognizer.listen_in_background(mic, _transcribe_callback,
                                                              phrase_time_limit=10)
            logger.info("Audio: listening for speech — speak to narrate your actions")

            while self._running:
                time.sleep(0.5)

            stop_listening(wait_for_stop=False)
            logger.info("Audio: stopped listening")

        except ImportError as e:
            err = f"Audio FAILED: missing library — run: pip install SpeechRecognition PyAudio ({e})"
            logger.error(err)
            self._recording.events.append(InputEvent(
                ts=time.time(), type="speech",
                data={"text": f"[ERROR: {err}]"}
            ))
        except AttributeError as e:
            err = f"Audio FAILED: PyAudio not installed — run: pip install PyAudio ({e})"
            logger.error(err)
            self._recording.events.append(InputEvent(
                ts=time.time(), type="speech",
                data={"text": f"[ERROR: {err}]"}
            ))
        except OSError as e:
            err = f"Audio FAILED: no microphone available ({e})"
            logger.error(err)
            self._recording.events.append(InputEvent(
                ts=time.time(), type="speech",
                data={"text": f"[ERROR: {err}]"}
            ))
        except Exception as e:
            err = f"Audio FAILED: {e}"
            logger.error(err)
            self._recording.events.append(InputEvent(
                ts=time.time(), type="speech",
                data={"text": f"[ERROR: {err}]"}
            ))


# Global singleton
_observer: Observer | None = None


def get_observer() -> Observer:
    global _observer
    if _observer is None:
        _observer = Observer()
    return _observer
