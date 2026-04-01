"""
Windows UI Automation wrapper for desktop app element discovery.

Uses comtypes and UIAutomationCore COM interface to find, inspect,
and interact with native Windows application UI elements.

This is a Windows-only module. Requires comtypes to be installed.
"""

import logging
import time
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# Lazy-loaded COM references (initialized on first use)
_uia = None
_uia_lock = threading.Lock()

# UIA Control Type IDs — subset of common types
CONTROL_TYPE_IDS = {
    "Button": 50000,
    "Calendar": 50001,
    "CheckBox": 50002,
    "ComboBox": 50003,
    "Edit": 50004,
    "Hyperlink": 50005,
    "Image": 50006,
    "ListItem": 50007,
    "List": 50008,
    "Menu": 50009,
    "MenuBar": 50010,
    "MenuItem": 50011,
    "ProgressBar": 50012,
    "RadioButton": 50013,
    "ScrollBar": 50014,
    "Slider": 50015,
    "Spinner": 50016,
    "StatusBar": 50017,
    "Tab": 50018,
    "TabItem": 50019,
    "Text": 50020,
    "ToolBar": 50021,
    "ToolTip": 50022,
    "Tree": 50023,
    "TreeItem": 50024,
    "Custom": 50025,
}

# Reverse lookup: ID → name
CONTROL_TYPE_NAMES = {v: k for k, v in CONTROL_TYPE_IDS.items()}


def _get_uia():
    """Lazy-initialize the UI Automation COM object. Thread-safe."""
    global _uia
    if _uia is not None:
        return _uia
    with _uia_lock:
        if _uia is not None:
            return _uia
        try:
            import comtypes.client
            _uia = comtypes.client.CreateObject(
                "{ff48dba4-60ef-4201-aa87-54103eef594e}",  # CUIAutomation CLSID
                interface=None,
            )
            # Get the IUIAutomation interface
            from comtypes.gen.UIAutomationClient import IUIAutomation
            _uia = _uia.QueryInterface(IUIAutomation)
            logger.info("UI Automation COM initialized")
        except Exception as e:
            logger.error(f"Failed to initialize UI Automation COM: {e}")
            _uia = None
        return _uia


def _element_to_dict(element) -> dict | None:
    """Convert a UIA element to a dict with name, position, size, and control type."""
    try:
        rect = element.CurrentBoundingRectangle
        x = rect.left
        y = rect.top
        width = rect.right - rect.left
        height = rect.bottom - rect.top

        # Skip zero-size or off-screen elements
        if width <= 0 or height <= 0:
            return None

        control_type_id = element.CurrentControlType
        control_type_name = CONTROL_TYPE_NAMES.get(control_type_id, f"Unknown({control_type_id})")

        return {
            "name": element.CurrentName or "",
            "automation_id": element.CurrentAutomationId or "",
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "control_type": control_type_name,
        }
    except Exception as e:
        logger.debug(f"Failed to read element properties: {e}")
        return None


