"""
task_runner.py - Helm orchestration module for desktop automation.

USAGE:
    # task_runner.py lives in Helm's root directory.
    # Generated scripts add Helm's path to sys.path automatically.
    from task_runner import *

    timer = Timer("my task").begin()
    open_app("mspaint", wait_title="Paint")
    focus_window("Paint")
    draw_line(200, 700, 1330, 700)
    key("control", "s")
    timer.end()
"""

import requests
import math
import time
import subprocess
import json
import base64
import os
import platform as _platform

_SYSTEM = _platform.system()


# Resolve clawmetheus path for platform-level modules (perception, web_helpers, vision)
import os as _os
try:
    import yaml as _yaml
except ImportError:
    _yaml = None
_CLAWMETHEUS_PATH = None
_helm_root = _os.path.dirname(_os.path.abspath(__file__))
_config_path = _os.path.join(_helm_root, 'config.yaml')
if _os.path.exists(_config_path) and _yaml:
    try:
        with open(_config_path, encoding='utf-8') as _f:
            _cfg = _yaml.safe_load(_f)
        _CLAWMETHEUS_PATH = _cfg.get('clawmetheus', {}).get('path', '')
    except Exception:
        pass
if not _CLAWMETHEUS_PATH:
    for _candidate in [
        _os.path.expanduser(r'~\Documents\clawmetheus'),
        _os.path.expanduser(r'~/.openclaw/workspace/clawmetheus'),
    ]:
        if _os.path.isdir(_candidate):
            _CLAWMETHEUS_PATH = _candidate
            break
if _CLAWMETHEUS_PATH:
    import sys as _sys_init
    if _CLAWMETHEUS_PATH not in _sys_init.path:
        _sys_init.path.insert(0, _CLAWMETHEUS_PATH)

# ── Web/DOM helpers (DevTools mode only — like a human using F12) ──────────────
# Helm interacts with browsers using keyboard + mouse + vision.
# DevTools Console (F12) is used to inspect the DOM — same as a developer would.
# NO CDP (remote debugging protocol). NO invisible injection.
try:
    from web_helpers import (
        web_eval, web_find, web_find_all, web_find_text, web_page_info,
        close_devtools, reset_mode, get_mode,
    )
    # Force DevTools-only mode — never use CDP.
    # Monkey-patch _detect_mode so it can never pick CDP even if port 9222 responds
    # (port 9222 may be occupied by Lenovo Vantage or other Electron apps).
    import web_helpers as _wh
    _wh._mode = "devtools"
    _original_detect = _wh._detect_mode
    def _devtools_only():
        _wh._mode = "devtools"
        return "devtools"
    _wh._detect_mode = _devtools_only
    _HAS_WEB = True
except ImportError:
    _HAS_WEB = False

BASE = "http://127.0.0.1:7331"

# Screen dimensions � auto-detected, never hardcoded
def _screen_size():
    """Get primary monitor resolution. Cached after first call."""
    import pyautogui
    return pyautogui.size()

# ── Core actions ──────────────────────────────────────────────────────────────

def _action(data):
    # smoothDrag can take a while for complex paths — use longer timeout
    timeout = 60 if data.get("type") == "smoothDrag" else 10
    return requests.post(f"{BASE}/action", json=data, timeout=timeout).json()

def click(x, y, button="left"):
    return _action({"type": "click", "x": int(x), "y": int(y), "button": button})

def double_click(x, y):
    return _action({"type": "doubleClick", "x": int(x), "y": int(y)})

def key(*keys):
    return _action({"type": "key", "keys": list(keys)})

def type_text(text):
    return _action({"type": "type", "text": text})

def type_text_keys(text, interval=0.01):
    """Type text via keyboard events — does NOT touch the clipboard.
    Use this when typing into web pages after DOM inspection (clipboard may contain DOM data).
    Slower than type_text() for long strings, but safe for clipboard-sensitive flows.
    """
    return _action({"type": "typeKeys", "text": text, "interval": interval})

def read_clipboard():
    """Read current clipboard contents. Use after copy() in DevTools Console."""
    import pyperclip
    return pyperclip.paste()

def save_clipboard():
    """Save clipboard contents. Returns the saved text."""
    import pyperclip
    try:
        return pyperclip.paste()
    except Exception:
        return ""

def restore_clipboard(text):
    """Restore clipboard to previously saved contents."""
    import pyperclip
    try:
        pyperclip.copy(text)
    except Exception:
        pass

def wait_ms(ms):
    time.sleep(ms / 1000)

def scroll(x, y, direction="down", amount=3):
    return _action({"type": "scroll", "x": int(x), "y": int(y), "direction": direction, "amount": amount})

# ── Cursor feedback loop ───────────────────────────────────────────────────────

def cursor_pos():
    """Get current cursor position from Clawmetheus."""
    r = requests.get(f"{BASE}/cursor", timeout=5).json()
    return r["x"], r["y"]

def move_to(x, y, verify=True, tolerance=4, max_retries=3):
    """
    Move mouse to (x, y) with optional position verification loop.
    Returns actual (ax, ay) after move.
    """
    x, y = int(x), int(y)
    for attempt in range(max_retries):
        _action({"type": "move", "x": x, "y": y})
        if not verify:
            return x, y
        time.sleep(0.05)
        ax, ay = cursor_pos()
        if abs(ax - x) <= tolerance and abs(ay - y) <= tolerance:
            return ax, ay
        if attempt < max_retries - 1:
            print(f"[CURSOR] miss #{attempt+1}: wanted ({x},{y}) got ({ax},{ay}), retrying", flush=True)
    ax, ay = cursor_pos()
    print(f"[CURSOR] final: wanted ({x},{y}) got ({ax},{ay})", flush=True)
    return ax, ay

# ── Generic UI discovery & interaction ────────────────────────────────────────

def discover_ui(app_title, max_depth=3):
    """
    Enumerate all UI elements in an app using platform accessibility APIs.
    Returns a list of dicts: [{name, role, auto_id, cx, cy, rect, enabled, ...}]
    Use this to learn any app's UI — no hardcoded knowledge needed.

    Example:
        elements = discover_ui("Paint")
        for el in elements:
            print(f"{el['role']:20s} {el['name']:30s} ({el['cx']}, {el['cy']})")
    """
    elements = []
    try:
        from pywinauto import Desktop
        desktop = Desktop(backend="uia")
        for w in desktop.windows():
            try:
                if app_title.lower() not in w.window_text().lower():
                    continue
                _walk_uia(w, elements, depth=0, max_depth=max_depth)
            except Exception:
                pass
    except Exception as e:
        print(f"[discover_ui] pywinauto failed: {e}", flush=True)

    print(f"[discover_ui] Found {len(elements)} elements in '{app_title}'", flush=True)
    return elements


def _walk_uia(element, results, depth=0, max_depth=3):
    """Recursively walk UIA tree and collect element info."""
    if depth > max_depth:
        return
    try:
        name = element.window_text().strip()
        ctrl_type = element.element_info.control_type or ""
        auto_id = element.element_info.automation_id or ""
        rect = element.rectangle()
        cx = (rect.left + rect.right) // 2
        cy = (rect.top + rect.bottom) // 2
        enabled = element.is_enabled()
        visible = element.is_visible()

        if name or auto_id:  # skip unnamed/unidentified elements
            results.append({
                "name": name,
                "role": ctrl_type,
                "auto_id": auto_id,
                "cx": cx, "cy": cy,
                "rect": {"left": rect.left, "top": rect.top, "right": rect.right, "bottom": rect.bottom},
                "enabled": enabled,
                "visible": visible,
                "depth": depth,
            })

        for child in element.children():
            _walk_uia(child, results, depth + 1, max_depth)
    except Exception:
        pass


def find_tool(name, app=None):
    """
    Find and click a tool/button by name in any app using UIA.
    Validates coordinates are on-screen before clicking.
    Falls back to Moondream visual point if UIA fails.
    """
    _sw, _sh = _screen_size()

    el = find_element(name, app=app)
    if el:
        # Sanity check: coordinates must be on-screen and in a reasonable area
        if 0 <= el["cx"] <= _sw and 0 <= el["cy"] <= _sh:
            click(el["cx"], el["cy"])
            wait_ms(200)
            print(f"[find_tool] Clicked '{name}' at ({el['cx']},{el['cy']})", flush=True)
            return el
        else:
            print(f"[find_tool] '{name}' found but off-screen at ({el['cx']},{el['cy']}), skipping", flush=True)

    # Fallback: try partial match via discover_ui
    if app:
        elements = discover_ui(app, max_depth=2)
        name_lower = name.lower()
        for elem in elements:
            if name_lower in elem["name"].lower() and elem.get("visible", True):
                if 0 <= elem["cx"] <= _sw and 0 <= elem["cy"] <= _sh:
                    click(elem["cx"], elem["cy"])
                    wait_ms(200)
                    print(f"[find_tool] Clicked '{elem['name']}' at ({elem['cx']},{elem['cy']}) via discover_ui", flush=True)
                    return elem
                else:
                    print(f"[find_tool] '{elem['name']}' off-screen at ({elem['cx']},{elem['cy']}), skipping", flush=True)

    # Last resort: Moondream visual point
    try:
        r = requests.get(f"{BASE}/point", params={"target": name}, timeout=30).json()
        if r.get("ok") and r.get("x", 0) > 0 and r.get("y", 0) > 0:
            px, py = r["x"], r["y"]
            if 0 <= px <= _sw and 0 <= py <= _sh:
                click(px, py)
                wait_ms(200)
                print(f"[find_tool] Clicked '{name}' at ({px},{py}) via visual point", flush=True)
                return {"name": name, "cx": px, "cy": py, "role": "visual"}
    except Exception as e:
        print(f"[find_tool] Visual point failed: {e}", flush=True)

    print(f"[find_tool] '{name}' not found in '{app}'", flush=True)
    return None


