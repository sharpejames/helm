import base64
import json
import logging
import re

import requests

logger = logging.getLogger(__name__)

# Default Ollama endpoint
_OLLAMA_URL = "http://localhost:11434"

# Default model names (overridable via config)
_DEFAULT_FAST_MODEL = "qwen3.5:0.8b"
_DEFAULT_DETAILED_MODEL = "qwen3.5:4b"


class VisionModule:
    """Screen understanding via local Qwen models. NOT for click coordinates."""

    def __init__(self, config: dict):
        self.config = config
        vision_cfg = config.get("vision", {})
        local_cfg = config.get("local_llm", {})

        self.ollama_url = local_cfg.get("base_url", _OLLAMA_URL).rstrip("/")
        self.fast_model = vision_cfg.get("fast_model", _DEFAULT_FAST_MODEL)
        self.detailed_model = vision_cfg.get("detailed_model", _DEFAULT_DETAILED_MODEL)
        self.timeout = local_cfg.get("timeout", 60)  # 60s to handle cold model loads

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _encode_image(self, png_bytes: bytes) -> str:
        """Base64-encode raw PNG bytes for the Ollama images field."""
        return base64.b64encode(png_bytes).decode("utf-8")

    def _chat(self, model: str, prompt: str, images: list[str] | None = None,
              timeout: int | None = None) -> str:
        """Send a chat completion request to Ollama /api/chat.

        *images* is a list of base64-encoded image strings.
        Returns the raw assistant text.
        """
        messages = [{"role": "user", "content": prompt}]
        if images:
            messages[0]["images"] = images

        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
        }

        try:
            resp = requests.post(
                f"{self.ollama_url}/api/chat",
                json=payload,
                timeout=timeout or self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("message", {}).get("content", "")
        except requests.exceptions.Timeout:
            logger.warning("Ollama request timed out (model=%s)", model)
            return ""
        except Exception as e:
            logger.error("Ollama chat failed (model=%s): %s", model, e)
            return ""

    def _parse_json(self, text: str) -> dict:
        """Best-effort JSON extraction from LLM output."""
        text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
        try:
            return json.loads(text)
        except Exception:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group())
                except Exception:
                    pass
        return {}

    def _select_model(self, detail: str) -> str:
        """Pick the Ollama model name based on detail level."""
        if detail == "detailed":
            return self.detailed_model
        return self.fast_model

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def describe_screen(self, screenshot: bytes, detail: str = "fast") -> dict:
        """Structured scene description.

        detail="fast"     → Qwen3.5:0.8B (<1 s)
        detail="detailed" → Qwen3.5:4B  (~5-10 s)

        Returns: {"app": str, "elements": list[str], "state": str, "description": str}
        """
        model = self._select_model(detail)
        prompt = (
            "Analyze this screenshot. Respond with JSON only, no extra text:\n"
            '{"app": "<active application name>", '
            '"elements": ["<visible UI element>", ...], '
            '"state": "<current application state>", '
            '"description": "<one-sentence summary of what is on screen>"}'
        )
        img_b64 = self._encode_image(screenshot)
        raw = self._chat(model, prompt, images=[img_b64])
        result = self._parse_json(raw)

        # Ensure required keys always present
        return {
            "app": result.get("app", "unknown"),
            "elements": result.get("elements", []),
            "state": result.get("state", "unknown"),
            "description": result.get("description", ""),
        }

    def verify_action(self, before: bytes, after: bytes, expected: str) -> dict:
        """Compare before/after screenshots to verify an action.

        Returns: {"success": bool, "confidence": float, "changes": str}
        """
        prompt = (
            f'Two images: BEFORE (first) and AFTER (second). Expected result: "{expected}"\n'
            "Did the expected change happen? Respond with JSON only, no extra text:\n"
            '{"success": true/false, "confidence": 0.0-1.0, "changes": "<description of visible changes>"}'
        )
        imgs = [self._encode_image(before), self._encode_image(after)]
        raw = self._chat(self.fast_model, prompt, images=imgs)
        result = self._parse_json(raw)

        return {
            "success": result.get("success", False),
            "confidence": float(result.get("confidence", 0.5)),
            "changes": result.get("changes", ""),
        }

    def describe_frame(self, frame: bytes) -> str:
        """Single frame description for video analysis. Uses 0.8B for speed.

        Returns plain text description including objects, people, actions, scene context.
        """
        prompt = (
            "Describe this image in one paragraph. Include visible objects, people, "
            "actions being performed, and scene context. Plain text only, no JSON."
        )
        img_b64 = self._encode_image(frame)
        text = self._chat(self.fast_model, prompt, images=[img_b64])
        return text.strip() if text else ""

    def compare_frames(self, desc_prev: str, desc_current: str) -> dict:
        """Compare two frame descriptions (text-only, no images).

        Returns: {"changed": bool, "differences": list[str]}
        """
        prompt = (
            "Compare these two scene descriptions and identify differences.\n\n"
            f"PREVIOUS:\n{desc_prev}\n\n"
            f"CURRENT:\n{desc_current}\n\n"
            "Respond with JSON only, no extra text:\n"
            '{"changed": true/false, "differences": ["<difference 1>", ...]}'
        )
        raw = self._chat(self.fast_model, prompt)
        result = self._parse_json(raw)

        return {
            "changed": result.get("changed", False),
            "differences": result.get("differences", []),
        }

    # ------------------------------------------------------------------
    # Legacy compatibility helpers
    # ------------------------------------------------------------------

    def check_state(self, screenshot: bytes) -> dict:
        """Backward-compatible screen state check (delegates to describe_screen)."""
        desc = self.describe_screen(screenshot, detail="fast")
        return {
            "active_app": desc.get("app", ""),
            "active_window": desc.get("app", ""),
            "description": desc.get("description", ""),
            "ready": True,
        }

    def check_blockers(self, screenshot: bytes) -> dict:
        """Check for modal dialogs / popups blocking the UI."""
        prompt = (
            "Is there any modal dialog, popup, or blocker covering the main UI? "
            "Respond with JSON only:\n"
            '{"has_blocker": true/false, "type": "<dialog/popup/none>", '
            '"description": "<what the blocker says>", '
            '"dismiss_action": "escape_key"|"close_button"|"accept_button"|"click_outside"|"none"}'
        )
        img_b64 = self._encode_image(screenshot)
        raw = self._chat(self.fast_model, prompt, images=[img_b64])
        result = self._parse_json(raw)
        return {
            "has_blocker": result.get("has_blocker", False),
            "type": result.get("type", "none"),
            "description": result.get("description", ""),
            "dismiss_action": result.get("dismiss_action", "none"),
        }

    def check_cursor(self, screenshot: bytes, cx: int, cy: int, target: str) -> dict:
        """Check if cursor is over the target element (backward compat)."""
        prompt = (
            f'Cursor is at ({cx}, {cy}). Target: "{target}"\n'
            "Is the cursor over the target? Respond with JSON only:\n"
            '{"on_target": true/false, "confidence": 0.0-1.0, "dx": 0, "dy": 0, "notes": ""}'
        )
        img_b64 = self._encode_image(screenshot)
        raw = self._chat(self.fast_model, prompt, images=[img_b64])
        result = self._parse_json(raw)
        return {
            "on_target": result.get("on_target", False),
            "confidence": float(result.get("confidence", 0.5)),
            "dx": result.get("dx", 0),
            "dy": result.get("dy", 0),
            "notes": result.get("notes", ""),
        }

    def verify_result(self, before: bytes, after: bytes, expected: str) -> dict:
        """Backward-compatible verify (delegates to verify_action)."""
        result = self.verify_action(before, after, expected)
        return {
            "success": result["success"],
            "confidence": result["confidence"],
            "changes": result["changes"],
            "notes": "",
        }

    def ask(self, screenshot: bytes, prompt: str) -> str:
        """Free-form question about a screenshot (backward compat)."""
        img_b64 = self._encode_image(screenshot)
        return self._chat(self.fast_model, prompt, images=[img_b64])

    def extract_url(self, screenshot: bytes) -> str | None:
        """Extract the current browser URL from a screenshot."""
        prompt = (
            "What is the current URL in the browser address bar? "
            'Respond with JSON only: {"url": "<url or null>"}'
        )
        img_b64 = self._encode_image(screenshot)
        raw = self._chat(self.fast_model, prompt, images=[img_b64])
        result = self._parse_json(raw)
        url = result.get("url")
        return url if url and url != "null" else None


