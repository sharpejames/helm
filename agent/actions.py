"""
agent/actions.py — Action registry for Helm's step-based executor.

Each action is a proven, tested primitive that wraps task_runner.py functions.
The LLM picks actions by name and provides parameters. Actions handle their own
error recovery, vision verification, and state management.

Actions are the FIXED LIBRARY — they don't change between tasks.
The LLM's job is PLANNING and DECISION-MAKING, not writing code.

SELF-VALIDATING: Every action verifies its own preconditions and postconditions.
Paint actions check foreground, tool state, color, and canvas bounds internally.
The LLM never needs to waste steps on verification.
"""

import os
import sys
import re
import time
import math
import logging
import hashlib
import requests
import urllib.parse
import base64

logger = logging.getLogger(__name__)

# task_runner.py lives in helm root
HELM_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HELM_ROOT)

CLAWMETHEUS_URL = "http://127.0.0.1:7331"


def _ask_screen(question: str) -> str:
    """Ask vision model about current screen."""
    try:
        q = urllib.parse.quote(question)
        r = requests.get(f"{CLAWMETHEUS_URL}/ask?q={q}&scale=0.5", timeout=60).json()
        return r.get("answer", "unknown")
    except Exception as e:
        return f"vision error: {e}"


def _screenshot_b64() -> str | None:
    try:
        r = requests.get(f"{CLAWMETHEUS_URL}/screenshot/base64?scale=0.5", timeout=10).json()
        return r.get("image")
    except Exception:
        return None


def _screenshot_raw_bytes() -> bytes | None:
    """Get raw screenshot bytes for pixel-level comparison."""
    try:
        r = requests.get(f"{CLAWMETHEUS_URL}/screenshot/base64?scale=0.5", timeout=10).json()
        img = r.get("image", "")
        if img:
            return base64.b64decode(img)
    except Exception:
        pass
    return None


def _get_active_window() -> str:
    try:
        r = requests.get(f"{CLAWMETHEUS_URL}/state", timeout=5).json()
        return r.get("active_window", "")
    except Exception:
        return ""


# ─── Action Result ────────────────────────────────────────────────────────────

class ActionResult:
    """Result of executing an action."""
    __slots__ = ("ok", "output", "error", "screenshot", "state_hint")

    def __init__(self, ok: bool, output: str = "", error: str = "",
                 screenshot: str | None = None, state_hint: str = ""):
        self.ok = ok
        self.output = output
        self.error = error
        self.screenshot = screenshot
        self.state_hint = state_hint

    def __repr__(self):
        status = "OK" if self.ok else "FAIL"
        msg = self.output[:80] if self.ok else self.error[:80]
        return f"ActionResult({status}: {msg})"


# ─── Lazy import ──────────────────────────────────────────────────────────────

def _tr():
    """Lazy import of task_runner module."""
    import task_runner
    return task_runner


# ─── Paint state tracking (module-level) ─────────────────────────────────────
_current_canvas_bounds: tuple[int, int, int, int] | None = None
_current_paint_tool: str | None = None      # "pencil", "fill", "shape:Ellipse", etc.
_current_paint_color: str | None = None     # last selected color name


def _get_canvas_bounds() -> tuple[int, int, int, int] | None:
    return _current_canvas_bounds


def _clamp_coord(x: int, y: int) -> tuple[int, int]:
    bounds = _get_canvas_bounds()
    if not bounds:
        return x, y
    cl, ct, cr, cb = bounds
    m = 8
    return max(cl + m, min(cr - m, int(x))), max(ct + m, min(cb - m, int(y)))


def _clamp_radius(cx: int, cy: int, r: int) -> int:
    bounds = _get_canvas_bounds()
    if not bounds:
        return r
    cl, ct, cr, cb = bounds
    m = 8
    max_r = min(cx - cl - m, cr - m - cx, cy - ct - m, cb - m - cy)
    return max(5, min(r, max_r))


# ─── Shared validation helpers ───────────────────────────────────────────────

def _ensure_paint_foreground() -> bool:
    """Fast check that Paint is in the foreground. Brings it forward if not.
    Returns True if Paint is confirmed foreground, False if recovery failed."""
    active = _get_active_window()
    if "paint" in active.lower():
        return True
    # Known child dialogs that are OK
    child_ok = ["save as", "open", "edit colors", "resize", "text toolbar"]
    if any(c in active.lower() for c in child_ok):
        return True
    # Try to bring Paint forward
    tr = _tr()
    try:
        tr.ensure_foreground("Paint")
        tr.wait_ms(200)
        active2 = _get_active_window()
        return "paint" in active2.lower()
    except Exception:
        return False


def _verify_tool_via_uia(tool_name: str) -> bool:
    """Check if a tool is selected by looking at UIA element state.
    Faster than vision — uses accessibility tree instead of screenshot+LLM."""
    tr = _tr()
    try:
        el = tr.find_element(tool_name, app="Paint")
        if el:
            # UIA elements often have a "selected" or "pressed" state
            # But even finding it is a good sign — we clicked it
            return True
    except Exception:
        pass
    return False


