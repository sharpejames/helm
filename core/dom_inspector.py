"""Chrome DevTools Protocol client for web element discovery.

Connects to Chrome via CDP on port 9222 (Chrome must be launched with
--remote-debugging-port=9222). Provides CSS selector queries that return
element coordinates in screen-space, adjusted for browser chrome offset.

Connection is lazy — only established when the first web action is requested.
If CDP connection fails, the `connected` property allows callers to fall back
to UIA-based element discovery.
"""

import asyncio
import json
import logging

import httpx
import websockets

logger = logging.getLogger(__name__)


class DOMInspector:
    """Chrome DevTools Protocol client for web element discovery."""

    def __init__(self):
        self._ws = None
        self._msg_id = 0
        self._port: int = 9222
        self._connected: bool = False

    @property
    def connected(self) -> bool:
        """Whether CDP connection is active."""
        return self._connected and self._ws is not None

    async def connect(self, port: int = 9222) -> bool:
        """Connect to Chrome CDP. Returns True if connected."""
        self._port = port
        try:
            # Get the first available page/tab's websocket debugger URL
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"http://127.0.0.1:{port}/json", timeout=5.0
                )
                targets = resp.json()

            # Find a page target (not extension, devtools, etc.)
            ws_url = None
            for target in targets:
                if target.get("type") == "page":
                    ws_url = target.get("webSocketDebuggerUrl")
                    break

            if not ws_url:
                logger.error("CDP: No page target found on port %d", port)
                self._connected = False
                return False

            self._ws = await websockets.connect(ws_url, max_size=10 * 1024 * 1024)
            self._connected = True

            # Enable DOM domain
            await self._send("DOM.enable")
            await self._send("Runtime.enable")

            logger.info("CDP: Connected to Chrome on port %d", port)
            return True

        except Exception as e:
            logger.error("CDP: Failed to connect on port %d — %s", port, e)
            self._connected = False
            self._ws = None
            return False

    async def disconnect(self):
        """Close CDP websocket connection."""
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        self._connected = False
        logger.info("CDP: Disconnected")

    async def _ensure_connected(self) -> bool:
        """Lazy connection — connect on first use if not already connected."""
        if not self.connected:
            return await self.connect(self._port)
        return True

    async def _send(self, method: str, params: dict | None = None) -> dict:
        """Send a CDP command and wait for the response."""
        if self._ws is None:
            raise ConnectionError("CDP: Not connected")

        self._msg_id += 1
        msg = {"id": self._msg_id, "method": method}
        if params:
            msg["params"] = params

        await self._ws.send(json.dumps(msg))

        # Wait for the matching response (skip events)
        while True:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
            data = json.loads(raw)
            if data.get("id") == self._msg_id:
                if "error" in data:
                    raise RuntimeError(
                        f"CDP error: {data['error'].get('message', data['error'])}"
                    )
                return data.get("result", {})

    async def _get_browser_chrome_offset(self) -> tuple[int, int]:
        """Get the offset from browser window origin to the content viewport.

        This accounts for the title bar, address bar, bookmarks bar, etc.
        Returns (offset_x, offset_y) in screen pixels.
        """
        try:
            # Get the window bounds (outer browser window)
            window = await self._send("Browser.getWindowBounds", {
                "windowId": (await self._send(
                    "Browser.getWindowForTarget"
                ))["windowId"]
            })
            bounds = window.get("bounds", {})
            win_x = bounds.get("left", 0)
            win_y = bounds.get("top", 0)

            # Get the layout viewport (content area position)
            layout = await self._send("Page.getLayoutMetrics")
            css_viewport = layout.get("cssVisualViewport", {})
            page_x = css_viewport.get("pageX", 0)
            page_y = css_viewport.get("pageY", 0)

            # Evaluate JS to get the screen position of the viewport
            result = await self.evaluate_js(
                "JSON.stringify({x: window.screenX + window.outerWidth - window.innerWidth, "
                "y: window.screenY + window.outerHeight - window.innerHeight})"
            )
            if result and isinstance(result, str):
                pos = json.loads(result)
                return int(pos["x"]), int(pos["y"])

            # Fallback: estimate from window bounds
            # Typical Chrome chrome: ~8px left, ~80px top (title + address + bookmarks)
            return win_x + 8, win_y + 80

        except Exception as e:
            logger.warning("CDP: Could not determine chrome offset — %s", e)
            return 0, 0

    async def query_selector(self, css: str) -> list[dict]:
        """Find elements by CSS selector.

        Returns list of dicts with screen-space coordinates:
        [{"nodeId": int, "x": int, "y": int, "width": int, "height": int, "text": str}, ...]
        """
        if not await self._ensure_connected():
            return []

        try:
            # Get the document root
            doc = await self._send("DOM.getDocument", {"depth": 0})
            root_id = doc["root"]["nodeId"]

            # Query all matching nodes
            result = await self._send("DOM.querySelectorAll", {
                "nodeId": root_id,
                "selector": css,
            })
            node_ids = result.get("nodeIds", [])

            if not node_ids:
                return []

            offset_x, offset_y = await self._get_browser_chrome_offset()
            elements = []

            for node_id in node_ids:
                try:
                    el = await self._get_element_info(node_id, offset_x, offset_y)
                    if el:
                        elements.append(el)
                except Exception as e:
                    logger.debug("CDP: Skipping node %d — %s", node_id, e)

            return elements

        except Exception as e:
            logger.error("CDP: query_selector('%s') failed — %s", css, e)
            return []

    async def _get_element_info(
        self, node_id: int, offset_x: int, offset_y: int
    ) -> dict | None:
        """Get bounding rect and text for a single DOM node.

        Coordinates are converted to screen-space using the provided offset.
        """
        # Resolve to a Runtime object so we can call getBoundingClientRect
        remote = await self._send("DOM.resolveNode", {"nodeId": node_id})
        object_id = remote["object"]["objectId"]

        try:
            # Get bounding rect via JS
            rect_result = await self._send("Runtime.callFunctionOn", {
                "objectId": object_id,
                "functionDeclaration": """function() {
                    var r = this.getBoundingClientRect();
                    return JSON.stringify({
                        x: Math.round(r.x),
                        y: Math.round(r.y),
                        width: Math.round(r.width),
                        height: Math.round(r.height)
                    });
                }""",
                "returnByValue": True,
            })
            rect_str = rect_result["result"]["value"]
            rect = json.loads(rect_str)

            # Get visible text
            text_result = await self._send("Runtime.callFunctionOn", {
                "objectId": object_id,
                "functionDeclaration": """function() {
                    return (this.innerText || this.textContent || this.value || '').trim().substring(0, 200);
                }""",
                "returnByValue": True,
            })
            text = text_result["result"].get("value", "")

            return {
                "nodeId": node_id,
                "x": rect["x"] + offset_x,
                "y": rect["y"] + offset_y,
                "width": rect["width"],
                "height": rect["height"],
                "text": text or "",
            }
        finally:
            # Release the remote object
            try:
                await self._send("Runtime.releaseObject", {"objectId": object_id})
            except Exception:
                pass

    async def query_selector_one(self, css: str) -> dict | None:
        """Find first matching element. Returns bounding rect + text or None."""
        if not await self._ensure_connected():
            return None

        try:
            doc = await self._send("DOM.getDocument", {"depth": 0})
            root_id = doc["root"]["nodeId"]

            result = await self._send("DOM.querySelector", {
                "nodeId": root_id,
                "selector": css,
            })
            node_id = result.get("nodeId", 0)

            if not node_id:
                return None

            offset_x, offset_y = await self._get_browser_chrome_offset()
            return await self._get_element_info(node_id, offset_x, offset_y)

        except Exception as e:
            logger.error("CDP: query_selector_one('%s') failed — %s", css, e)
            return None

    async def get_page_url(self) -> str:
        """Get current page URL."""
        if not await self._ensure_connected():
            return ""

        try:
            result = await self.evaluate_js("window.location.href")
            return result or ""
        except Exception as e:
            logger.error("CDP: get_page_url failed — %s", e)
            return ""

    async def evaluate_js(self, expression: str) -> any:
        """Execute JavaScript in page context. For complex DOM queries."""
        if not await self._ensure_connected():
            return None

        try:
            result = await self._send("Runtime.evaluate", {
                "expression": expression,
                "returnByValue": True,
            })
            resp = result.get("result", {})
            if resp.get("type") == "undefined":
                return None
            return resp.get("value")
        except Exception as e:
            logger.error("CDP: evaluate_js failed — %s", e)
            return None