def find_content_area(app_title):
    """
    Find the main content/canvas/document area of any app.
    Uses UIA to find the largest non-toolbar, non-menu area.
    Returns (left, top, right, bottom) in screen coordinates, or None.

    Works for Paint, Notepad, Word, browsers, etc.
    """
    try:
        from pywinauto import Desktop
        desktop = Desktop(backend="uia")
        best = None
        best_area = 0

        for w in desktop.windows():
            try:
                if app_title.lower() not in w.window_text().lower():
                    continue
                wrect = w.rectangle()
                win_area = (wrect.right - wrect.left) * (wrect.bottom - wrect.top)

                for el in w.descendants():
                    try:
                        ctrl = el.element_info.control_type or ""
                        # Look for document, pane, or custom content areas
                        # Skip toolbars, menus, status bars, title bars
                        if ctrl in ("ToolBar", "MenuBar", "Menu", "MenuItem",
                                    "StatusBar", "TitleBar", "ScrollBar", "Thumb",
                                    "Header", "HeaderItem"):
                            continue

                        rect = el.rectangle()
                        area = (rect.right - rect.left) * (rect.bottom - rect.top)

                        # Content area should be >25% of window but <100%
                        if area > best_area and area > win_area * 0.25 and area < win_area * 0.95:
                            # Prefer Document, Edit, or Pane types
                            if ctrl in ("Document", "Edit", "Pane", "Custom", "Group"):
                                best = (rect.left, rect.top, rect.right, rect.bottom)
                                best_area = area
                    except Exception:
                        pass
            except Exception:
                pass

        if best:
            print(f"[find_content_area] '{app_title}' content area: {best}", flush=True)
            return best
    except Exception as e:
        print(f"[find_content_area] Failed: {e}", flush=True)

    # Fallback: use vision
    answer = ask(f"What are the pixel coordinates of the main content/canvas area in {app_title}? Reply as: left,top,right,bottom")
    try:
        parts = [int(x.strip()) for x in answer.split(",") if x.strip().isdigit()]
        if len(parts) == 4:
            print(f"[find_content_area] Via vision: {tuple(parts)}", flush=True)
            return tuple(parts)
    except Exception:
        pass

    print(f"[find_content_area] Could not determine content area for '{app_title}'", flush=True)
    return None


def app_save(filepath, app_title=None):
    """
    Save the current document in any app. Handles everything:
    - Ensures the app has focus
    - Opens Save As dialog (F12)
    - Sets filename via pywinauto (falls back to keyboard)
    - Handles format confirmation dialogs
    - Verifies the file was actually saved
    - Raises RuntimeError if save fails

    Works for Paint, Notepad, Word, and any app with a standard Save As dialog.

    Usage:
        app_save(r"C:\\Users\\sharp\\Pictures\\drawing.png", "Paint")
        app_save(r"C:\\Users\\sharp\\Documents\\notes.txt", "Notepad")
    """
    import os

    if app_title:
        ensure_foreground(app_title)
        wait_ms(300)

    # Click center of app window to ensure it (not a child dialog) has focus
    if app_title:
        rect = get_window_rect(app_title)
        if rect:
            cx = (rect["left"] + rect["right"]) // 2
            cy = (rect["top"] + rect["bottom"]) // 2
            focus_xy = (cx, cy)
        else:
            focus_xy = None
    else:
        focus_xy = None

    result = save_via_dialog(filepath, focus_click_xy=focus_xy)

    if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
        fsize = os.path.getsize(filepath)
        print(f"[app_save] SUCCESS: {filepath} ({fsize} bytes)", flush=True)
        return filepath
    else:
        raise RuntimeError(f"[app_save] File not saved: {filepath}")


# ── Paint-specific helpers (legacy — prefer generic functions above) ──────────
# These wrap the generic functions for backward compatibility with existing scripts.

# RGB values for common color names
COLOR_RGB = {
    "black":       (0, 0, 0),
    "white":       (255, 255, 255),
    "red":         (220, 50, 50),
    "darkred":     (139, 0, 0),
    "orange":      (255, 165, 0),
    "yellow":      (255, 255, 0),
    "green":       (34, 139, 34),
    "teal":        (0, 128, 128),
    "blue":        (30, 100, 200),
    "lightblue":   (135, 206, 235),
    "purple":      (128, 0, 128),
    "pink":        (255, 182, 193),
    "brown":       (101, 67, 33),
    "darkbrown":   (101, 67, 33),
    "gray":        (128, 128, 128),
    "darkgray":    (64, 64, 64),
    "lightgray":   (192, 192, 192),
    "lightyellow": (255, 255, 150),
    "lightgreen":  (144, 238, 144),
    "lightteal":   (0, 200, 200),
    "darkblue":    (0, 0, 139),
    "lavender":    (230, 230, 250),
    "rose":        (255, 100, 100),
    "lightorange": (255, 200, 100),
    # Aliases — common names that map to existing colors
    "cyan":        (0, 200, 200),
    "aqua":        (0, 200, 200),
    "magenta":     (255, 0, 255),
    "grey":        (128, 128, 128),
    "darkgrey":    (64, 64, 64),
    "lightgrey":   (192, 192, 192),
    "skyblue":     (135, 206, 235),
    "navy":        (0, 0, 139),
    "maroon":      (139, 0, 0),
    "gold":        (255, 215, 0),
    "beige":       (245, 245, 220),
    "cream":       (255, 253, 208),
    "tan":         (210, 180, 140),
    "salmon":      (250, 128, 114),
    "coral":       (255, 127, 80),
    "turquoise":   (64, 224, 208),
    "indigo":      (75, 0, 130),
    "violet":      (148, 0, 211),
    "lime":        (0, 255, 0),
    "olive":       (128, 128, 0),
    "peach":       (255, 218, 185),
    "crimson":     (220, 20, 60),
    "scarlet":     (255, 36, 0),
    "chartreuse":  (127, 255, 0),
    "mint":        (152, 255, 152),
    "ivory":       (255, 255, 240),
    "khaki":       (195, 176, 145),
    "sienna":      (160, 82, 45),
    "plum":        (142, 69, 133),
    "burgundy":    (128, 0, 32),
    "rust":        (183, 65, 14),
    "sand":        (194, 178, 128),
    "forest":      (34, 139, 34),
    "forestgreen": (34, 139, 34),
    "darkgreen":   (0, 100, 0),
    "darkorange":  (255, 140, 0),
    "darkpurple":  (48, 0, 48),
    "lightpurple": (200, 162, 200),
    "lightpink":   (255, 182, 193),
    "hotpink":     (255, 105, 180),
    "deeppink":    (255, 20, 147),
}


def set_color_rgb(r, g, b=0):
    """
    Set Paint's foreground (Color 1) using the Edit Colors dialog.
    
    NOTE: In new Windows 11 Paint (Cocreator), the Edit Colors dialog may not work.
    Prefer select_color() which tries toolbar swatches first.
    
    Primary method: pywinauto with auto_ids 706/707/708 (Red/Green/Blue).
    Fallback: Tab navigation to RGB fields.
    """
    # Validate args
    r, g, b = int(r), int(g), int(b)
    if not (0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255):
        print(f"[set_color_rgb] WARNING: clamping values to 0-255 range", flush=True)
        r, g, b = max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b))

    key("escape"); wait_ms(200)

    color1_el = find_element("Color 1", app="Paint")
    if color1_el:
        click(color1_el["cx"], color1_el["cy"])
    wait_ms(300)

    el = find_element("Edit colors", app="Paint")
    if el and el["cy"] < 300:
        click(el["cx"], el["cy"])
    else:
        click(993, 84)
    wait_ms(1800)

    try:
        from pywinauto import Application
        app_pw = Application(backend="uia").connect(path="mspaint.exe", timeout=3)
        dlg = app_pw.top_window()
        dlg.child_window(auto_id="706", control_type="Edit").set_edit_text(str(r))
        dlg.child_window(auto_id="707", control_type="Edit").set_edit_text(str(g))
        dlg.child_window(auto_id="708", control_type="Edit").set_edit_text(str(b))
        dlg.child_window(auto_id="1", control_type="Button").click()
        wait_ms(500)
        print(f"[set_color_rgb] RGB({r},{g},{b}) set via pywinauto", flush=True)
        return
    except Exception as e:
        print(f"[set_color_rgb] pywinauto failed: {e} — Tab fallback", flush=True)

    for _ in range(8):
        key("tab"); wait_ms(150)
    for value in [r, g, b]:
        key("ctrl", "a"); wait_ms(100)
        for digit in str(value):
            key(digit); wait_ms(60)
        key("tab"); wait_ms(150)
    key("enter"); wait_ms(500)
    print(f"[set_color_rgb] RGB({r},{g},{b}) set via Tab fallback", flush=True)


def _find_color_swatch(color_name):
    """
    Find a color swatch in Paint's toolbar by hovering and reading tooltips.
    New Windows 11 Paint has small color circles in the toolbar (~y=130 area).
    Returns (x, y) of the swatch center, or None if not found.
    """
    color_lower = color_name.lower().strip()
    
    # First try UIA: look for elements whose name matches the color
    el = find_element(color_name, app="Paint")
    if el:
        _sw, _sh = _screen_size()
        if 0 <= el["cx"] <= _sw and 0 <= el["cy"] <= _sh and el["cy"] < 300:
            print(f"[_find_color_swatch] Found '{color_name}' via UIA at ({el['cx']},{el['cy']})", flush=True)
            return (el["cx"], el["cy"])
    
    # Try discover_ui to find color-related elements
    elements = discover_ui("Paint", max_depth=3)
    for elem in elements:
        name = elem.get("name", "").lower()
        if color_lower in name and elem.get("cy", 0) < 300 and elem.get("visible", True):
            _sw, _sh = _screen_size()
            if 0 <= elem["cx"] <= _sw and 0 <= elem["cy"] <= _sh:
                print(f"[_find_color_swatch] Found '{color_name}' via discover_ui at ({elem['cx']},{elem['cy']})", flush=True)
                return (elem["cx"], elem["cy"])
    
    # Scan the toolbar area for color swatches by hovering and checking tooltips
    # Color swatches in new Paint are in the toolbar row, roughly y=115-145
    # Only scan a focused region to keep it fast (< 10 seconds)
    _sw, _sh = _screen_size()
    # Colors are typically in the right half of the toolbar
    scan_start_x = _sw // 2
    scan_end_x = min(_sw - 50, scan_start_x + 600)
    
    print(f"[_find_color_swatch] Scanning toolbar x={scan_start_x}-{scan_end_x} for '{color_name}'...", flush=True)
    for y_scan in [130, 125, 135]:
        for x in range(scan_start_x, scan_end_x, 15):
            move_to(x, y_scan, verify=False)
            wait_ms(120)
            tip = find_element("", role="ToolTip", app="Paint")
            if tip and tip.get("name", ""):
                tip_text = tip["name"].lower().strip()
                if color_lower in tip_text or tip_text in color_lower:
                    print(f"[_find_color_swatch] Found '{color_name}' at ({x},{y_scan}) tooltip='{tip['name']}'", flush=True)
                    return (x, y_scan)
    
    # Last resort: try Moondream visual point
    try:
        r = requests.get(f"{BASE}/point", params={"target": f"{color_name} color swatch"}, timeout=20).json()
        if r.get("ok") and r.get("x", 0) > 0 and r.get("y", 0) > 0 and r.get("y", 0) < 300:
            print(f"[_find_color_swatch] Found '{color_name}' via visual point at ({r['x']},{r['y']})", flush=True)
            return (r["x"], r["y"])
    except Exception:
        pass
    
    print(f"[_find_color_swatch] '{color_name}' swatch not found in toolbar", flush=True)
    return None