class UIAModule:
    """Windows UI Automation for desktop app element discovery.

    Caches element trees per window title to avoid repeated tree walks.
    Implements a 5-second timeout on element search before returning None.
    """

    SEARCH_TIMEOUT = 5.0  # seconds

    def __init__(self):
        self._tree_cache: dict[str, dict] = {}  # window_title → element tree

    def _get_root_element(self, window_title: str | None = None):
        """Get the root UIA element — either a specific window or the desktop root."""
        uia = _get_uia()
        if uia is None:
            return None

        if window_title is None:
            return uia.GetRootElement()

        # Search for window by title
        from comtypes.gen.UIAutomationClient import (
            IUIAutomation,
            TreeScope_Children,
        )

        root = uia.GetRootElement()
        condition = uia.CreatePropertyCondition(30005, window_title)  # UIA_NamePropertyId
        window = root.FindFirst(TreeScope_Children, condition)

        if window is not None:
            return window

        # Try partial match — find windows whose title contains the search string
        children = root.FindAll(TreeScope_Children, uia.CreateTrueCondition())
        if children is not None:
            for i in range(children.Length):
                child = children.GetElement(i)
                try:
                    name = child.CurrentName or ""
                    if window_title.lower() in name.lower():
                        return child
                except Exception:
                    continue

        return None

    def _build_condition(self, uia, name: str = None, automation_id: str = None,
                         control_type: str = None):
        """Build a UIA property condition from search criteria."""
        conditions = []

        if name is not None:
            conditions.append(uia.CreatePropertyCondition(30005, name))  # UIA_NamePropertyId

        if automation_id is not None:
            conditions.append(uia.CreatePropertyCondition(30011, automation_id))  # UIA_AutomationIdPropertyId

        if control_type is not None:
            ct_id = CONTROL_TYPE_IDS.get(control_type)
            if ct_id is not None:
                conditions.append(uia.CreatePropertyCondition(30003, ct_id))  # UIA_ControlTypePropertyId

        if not conditions:
            return uia.CreateTrueCondition()

        # AND all conditions together
        result = conditions[0]
        for cond in conditions[1:]:
            result = uia.CreateAndCondition(result, cond)
        return result

    def find_element(self, name: str = None, automation_id: str = None,
                     control_type: str = None, window_title: str = None) -> dict | None:
        """Find a UI element matching the given criteria.

        Searches with a 5-second timeout. Returns element dict with name, x, y,
        width, height, control_type — or None if not found within timeout.

        Requirement 4.1: Discover UI elements by automation ID, name, or control type.
        Requirement 4.2: Return element bounding rectangle for precise click targeting.
        Requirement 4.3: 5-second timeout before returning None.
        """
        uia = _get_uia()
        if uia is None:
            logger.warning("UIA not available — COM not initialized")
            return None

        from comtypes.gen.UIAutomationClient import TreeScope_Descendants

        deadline = time.monotonic() + self.SEARCH_TIMEOUT
        condition = self._build_condition(uia, name, automation_id, control_type)

        while time.monotonic() < deadline:
            root = self._get_root_element(window_title)
            if root is None:
                time.sleep(0.3)
                continue

            try:
                element = root.FindFirst(TreeScope_Descendants, condition)
                if element is not None:
                    result = _element_to_dict(element)
                    if result is not None:
                        return result
            except Exception as e:
                logger.debug(f"UIA search error: {e}")

            time.sleep(0.3)

        logger.info(
            f"UIA element not found within {self.SEARCH_TIMEOUT}s: "
            f"name={name}, automation_id={automation_id}, control_type={control_type}"
        )
        return None

    def find_all(self, control_type: str = None, window_title: str = None) -> list[dict]:
        """Find all matching elements in the specified or active window.

        Requirement 4.1: Discover UI elements by control type.
        Requirement 4.2: Return bounding rectangles.
        """
        uia = _get_uia()
        if uia is None:
            return []

        from comtypes.gen.UIAutomationClient import TreeScope_Descendants

        root = self._get_root_element(window_title)
        if root is None:
            return []

        condition = self._build_condition(uia, control_type=control_type)

        try:
            elements = root.FindAll(TreeScope_Descendants, condition)
        except Exception as e:
            logger.error(f"UIA find_all error: {e}")
            return []

        results = []
        if elements is not None:
            for i in range(elements.Length):
                el = elements.GetElement(i)
                info = _element_to_dict(el)
                if info is not None:
                    results.append(info)

        return results

    def _build_tree_recursive(self, element, depth: int, max_depth: int) -> dict | None:
        """Recursively build element tree dict from a UIA element."""
        info = _element_to_dict(element)
        if info is None:
            return None

        info["children"] = []

        if depth >= max_depth:
            return info

        uia = _get_uia()
        if uia is None:
            return info

        from comtypes.gen.UIAutomationClient import TreeScope_Children

        try:
            children = element.FindAll(TreeScope_Children, uia.CreateTrueCondition())
            if children is not None:
                for i in range(children.Length):
                    child = children.GetElement(i)
                    child_info = self._build_tree_recursive(child, depth + 1, max_depth)
                    if child_info is not None:
                        info["children"].append(child_info)
        except Exception as e:
            logger.debug(f"Error walking children: {e}")

        return info

    def get_element_tree(self, window_title: str = None, max_depth: int = 3) -> dict:
        """Get the UI element tree for the specified window. Cached per window_title.

        Requirement 4.4: Cache element tree to avoid repeated tree walks.
        """
        cache_key = window_title or "__desktop__"

        if cache_key in self._tree_cache:
            logger.debug(f"Returning cached element tree for '{cache_key}'")
            return self._tree_cache[cache_key]

        root = self._get_root_element(window_title)
        if root is None:
            return {"error": f"Window not found: {window_title}", "children": []}

        tree = self._build_tree_recursive(root, depth=0, max_depth=max_depth)
        if tree is None:
            tree = {"error": "Failed to read root element", "children": []}

        self._tree_cache[cache_key] = tree
        logger.debug(f"Cached element tree for '{cache_key}' (depth={max_depth})")
        return tree

    def click_element(self, name: str = None, automation_id: str = None) -> bool:
        """Find an element and click its center point.

        Returns True if the element was found and clicked, False otherwise.
        Uses pyautogui for the actual click (consistent with core/input.py).
        """
        element = self.find_element(name=name, automation_id=automation_id)
        if element is None:
            return False

        center_x = element["x"] + element["width"] // 2
        center_y = element["y"] + element["height"] // 2

        try:
            import pyautogui
            pyautogui.click(center_x, center_y)
            logger.info(f"Clicked element at ({center_x}, {center_y}): name={name}, id={automation_id}")
            return True
        except Exception as e:
            logger.error(f"Click failed: {e}")
            return False

    def invalidate_cache(self, window_title: str = None):
        """Clear cached element tree. Call after window state changes.

        If window_title is provided, only that window's cache is cleared.
        If None, all cached trees are cleared.
        """
        if window_title is not None:
            cache_key = window_title or "__desktop__"
            removed = self._tree_cache.pop(cache_key, None)
            if removed:
                logger.debug(f"Invalidated cache for '{cache_key}'")
        else:
            count = len(self._tree_cache)
            self._tree_cache.clear()
            logger.debug(f"Invalidated all cached element trees ({count} entries)")