# ======================================================================
# Context-aware frame description helper
# ======================================================================


def describe_frame_with_context(
    vision: VisionModule,
    frame: bytes,
    recent_descriptions: list[str],
) -> str:
    """Describe a frame with awareness of previous scene state.

    Wraps VisionModule.describe_frame by injecting the last 3 descriptions
    as context so the model focuses on meaningful changes rather than
    re-describing the entire scene.

    Args:
        vision: A VisionModule instance.
        frame: Raw PNG bytes of the frame to describe.
        recent_descriptions: The most recent descriptions (up to last 3).

    Returns:
        Plain text description string.
    """
    # Use at most the last 3 descriptions for context
    context = recent_descriptions[-3:] if recent_descriptions else []

    if not context:
        # No prior context — detailed activity-focused prompt
        prompt = (
            "You are a live video activity narrator. Describe what is happening:\n"
            "- How many people/animals/vehicles are visible?\n"
            "- What is each one doing? (walking, running, playing, standing, etc.)\n"
            "- Where are they in the frame? (foreground, background, left, right)\n"
            "- What direction are they moving?\n"
            "If there are NO people, animals, or vehicles and nothing is moving, "
            "respond with exactly: NO_ACTIVITY\n"
            "Be specific and concise. 2-3 sentences max. Plain text only."
        )
        img_b64 = vision._encode_image(frame)
        text = vision._chat(vision.fast_model, prompt, images=[img_b64],
                            timeout=120)
        return text.strip() if text else ""

    # Build context block from recent descriptions
    context_block = "\n".join(
        f"  {i + 1}. {desc}" for i, desc in enumerate(context)
    )

    prompt = (
        "You are a live video activity narrator. Previous updates:\n"
        f"{context_block}\n\n"
        "Now describe what changed in this frame:\n"
        "- How many people/animals/vehicles are visible now?\n"
        "- What is each one doing differently from before?\n"
        "- Did anyone new appear or leave the frame?\n"
        "- Did their position or action change?\n"
        "Do NOT repeat what was already said. Only describe changes and new details. "
        "If nothing changed at all, respond with exactly: NO_ACTIVITY\n"
        "Be specific and concise. 2-3 sentences max. Plain text only."
    )

    img_b64 = vision._encode_image(frame)
    text = vision._chat(vision.fast_model, prompt, images=[img_b64],
                        timeout=120)
    return text.strip() if text else ""


# ======================================================================
# Module-level accessors (preserved from original)
# ======================================================================

_vision: VisionModule | None = None


def init_vision(config: dict):
    """Initialize the global VisionModule singleton."""
    global _vision
    _vision = VisionModule(config)


def get_vision() -> VisionModule:
    """Return the global VisionModule instance."""
    return _vision