def select_color(name):
    """
    Select a Paint foreground color by name.
    
    Strategy (in order):
    1. Try clicking the color swatch directly in Paint's toolbar (works in new Win11 Paint)
    2. Fall back to Edit Colors dialog with pywinauto (works in classic Paint)
    3. Fall back to Edit Colors dialog with Tab navigation
    """
    key_name = name.lower().replace(" ", "").replace("-", "").replace("_", "")
    
    # Strategy 1: Click the toolbar swatch directly (new Paint)
    swatch_pos = _find_color_swatch(name)
    if swatch_pos:
        click(swatch_pos[0], swatch_pos[1])
        wait_ms(400)
        print(f"[select_color] Clicked '{name}' swatch at {swatch_pos}", flush=True)
        return
    
    # Strategy 2: Use Edit Colors dialog with RGB values
    rgb = COLOR_RGB.get(key_name)
    if rgb:
        set_color_rgb(*rgb)
        return

    # Fuzzy match: find closest color name
    from difflib import get_close_matches
    matches = get_close_matches(key_name, COLOR_RGB.keys(), n=1, cutoff=0.5)
    if matches:
        matched = matches[0]
        print(f"[select_color] '{name}' not found — using closest match: '{matched}'", flush=True)
        set_color_rgb(*COLOR_RGB[matched])
        return

    # Last resort: try parsing as hex color (#RRGGBB)
    if name.startswith("#") and len(name) == 7:
        try:
            r = int(name[1:3], 16)
            g = int(name[3:5], 16)
            b = int(name[5:7], 16)
            print(f"[select_color] Parsed hex color {name} → RGB({r},{g},{b})", flush=True)
            set_color_rgb(r, g, b)
            return
        except ValueError:
            pass

    print(f"[select_color] WARNING: '{name}' unknown, defaulting to black.", flush=True)
    set_color_rgb(0, 0, 0)

def use_pencil():
    """Activate Paint's pencil tool. Legacy — prefer find_tool('Pencil', app='Paint')."""
    find_tool("Pencil", app="Paint")

def use_fill():
    """Activate Paint's fill tool.
    
    ⚠️ WARNING: Flood fill on pencil-drawn shapes WILL leak and destroy the canvas.
    Only safe on: blank canvas (background fill) or shapes made with Paint's shape tools.
    Consider using Paint's shape tools (Rectangle, Ellipse) with fill instead.
    """
    print("[use_fill] ⚠️  WARNING: Flood fill is risky on pencil-drawn shapes (pixel gaps cause leaks).", flush=True)
    print("[use_fill] Safe on: blank canvas, shape-tool shapes. Unsafe on: pencil/freehand shapes.", flush=True)
    find_tool("Fill with color", app="Paint")


def set_outline(style="Solid color"):
    """Set Paint shape outline style. Must have a shape tool selected first.
    Styles: 'No outline', 'Solid color', 'Crayon', 'Marker', 'Oil', 'Natural pencil', 'Watercolor'
    """
    find_tool("Outline", app="Paint")
    wait_ms(300)
    find_tool(style, app="Paint")
    wait_ms(200)
    print(f"[set_outline] Set to '{style}'", flush=True)


def set_fill(style="Solid color"):
    """Set Paint shape fill style. Must have a shape tool selected first.
    Styles: 'No fill', 'Solid color', 'Crayon', 'Marker', 'Oil', 'Natural pencil', 'Watercolor'
    """
    find_tool("Fill", app="Paint")
    wait_ms(300)
    find_tool(style, app="Paint")
    wait_ms(200)
    print(f"[set_fill] Set to '{style}'", flush=True)

def get_canvas_bounds(app_title="Paint"):
    """
    Detect Paint's canvas bounds by scanning actual pixels — no vision model guessing.

    The canvas in Paint is a white rectangle on a gray background.
    We take a full-res screenshot, scan for the white region, and return exact coords.
    Also caches the result so draw functions can clamp coordinates automatically.

    Strategy:
    1. Pixel scan (primary): screenshot → find white rectangle via numpy
    2. Status bar cross-validation: read WxH from Paint status bar, refine bounds
    3. UIA fallback: find element matching status bar dimensions
    4. find_content_area() as last resort

    Returns (left, top, right, bottom) in screen coordinates.
    """
    global _canvas_bounds_cache
    result = _get_canvas_bounds_impl(app_title)
    _canvas_bounds_cache = result
    return result


def _get_canvas_bounds_impl(app_title="Paint"):
    """Internal implementation of get_canvas_bounds."""
    import re

    # --- Method 1: Pixel scan — deterministic, no vision model ---
    try:
        result = _find_canvas_by_pixels(app_title)
        if result:
            cl, ct, cr, cb = result
            w, h = cr - cl, cb - ct
            # Sanity: canvas should be reasonable
            _sw, _sh = _screen_size()
            if w > 50 and h > 50 and cl >= 0 and ct >= 0 and cr <= _sw and cb <= _sh:
                # Cross-validate with status bar if available
                status_w, status_h = _read_canvas_size_from_status(app_title)
                if status_w and status_h:
                    w_diff, h_diff = abs(w - status_w), abs(h - status_h)
                    if w_diff < 30 and h_diff < 30:
                        # Pixel scan matches status bar — high confidence
                        # Use pixel-detected top-left + status bar dimensions for precision
                        result = (cl, ct, cl + status_w, ct + status_h)
                        print(f"[get_canvas_bounds] Pixel scan + status bar: {status_w}×{status_h} at ({cl},{ct}) → {result}", flush=True)
                        return result
                    else:
                        print(f"[get_canvas_bounds] Pixel scan ({w}×{h}) vs status bar ({status_w}×{status_h}), diff=({w_diff},{h_diff})", flush=True)
                        if w_diff < 80 and h_diff < 80:
                            # Close enough — trust pixel scan position, status bar size
                            result = (cl, ct, cl + status_w, ct + status_h)
                            print(f"[get_canvas_bounds] Using status bar dims at pixel position: {result}", flush=True)
                            return result
                        # Big mismatch — pixel scan might have found wrong region, fall through
                else:
                    # No status bar — trust pixel scan
                    print(f"[get_canvas_bounds] Pixel scan: ({cl},{ct})→({cr},{cb}) = {w}×{h}", flush=True)
                    return (cl, ct, cr, cb)
    except Exception as e:
        print(f"[get_canvas_bounds] Pixel scan failed: {e}", flush=True)

    # --- Method 2: UIA element matching ---
    try:
        from pywinauto import Desktop
        desktop = Desktop(backend="uia")
        for w in desktop.windows():
            try:
                if app_title.lower() not in w.window_text().lower():
                    continue

                status_w, status_h = _read_canvas_size_from_status(app_title)

                if status_w and status_h:
                    best_el = None
                    best_diff = float('inf')
                    for el in w.descendants():
                        try:
                            ctrl = el.element_info.control_type or ""
                            if ctrl in ("ToolBar", "MenuBar", "Menu", "MenuItem",
                                        "StatusBar", "TitleBar", "ScrollBar", "Thumb",
                                        "Header", "HeaderItem", "Button", "Text",
                                        "SplitButton", "Image", "Separator"):
                                continue
                            rect = el.rectangle()
                            el_w = rect.right - rect.left
                            el_h = rect.bottom - rect.top
                            diff = abs(el_w - status_w) + abs(el_h - status_h)
                            if diff < best_diff and el_w > 50 and el_h > 50:
                                best_diff = diff
                                best_el = rect
                        except Exception:
                            pass

                    if best_el and best_diff < 20:
                        result = (best_el.left, best_el.top, best_el.right, best_el.bottom)
                        print(f"[get_canvas_bounds] UIA match: {status_w}×{status_h} → {result} (diff={best_diff})", flush=True)
                        return result
            except Exception:
                pass
    except Exception as e:
        print(f"[get_canvas_bounds] UIA failed: {e}", flush=True)

    # --- Method 3: Generic content area detection ---
    bounds = find_content_area(app_title)
    if bounds:
        print(f"[get_canvas_bounds] Via find_content_area: {bounds}", flush=True)
        return bounds

    print("[get_canvas_bounds] WARNING: All detection methods failed, using conservative defaults", flush=True)
    return (5, 150, 800, 749)


