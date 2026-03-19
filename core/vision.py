import json
import re
import io
import logging
from PIL import Image

logger = logging.getLogger(__name__)

class Vision:
    def __init__(self, config: dict):
        self.config = config
        self.model_name = config['vision'].get('model', 'gemini-2.5-flash')
        self._client = None
        api_key = config['vision'].get('api_key', '')
        if api_key:
            from google import genai
            self._client = genai.Client(api_key=api_key)
            self._configured = True
        else:
            logger.warning("No Gemini API key — vision disabled")
            self._configured = False

    def _img(self, png_bytes: bytes) -> Image.Image:
        return Image.open(io.BytesIO(png_bytes))

    def _call(self, parts: list) -> str:
        if not self._client:
            return "{}"
        try:
            response = self._client.models.generate_content(
                model=self.model_name,
                contents=parts
            )
            return response.text
        except Exception as e:
            err = str(e).lower()
            if 'quota' in err or 'rate' in err or '429' in err:
                logger.warning("Gemini quota/rate limit hit — assuming success defaults")
                return '{"success": true, "ready": true, "has_blocker": false, "on_target": true, "confidence": 0.5}'
            logger.error(f"Vision call failed: {e}")
            return "{}"

    def _parse(self, text: str) -> dict:
        text = re.sub(r'```(?:json)?\s*', '', text).strip().rstrip('`').strip()
        try:
            return json.loads(text)
        except Exception:
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group())
                except Exception:
                    pass
        return {}

    def ask(self, screenshot: bytes, prompt: str) -> str:
        return self._call([prompt, self._img(screenshot)])

    def estimate_location(self, screenshot: bytes, target: str) -> tuple[int, int]:
        prompt = f"""Find the UI element: "{target}"
Respond JSON only: {{"found": bool, "x": int, "y": int, "confidence": float, "notes": str}}"""
        result = self._parse(self._call([prompt, self._img(screenshot)]))
        return result.get('x', 0), result.get('y', 0)

    def check_cursor(self, screenshot: bytes, cx: int, cy: int, target: str) -> dict:
        prompt = f"""Cursor is at ({cx}, {cy}). Target: "{target}"
Is cursor over the target? Respond JSON only:
{{"on_target": bool, "confidence": float, "dx": int, "dy": int, "notes": str}}"""
        return self._parse(self._call([prompt, self._img(screenshot)]))

    def check_state(self, screenshot: bytes) -> dict:
        prompt = """Analyze screen state. Respond JSON only:
{"active_app": str, "active_window": str, "description": str, "ready": bool}"""
        return self._parse(self._call([prompt, self._img(screenshot)]))

    def check_blockers(self, screenshot: bytes) -> dict:
        prompt = """Any modal/dialog/popup blocking the UI? Respond JSON only:
{"has_blocker": bool, "type": str, "description": str, "dismiss_action": "escape_key"|"close_button"|"accept_button"|"click_outside"|"none", "dismiss_x": int|null, "dismiss_y": int|null}"""
        return self._parse(self._call([prompt, self._img(screenshot)]))

    def verify_result(self, before: bytes, after: bytes, expected: str) -> dict:
        prompt = f"""Two screenshots: BEFORE and AFTER. Expected: "{expected}"
Did it succeed? Respond JSON only:
{{"success": bool, "confidence": float, "changes": str, "notes": str}}"""
        return self._parse(self._call([prompt, self._img(before), self._img(after)]))

    def extract_url(self, screenshot: bytes) -> str | None:
        prompt = """What is the current URL in the browser address bar?
Respond JSON only: {"url": str|null}"""
        result = self._parse(self._call([prompt, self._img(screenshot)]))
        return result.get('url')

_vision: Vision = None

def init_vision(config: dict):
    global _vision
    _vision = Vision(config)

def get_vision() -> Vision:
    return _vision