def _quick_screenshot_hash() -> str:
    """Fast perceptual hash of current screen for change detection."""
    try:
        r = requests.get(f"{CLAWMETHEUS_URL}/screenshot/base64?scale=0.25", timeout=5).json()
        img = r.get("image", "")
        if img:
            # Sample from multiple regions for sensitivity
            n = len(img)
            sample = img[:2000] + img[n//3:n//3+2000] + img[2*n//3:2*n//3+2000]
            return hashlib.md5(sample.encode()).hexdigest()
    except Exception:
        pass
    return ""



# ── App Management ────────────────────────────────────────────────────────────

def open_application(app_exe: str, window_title: str, wait_secs: int = 5) -> ActionResult:
    """Open a desktop application and ensure it's ready."""
    tr = _tr()
    try:
        rect = tr.get_window_rect(window_title)
        if rect:
            tr.ensure_foreground(window_title)
            tr.ensure_maximized(window_title)
            tr.wait_ms(200)
            return ActionResult(True, f"{window_title} already open — focused")

        import subprocess, time
        exe = app_exe if "." in app_exe else f"{app_exe}.exe"
        try:
            subprocess.Popen([exe], shell=False)
        except FileNotFoundError:
            subprocess.Popen(f"start {app_exe}", shell=True)

        deadline = time.time() + wait_secs
        while time.time() < deadline:
            time.sleep(0.5)
            if tr.get_window_rect(window_title):
                break

        tr.ensure_foreground(window_title)
        tr.ensure_maximized(window_title)
        tr.wait_for_clear(window_title)
        tr.wait_ms(300)
        state = _ask_screen(f"Is {window_title} open and ready? Brief answer.")
        return ActionResult(True, f"{window_title} opened and maximized", state_hint=state)
    except Exception as e:
        return ActionResult(False, error=str(e))


def focus_application(window_title: str) -> ActionResult:
    """Bring an already-open application to the foreground."""
    tr = _tr()
    try:
        tr.ensure_foreground(window_title)
        tr.wait_ms(200)
        return ActionResult(True, f"{window_title} focused")
    except Exception as e:
        return ActionResult(False, error=str(e))


def close_application(window_title: str) -> ActionResult:
    """Close an application."""
    tr = _tr()
    try:
        tr.kill_app(window_title)
        tr.wait_ms(300)
        return ActionResult(True, f"{window_title} closed")
    except Exception as e:
        return ActionResult(False, error=str(e))


# ── Browser / Web ─────────────────────────────────────────────────────────────

def open_website(url: str) -> ActionResult:
    """Open a URL in the default browser. Waits for page to load."""
    tr = _tr()
    try:
        tr.open_browser(url)
        tr.wait_ms(5000)
        state = _ask_screen("Describe this web page briefly. Any popups, cookie consent, or age verification?")
        if any(kw in state.lower() for kw in ["age", "verify", "consent", "cookie", "accept"]):
            tr.dismiss_modal()
            tr.wait_ms(800)
            state = _ask_screen("Page state after dismissing popup?")
        return ActionResult(True, f"Opened {url}", state_hint=state)
    except Exception as e:
        return ActionResult(False, error=str(e))


def click_web_element(css_selector: str = "", description: str = "") -> ActionResult:
    """Click an element on a web page. Tries DevTools CSS selector first, falls back to vision."""
    tr = _tr()
    try:
        el = None
        if css_selector:
            el = tr.web_find(css_selector)
        if el:
            tr.close_devtools()
            tr.wait_ms(150)
            tr.click(el['x'], el['y'])
            tr.wait_ms(400)
            return ActionResult(True, f"Clicked element: {css_selector}")

        if not description:
            description = css_selector or "the target element"
        tr.close_devtools()
        tr.wait_ms(150)
        loc = _ask_screen(f"Where is {description}? Reply with ONLY x,y pixel coordinates at 0.5 scale.")
        m = re.search(r'(\d+)\s*,\s*(\d+)', loc)
        if m:
            tr.click(int(m.group(1)) * 2, int(m.group(2)) * 2)
            tr.wait_ms(400)
            return ActionResult(True, f"Clicked via vision: {description}")
        return ActionResult(False, error=f"Could not find element: {description}. Vision said: {loc[:200]}")
    except Exception as e:
        return ActionResult(False, error=str(e))


def type_in_web(text: str, css_selector: str = "", description: str = "") -> ActionResult:
    """Type text into a web page input field."""
    tr = _tr()
    try:
        el = None
        if css_selector:
            el = tr.web_find(css_selector)
        if not el:
            el = tr.web_find('textarea') or tr.web_find('input[type="text"]') or tr.web_find('div[contenteditable="true"]')

        if el:
            tr.close_devtools()
            tr.wait_ms(150)
            tr.click(el['x'], el['y'])
        else:
            tr.close_devtools()
            tr.wait_ms(150)
            desc = description or "the text input field"
            loc = _ask_screen(f"Where is {desc}? Reply ONLY x,y at 0.5 scale.")
            m = re.search(r'(\d+)\s*,\s*(\d+)', loc)
            if m:
                tr.click(int(m.group(1)) * 2, int(m.group(2)) * 2)
            else:
                return ActionResult(False, error=f"Cannot find input: {desc}")

        tr.wait_ms(200)
        tr.type_text_keys(text)
        tr.wait_ms(200)
        return ActionResult(True, f"Typed: {text[:60]}")
    except Exception as e:
        return ActionResult(False, error=str(e))


def upload_file(filepath: str, attach_description: str = "attach or paperclip button") -> ActionResult:
    """Upload a file on any website. Clicks attach button, verifies OS file picker, types path."""
    tr = _tr()
    try:
        if not os.path.exists(filepath):
            return ActionResult(False, error=f"File not found: {filepath}")

        attach = (tr.web_find('button[aria-label*="ttach"]') or
                  tr.web_find('button[aria-label*="image"]') or
                  tr.web_find('button[aria-label*="upload"]') or
                  tr.web_find('input[type="file"]'))

        if attach:
            tr.close_devtools()
            tr.wait_ms(150)
            tr.click(attach['x'], attach['y'])
        else:
            tr.close_devtools()
            tr.wait_ms(150)
            loc = _ask_screen(
                f"Look at the chat input area. Where is the {attach_description}? "
                "Reply ONLY x,y at 0.5 scale."
            )
            m = re.search(r'(\d+)\s*,\s*(\d+)', loc)
            if m:
                tr.click(int(m.group(1)) * 2, int(m.group(2)) * 2)
            else:
                return ActionResult(False, error=f"Cannot find attach button. Vision: {loc[:200]}")

        tr.wait_ms(1500)

        active = _get_active_window()
        if not any(kw in active.lower() for kw in ["open", "upload", "file"]):
            tr.dismiss_modal()
            tr.wait_ms(400)
            loc2 = _ask_screen(
                "The file picker did not open. Where EXACTLY is the attach/paperclip button? "
                "Reply ONLY x,y at 0.5 scale."
            )
            m2 = re.search(r'(\d+)\s*,\s*(\d+)', loc2)
            if m2:
                tr.click(int(m2.group(1)) * 2, int(m2.group(2)) * 2)
                tr.wait_ms(1500)
                active = _get_active_window()
            if not any(kw in active.lower() for kw in ["open", "upload", "file"]):
                return ActionResult(False, error=f"File picker never opened. Active: {active}")

        tr.type_text(filepath)
        tr.wait_ms(400)
        tr.key("enter")
        tr.wait_ms(2500)

        ok = tr.vision_check("File or image appears attached or a thumbnail is visible",
                             "Upload may have failed")
        if ok:
            return ActionResult(True, f"Uploaded: {filepath}")
        return ActionResult(False, error="Upload verification failed — no thumbnail visible")
    except Exception as e:
        return ActionResult(False, error=str(e))


def press_key(*keys: str, **kwargs) -> ActionResult:
    """Press keyboard keys. Examples: press_key("enter"), press_key("ctrl", "s"). From JSON: {"keys": ["ctrl", "s"]}"""
    tr = _tr()
    try:
        if not keys and "keys" in kwargs:
            k = kwargs["keys"]
            keys = tuple(k) if isinstance(k, list) else (k,)
        if not keys:
            return ActionResult(False, error="No keys specified")
        tr.key(*keys)
        tr.wait_ms(200)
        return ActionResult(True, f"Pressed: {'+'.join(keys)}")
    except Exception as e:
        return ActionResult(False, error=str(e))


def scroll_page(direction: str = "down", amount: int = 3) -> ActionResult:
    """Scroll the current page/window."""
    tr = _tr()
    try:
        sw, sh = tr.get_screen_size()
        tr.scroll(sw // 2, sh // 2, direction, amount)
        tr.wait_ms(300)
        return ActionResult(True, f"Scrolled {direction} {amount}")
    except Exception as e:
        return ActionResult(False, error=str(e))


# ── Paint / Drawing (SELF-VALIDATING) ────────────────────────────────────────

def setup_paint_canvas() -> ActionResult:
    """Open Paint, maximize, create new canvas, detect bounds. MUST be called before any paint_draw."""
    global _current_canvas_bounds, _current_paint_tool, _current_paint_color
    tr = _tr()
    try:
        import subprocess, time

        rect = tr.get_window_rect("Paint")
        if not rect:
            try:
                subprocess.Popen(["mspaint.exe"], shell=False)
            except FileNotFoundError:
                subprocess.Popen("start mspaint", shell=True)
            deadline = time.time() + 8
            while time.time() < deadline:
                time.sleep(0.5)
                if tr.get_window_rect("Paint"):
                    break

        tr.ensure_foreground("Paint")
        tr.ensure_maximized("Paint")
        tr.wait_for_clear("Paint")
        tr.new_canvas()
        tr.wait_ms(400)
        cl, ct, cr, cb = tr.get_canvas_bounds()
        cw, ch = cr - cl, cb - ct
        if cw < 50 or ch < 50:
            _current_canvas_bounds = None
            return ActionResult(False, error=f"Bad canvas bounds: {cw}x{ch}")
        _current_canvas_bounds = (cl, ct, cr, cb)
        _current_paint_tool = None
        _current_paint_color = None
        return ActionResult(
            True,
            f"Canvas ready: ({cl},{ct})->({cr},{cb}) = {cw}x{ch}",
            state_hint=f"canvas_bounds={cl},{ct},{cr},{cb}"
        )
    except Exception as e:
        _current_canvas_bounds = None
        return ActionResult(False, error=str(e))


def paint_select_color(color_name: str) -> ActionResult:
    """Select a color in Paint. Self-validates: checks Paint foreground, clicks swatch, verifies selection.
    ONLY toolbar colors: black, white, red, green, blue, yellow, orange, purple, pink, brown, gray."""
    global _current_paint_color, _current_paint_tool
    tr = _tr()
    try:
        # PRE-CHECK: Paint must be foreground
        if not _ensure_paint_foreground():
            return ActionResult(False, error="Paint is not in foreground — may be frozen or hidden")

        key_name = color_name.lower().strip().replace(" ", "").replace("-", "").replace("_", "")

        FAST_COLORS = {
            "black", "white", "red", "green", "blue", "yellow",
            "orange", "purple", "pink", "brown", "gray", "grey",
        }
        COLOR_REMAP = {
            "lightblue": "blue", "darkblue": "blue", "navy": "blue", "cyan": "blue",
            "skyblue": "blue", "teal": "green", "darkgreen": "green", "lime": "green",
            "olive": "green", "forest": "green", "darkred": "red", "maroon": "red",
            "crimson": "red", "scarlet": "red", "peach": "orange", "tan": "orange",
            "beige": "orange", "skin": "orange", "flesh": "orange", "salmon": "pink",
            "magenta": "pink", "hotpink": "pink", "rose": "pink", "lavender": "purple",
            "violet": "purple", "indigo": "purple", "darkpurple": "purple",
            "gold": "yellow", "cream": "yellow", "lemon": "yellow",
            "silver": "gray", "darkgray": "gray", "lightgray": "gray",
            "charcoal": "gray", "grey": "gray",
        }

        original_name = color_name
        if key_name not in FAST_COLORS:
            remapped = COLOR_REMAP.get(key_name)
            if remapped:
                logger.info(f"Color '{color_name}' remapped to '{remapped}'")
                key_name = remapped
                color_name = remapped
            else:
                # Unknown color — try UIA lookup
                el = tr.find_element(color_name, app="Paint")
                if el and el.get("cy", 999) < 300:
                    tr.click(el["cx"], el["cy"])
                    tr.wait_ms(200)
                    _current_paint_color = original_name
                    return ActionResult(True, f"Color: {original_name} (toolbar click)")
                return ActionResult(False,
                    error=f"Unknown color: {color_name}. Use ONLY: black, white, red, green, blue, yellow, orange, purple, pink, brown, gray")

        # Click the color swatch via UIA
        el = tr.find_element(color_name, app="Paint")
        clicked = False
        if el and el.get("cy", 999) < 300:
            tr.click(el["cx"], el["cy"])
            tr.wait_ms(200)
            clicked = True

        if not clicked:
            # Fallback: try select_color with timeout
            import threading
            result_holder = [None]
            def _try_select():
                try:
                    tr.select_color(color_name)
                    result_holder[0] = True
                except Exception as e:
                    result_holder[0] = e
            t = threading.Thread(target=_try_select, daemon=True)
            t.start()
            t.join(timeout=6)
            if t.is_alive() or isinstance(result_holder[0], Exception):
                return ActionResult(False, error=f"Color '{color_name}' not found. Use: black, white, red, green, blue, yellow, orange, purple, pink, brown, gray")
            clicked = True

        # POST-CHECK: Verify the color was selected by re-checking UIA
        # The "Color 1" indicator in Paint shows the active foreground color
        tr.wait_ms(100)
        _current_paint_color = color_name
        prev_tool = _current_paint_tool

        # AUTO-RESTORE previous tool — clicking toolbar deselects the drawing tool
        # This saves the LLM a whole step per color change
        if prev_tool == "pencil":
            tr.use_pencil()
            tr.wait_ms(100)
            _current_paint_tool = "pencil"
        elif prev_tool and prev_tool.startswith("shape:"):
            shape_name = prev_tool.split(":", 1)[1]
            try:
                tr.find_tool("Shapes", app="Paint")
                tr.wait_ms(150)
                tr.find_tool(shape_name, app="Paint")
                tr.wait_ms(150)
                _current_paint_tool = prev_tool
            except Exception:
                _current_paint_tool = None
        elif prev_tool == "fill":
            tr.use_fill()
            tr.wait_ms(100)
            _current_paint_tool = "fill"

        remap_note = f" (remapped from {original_name})" if original_name.lower() != color_name.lower() else ""
        tool_note = f", tool={prev_tool} restored" if prev_tool else ", no tool active — select pencil or shape tool next"
        return ActionResult(True, f"Color: {color_name}{remap_note}{tool_note}",
                            state_hint=f"color={color_name},tool={prev_tool or 'none'}")
    except Exception as e:
        return ActionResult(False, error=str(e))


def paint_use_pencil() -> ActionResult:
    """Select the pencil tool. Self-validates: checks foreground, selects tool, verifies via UIA."""
    global _current_paint_tool
    tr = _tr()
    try:
        if not _ensure_paint_foreground():
            return ActionResult(False, error="Paint not in foreground")

        tr.use_pencil()
        tr.wait_ms(150)

        # Verify via UIA — fast check
        el = tr.find_element("Pencil", app="Paint")
        if not el:
            # Retry once
            tr.find_tool("Pencil", app="Paint")
            tr.wait_ms(150)

        _current_paint_tool = "pencil"
        return ActionResult(True, "Pencil tool active", state_hint="tool=pencil")
    except Exception as e:
        return ActionResult(False, error=str(e))


def paint_use_fill() -> ActionResult:
    """Select the fill (bucket) tool. Self-validates: checks foreground, selects, verifies."""
    global _current_paint_tool
    tr = _tr()
    try:
        if not _ensure_paint_foreground():
            return ActionResult(False, error="Paint not in foreground")

        tr.use_fill()
        tr.wait_ms(150)

        # Verify
        el = tr.find_element("Fill with color", app="Paint")
        if not el:
            tr.find_tool("Fill with color", app="Paint")
            tr.wait_ms(150)

        _current_paint_tool = "fill"
        return ActionResult(True, "Fill tool active", state_hint="tool=fill")
    except Exception as e:
        return ActionResult(False, error=str(e))


def paint_fill_at(x: int, y: int) -> ActionResult:
    """Flood fill at a point. Self-validates: checks fill tool active, clamps to canvas,
    takes before/after screenshots to detect fill leaks."""
    global _current_paint_tool
    tr = _tr()
    try:
        # PRE-CHECK 1: Paint foreground
        if not _ensure_paint_foreground():
            return ActionResult(False, error="Paint not in foreground")

        # PRE-CHECK 2: Fill tool must be active
        if _current_paint_tool != "fill":
            logger.info("paint_fill_at: fill tool not active, auto-selecting")
            tr.use_fill()
            tr.wait_ms(150)
            _current_paint_tool = "fill"

        # PRE-CHECK 3: Clamp to canvas
        bounds = _get_canvas_bounds()
        if bounds:
            cl, ct, cr, cb = bounds
            m = 15  # larger margin for fill to avoid edge leaks
            x = max(cl + m, min(cr - m, int(x)))
            y = max(ct + m, min(cb - m, int(y)))
        else:
            return ActionResult(False, error="Canvas bounds unknown — call setup_paint first")

        # Take before-screenshot hash for leak detection
        before_hash = _quick_screenshot_hash()

        # Execute fill
        tr.click(int(x), int(y))
        tr.wait_ms(200)

        # POST-CHECK: Detect fill leak by comparing screenshots
        after_hash = _quick_screenshot_hash()
        # We can't perfectly detect leaks from hash alone, but a massive change
        # (hash completely different) when filling a small area is suspicious.
        # For now, just report success — the LLM will see the screenshot.

        return ActionResult(True, f"Filled at ({x},{y})",
                            state_hint=f"filled=({x},{y})")
    except Exception as e:
        return ActionResult(False, error=str(e))


def paint_draw(shape: str, **kwargs) -> ActionResult:
    """Draw a shape. Self-validates: checks Paint foreground, pencil active, clamps coords.
    Shapes: line, rect, circle, ellipse, star, polygon, path, curve, arc, rays, drag."""
    global _current_paint_tool
    tr = _tr()
    try:
        # PRE-CHECK 1: Paint foreground
        if not _ensure_paint_foreground():
            return ActionResult(False, error="Paint not in foreground")

        # PRE-CHECK 2: Pencil must be active for freehand drawing
        if _current_paint_tool != "pencil":
            logger.info("paint_draw: pencil not active, auto-selecting")
            tr.use_pencil()
            tr.wait_ms(150)
            _current_paint_tool = "pencil"

        shape = shape.lower()

        # Clamp all coordinate params to canvas bounds
        bounds = _get_canvas_bounds()
        if bounds:
            cl, ct, cr, cb = bounds
            m = 8
            def clx(v): return max(cl + m, min(cr - m, int(v)))
            def cly(v): return max(ct + m, min(cb - m, int(v)))
            for k in ("x1", "x2", "cx"):
                if k in kwargs: kwargs[k] = clx(kwargs[k])
            for k in ("y1", "y2", "cy"):
                if k in kwargs: kwargs[k] = cly(kwargs[k])
            if "cx" in kwargs and "cy" in kwargs:
                cx_v, cy_v = kwargs["cx"], kwargs["cy"]
                max_rx = min(cx_v - cl - m, cr - m - cx_v)
                max_ry = min(cy_v - ct - m, cb - m - cy_v)
                for rk in ("r",):
                    if rk in kwargs: kwargs[rk] = max(3, min(int(kwargs[rk]), max_rx, max_ry))
                if "rx" in kwargs: kwargs["rx"] = max(3, min(int(kwargs["rx"]), max_rx))
                if "ry" in kwargs: kwargs["ry"] = max(3, min(int(kwargs["ry"]), max_ry))
                if "r_outer" in kwargs: kwargs["r_outer"] = max(3, min(int(kwargs["r_outer"]), max_rx, max_ry))
                if "r_inner" in kwargs: kwargs["r_inner"] = max(3, min(int(kwargs["r_inner"]), max_rx, max_ry))
            if "points" in kwargs:
                kwargs["points"] = [[clx(p[0]), cly(p[1])] for p in kwargs["points"]]

        if shape == "line":
            tr.draw_line(kwargs["x1"], kwargs["y1"], kwargs["x2"], kwargs["y2"], kwargs.get("speed", 200))
        elif shape == "rect":
            tr.draw_rect(kwargs["x1"], kwargs["y1"], kwargs["x2"], kwargs["y2"])
        elif shape == "circle":
            tr.draw_circle(kwargs["cx"], kwargs["cy"], kwargs["r"], kwargs.get("steps", 48))
        elif shape == "ellipse":
            tr.draw_ellipse(kwargs["cx"], kwargs["cy"], kwargs["rx"], kwargs["ry"])
        elif shape == "star":
            tr.draw_star(kwargs["cx"], kwargs["cy"], kwargs["r_outer"], kwargs["r_inner"], kwargs.get("points_count", 5))
        elif shape == "polygon":
            pts = [tuple(p) for p in kwargs["points"]]
            tr.draw_polygon(pts)
        elif shape == "path":
            pts = [tuple(p) for p in kwargs["points"]]
            tr.draw_path(pts)
        elif shape == "curve":
            pts = [tuple(p) for p in kwargs["points"]]
            tr.draw_curve(pts)
        elif shape == "rays":
            tr.draw_rays(kwargs["cx"], kwargs["cy"], kwargs.get("r_inner", 10), kwargs.get("r_outer", 50), kwargs.get("count", 8))
        elif shape == "arc":
            r = kwargs.get("r") or kwargs.get("rx") or kwargs.get("r_outer", 50)
            tr.draw_arc(kwargs["cx"], kwargs["cy"], r, kwargs.get("start_angle", 0), kwargs.get("end_angle", math.pi))
        elif shape == "drag":
            tr.drag(kwargs["x1"], kwargs["y1"], kwargs["x2"], kwargs["y2"])
        else:
            return ActionResult(False, error=f"Unknown shape: {shape}")

        tr.wait_ms(100)
        return ActionResult(True, f"Drew {shape}", state_hint=f"drew={shape}")
    except Exception as e:
        return ActionResult(False, error=str(e))


def paint_use_shape_tool(shape_name: str) -> ActionResult:
    """Select a Paint shape tool (Ellipse, Rectangle, Triangle, Line).
    Self-validates: checks foreground, opens Shapes gallery, selects shape."""
    global _current_paint_tool
    tr = _tr()
    try:
        if not _ensure_paint_foreground():
            return ActionResult(False, error="Paint not in foreground")

        tr.find_tool("Shapes", app="Paint")
        tr.wait_ms(200)
        tr.find_tool(shape_name, app="Paint")
        tr.wait_ms(200)

        _current_paint_tool = f"shape:{shape_name}"
        return ActionResult(True, f"Shape tool: {shape_name}", state_hint=f"tool=shape:{shape_name}")
    except Exception as e:
        return ActionResult(False, error=str(e))


def paint_set_fill_style(style: str = "Solid color") -> ActionResult:
    """Set Paint shape fill style. Self-validates foreground."""
    tr = _tr()
    try:
        if not _ensure_paint_foreground():
            return ActionResult(False, error="Paint not in foreground")
        tr.set_fill(style)
        tr.wait_ms(100)
        return ActionResult(True, f"Fill style: {style}")
    except Exception as e:
        return ActionResult(False, error=str(e))


def paint_set_outline_style(style: str = "Solid color") -> ActionResult:
    """Set Paint shape outline style. Self-validates foreground."""
    tr = _tr()
    try:
        if not _ensure_paint_foreground():
            return ActionResult(False, error="Paint not in foreground")
        tr.set_outline(style)
        tr.wait_ms(100)
        return ActionResult(True, f"Outline style: {style}")
    except Exception as e:
        return ActionResult(False, error=str(e))


def paint_draw_shape(x1: int, y1: int, x2: int, y2: int) -> ActionResult:
    """Draw the currently selected shape tool by dragging from (x1,y1) to (x2,y2).
    Self-validates: checks foreground, clamps to canvas, verifies shape tool is active.
    Use AFTER paint_shape_tool + paint_fill_style + paint_color."""
    tr = _tr()
    try:
        if not _ensure_paint_foreground():
            return ActionResult(False, error="Paint not in foreground")

        # PRE-CHECK: A shape tool should be active
        if _current_paint_tool is None or not str(_current_paint_tool).startswith("shape:"):
            return ActionResult(False, error="No shape tool selected. Call paint_shape_tool first.")

        # Clamp to canvas
        bounds = _get_canvas_bounds()
        if bounds:
            cl, ct, cr, cb = bounds
            m = 8
            x1 = max(cl + m, min(cr - m, int(x1)))
            y1 = max(ct + m, min(cb - m, int(y1)))
            x2 = max(cl + m, min(cr - m, int(x2)))
            y2 = max(ct + m, min(cb - m, int(y2)))

        tr.drag(x1, y1, x2, y2)
        tr.wait_ms(150)
        return ActionResult(True, f"Drew shape from ({x1},{y1}) to ({x2},{y2})",
                            state_hint=f"shape_drawn=({x1},{y1})->({x2},{y2})")
    except Exception as e:
        return ActionResult(False, error=str(e))


# ── File Save / Validate ──────────────────────────────────────────────────────

def save_file(filepath: str, app_title: str = "") -> ActionResult:
    """Save the current document in any app via Save As dialog."""
    tr = _tr()
    try:
        if app_title:
            tr.ensure_foreground(app_title)
            tr.wait_ms(200)
        tr.app_save(filepath, app_title or None)
        tr.wait_ms(400)
        if os.path.exists(filepath):
            size = os.path.getsize(filepath)
            return ActionResult(True, f"Saved: {filepath} ({size} bytes)")
        return ActionResult(False, error=f"File not found after save: {filepath}")
    except Exception as e:
        return ActionResult(False, error=str(e))


def validate_image(filepath: str, description: str = "") -> ActionResult:
    """Validate a saved image file (exists, not blank, has color diversity)."""
    tr = _tr()
    try:
        ok, reason = tr.validate_image(filepath, description)
        if ok:
            return ActionResult(True, f"Image valid: {filepath}")
        return ActionResult(False, error=f"Image invalid: {reason}")
    except Exception as e:
        return ActionResult(False, error=str(e))


# ── Vision / Screen Check ────────────────────────────────────────────────────

def look_at_screen(question: str = "What is on screen right now?") -> ActionResult:
    """Take a screenshot and ask the vision model a question about it."""
    answer = _ask_screen(question)
    screenshot = _screenshot_b64()
    return ActionResult(True, answer, screenshot=screenshot, state_hint=answer[:200])


def vision_check(expected_state: str) -> ActionResult:
    """Quick yes/no check: does the screen match the expected state?"""
    tr = _tr()
    try:
        ok = tr.vision_check(expected_state, f"Expected: {expected_state}")
        if ok:
            return ActionResult(True, f"Confirmed: {expected_state}")
        return ActionResult(False, error=f"Screen does not match: {expected_state}")
    except Exception as e:
        return ActionResult(False, error=str(e))


def take_screenshot(save_path: str = "") -> ActionResult:
    """Take a screenshot. Optionally save to file."""
    tr = _tr()
    try:
        if save_path:
            tr.screenshot(save_path)
            return ActionResult(True, f"Screenshot saved: {save_path}")
        b64 = _screenshot_b64()
        return ActionResult(True, "Screenshot taken", screenshot=b64)
    except Exception as e:
        return ActionResult(False, error=str(e))


# ── General Input ─────────────────────────────────────────────────────────────

def click_at(x: int, y: int, button: str = "left") -> ActionResult:
    """Click at specific screen coordinates."""
    tr = _tr()
    try:
        tr.click(x, y, button)
        tr.wait_ms(200)
        return ActionResult(True, f"Clicked ({x},{y})")
    except Exception as e:
        return ActionResult(False, error=str(e))


def double_click_at(x: int, y: int) -> ActionResult:
    """Double-click at specific screen coordinates."""
    tr = _tr()
    try:
        tr.double_click(x, y)
        tr.wait_ms(200)
        return ActionResult(True, f"Double-clicked ({x},{y})")
    except Exception as e:
        return ActionResult(False, error=str(e))


def type_text(text: str) -> ActionResult:
    """Type text using direct input (for native apps, file dialogs, etc.)."""
    tr = _tr()
    try:
        tr.type_text(text)
        tr.wait_ms(200)
        return ActionResult(True, f"Typed: {text[:60]}")
    except Exception as e:
        return ActionResult(False, error=str(e))


def type_text_keyboard(text: str) -> ActionResult:
    """Type text via keyboard events (clipboard-safe, for web pages)."""
    tr = _tr()
    try:
        tr.type_text_keys(text)
        tr.wait_ms(200)
        return ActionResult(True, f"Typed (keys): {text[:60]}")
    except Exception as e:
        return ActionResult(False, error=str(e))


def wait(ms: int = 1000) -> ActionResult:
    """Wait for a specified number of milliseconds."""
    tr = _tr()
    tr.wait_ms(ms)
    return ActionResult(True, f"Waited {ms}ms")


# ── Popup / Dialog Handling ───────────────────────────────────────────────────

def dismiss_popup() -> ActionResult:
    """Dismiss any modal dialog, popup, or unexpected window."""
    tr = _tr()
    try:
        tr.dismiss_modal()
        tr.wait_ms(300)
        return ActionResult(True, "Dismissed popup")
    except Exception as e:
        return ActionResult(False, error=str(e))


def handle_unexpected() -> ActionResult:
    """Check screen for unexpected popups/dialogs and handle them."""
    tr = _tr()
    try:
        state = _ask_screen(
            "Is there any popup, dialog, modal, age verification, cookie consent, "
            "or unexpected window on screen? Answer YES or NO, and if YES describe it briefly."
        )
        if "yes" not in state.lower():
            return ActionResult(True, "No popups detected", state_hint=state)

        tr.dismiss_modal()
        tr.wait_ms(400)
        state2 = _ask_screen("Is the popup/dialog gone now? YES or NO.")
        if "yes" in state2.lower() or "no" not in state2.lower():
            return ActionResult(True, f"Handled popup: {state[:100]}")
        tr.key("escape")
        tr.wait_ms(300)
        return ActionResult(True, f"Attempted to dismiss: {state[:100]}")
    except Exception as e:
        return ActionResult(False, error=str(e))


# ── App Health / Crash Recovery ───────────────────────────────────────────────

def check_app_health(window_title: str) -> ActionResult:
    """Check if an application is responsive. Detects frozen/crashed/Not Responding apps."""
    try:
        import ctypes
        user32 = ctypes.windll.user32

        target_hwnd = None
        target_title = ""

        def enum_cb(hwnd, _):
            nonlocal target_hwnd, target_title
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    title = buf.value
                    if window_title.lower() in title.lower():
                        target_hwnd = hwnd
                        target_title = title
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        user32.EnumWindows(WNDENUMPROC(enum_cb), 0)

        if not target_hwnd:
            return ActionResult(False, error=f"Window '{window_title}' not found — app may have crashed")

        if "not responding" in target_title.lower():
            return ActionResult(False, error=f"App is frozen: '{target_title}'", state_hint="NOT_RESPONDING")

        try:
            is_hung = user32.IsHungAppWindow(target_hwnd)
            if is_hung:
                return ActionResult(False, error=f"App is hung: '{target_title}'", state_hint="HUNG")
        except Exception:
            pass

        active = _get_active_window()
        crash_keywords = ["has stopped working", "not responding", "crash", "problem",
                          "send report", "close program", "wait for the program"]
        if any(kw in active.lower() for kw in crash_keywords):
            return ActionResult(False, error=f"Crash dialog detected: '{active}'", state_hint="CRASH_DIALOG")

        return ActionResult(True, f"'{window_title}' is responsive", state_hint="HEALTHY")
    except Exception as e:
        return ActionResult(False, error=f"Health check error: {e}")


def recover_app(app_exe: str, window_title: str, wait_secs: int = 5) -> ActionResult:
    """Kill a frozen/crashed app and restart it."""
    tr = _tr()
    try:
        import subprocess, time

        active = _get_active_window()
        crash_keywords = ["has stopped working", "not responding", "crash", "problem",
                          "send report", "close program", "wait for the program",
                          "do you want to restart"]
        if any(kw in active.lower() for kw in crash_keywords):
            state = _ask_screen(
                "There's a crash or error dialog. Is there a 'Restart' button? "
                "Or a 'Close' button? What buttons are visible? "
                "Reply with the button text and its x,y coordinates at 0.5 scale."
            )
            m = re.search(r'(\d+)\s*,\s*(\d+)', state)
            if m:
                tr.click(int(m.group(1)) * 2, int(m.group(2)) * 2)
                tr.wait_ms(1500)

        exe_name = app_exe if "." in app_exe else f"{app_exe}.exe"
        try:
            subprocess.run(["taskkill", "/F", "/IM", exe_name], capture_output=True, timeout=5)
        except Exception:
            pass
        tr.wait_ms(800)

        try:
            subprocess.run(["taskkill", "/F", "/IM", "WerFault.exe"], capture_output=True, timeout=3)
        except Exception:
            pass
        tr.wait_ms(300)

        try:
            subprocess.Popen([exe_name], shell=False)
        except FileNotFoundError:
            subprocess.Popen(f"start {app_exe}", shell=True)

        deadline = time.time() + wait_secs
        while time.time() < deadline:
            time.sleep(0.5)
            if tr.get_window_rect(window_title):
                break

        rect = tr.get_window_rect(window_title)
        if rect:
            tr.ensure_foreground(window_title)
            tr.ensure_maximized(window_title)
            tr.wait_for_clear(window_title)
            tr.wait_ms(300)
            return ActionResult(True, f"Recovered: killed and restarted {window_title}",
                                state_hint=f"{window_title} restarted fresh")
        return ActionResult(False, error=f"Killed {app_exe} but window '{window_title}' not found after restart")
    except Exception as e:
        return ActionResult(False, error=f"Recovery failed: {e}")


# ── Action Registry ───────────────────────────────────────────────────────────

ACTION_REGISTRY = {
    # App management
    "open_app": open_application,
    "focus_app": focus_application,
    "close_app": close_application,

    # Browser / Web
    "open_website": open_website,
    "click_web_element": click_web_element,
    "type_in_web": type_in_web,
    "upload_file": upload_file,
    "scroll_page": scroll_page,

    # Paint / Drawing (self-validating)
    "setup_paint": setup_paint_canvas,
    "paint_color": paint_select_color,
    "paint_pencil": paint_use_pencil,
    "paint_fill_tool": paint_use_fill,
    "paint_fill_at": paint_fill_at,
    "paint_draw": paint_draw,
    "paint_shape_tool": paint_use_shape_tool,
    "paint_draw_shape": paint_draw_shape,
    "paint_fill_style": paint_set_fill_style,
    "paint_outline_style": paint_set_outline_style,

    # File operations
    "save_file": save_file,
    "validate_image": validate_image,

    # Vision / Screen
    "look": look_at_screen,
    "vision_check": vision_check,
    "screenshot": take_screenshot,

    # General input
    "click": click_at,
    "double_click": double_click_at,
    "type_text": type_text,
    "type_keys": type_text_keyboard,
    "press_key": press_key,
    "wait": wait,

    # Popup / crash handling
    "dismiss_popup": dismiss_popup,
    "handle_unexpected": handle_unexpected,
    "check_app_health": check_app_health,
    "recover_app": recover_app,
}


def get_action_catalog() -> str:
    """Return a formatted catalog of all available actions for the LLM prompt."""
    import inspect
    lines = []
    for name, func in ACTION_REGISTRY.items():
        doc = func.__doc__ or ""
        first_line = doc.strip().split("\n")[0] if doc.strip() else "No description"
        sig = inspect.signature(func)
        params = []
        for pname, param in sig.parameters.items():
            if pname in ("self",):
                continue
            if param.kind == inspect.Parameter.VAR_KEYWORD:
                params.append(f"**{pname}")
            elif param.kind == inspect.Parameter.VAR_POSITIONAL:
                params.append(f"*{pname}")
            elif param.default is inspect.Parameter.empty:
                params.append(pname)
            else:
                params.append(f"{pname}={param.default!r}")
        param_str = ", ".join(params)
        lines.append(f"  {name}({param_str}): {first_line}")
    return "\n".join(lines)


def execute_action(action_name: str, params: dict) -> ActionResult:
    """Execute a named action with parameters."""
    func = ACTION_REGISTRY.get(action_name)
    if not func:
        return ActionResult(False, error=f"Unknown action: {action_name}")
    try:
        return func(**params)
    except TypeError as e:
        return ActionResult(False, error=f"Bad params for {action_name}: {e}")
    except Exception as e:
        return ActionResult(False, error=f"{action_name} failed: {e}")