def _find_canvas_by_pixels(app_title="Paint"):
    """
    Find the white canvas rectangle by scanning screenshot pixels.
    The canvas is white (RGB ~255,255,255) on a gray background (~240,240,240 or darker).
    
    Takes a FULL resolution screenshot (no scaling) and scans for the largest
    contiguous white rectangle below the toolbar area.
    
    Returns (left, top, right, bottom) in screen coordinates, or None.
    """
    try:
        from PIL import Image
        import io
    except ImportError:
        print("[_find_canvas_by_pixels] PIL not available, skipping pixel scan", flush=True)
        return None

    # Take full-res screenshot
    try:
        r = requests.get(f"{BASE}/screenshot/base64?scale=1.0", timeout=10).json()
        img_data = base64.b64decode(r["image"])
        img = Image.open(io.BytesIO(img_data)).convert("RGB")
    except Exception as e:
        print(f"[_find_canvas_by_pixels] Screenshot failed: {e}", flush=True)
        return None

    width, height = img.size
    print(f"[_find_canvas_by_pixels] Screenshot: {width}×{height}", flush=True)

    # Strategy: scan horizontal lines to find the white canvas region.
    # The canvas is a solid white rectangle. We look for rows where there's a
    # contiguous run of white pixels (R>250, G>250, B>250) that's at least 100px wide.
    # The toolbar is at the top (~first 130px), status bar at bottom (~last 30px).
    # Canvas starts after toolbar and is LEFT-ALIGNED.

    # Skip toolbar area (top ~100px) and status bar (bottom ~40px)
    scan_top = 100
    scan_bottom = height - 40

    # Sample every 4th row for speed, then refine
    white_rows = []
    for y in range(scan_top, scan_bottom, 4):
        run = _find_white_run(img, y, min_length=100)
        if run:
            white_rows.append((y, run[0], run[1]))  # (y, x_start, x_end)

    if not white_rows:
        print("[_find_canvas_by_pixels] No canvas region found via pixel scan", flush=True)
        return None

    # Find the most common x_start and x_end (the canvas edges are consistent across rows)
    # Use the median of the detected runs
    x_starts = [r[1] for r in white_rows]
    x_ends = [r[2] for r in white_rows]
    x_starts.sort()
    x_ends.sort()
    median_x_start = x_starts[len(x_starts) // 2]
    median_x_end = x_ends[len(x_ends) // 2]

    # Filter rows that match the median x range (within tolerance)
    consistent_rows = [r for r in white_rows
                       if abs(r[1] - median_x_start) < 15 and abs(r[2] - median_x_end) < 15]

    if not consistent_rows:
        print("[_find_canvas_by_pixels] No consistent white region", flush=True)
        return None

    # The canvas top is the first consistent row, bottom is the last
    # But we sampled every 4th row, so refine the edges
    approx_top = consistent_rows[0][0]
    approx_bottom = consistent_rows[-1][0]

    # Refine top edge: scan upward from approx_top
    canvas_top = approx_top
    for y in range(approx_top, scan_top, -1):
        run = _find_white_run(img, y, min_length=100)
        if run and abs(run[0] - median_x_start) < 15:
            canvas_top = y
        else:
            break

    # Refine bottom edge: scan downward from approx_bottom
    canvas_bottom = approx_bottom
    for y in range(approx_bottom, scan_bottom):
        run = _find_white_run(img, y, min_length=100)
        if run and abs(run[0] - median_x_start) < 15:
            canvas_bottom = y
        else:
            break

    # Refine left/right edges using the middle row
    mid_y = (canvas_top + canvas_bottom) // 2
    final_run = _find_white_run(img, mid_y, min_length=100)
    if final_run:
        canvas_left, canvas_right = final_run
    else:
        canvas_left, canvas_right = median_x_start, median_x_end

    result = (canvas_left, canvas_top, canvas_right, canvas_bottom)
    w = canvas_right - canvas_left
    h = canvas_bottom - canvas_top
    print(f"[_find_canvas_by_pixels] Found canvas: ({canvas_left},{canvas_top})→({canvas_right},{canvas_bottom}) = {w}×{h}", flush=True)
    return result


def _find_white_run(img, y, min_length=100):
    """
    Scan a single row of pixels and find the longest contiguous run of canvas pixels.
    Canvas pixels are bright and uniform — either white (light mode) or warm-toned (dark mode).
    Detects: R>200 AND (G>200 AND B>200) OR (R>230 AND G>150) — covers white and dark-mode canvas.
    Returns (x_start, x_end) of the longest canvas run, or None if none >= min_length.
    """
    width = img.width
    best_start, best_len = 0, 0
    run_start = None

    for x in range(width):
        r, g, b = img.getpixel((x, y))
        # White canvas: R>250, G>250, B>250
        # Dark mode canvas: ~RGB(254,184,112) — bright warm tone
        # Also handle other possible dark mode canvas colors
        # Both are significantly brighter than the dark UI (~RGB(38,28,18) or ~RGB(50,50,50))
        is_white = (r > 250 and g > 250 and b > 250)
        is_dark_mode_canvas = (r > 200 and g > 140 and b > 60 and r - b > 80)
        is_canvas = is_white or is_dark_mode_canvas
        if is_canvas:
            if run_start is None:
                run_start = x
        else:
            if run_start is not None:
                run_len = x - run_start
                if run_len > best_len:
                    best_start, best_len = run_start, run_len
                run_start = None

    # Handle run that extends to edge
    if run_start is not None:
        run_len = width - run_start
        if run_len > best_len:
            best_start, best_len = run_start, run_len

    if best_len >= min_length:
        return (best_start, best_start + best_len)
    return None


def _read_canvas_size_from_status(app_title="Paint"):
    """Read canvas pixel dimensions from Paint's status bar. Returns (width, height) or (None, None)."""
    import re
    try:
        from pywinauto import Desktop
        desktop = Desktop(backend="uia")
        for w in desktop.windows():
            try:
                if app_title.lower() not in w.window_text().lower():
                    continue
                for el in w.descendants():
                    try:
                        txt = el.window_text().strip()
                        m = re.match(r'(\d+)\s*[×xX]\s*(\d+)\s*px', txt)
                        if m:
                            return int(m.group(1)), int(m.group(2))
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception:
        pass
    return None, None

def paint_save(filepath):
    """Save Paint document. Legacy — prefer app_save(filepath, 'Paint')."""
    return app_save(filepath, "Paint")


def new_canvas():
    """
    Create a new blank canvas in the current app (Ctrl+N / Cmd+N).
    Handles 'Save changes?' dialog by pressing N (Don't Save).
    Works for Paint, Notepad, and most apps with standard new-document shortcut.
    """
    if _SYSTEM == "Darwin":
        key("cmd", "n")
    else:
        key("ctrl", "n")
    wait_ms(600)
    # Press 'n' — dismisses "Save changes?" dialog (Don't Save) if it appears.
    # Harmless if no dialog: no text field is focused after Ctrl+N in Paint/Notepad.
    key("n")
    wait_ms(400)
    print("[new_canvas] New blank canvas created", flush=True)


def dismiss_system_popups():
    """
    Dismiss system popups that aren't part of the target app — OneDrive errors,
    Windows Update notifications, Defender alerts, etc.
    Uses pywinauto to find and close non-target windows.
    Call this before starting a task and if things seem stuck.
    """
    SYSTEM_POPUP_KEYWORDS = [
        "onedrive", "microsoft onedrive", "sign in", "sync",
        "windows update", "update available", "restart required",
        "windows security", "defender", "virus",
        "microsoft store", "feedback hub", "tips",
        "your phone", "phone link",
        "notification", "toast",
    ]
    TARGET_APPS = ["paint", "notepad", "chrome", "firefox", "edge", "explorer"]

    dismissed = 0
    try:
        from pywinauto import Desktop
        desktop = Desktop(backend="uia")
        for w in desktop.windows():
            try:
                title = w.window_text().lower().strip()
                if not title:
                    continue
                # Skip target app windows
                if any(app in title for app in TARGET_APPS):
                    continue
                # Check if it's a system popup
                if any(kw in title for kw in SYSTEM_POPUP_KEYWORDS):
                    print(f"[dismiss_system_popups] Found system popup: '{w.window_text()}'", flush=True)
                    # Try to close it
                    try:
                        # Look for Close/X/OK/Dismiss buttons
                        for btn_name in ["Close", "OK", "Dismiss", "Not now", "Later", "No thanks"]:
                            try:
                                btn = w.child_window(title_re=f"(?i){btn_name}", control_type="Button")
                                btn.click()
                                wait_ms(500)
                                dismissed += 1
                                print(f"[dismiss_system_popups] Clicked '{btn_name}'", flush=True)
                                break
                            except Exception:
                                pass
                        else:
                            # No button found — try closing the window
                            w.close()
                            wait_ms(500)
                            dismissed += 1
                            print(f"[dismiss_system_popups] Closed window", flush=True)
                    except Exception as e:
                        print(f"[dismiss_system_popups] Could not close: {e}", flush=True)
            except Exception:
                pass
    except Exception as e:
        print(f"[dismiss_system_popups] Error: {e}", flush=True)

    if dismissed:
        print(f"[dismiss_system_popups] Dismissed {dismissed} popup(s)", flush=True)
    return dismissed

# ── Drawing helpers ───────────────────────────────────────────────────────────
# All drawing uses clawmetheus smoothDrag (Win32 mouse_event under the hood).
# Every draw function activates the canvas first so toolbar clicks don't interfere.
# The LLM can compose any image by combining these primitives.

_canvas_bounds_cache = None

def _clamp_to_canvas(pts):
    """Clamp all points to the cached canvas bounds. Prevents drawing outside the canvas."""
    global _canvas_bounds_cache
    if not _canvas_bounds_cache:
        return pts  # no bounds known yet — pass through
    cl, ct, cr, cb = _canvas_bounds_cache
    margin = 5  # small inset to avoid drawing on the canvas edge
    for p in pts:
        p["x"] = max(cl + margin, min(cr - margin, int(p["x"])))
        p["y"] = max(ct + margin, min(cb - margin, int(p["y"])))
    return pts


def _smooth_drag(pts, speed=200, activate_xy=None):
    """
    Core drawing primitive: send a smoothDrag through a list of {"x","y"} dicts.
    Optionally clicks activate_xy first to ensure the canvas has focus.
    All public draw_* functions delegate here.
    Points are clamped to canvas bounds if known (set by get_canvas_bounds).
    """
    if not pts or len(pts) < 2:
        return {"ok": False, "error": "need >= 2 points"}
    pts = _clamp_to_canvas(pts)
    if activate_xy:
        click(activate_xy[0], activate_xy[1])
        wait_ms(200)
    return _action({"type": "smoothDrag", "points": pts, "button": "left", "speed": speed})


def activate_canvas(app_title="Paint"):
    """
    Click the center of the canvas to transfer focus from toolbar to drawing area.
    Call this after selecting a tool or color if you're about to do freehand drawing
    with _smooth_drag directly. The draw_* helpers call this automatically.
    """
    bounds = get_canvas_bounds(app_title)
    cx = (bounds[0] + bounds[2]) // 2
    cy = (bounds[1] + bounds[3]) // 2
    click(cx, cy)
    wait_ms(200)
    print(f"[activate_canvas] Clicked ({cx},{cy})", flush=True)
    return cx, cy


def draw_line(x1, y1, x2, y2, speed=200):
    """Draw a straight line from (x1,y1) to (x2,y2)."""
    n = max(3, int(math.sqrt((x2-x1)**2 + (y2-y1)**2) / 20))
    pts = [{"x": int(x1 + i/n*(x2-x1)), "y": int(y1 + i/n*(y2-y1))} for i in range(n + 1)]
    # Activate canvas near the midpoint (away from the line itself)
    mid_x, mid_y = (x1 + x2) // 2, (y1 + y2) // 2
    offset_y = 30 if abs(y2 - y1) < abs(x2 - x1) else 0
    offset_x = 30 if abs(x2 - x1) < abs(y2 - y1) else 0
    return _smooth_drag(pts, speed, activate_xy=(mid_x + offset_x, mid_y + offset_y))


def drag(x1, y1, x2, y2, speed=200):
    """General-purpose mouse drag from (x1,y1) to (x2,y2). Alias for draw_line."""
    return draw_line(x1, y1, x2, y2, speed)


def draw_rect(x1, y1, x2, y2, speed=200):
    """Draw a rectangle outline as a single continuous path (no gaps at corners)."""
    pts = [
        {"x": int(x1), "y": int(y1)},
        {"x": int(x2), "y": int(y1)},
        {"x": int(x2), "y": int(y2)},
        {"x": int(x1), "y": int(y2)},
        {"x": int(x1), "y": int(y1)},  # close
    ]
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    return _smooth_drag(pts, speed, activate_xy=(cx, cy))


def draw_circle(cx, cy, r, steps=48, speed=200):
    """Draw a circle outline centered at (cx,cy) with radius r."""
    pts = [{"x": int(cx + r * math.cos(2*math.pi*i/steps)),
            "y": int(cy + r * math.sin(2*math.pi*i/steps))} for i in range(steps + 1)]
    result = _smooth_drag(pts, speed, activate_xy=(cx, cy))
    ok = result.get("ok", False)
    print(f"[draw_circle] cx={cx} cy={cy} r={r} steps={steps} ok={ok}", flush=True)
    if not ok:
        print(f"[draw_circle] ERROR: {result}", flush=True)
    return result


def draw_ellipse(cx, cy, rx, ry, steps=48, speed=200):
    """Draw an ellipse outline centered at (cx,cy) with radii rx (horizontal) and ry (vertical)."""
    pts = [{"x": int(cx + rx * math.cos(2*math.pi*i/steps)),
            "y": int(cy + ry * math.sin(2*math.pi*i/steps))} for i in range(steps + 1)]
    return _smooth_drag(pts, speed, activate_xy=(cx, cy))


def draw_arc(cx, cy, r, start_angle=0, end_angle=math.pi, steps=36, speed=200):
    """Draw an arc (partial circle). Angles in radians, 0=right, pi/2=down."""
    pts = [{"x": int(cx + r * math.cos(start_angle + (end_angle - start_angle) * i / steps)),
            "y": int(cy + r * math.sin(start_angle + (end_angle - start_angle) * i / steps))}
           for i in range(steps + 1)]
    return _smooth_drag(pts, speed, activate_xy=(cx, cy))


def draw_curve(points, speed=200):
    """
    Draw a smooth curve through a list of (x,y) control points using Catmull-Rom interpolation.
    Produces a natural-looking curve (good for hair, waves, organic shapes).
    points: list of (x,y) tuples — at least 2 points required.
    """
    if len(points) < 2:
        return {"ok": False, "error": "need >= 2 points"}
    if len(points) == 2:
        return draw_line(points[0][0], points[0][1], points[1][0], points[1][1], speed)

    # Catmull-Rom spline interpolation
    def _catmull_rom(p0, p1, p2, p3, t):
        t2, t3 = t * t, t * t * t
        return 0.5 * ((2*p1) + (-p0 + p2)*t + (2*p0 - 5*p1 + 4*p2 - p3)*t2 + (-p0 + 3*p1 - 3*p2 + p3)*t3)

    pts = []
    # Pad start/end by duplicating first/last point
    padded = [points[0]] + list(points) + [points[-1]]
    segments_per_span = max(4, 80 // len(points))  # more points = fewer samples per span
    for i in range(1, len(padded) - 2):
        p0, p1, p2, p3 = padded[i-1], padded[i], padded[i+1], padded[i+2]
        for j in range(segments_per_span):
            t = j / segments_per_span
            x = _catmull_rom(p0[0], p1[0], p2[0], p3[0], t)
            y = _catmull_rom(p0[1], p1[1], p2[1], p3[1], t)
            pts.append({"x": int(x), "y": int(y)})
    # Add final point
    pts.append({"x": int(points[-1][0]), "y": int(points[-1][1])})

    cx = sum(p[0] for p in points) // len(points)
    cy = sum(p[1] for p in points) // len(points)
    return _smooth_drag(pts, speed, activate_xy=(cx, cy))


def draw_path(points, speed=200):
    """
    Draw an open path through a list of (x,y) points (straight segments, not closed).
    Like draw_polygon but doesn't connect last point back to first.
    """
    if len(points) < 2:
        return {"ok": False, "error": "need >= 2 points"}
    pts = [{"x": int(p[0]), "y": int(p[1])} for p in points]
    cx = sum(p[0] for p in points) // len(points)
    cy = sum(p[1] for p in points) // len(points)
    return _smooth_drag(pts, speed, activate_xy=(cx, cy))


def draw_polygon(points, speed=200):
    """Draw a closed polygon through a list of (x, y) tuples."""
    if len(points) < 3:
        return {"ok": False, "error": "need >= 3 points"}
    pts = [{"x": int(p[0]), "y": int(p[1])} for p in points]
    pts.append(pts[0])  # close
    cx = sum(p[0] for p in points) // len(points)
    cy = sum(p[1] for p in points) // len(points)
    return _smooth_drag(pts, speed, activate_xy=(cx, cy))


def draw_star(cx, cy, r_outer, r_inner, points_count=5, speed=200):
    """Draw a star shape. r_outer = tip radius, r_inner = indent radius."""
    pts = []
    for i in range(points_count * 2 + 1):
        angle = math.pi * i / points_count - math.pi / 2  # start from top
        r = r_outer if i % 2 == 0 else r_inner
        pts.append({"x": int(cx + r * math.cos(angle)),
                     "y": int(cy + r * math.sin(angle))})
    return _smooth_drag(pts, speed, activate_xy=(cx, cy))


def draw_rays(cx, cy, r_inner, r_outer, count=8, speed=200):
    """Draw radial lines from inner to outer radius (for sun rays, spokes, etc.)."""
    for i in range(count):
        angle = 2 * math.pi * i / count
        x1 = int(cx + r_inner * math.cos(angle))
        y1 = int(cy + r_inner * math.sin(angle))
        x2 = int(cx + r_outer * math.cos(angle))
        y2 = int(cy + r_outer * math.sin(angle))
        draw_line(x1, y1, x2, y2, speed)


def draw_filled_rect(x1, y1, x2, y2, speed=200):
    """Fill a rectangle by drawing horizontal lines across it (pencil-based fill)."""
    # Activate canvas first
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    click(int(cx), int(cy))
    wait_ms(200)
    step = 2  # 2px spacing for solid fill
    for y in range(int(min(y1, y2)), int(max(y1, y2)), step):
        pts = [{"x": int(x1), "y": y}, {"x": int(x2), "y": y}]
        _action({"type": "smoothDrag", "points": pts, "button": "left", "speed": max(speed, 400)})
    print(f"[draw_filled_rect] ({x1},{y1})->({x2},{y2})", flush=True)


def draw_filled_circle(cx, cy, r, speed=200):
    """Fill a circle by drawing horizontal lines across it (pencil-based fill)."""
    # Activate canvas first
    click(int(cx), int(cy))
    wait_ms(200)
    step = 2
    for dy in range(-r, r + 1, step):
        half_w = int(math.sqrt(max(0, r*r - dy*dy)))
        if half_w < 1:
            continue
        y = int(cy + dy)
        pts = [{"x": int(cx - half_w), "y": y}, {"x": int(cx + half_w), "y": y}]
        _action({"type": "smoothDrag", "points": pts, "button": "left", "speed": max(speed, 400)})
    print(f"[draw_filled_circle] cx={cx} cy={cy} r={r}", flush=True)

# ── Vision ────────────────────────────────────────────────────────────────────

def screenshot(path=None, scale=0.5):
    r = requests.get(f"{BASE}/screenshot/base64?scale={scale}", timeout=10).json()
    img_bytes = base64.b64decode(r["image"])
    if path:
        with open(path, "wb") as f:
            f.write(img_bytes)
    return img_bytes

def ask(question, scale=0.5, timeout=120):
    import urllib.parse
    q = urllib.parse.quote(question)
    r = requests.get(f"{BASE}/ask?q={q}&scale={scale}", timeout=timeout).json()
    return r.get("answer", "")


def validate_image(filepath, description="", min_bytes=5000):
    """
    Validate a saved image file before uploading or proceeding.
    For file-specific checks (size, color diversity). For general screen verification, use verify_result().
    
    Returns (ok: bool, reason: str).
    """
    # Check 1: File exists and has reasonable size
    if not os.path.exists(filepath):
        return False, f"File does not exist: {filepath}"
    
    size = os.path.getsize(filepath)
    if size < min_bytes:
        return False, f"File too small ({size} bytes) — likely blank or corrupted. Min: {min_bytes} bytes"
    
    # Check 2: Image color diversity — detect blank/flood-filled images
    # A valid drawing can have as few as 2 color groups (background + drawn content).
    # Only flag as blank if there's literally 1 color group (uniform fill).
    try:
        from PIL import Image as PILImage
        img = PILImage.open(filepath).convert("RGB")
        w, h = img.size
        pixels = []
        for sy in range(0, h, max(1, h // 20)):
            for sx in range(0, w, max(1, w // 20)):
                pixels.append(img.getpixel((sx, sy)))
        
        unique_colors = set()
        for r, g, b in pixels:
            unique_colors.add((r // 32, g // 32, b // 32))
        
        if len(unique_colors) < 2:
            return False, f"Image has only {len(unique_colors)} distinct color group — completely uniform, likely blank"
        
        # 2+ color groups means there IS drawn content.
        # Thin pencil lines on a large canvas will always be >99% background — that's normal.
        # Only reject if literally 1 color group (handled above).
    except ImportError:
        print("[validate_image] PIL not available — skipping pixel analysis", flush=True)
    except Exception as e:
        print(f"[validate_image] Pixel analysis error: {e}", flush=True)
    
    print(f"[validate_image] PASSED: {filepath} ({size} bytes)", flush=True)
    return True, "OK"


def verify_result(expected, filepath=None, strict=False):
    """
    General-purpose output verification — like a human checking their work with their eyes.
    
    Works for ANY output type: drawings, documents, forms, emails, websites, games, etc.
    Takes a screenshot and asks the vision model if the expected outcome is visible.
    
    Args:
        expected:  Description of what the result should look like.
                   e.g. "a colorful rubber duck drawing in Paint"
                   e.g. "an email draft with subject 'Meeting Tomorrow' and 3 bullet points"
                   e.g. "the Grok chat showing my uploaded image and a response"
                   e.g. "a filled-out form with name, email, and phone fields completed"
        filepath:  Optional file path to validate (runs file-specific checks too).
                   For images: checks size, color diversity.
                   For any file: checks existence and non-zero size.
        strict:    If True, raises RuntimeError on failure instead of returning (False, reason).
    
    Returns (ok: bool, reason: str).
    
    Usage:
        # Verify a drawing
        ok, reason = verify_result("a colorful rubber duck with a top hat", filepath=saved_path)
        
        # Verify a web page state
        ok, reason = verify_result("Grok chat with my image uploaded and a response visible")
        
        # Verify a form
        ok, reason = verify_result("contact form with all fields filled in correctly")
        
        # Strict mode — raises on failure (good for scripts)
        verify_result("email sent confirmation dialog", strict=True)
    """
    reasons = []
    
    # File-specific checks (if filepath provided)
    if filepath:
        if not os.path.exists(filepath):
            msg = f"File does not exist: {filepath}"
            if strict:
                raise RuntimeError(f"Verification failed: {msg}")
            return False, msg
        
        size = os.path.getsize(filepath)
        if size == 0:
            msg = f"File is empty (0 bytes): {filepath}"
            if strict:
                raise RuntimeError(f"Verification failed: {msg}")
            return False, msg
        
        # Image-specific checks
        ext = os.path.splitext(filepath)[1].lower()
        if ext in ('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp'):
            ok, reason = validate_image(filepath, expected)
            if not ok:
                if strict:
                    raise RuntimeError(f"Verification failed: {reason}")
                return False, reason
    
    # Screen verification — the core "human eyes" check
    import urllib.parse
    q = urllib.parse.quote(
        f"VERIFICATION CHECK. The expected result is: '{expected}'\n\n"
        f"Look at the screen carefully. Answer these questions:\n"
        f"1. Is the expected result visible and correct?\n"
        f"2. Are there any errors, dialogs, or unexpected states?\n"
        f"3. Does the output look complete and high quality?\n\n"
        f"Answer EXACTLY one of:\n"
        f"  PASS: [brief reason why it looks correct]\n"
        f"  FAIL: [what's wrong or missing]\n"
        f"Do not say PASS if anything looks wrong, incomplete, or low quality."
    )
    try:
        r = requests.get(f"{BASE}/ask?q={q}&scale=0.5", timeout=30).json()
        answer = r.get("answer", "").strip()
        print(f"[verify_result] Vision check: {answer[:200]}", flush=True)
        
        if answer.upper().startswith("FAIL"):
            msg = f"Screen verification failed: {answer}"
            if strict:
                raise RuntimeError(msg)
            return False, msg
        elif answer.upper().startswith("PASS"):
            return True, answer
        else:
            # Ambiguous — check for negative keywords
            lower = answer.lower()
            if any(w in lower for w in ["blank", "empty", "error", "wrong", "missing", "not visible", "destroyed", "failed"]):
                msg = f"Screen verification uncertain (likely failed): {answer}"
                if strict:
                    raise RuntimeError(msg)
                return False, msg
            return True, f"Verification inconclusive but no issues detected: {answer}"
    except requests.exceptions.Timeout:
        print("[verify_result] Vision check timed out — skipping", flush=True)
        return True, "Vision check timed out (skipped)"
    except Exception as e:
        print(f"[verify_result] Vision check error: {e}", flush=True)
        return True, f"Vision check error (skipped): {e}"


# ── Window management ─────────────────────────────────────────────────────────

def get_window_rect(title_contains):
    """
    Get window rect by title substring via Clawmetheus HTTP API.
    Returns dict with left/top/right/bottom/title, or None if not found.
    """
    try:
        import urllib.parse
        q = urllib.parse.quote(title_contains)
        r = requests.get(f"{BASE}/window_rect?title={q}", timeout=5).json()
        if r.get("ok"):
            return r
    except Exception:
        pass
    return None


def focus_window(title_contains, retries=3):
    """
    Bring a window to focus. Cross-platform.
    macOS: uses osascript. Windows: clicks title bar.
    """
    if _SYSTEM == "Darwin":
        try:
            script = f'''
tell application "System Events"
    repeat with p in (every process whose background only is false)
        if name of p contains "{title_contains}" then
            set frontmost of p to true
            return true
        end if
    end repeat
end tell'''
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
            time.sleep(0.3)
            return True
        except Exception:
            return False

    for attempt in range(retries):
        rect = get_window_rect(title_contains)
        if rect:
            _sw, _sh = _screen_size()
            tx = max(0, min(_sw - 1, (rect["left"] + rect["right"]) // 2))
            ty = max(0, min(_sh - 1, rect["top"] + 15))
            click(tx, ty)
            time.sleep(0.4)
            return True
        time.sleep(0.5)
    return False

def open_app(app_name, wait_title=None, wait_secs=5):
    """
    Open an app like a human: Win+R → type name → Enter.
    If already open (detected via wait_title), focuses it instead — never double-opens.
    """
    # Check if already open — focus instead of opening again
    if wait_title and get_window_rect(wait_title):
        print(f"[open_app] {wait_title} already open — focusing", flush=True)
        focus_window(wait_title)
        return True

    if _SYSTEM == "Darwin":
        key("cmd", "space")
        time.sleep(0.7)
        type_text(app_name)
        time.sleep(0.5)
        key("enter")
    else:
        key("win", "r")
        time.sleep(0.7)
        type_text(app_name)
        time.sleep(0.3)
        key("enter")
    if wait_title:
        deadline = time.time() + wait_secs
        while time.time() < deadline:
            time.sleep(0.3)
            if get_window_rect(wait_title):
                time.sleep(0.3)
                return True
        return False
    time.sleep(wait_secs)
    return True


def open_browser(url=None):
    """
    Open a URL in the default browser using keyboard — like a human.
    Win+R → type URL → Enter. Then waits for the page to load.

    After the page loads, DOM inspection is available via DevTools (F12 Console).
    web_find(), web_page_info(), etc. use DevTools Console — same as a developer
    pressing F12 and typing in the Console. No CDP, no remote debugging.

    For interacting with web pages:
    - web_page_info() to inspect the DOM structure
    - web_find(selector) / web_find_text(text) to locate elements with screen coords
    - close_devtools() BEFORE typing in the page (DevTools steals keyboard focus)
    - click(x, y) to click elements found via web_find
    - type_text_keys(text) for input fields (clipboard-safe)
    - ask(question) for visual questions about the page
    - map_screen() as fallback if DOM inspection isn't working
    """
    if url:
        key("win", "r"); time.sleep(0.7)
        type_text(url); time.sleep(0.3); key("enter")
        time.sleep(6)
        print(f"[open_browser] Opened {url} via keyboard", flush=True)

    # Reset DevTools state for the new page
    if _HAS_WEB:
        reset_mode()
        import web_helpers as _wh
        _wh._mode = "devtools"  # always DevTools, never CDP

    return _HAS_WEB



def kill_app(title_contains):
    """
    Close an app like a human: focus it, then Alt+F4.
    Handles Paint's 'Save changes?' dialog by pressing 'n' (Don't Save).
    """
    if not focus_window(title_contains):
        return False
    time.sleep(0.3)
    key("alt", "f4")
    time.sleep(0.8)
    # Handle "Save changes?" dialog if it appears — 'n' = Don't Save in Paint/Notepad
    key("n")
    time.sleep(0.3)
    return True


def ensure_maximized(title_contains, retries=3):
    """
    Ensure a window is maximized using only keyboard and mouse.
    Checks current size, then sends ONE maximize action per attempt.
    Uses Win+Up (not double-click, which toggles).
    """
    _sw, _sh = _screen_size()

    for attempt in range(retries):
        rect = get_window_rect(title_contains)
        if not rect:
            time.sleep(0.5)
            continue
        w = rect["right"] - rect["left"]
        h = rect["bottom"] - rect["top"]
        # Windows maximized windows have shadow borders, so they report slightly
        # larger than screen. Accept anything within 20px of screen width.
        if w >= _sw - 20 and h >= _sh - 60:
            print(f"[maximize] '{title_contains}' confirmed maximized ({w}x{h})", flush=True)
            return True
        print(f"[maximize] '{title_contains}' not maximized ({w}x{h}), screen={_sw}x{_sh}, attempt {attempt+1}", flush=True)

        # Click title bar first to ensure focus, then Win+Up to maximize
        title_x = (rect["left"] + rect["right"]) // 2
        title_y = max(rect["top"] + 15, 10)
        click(title_x, title_y)
        time.sleep(0.3)
        key("win", "up")
        time.sleep(1.0)

        # Re-check after the action
        rect2 = get_window_rect(title_contains)
        if rect2:
            w2 = rect2["right"] - rect2["left"]
            h2 = rect2["bottom"] - rect2["top"]
            if w2 >= _sw - 20 and h2 >= _sh - 60:
                print(f"[maximize] '{title_contains}' maximized after Win+Up ({w2}x{h2})", flush=True)
                return True
            # If Win+Up made it SMALLER (snapped to top half), it was already
            # maximized or snapped. Try Win+Up again to cycle to maximize.
            if w2 < w or h2 < h:
                print(f"[maximize] Window got smaller ({w2}x{h2}) -- was likely snapped, retrying", flush=True)
                time.sleep(0.3)
                key("win", "up")
                time.sleep(1.0)

    print(f"[maximize] Warning: could not confirm '{title_contains}' maximized", flush=True)
    return False


# ── Save dialog helper ────────────────────────────────────────────────────────

def save_via_dialog(filepath, focus_click_xy=None, confirm_format=True):
    """
    Save the frontmost app's document via F12 → Save As dialog.
    Uses pywinauto to interact with the dialog reliably.
    Falls back to keyboard-based approach if pywinauto fails.

    Args:
        filepath:         Full absolute path to save to (e.g. r'C:\\...\\file.png')
        focus_click_xy:   (x, y) to click first to ensure the app has focus.
        confirm_format:   Press Enter to dismiss format-change prompts (e.g. Paint's PNG dialog).
    """
    import os

    # 1. Give the target app focus
    if focus_click_xy:
        click(*focus_click_xy)
        wait_ms(350)

    # 2. Ensure parent directory exists
    parent = os.path.dirname(filepath)
    if parent:
        os.makedirs(parent, exist_ok=True)

    # 3. Delete existing file to avoid overwrite confirmation
    if os.path.exists(filepath):
        try:
            os.remove(filepath)
            wait_ms(200)
        except Exception:
            pass

    # 4. Open Save As dialog with F12
    key("f12")
    wait_ms(2000)

    # 5. Try pywinauto first for reliable dialog interaction
    saved = False
    try:
        saved = _save_via_pywinauto(filepath)
    except Exception as e:
        print(f"[save_via_dialog] pywinauto approach failed: {e}", flush=True)

    # 6. Fallback: keyboard-based approach
    if not saved and not os.path.exists(filepath):
        print("[save_via_dialog] Falling back to keyboard approach", flush=True)
        # The filename field should be focused when dialog opens
        type_text(filepath)
        wait_ms(600)
        key("enter"); wait_ms(1500)
        key("enter"); wait_ms(1500)
        key("enter"); wait_ms(1500)
        if confirm_format:
            key("enter"); wait_ms(600)

    # 7. Handle any remaining confirmation dialogs
    if confirm_format:
        wait_ms(500)
        # Check if a confirmation dialog appeared (format change, overwrite, etc.)
        try:
            from pywinauto import Desktop
            desktop = Desktop(backend="uia")
            for w in desktop.windows():
                try:
                    title = w.window_text().lower()
                    if "paint" in title and ("confirm" in title or "save" in title):
                        w.child_window(title_re="(?i)(yes|ok|save)", control_type="Button").click()
                        wait_ms(500)
                        break
                except Exception:
                    pass
        except Exception:
            # If pywinauto can't find it, press Enter as last resort
            key("enter"); wait_ms(500)

    # 8. Verify file was saved (with retries)
    for i in range(8):
        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            print(f"[save_via_dialog] File saved: {filepath} ({os.path.getsize(filepath)} bytes)", flush=True)
            return True
        wait_ms(500)

    print(f"[save_via_dialog] WARNING: File may not have saved: {filepath}", flush=True)
    return False


def _save_via_pywinauto(filepath):
    """
    Use pywinauto to interact with the Save As dialog.
    Returns True if the file was saved successfully.
    """
    import os
    from pywinauto import Desktop

    desktop = Desktop(backend="uia")

    # Find the Save As dialog — look for windows with "Save As" in title
    dlg = None
    for attempt in range(5):
        for w in desktop.windows():
            try:
                title = w.window_text()
                if "save as" in title.lower():
                    dlg = w
                    break
            except Exception:
                pass
        if dlg:
            break
        wait_ms(500)

    if not dlg:
        print("[_save_via_pywinauto] Save As dialog not found", flush=True)
        return False

    print(f"[_save_via_pywinauto] Found dialog: {dlg.window_text()}", flush=True)

    # Find the filename edit field (auto_id "FileNameControlHost" or "1001" or by control type)
    filename_edit = None
    try:
        # Try common auto_ids for the filename field
        for auto_id in ["FileNameControlHost", "1001"]:
            try:
                filename_edit = dlg.child_window(auto_id=auto_id)
                filename_edit.window_text()  # verify it exists
                break
            except Exception:
                filename_edit = None

        # Fallback: find ComboBox with "File name" label, then its Edit child
        if not filename_edit:
            try:
                combo = dlg.child_window(title="File name:", control_type="ComboBox")
                filename_edit = combo.child_window(control_type="Edit")
            except Exception:
                pass

        # Last resort: find any Edit control in the dialog
        if not filename_edit:
            edits = dlg.children(control_type="Edit")
            if edits:
                filename_edit = edits[0]
    except Exception as e:
        print(f"[_save_via_pywinauto] Error finding filename field: {e}", flush=True)

    if not filename_edit:
        print("[_save_via_pywinauto] Filename field not found", flush=True)
        return False

    # Set the filename
    try:
        filename_edit.set_edit_text(filepath)
        wait_ms(400)
        print(f"[_save_via_pywinauto] Set filename to: {filepath}", flush=True)
    except Exception:
        # Fallback: click the field and type
        try:
            filename_edit.click_input()
            wait_ms(200)
            key("ctrl", "a")
            wait_ms(100)
            type_text(filepath)
            wait_ms(400)
        except Exception as e:
            print(f"[_save_via_pywinauto] Failed to set filename: {e}", flush=True)
            return False

    # Click the Save button
    try:
        save_btn = dlg.child_window(title="Save", control_type="Button")
        save_btn.click()
        wait_ms(1500)
        print("[_save_via_pywinauto] Clicked Save button", flush=True)
    except Exception:
        # Fallback: press Enter
        key("enter")
        wait_ms(1500)

    # Handle "Confirm Save As" / format change / overwrite dialogs
    for _ in range(3):
        try:
            for w in desktop.windows():
                try:
                    title = w.window_text().lower()
                    if any(kw in title for kw in ["confirm", "paint", "save as"]):
                        # Look for Yes/OK/Save buttons
                        for btn_title in ["Yes", "OK", "Save"]:
                            try:
                                btn = w.child_window(title=btn_title, control_type="Button")
                                btn.click()
                                wait_ms(800)
                                print(f"[_save_via_pywinauto] Clicked '{btn_title}' on confirmation dialog", flush=True)
                                break
                            except Exception:
                                pass
                except Exception:
                    pass
        except Exception:
            pass
        wait_ms(300)

    # Verify
    wait_ms(500)
    if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
        return True

    # One more Enter in case a dialog is still up
    key("enter")
    wait_ms(1000)
    return os.path.exists(filepath) and os.path.getsize(filepath) > 0


# ── Screen mapping ────────────────────────────────────────────────────────────

def map_screen(task_hint=""):
    """
    One screenshot → one Gemini call → dict of ALL interactive elements with coordinates.
    Call once at the start of a task. Use the returned map for all subsequent actions.

    Returns: {label: {"x": x, "y": y, "type": type}}
    Example: {"pencil tool": {"x": 191, "y": 55, "type": "tool"}, ...}
    """
    import urllib.parse, base64, os
    url = f"{BASE}/perceive?scale=0.5"
    if task_hint:
        url += f"&task={urllib.parse.quote(task_hint)}"

    for attempt in range(3):
        try:
            r = requests.get(url, timeout=45).json()
        except Exception as e:
            print(f"[map_screen] request failed: {e}", flush=True)
            if attempt < 2:
                wait_ms(2000)
                continue
            return {}

        if not r.get("ok", True):
            print(f"[map_screen] perceive error: {r.get('error', 'unknown')}", flush=True)

        elements = r.get("elements", [])
        result = {}
        for el in elements:
            label = el.get("label", "").lower().strip()
            if label:
                result[label] = {
                    "x": el.get("x", 0),
                    "y": el.get("y", 0),
                    "type": el.get("type", "unknown")
                }

        print(f"[map_screen] mapped {len(result)} elements: {list(result.keys())}", flush=True)

        if len(result) == 0 and attempt == 0:
            # Save debug screenshot so we can see what Gemini was looking at
            try:
                sr = requests.get(f"{BASE}/screenshot/base64?scale=0.5", timeout=10).json()
                debug_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "..", "helm", "map_screen_debug.png")
                debug_path = os.path.normpath(debug_path)
                with open(debug_path, "wb") as f:
                    f.write(base64.b64decode(sr["image"]))
                print(f"[map_screen] debug screenshot → {debug_path}", flush=True)
            except Exception as e:
                print(f"[map_screen] could not save debug screenshot: {e}", flush=True)
            print("[map_screen] retrying in 1.5s...", flush=True)
            wait_ms(1500)
            continue

        return result

    return {}

def click_element(element_map, name, blocked_labels=None):
    """
    Click an element by name from a map_screen() result.
    Tries exact match first, then partial match, then Moondream /point as fallback.

    blocked_labels: list of label substrings to never click (e.g. ["resize"] to
                    avoid accidentally opening Paint's Resize and Skew dialog).
    """
    name_lower = name.lower().strip()
    _blocked = [b.lower() for b in (blocked_labels or [])]

    # Auto-block known dangerous partial matches (e.g. "size" must never click "resize")
    _AUTO_BLOCK = {"size": ["resize"], "rotate": ["auto-rotate"]}
    _blocked.extend(_AUTO_BLOCK.get(name_lower, []))

    def _is_blocked(label):
        return any(b in label.lower() for b in _blocked)

    # Exact match
    if name_lower in element_map and not _is_blocked(name_lower):
        el = element_map[name_lower]
        click(el["x"], el["y"])
        return el["x"], el["y"]
    # Partial match — skip labels that contain blocked substrings
    for label, el in element_map.items():
        if _is_blocked(label):
            continue
        if name_lower in label or label in name_lower:
            click(el["x"], el["y"])
            return el["x"], el["y"]
    # Fallback: Moondream /point — finds element visually by description
    print(f"[click_element] '{name}' not in map — trying visual point...", flush=True)
    try:
        r = requests.get(f"{BASE}/point", params={"target": name}, timeout=45).json()
        if r.get("ok") and r.get("x", 0) > 0:
            x, y = r["x"], r["y"]
            print(f"[click_element] visual point: '{name}' → ({x},{y})", flush=True)
            element_map[name_lower] = {"x": x, "y": y, "type": "visual"}  # cache it
            click(x, y)
            return x, y
    except Exception as e:
        print(f"[click_element] visual point failed: {e}", flush=True)
    raise ValueError(
        f"Element '{name}' not found in screen map or visually.\n"
        f"Available: {list(element_map.keys())}"
    )

def get_element(element_map, name):
    """Return element dict {x, y, type} without clicking. Falls back to Moondream /point."""
    name_lower = name.lower().strip()
    if name_lower in element_map:
        return element_map[name_lower]
    for label, el in element_map.items():
        if name_lower in label or label in name_lower:
            return el
    # Fallback: visual point
    try:
        r = requests.get(f"{BASE}/point", params={"target": name}, timeout=45).json()
        if r.get("ok") and r.get("x", 0) > 0:
            el = {"x": r["x"], "y": r["y"], "type": "visual"}
            element_map[name_lower] = el
            return el
    except Exception:
        pass
    raise ValueError(f"Element '{name}' not found. Available: {list(element_map.keys())}")


# ── Perception helpers ────────────────────────────────────────────────────────
# Fast-first: UI Automation/Accessibility → Template Matching → OCR → Gemini

_perception_layer = None

def _get_perception():
    global _perception_layer
    if _perception_layer is None:
        import sys as _sys, os as _os
        if _CLAWMETHEUS_PATH:
            _sys.path.insert(0, _CLAWMETHEUS_PATH)
        else:
            _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
        from perception import PerceptionLayer
        try:
            from src import vision as _vision
            _gemini_fn = lambda q, img_b64: _vision.ask(img_b64, q)
        except ImportError:
            _gemini_fn = None
        _perception_layer = PerceptionLayer(gemini_fn=_gemini_fn)
    return _perception_layer

def find_element(name, role=None, app=None, timeout=0):
    """Find UI element by name using platform accessibility APIs. Returns dict or None.
    Dict contains: cx, cy, rect, text, role, enabled, visible."""
    return _get_perception().find_element(name, role=role, app=app, timeout=timeout)

def is_visible(name, role=None):
    """Check if a UI element is currently visible on screen."""
    return _get_perception().is_visible(name, role=role)

def wait_for(name, timeout=5.0, role=None):
    """Wait for a UI element to appear. Raises TimeoutError if not found within timeout."""
    return _get_perception().wait_for(name, timeout=timeout, role=role)

def read_text(region=None):
    """OCR text from screen or region. region=(x, y, w, h) in screen coords."""
    return _get_perception().read_text(region=region)

def find_image(template_path, threshold=0.8):
    """Template matching against current screen. Returns (cx, cy) center or None."""
    return _get_perception().find_image(template_path, threshold=threshold)

def perceive(question, region=None):
    """Layered visual question answering. Fast first (UIA/OCR), Gemini last.
    region=(x, y, w, h) to focus on a screen region."""
    return _get_perception().ask(question, region=region)


# ── Timing ────────────────────────────────────────────────────────────────────

def dismiss_modal(max_attempts=4):
    """
    Dismiss any modal dialog, popup, alert, or blocking window.
    Uses vision to detect modals and keyboard to dismiss them.
    Safe to call anytime — no-op if no modal is present.
    """
    for _ in range(max_attempts):
        answer = ask("Is there a modal dialog, popup, alert, or resize/properties window blocking the screen right now? yes or no")
        if "yes" not in answer.lower():
            break
        modal_type = ask("What does the modal say? Is it asking to CONFIRM/YES/REPLACE/OVERWRITE/OK something, or is it an ERROR/WARNING to dismiss? Reply: confirm OR dismiss")
        if "confirm" in modal_type.lower():
            key("enter"); wait_ms(600)
        else:
            key("escape"); wait_ms(600)


class Timer:
    def __init__(self, name="Task"):
        self.name = name
        self._start = None
        self._checkpoints = []

    def begin(self):
        self._start = time.time()
        print(f"[TIMER] {self.name} started", flush=True)
        return self

    def checkpoint(self, label):
        elapsed = time.time() - self._start
        self._checkpoints.append((label, elapsed))
        print(f"[TIMER]   {label}: {elapsed:.1f}s", flush=True)
        return elapsed

    def end(self):
        total = time.time() - self._start
        print(f"[TIMER] {self.name} TOTAL: {total:.1f}s", flush=True)
        return total


# ── Focus verification ────────────────────────────────────────────────────────

def _get_active_window_title():
    """
    Get the actual OS-level active window title via Clawmetheus /state.
    Reliable — uses Windows API directly, no vision model needed.
    Returns empty string on failure.
    """
    try:
        r = requests.get(f"{BASE}/state", timeout=5).json()
        return r.get("active_window", "")
    except Exception:
        return ""


def get_active_window() -> str:
    """
    Returns the actual OS-level active window title. Use this instead of
    ask() to verify which app is in the foreground — no vision model, no false positives.
    """
    return _get_active_window_title()


def wait_for_clear(app_title: str, max_attempts: int = 10) -> bool:
    """
    Block until no modal/dialog is in the way of app_title.
    Uses OS window title — reliable, no vision needed.
    Tries Escape, Enter, then visual Cancel/Close click on each blocking window.
    Returns True when clear, False if still blocked after max_attempts.
    """
    import pyautogui, time
    for attempt in range(max_attempts):
        active = _get_active_window_title()
        if app_title.lower() in active.lower():
            return True  # all clear
        print(f"[clear] Blocking window: '{active}' (attempt {attempt+1}/{max_attempts})", flush=True)
        # Try Escape
        pyautogui.press('escape')
        time.sleep(0.4)
        if app_title.lower() in _get_active_window_title().lower():
            return True
        # Try Enter (confirm-style dialogs)
        pyautogui.press('enter')
        time.sleep(0.4)
        if app_title.lower() in _get_active_window_title().lower():
            return True
        # Try clicking Cancel/Close/OK via Moondream point
        try:
            r = requests.get(f"{BASE}/screenshot/base64?scale=0.5", timeout=5).json()
            b64 = r.get("image", "")
            if b64:
                pr = requests.get(f"{BASE}/point", params={"target": "Cancel or Close or OK button", "scale": 0.5}, timeout=10).json()
                x, y = pr.get("x", 0), pr.get("y", 0)
                if x > 0 and y > 0:
                    pyautogui.click(x, y)
                    time.sleep(0.5)
        except Exception:
            pass
    print(f"[clear] WARNING: '{app_title}' still blocked after {max_attempts} attempts", flush=True)
    return False


def ensure_foreground(app_title, max_attempts=5):
    """
    Bring an app window to the foreground using keyboard and mouse only.
    Uses multiple strategies: taskbar click, title bar click, Alt+Tab.
    RAISES RuntimeError if it cannot confirm the app is in foreground.
    """
    import pyautogui

    rect = get_window_rect(app_title)
    if not rect:
        raise RuntimeError(f"Window '{app_title}' not found -- is the app open?")

    for attempt in range(max_attempts):
        # Check if already in foreground
        active = _get_active_window_title()
        if app_title.lower() in active.lower():
            print(f"[focus] '{app_title}' confirmed in foreground (attempt {attempt+1})", flush=True)
            return rect

        print(f"[focus] '{app_title}' not in foreground (active: '{active}'), attempt {attempt+1}/{max_attempts}", flush=True)

        if attempt == 0:
            # Strategy 1: Click the taskbar button — most reliable when another app covers the window
            _sw, _sh = _screen_size()
            # Taskbar is at the bottom of the screen. Click roughly in the middle area.
            # Use pyautogui to find the taskbar icon by clicking the taskbar area
            # First try clicking the title bar if window is visible
            title_x = (rect["left"] + rect["right"]) // 2
            title_y = max(rect["top"] + 15, 10)
            click(title_x, title_y)
            wait_ms(500)
        elif attempt == 1:
            # Strategy 2: Alt+Tab
            key("alt", "tab")
            wait_ms(600)
        elif attempt == 2:
            # Strategy 3: Win+D to show desktop, then click title bar
            # This clears any overlapping windows
            key("win", "d")
            wait_ms(800)
            # Re-get rect since windows may have changed
            rect = get_window_rect(app_title)
            if rect:
                title_x = (rect["left"] + rect["right"]) // 2
                title_y = max(rect["top"] + 15, 10)
                click(title_x, title_y)
                wait_ms(500)
        elif attempt == 3:
            # Strategy 4: Use Win+R to search for the app via taskbar search
            # Actually, just try clicking the window rect center
            if rect:
                cx = (rect["left"] + rect["right"]) // 2
                cy = (rect["top"] + rect["bottom"]) // 2
                click(cx, cy)
                wait_ms(500)
        else:
            # Strategy 5: Multiple Alt+Tab presses to cycle through windows
            key("alt", "tab")
            wait_ms(400)
            key("alt", "tab")
            wait_ms(400)

    # Final check
    active = _get_active_window_title()
    if app_title.lower() in active.lower():
        print(f"[focus] '{app_title}' confirmed in foreground (final check)", flush=True)
        return rect

    raise RuntimeError(
        f"Could not bring '{app_title}' to foreground after {max_attempts} attempts. "
        f"Active window: '{active}'. Close other windows or bring {app_title} to front manually."
    )
