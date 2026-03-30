"""
agent/step_executor.py — Hybrid step-based task executor for Helm.

Architecture (Option C — Hybrid local/remote):
  1. Remote Claude receives task + screen state → returns structured drawing plan
  2. Local Qwen2.5-3B follows the plan, emitting one JSON action per step (~2-3s each)
  3. On error/unexpected: escalate back to Claude for replanning
  4. Fallback: if local LLM unavailable, use remote-only mode (original behavior)

This replaces the monolithic remote-only approach. ~70% of LLM time is eliminated
by using the local model for routine step execution.
"""

import asyncio
import json
import os
import re
import logging
import hashlib
import requests
import time as _time
from datetime import datetime, timezone
from typing import AsyncIterator

from agent.models import (
    LLMClient, LocalLLMClient, ModelRouter,
    get_local_llm, get_router,
    TIER_LOCAL, TIER_FAST, TIER_SMART,
)
from agent.actions import (
    ACTION_REGISTRY, ActionResult, execute_action, get_action_catalog,
    _ask_screen, _screenshot_b64, _get_active_window,
)

logger = logging.getLogger(__name__)

CLAWMETHEUS_URL = "http://127.0.0.1:7331"
MAX_STEPS = 80
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")

# Performance tuning
STEP_MAX_TOKENS = 1024
STEP_LLM_TIMEOUT = 60.0        # Remote LLM timeout
LOCAL_LLM_TIMEOUT = 15.0       # Local LLM timeout — should be fast
ACTION_TIMEOUT_SECS = 45
HEALTH_CHECK_INTERVAL = 8
NO_CHANGE_THRESHOLD = 5
CONVERSATION_MAX = 24
CONVERSATION_KEEP = 14

# ── System prompts ──────────────────────────────────────────────────────────

PLANNER_SYSTEM = """You are Helm, a desktop automation planner. Given a task, canvas bounds, and screen state,
create a STRUCTURED DRAWING PLAN as JSON.

## ACTIONS AVAILABLE:
{action_catalog}

## PLAN FORMAT:
Return a JSON object with a list of steps. Each step is an action with params.
```json
{{
  "plan_summary": "Brief description of what we'll draw",
  "steps": [
    {{"action": "paint_color", "params": {{"color_name": "orange"}}}},
    {{"action": "paint_shape_tool", "params": {{"shape_name": "Oval"}}}},
    {{"action": "paint_fill_style", "params": {{"style": "Solid color"}}}},
    {{"action": "paint_draw_shape", "params": {{"x1": 1100, "y1": 500, "x2": 1450, "y2": 850}}}},
    ...
    {{"action": "save_file", "params": {{"filepath": "C:\\\\Users\\\\sharp\\\\Pictures\\\\drawing.png", "app_title": "Paint"}}}}
  ]
}}
```

## CANVAS BOUNDS — CRITICAL:
The canvas area is: left={canvas_left}, top={canvas_top}, right={canvas_right}, bottom={canvas_bottom}.
Center: cx={canvas_cx}, cy={canvas_cy}. Size: {canvas_w}x{canvas_h}.
ALL coordinates MUST be within these bounds. Coordinates outside will be clamped to the edge.
Center your drawing around (cx, cy). Use the FULL canvas area.

## PAINT RULES:
1. Do NOT include setup_paint — it's already done before planning.
2. ONLY toolbar colors: red, blue, yellow, green, orange, purple, pink, brown, black, white, gray.
3. paint_color auto-restores pencil or shape tool. No need to re-select after color change.
4. For FILLED shapes: set shape tool + fill style ONCE, then draw multiple shapes. Do NOT repeat paint_shape_tool/paint_fill_style/paint_outline_style if they haven't changed.
5. For outlines/details: paint_color → paint_draw (pencil auto-selected if needed).
6. NEVER use paint_fill_at on pencil shapes — gaps cause fill to leak.
7. NEVER use paint_fill_at AFTER drawing shapes — it will erase them.
8. If you need a colored background, use paint_fill_at FIRST (before any shapes), then draw shapes on top.
9. Canvas starts WHITE. NEVER draw white on white or black on black — ALWAYS ensure contrast.
10. Group by color to minimize switches. Draw ALL shapes of one color before switching.

## STEP BUDGET — CRITICAL:
11. Maximum {max_steps} steps. Your plan MUST be under {plan_limit} steps to leave room for save.
12. ALWAYS end with: save_file(filepath="C:\\\\Users\\\\sharp\\\\Pictures\\\\drawing.png", app_title="Paint")
13. Be EFFICIENT: don't repeat tool/style selections. Set once, draw many.
14. ONLY include Paint drawing actions in the plan. Do NOT include web/browser actions.
"""

LOCAL_EXECUTOR_SYSTEM = """You are Helm's step executor. You follow a drawing plan step by step.

Given the PLAN and current STEP INDEX, output the next action as JSON:
{{"action": "action_name", "params": {{...}}}}

Rules:
- Output ONLY the JSON object. No explanation, no markdown.
- Follow the plan exactly. If the plan says paint_color("red"), output that.
- If an action failed, adapt: skip it or try an alternative, then continue the plan.
- If you're done with all steps: {{"action": "DONE", "params": {{"summary": "..."}}}}
- If stuck after 3 failures: {{"action": "FAIL", "params": {{"reason": "..."}}}}
"""

# Remote-only fallback prompt — used for reactive tasks (games, browsing, general desktop)
STEP_SYSTEM_PROMPT = """You are Helm, a desktop automation agent. You control a Windows 11 computer step by step.
You are a SAVVY computer user — you know how to use apps, handle popups, and navigate UI efficiently.

Each turn: pick ONE action, provide params as JSON. I execute it and show the result.

## ACTIONS:
{action_catalog}

## RESPONSE FORMAT:
{{"thinking": "1-2 sentences", "action": "name", "params": {{...}}}}
When done: {{"thinking": "...", "action": "DONE", "params": {{"summary": "..."}}}}
If stuck: {{"thinking": "...", "action": "FAIL", "params": {{"reason": "..."}}}}

## CORE RULES — BE EFFICIENT:
1. ONE action per turn. ACT, don't just look.
2. NEVER use "look" or "screenshot" more than twice in a row. After looking, you MUST act on what you see.
3. Trust action results. If open_app says "opened", don't screenshot to verify — just proceed.
4. If an action FAILS, try a DIFFERENT approach. Never retry the same failing action 3 times.
5. Use EXACT parameter names from the catalog.
6. press_key with multiple keys: press_key(keys=["ctrl", "s"]).
7. You have {max_steps} steps MAX. Don't waste them on looking — spend them on doing.

## OPENING APPS:
8. Use open_app(app_exe="...", window_title="...") first. It handles search, taskbar, UWP apps.
9. Common apps: mspaint="Paint", notepad="Notepad", chrome="Chrome", "solitaire"="Solitaire".
10. For Microsoft Store apps, use the app name: open_app(app_exe="solitaire", window_title="Solitaire").
11. If open_app fails, try: press_key(keys=["win"]), type_text("app name"), press_key(keys=["return"]).
12. If the app opens minimized, use focus_app(window_title="...") to bring it to front.

## HANDLING POPUPS & UNEXPECTED UI — CRITICAL:
13. If you see a popup, dialog, error, sign-in prompt, or update notification: use dismiss_popup() immediately.
14. dismiss_popup reads the popup and clicks the right button (Cancel, Close, Skip, Not Now, X, etc.).
15. For internet/connection errors: dismiss them — most tasks don't need internet.
16. For "sign in" prompts: dismiss/skip them unless the task requires signing in.
17. For cookie consent: click Accept/OK.
18. For "save changes?" when closing: click Don't Save unless the task requires saving.
19. NEVER get stuck on a popup. Dismiss it and move on. If dismiss_popup doesn't work, try press_key(keys=["escape"]) or click the X button.
20. After dismissing a popup, continue with your task — don't restart from scratch.

## FOCUS STEALING — CRITICAL:
21. Apps sometimes open browser tabs, ads, or other windows that steal focus.
22. NEVER close a browser window — Helm runs in a browser tab. Closing it kills Helm.
23. If a browser/ad steals focus, use focus_app(window_title="YourTargetApp") to switch back.
24. If you can't find the app, try press_key(keys=["alt", "tab"]) to cycle windows.
25. The system auto-detects focus theft and refocuses, but if you notice it, use focus_app.

## DRAGGING (cards, files, windows):
26. Use drag(x1=start_x, y1=start_y, x2=end_x, y2=end_y) to drag items.
27. For card games: drag cards between columns, or double_click to auto-move to foundations.

## FILE UPLOADS ON WEBSITES — CRITICAL:
28. Do NOT use upload_file — it often fails on modern web apps.
29. Instead, use vision to find the attach/paperclip button: look("Where is the attach or paperclip button?")
30. Click the button with click(x, y) using coordinates from the vision response.
31. A file picker dialog will open. Type the file path with type_text and press Enter.
32. Wait for the file thumbnail to appear, then click the send/submit button.

## PAINT DRAWING RULES:
23. Start with setup_paint. Parse canvas_bounds from state_hint.
24. ONLY toolbar colors: red, blue, yellow, green, orange, purple, pink, brown, black, white, gray.
25. paint_color auto-restores the previous tool.
26. Canvas starts WHITE. NEVER draw white on white or black on black.

## STEP BUDGET:
27. At step {save_threshold}+, wrap up and finish the task.
28. save_file(filepath="C:\\\\Users\\\\sharp\\\\Pictures\\\\drawing.png", app_title="Paint").
"""


def _screenshot_hash() -> str | None:
    """Take a screenshot and return a perceptual hash for change detection."""
    try:
        r = requests.get(f"{CLAWMETHEUS_URL}/screenshot/base64?scale=0.25", timeout=8).json()
        img_data = r.get("image", "")
        if img_data:
            n = len(img_data)
            sample = img_data[:2000] + img_data[n//3:n//3+2000] + img_data[2*n//3:2*n//3+2000]
            return hashlib.md5(sample.encode()).hexdigest()
    except Exception:
        pass
    return None


class StepExecutor:
    """Hybrid step-based task executor. Claude plans, local LLM executes."""

    def __init__(self, llm: LLMClient, config: dict = None):
        self.llm = llm
        self.local_llm: LocalLLMClient | None = get_local_llm()
        self.router: ModelRouter | None = get_router()
        self._stopped = False
        self._run_log: list[dict] = []
        self._last_task: str | None = None
        # Speed setting: 1=careful, 3=balanced, 5=fast
        self._speed = (config or {}).get("executor", {}).get("speed", 3)
        # Vision checkpoint interval based on speed
        self._checkpoint_interval = max(5, self._speed * 5)  # 5, 15, or 25 steps
        os.makedirs(LOG_DIR, exist_ok=True)

    def _log_event(self, event_type: str, data, step: int = 0):
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            "step": step,
            "data": data if isinstance(data, str) else json.dumps(data, default=str),
        }
        self._run_log.append(entry)
        logger.info(f"[step {step}] {event_type}: {str(data)[:200]}")

    def _flush_log(self, task: str, status: str):
        if not self._run_log:
            return
        filepath = None
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            slug = re.sub(r'[^a-z0-9]+', '_', task.lower()[:40]).strip('_')
            filename = f"{ts}_{slug}_{status}.json"
            filepath = os.path.join(LOG_DIR, filename)
            log_data = {
                "task": task, "status": status, "executor": "hybrid",
                "started": self._run_log[0]["ts"] if self._run_log else None,
                "finished": datetime.now(timezone.utc).isoformat(),
                "events": self._run_log,
            }
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(log_data, f, indent=2, ensure_ascii=False)
            logger.info(f"Step log saved: {filepath}")
        except Exception as e:
            logger.error(f"Failed to save step log: {e}")
        finally:
            self._run_log = []

        # Auto-learn from this task's log
        if filepath and os.path.exists(filepath):
            try:
                from kb.learner import learn_from_log
                result = learn_from_log(filepath)
                if result.get("learned"):
                    logger.info(f"Auto-learned {len(result['learned'])} facts about {result['app']}")
            except Exception as e:
                logger.debug(f"Auto-learn failed: {e}")

    def _clawmetheus_running(self) -> bool:
        try:
            return requests.get(f"{CLAWMETHEUS_URL}/status", timeout=3).status_code == 200
        except Exception:
            return False

    def _parse_llm_response(self, text: str) -> dict | None:
        """Parse JSON from LLM output. Handles markdown fences and messy output."""
        text = re.sub(r'```(?:json)?\s*', '', text).strip().rstrip('`').strip()
        # Strip <think> blocks from reasoning models
        text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        m = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
        action_m = re.search(r'"action"\s*:\s*"(\w+)"', text)
        if action_m:
            return {"action": action_m.group(1), "params": {}, "thinking": "parse fallback"}
        return None

    def stop(self):
        self._stopped = True
        return True

    def _detect_target_app(self, task: str) -> tuple[str | None, str | None]:
        task_lower = task.lower()
        for keywords, (title, exe) in [
            (["paint", "draw", "sketch"], ("Paint", "mspaint")),
            (["solitaire", "klondike", "spider", "freecell", "card game"], ("Solitaire & Casual Games", "solitaire")),
            (["notepad"], ("Notepad", "notepad")),
            (["chrome", "browser", "web"], ("Chrome", "chrome")),
            (["outlook", "email", "mail"], ("Outlook", "outlook")),
            (["word", "document"], ("Word", "winword")),
            (["excel", "spreadsheet"], ("Excel", "excel")),
        ]:
            if any(kw in task_lower for kw in keywords):
                return title, exe
        return None, None

    async def _execute_with_timeout(self, action_name: str, params: dict,
                                     timeout: int = ACTION_TIMEOUT_SECS) -> ActionResult:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(execute_action, action_name, params), timeout=timeout)
        except asyncio.TimeoutError:
            return ActionResult(False, error=f"{action_name} timed out after {timeout}s",
                                state_hint="TIMEOUT")

    async def _check_health_and_recover(self, app_title: str, app_exe: str,
                                         step: int) -> tuple[bool, list[dict]]:
        from agent.actions import check_app_health, recover_app
        events = []
        health = await asyncio.wait_for(
            asyncio.to_thread(check_app_health, app_title), timeout=10)
        if health.ok:
            return False, events
        self._log_event("app_frozen", {"app": app_title}, step)
        events.append({"type": "warning", "data": f"⚠ {app_title} not responding"})
        events.append({"type": "status", "data": f"Recovering {app_title}..."})
        try:
            recovery = await asyncio.wait_for(
                asyncio.to_thread(recover_app, app_exe, app_title, 8), timeout=30)
        except asyncio.TimeoutError:
            recovery = ActionResult(False, error="Recovery timed out")
        self._log_event("app_recovery", {"ok": recovery.ok}, step)
        if recovery.ok:
            events.append({"type": "step", "data": f"✓ {recovery.output}"})
            return True, events
        events.append({"type": "warning", "data": f"✗ Recovery failed: {recovery.error}"})
        return False, events

    async def stream_task(self, task: str) -> AsyncIterator[dict]:
        """Execute a task step by step. Yields SSE event dicts."""
        self._stopped = False
        self._last_task = task
        self._run_log = []
        try:
            async for event in self._stream_task_inner(task):
                yield event
        except Exception as e:
            logger.error(f"stream_task crashed: {e}", exc_info=True)
            self._log_event("crash", f"Unhandled exception: {e}")
            self._flush_log(task, "failed")
            yield {"type": "error", "data": f"Internal error: {e}"}

    def _call_remote_llm(self, system: str, messages: list, max_tokens: int = None,
                          timeout: float = None, tier: str = TIER_SMART) -> str | None:
        """Call LLM with retry logic. Uses ModelRouter for tier-based routing."""
        for attempt in range(3):
            try:
                if self.router:
                    return self.router.complete(system, messages, tier=tier,
                                                 max_tokens=max_tokens or STEP_MAX_TOKENS,
                                                 timeout=timeout)
                else:
                    return self.llm.complete(system, messages,
                                              max_tokens=max_tokens or STEP_MAX_TOKENS,
                                              timeout=timeout or STEP_LLM_TIMEOUT)
            except Exception as e:
                err_str = str(e).lower()
                retryable = any(kw in err_str for kw in [
                    "timeout", "timed out", "502", "503", "504",
                    "connection", "connect", "getaddrinfo", "network",
                    "temporarily", "overloaded", "rate_limit",
                ])
                if retryable and attempt < 2:
                    wait = (attempt + 1) * 5
                    self._log_event("llm_retry", f"Attempt {attempt+1} ({tier}): {e}")
                    _time.sleep(wait)
                    continue
                raise
        return None

    def _call_local_llm(self, system: str, messages: list) -> str | None:
        """Call local LLM. Returns None if unavailable."""
        if not self.local_llm:
            return None
        try:
            return self.local_llm.complete(system, messages,
                                            max_tokens=512, timeout=LOCAL_LLM_TIMEOUT)
        except Exception as e:
            self._log_event("local_llm_error", str(e))
            return None

    async def _stream_task_inner(self, task: str) -> AsyncIterator[dict]:
        self._log_event("task_start", task)

        if not self._clawmetheus_running():
            self._log_event("error", "Clawmetheus not running")
            self._flush_log(task, "error")
            yield {"type": "error", "data": "Clawmetheus not running."}
            return

        # Initial screen state
        yield {"type": "status", "data": "Looking at screen..."}
        screen_state = _ask_screen("What application is in focus? List all visible windows briefly.")
        self._log_event("screen_state", screen_state)
        yield {"type": "step", "data": f"Screen: {screen_state[:200]}"}

        action_catalog = get_action_catalog()
        target_app, target_exe = self._detect_target_app(task)

        # ── Decide mode: hybrid (plan+local) or remote-only ──
        # Hybrid plan-then-execute ONLY works for Paint drawing tasks.
        # All other tasks (games, browsing, general desktop) use remote-only reactive mode.
        use_hybrid = self.local_llm is not None and target_app == "Paint"
        plan = None

        if use_hybrid:
            # Step 0: Run setup_paint FIRST to get canvas bounds
            yield {"type": "status", "data": "Setting up Paint canvas..."}
            setup_result = await self._execute_with_timeout("setup_paint", {})
            if not setup_result.ok:
                yield {"type": "warning", "data": f"Setup failed: {setup_result.error}. Falling back to remote-only."}
                use_hybrid = False
            else:
                self._log_event("setup_paint", setup_result.output, 0)
                yield {"type": "step", "data": f"✓ {setup_result.output}"}

                # Extract canvas bounds from setup result
                canvas_bounds = self._parse_canvas_bounds(setup_result.output)

                # Now plan WITH canvas bounds
                yield {"type": "status", "data": "🧠 Claude planning (1 remote call)..."}
                plan = await self._get_plan(task, screen_state, action_catalog, canvas_bounds)
                if plan:
                    self._log_event("plan", {"summary": plan.get("plan_summary", ""),
                                              "steps": len(plan.get("steps", []))})
                    yield {"type": "step", "data":
                        f"📋 Plan: {plan.get('plan_summary', '?')} ({len(plan.get('steps', []))} steps)"}
                    async for event in self._execute_plan(plan, task, target_app, target_exe):
                        yield event

                    # Check if the task has more to do beyond Paint (e.g. "upload to grok")
                    # If so, continue with reactive mode for the remaining work
                    task_lower = task.lower()
                    has_web_part = any(kw in task_lower for kw in [
                        "upload", "grok", "post", "share", "send", "email",
                        "browser", "website", "x.com", "twitter"])
                    if has_web_part:
                        yield {"type": "step", "data": "📤 Drawing saved. Continuing with upload/web task..."}
                        screen_state = _ask_screen("What is on screen now?")
                        # Give the reactive mode a FOCUSED task — only the upload part
                        # Tell it the drawing is DONE and where the file is saved
                        upload_task = (
                            f"The drawing has been completed and saved to C:\\Users\\sharp\\Pictures\\drawing.png. "
                            f"DO NOT go back to Paint. DO NOT redraw anything. The drawing is DONE. "
                            f"Now complete the remaining part of the original task: {task}. "
                            f"Specifically: upload the saved image file and interact with the website/app as needed. "
                            f"Use the look action to find buttons. Do NOT use upload_file — instead, "
                            f"use vision to find the attach/paperclip button, click it with click action, "
                            f"then type the file path in the file picker dialog."
                        )
                        async for event in self._execute_remote_only(
                                upload_task, screen_state, action_catalog, None, None):
                            yield event
                    return
                else:
                    yield {"type": "warning", "data": "Plan failed, falling back to remote-only mode"}

        # ── Remote-only reactive mode (games, browsing, general desktop) ──
        async for event in self._execute_remote_only(task, screen_state, action_catalog,
                                                       target_app, target_exe):
            yield event

    def _parse_canvas_bounds(self, setup_output: str) -> dict:
        """Extract canvas bounds from setup_paint output like 'Canvas ready: (512,385)->(2048,1345) = 1536x960'"""
        import re
        m = re.search(r'\((\d+),(\d+)\)->\((\d+),(\d+)\)', setup_output)
        if m:
            left, top, right, bottom = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
            return {"left": left, "top": top, "right": right, "bottom": bottom,
                    "cx": (left + right) // 2, "cy": (top + bottom) // 2,
                    "w": right - left, "h": bottom - top}
        return {"left": 512, "top": 385, "right": 2048, "bottom": 1345,
                "cx": 1280, "cy": 865, "w": 1536, "h": 960}

    async def _get_plan(self, task: str, screen_state: str, action_catalog: str,
                         canvas_bounds: dict | None = None) -> dict | None:
        """Ask remote Claude to create a structured drawing plan."""
        plan_limit = MAX_STEPS - 5  # Leave room for save + buffer
        system = (PLANNER_SYSTEM
                  .replace("{action_catalog}", action_catalog)
                  .replace("{max_steps}", str(MAX_STEPS))
                  .replace("{plan_limit}", str(plan_limit)))

        # Inject canvas bounds into the system prompt
        if canvas_bounds:
            system = (system
                      .replace("{canvas_left}", str(canvas_bounds["left"]))
                      .replace("{canvas_top}", str(canvas_bounds["top"]))
                      .replace("{canvas_right}", str(canvas_bounds["right"]))
                      .replace("{canvas_bottom}", str(canvas_bounds["bottom"]))
                      .replace("{canvas_cx}", str(canvas_bounds["cx"]))
                      .replace("{canvas_cy}", str(canvas_bounds["cy"]))
                      .replace("{canvas_w}", str(canvas_bounds["w"]))
                      .replace("{canvas_h}", str(canvas_bounds["h"])))
        else:
            # Remove canvas bounds section if not available
            system = re.sub(r'## CANVAS BOUNDS.*?Use the FULL canvas area\.', '', system, flags=re.DOTALL)

        messages = [{"role": "user", "content": (
            f"TASK: {task}\n\n"
            f"SCREEN: {screen_state}\n"
            f"ACTIVE WINDOW: {_get_active_window()}\n\n"
            f"Canvas is already set up. Bounds: {canvas_bounds}\n\n"
            f"Create a drawing plan. MUST be under {plan_limit} steps. "
            f"MUST end with save_file. Return ONLY the JSON plan object."
        )}]
        try:
            raw = await asyncio.to_thread(
                self._call_remote_llm, system, messages,
                max_tokens=4096, timeout=90.0, tier=TIER_SMART)
            if not raw:
                return None
            self._log_event("plan_raw", raw[:500])
            parsed = self._parse_llm_response(raw)
            if parsed and "steps" in parsed:
                steps = parsed["steps"]
                # Validate: ensure plan ends with save_file
                if steps and steps[-1].get("action") != "save_file":
                    steps.append({"action": "save_file",
                                  "params": {"filepath": "C:\\Users\\sharp\\Pictures\\drawing.png",
                                             "app_title": "Paint"}})
                # Truncate if over limit
                if len(steps) > plan_limit:
                    self._log_event("plan_truncated", f"{len(steps)} -> {plan_limit}")
                    steps = steps[:plan_limit - 1]
                    steps.append({"action": "save_file",
                                  "params": {"filepath": "C:\\Users\\sharp\\Pictures\\drawing.png",
                                             "app_title": "Paint"}})
                parsed["steps"] = steps
                return parsed
            if parsed and isinstance(parsed.get("plan"), list):
                return {"plan_summary": "extracted", "steps": parsed["plan"]}
        except Exception as e:
            self._log_event("plan_error", str(e))
        return None

    async def _execute_plan(self, plan: dict, task: str,
                             target_app: str | None, target_exe: str | None) -> AsyncIterator[dict]:
        """Execute a plan using the local LLM for step-by-step decisions."""
        steps = plan.get("steps", [])
        if not steps:
            yield {"type": "warning", "data": "Empty plan"}
            return

        step_num = 0
        plan_idx = 0
        consecutive_fails = 0
        action_fail_streak = 0
        no_change_streak = 0
        last_screen_hash = _screenshot_hash()
        replan_count = 0

        while step_num < MAX_STEPS and plan_idx <= len(steps):
            if self._stopped:
                self._log_event("stopped", "User requested stop", step_num)
                self._flush_log(task, "stopped")
                yield {"type": "warning", "data": "Stopped by user."}
                yield {"type": "done", "data": "Task stopped."}
                return

            step_num += 1

            # ── Get next action from local LLM ──
            if plan_idx < len(steps):
                planned_step = steps[plan_idx]
                yield {"type": "status", "data":
                    f"Step {step_num}/{MAX_STEPS} — Local LLM (step {plan_idx+1}/{len(steps)})..."}
            else:
                planned_step = {"action": "DONE", "params": {"summary": "Plan complete"}}

            # Ask local LLM to confirm/adapt the next step
            llm_start = _time.time()
            decision = await self._get_local_decision(plan, plan_idx, step_num)
            llm_elapsed = _time.time() - llm_start

            if not decision:
                # Local LLM failed — use the planned step directly
                decision = planned_step
                self._log_event("local_fallback", "Using planned step directly", step_num)

            action_name = decision.get("action", "")
            params = decision.get("params", {})
            thinking = decision.get("thinking", "")

            if thinking:
                yield {"type": "step", "data": f"💭 {thinking[:100]}"}
            self._log_event("decision", {"action": action_name, "params": params,
                                          "llm_ms": int(llm_elapsed*1000),
                                          "source": "local"}, step_num)

            # Handle DONE
            if action_name == "DONE":
                summary = params.get("summary", "Task completed")

                # Vision verification before accepting completion (skip at speed 5)
                verification_failed = False
                if self._speed < 5:
                    verify = await asyncio.to_thread(
                        _ask_screen,
                        f"The task was: '{task[:100]}'. The agent says it's done. "
                        f"Does the screen show a reasonable result? Is the work visible and recognizable? "
                        f"YES or NO with brief reason.",
                        0.75)
                    self._log_event("done_verify", verify[:300], step_num)
                    yield {"type": "step", "data": f"🔍 Verify: {verify[:100]}"}
                    first_line = verify.strip().split('\n')[0].lower().strip()
                    verification_failed = first_line.startswith("no")

                if verification_failed and replan_count < 3:
                    replan_count += 1
                    yield {"type": "warning", "data": f"⚠ Result doesn't look right. Replanning..."}
                    new_plan = await self._replan(task, plan, plan_idx,
                                                   f"Vision says result is wrong: {verify[:200]}")
                    if new_plan and new_plan.get("steps"):
                        plan = new_plan
                        steps = plan["steps"]
                        plan_idx = 0
                        yield {"type": "step", "data": f"📋 Fixing: {len(steps)} steps"}
                        continue
                    self._flush_log(task, "partial")
                    yield {"type": "done", "data": f"Partial: {summary}"}
                    return

                self._log_event("task_done", summary, step_num)
                img = _screenshot_b64()
                if img:
                    yield {"type": "artifact", "data": {"type": "screenshot", "value": img,
                                                         "label": "Final result"}}
                self._flush_log(task, "completed")
                yield {"type": "done", "data": f"Done: {summary}"}
                return

            # Handle FAIL
            if action_name == "FAIL":
                reason = params.get("reason", "Unknown")
                # Escalate to Claude for replanning
                if replan_count < 2:
                    replan_count += 1
                    yield {"type": "warning", "data": f"Local LLM stuck: {reason}. Replanning..."}
                    new_plan = await self._replan(task, plan, plan_idx, reason)
                    if new_plan and new_plan.get("steps"):
                        plan = new_plan
                        steps = plan["steps"]
                        plan_idx = 0
                        self._log_event("replan", {"steps": len(steps)}, step_num)
                        yield {"type": "step", "data": f"📋 New plan: {len(steps)} steps"}
                        continue
                self._log_event("task_fail", reason, step_num)
                self._flush_log(task, "failed")
                yield {"type": "error", "data": f"Agent gave up: {reason}"}
                return

            # Execute the action
            yield {"type": "status", "data": f"Step {step_num}/{MAX_STEPS} — {action_name}..."}
            result = await self._execute_with_timeout(action_name, params)

            self._log_event("action_result", {
                "action": action_name, "ok": result.ok,
                "output": result.output[:200], "error": (result.error or "")[:200],
            }, step_num)

            if result.ok:
                yield {"type": "step", "data": f"✓ {result.output[:150]}"}
                action_fail_streak = 0
                plan_idx += 1
            else:
                yield {"type": "warning", "data": f"✗ {action_name}: {result.error[:150]}"}
                action_fail_streak += 1
                # After 3 consecutive failures, escalate to Claude
                if action_fail_streak >= 3 and replan_count < 2:
                    replan_count += 1
                    yield {"type": "status", "data": "Escalating to Claude..."}
                    new_plan = await self._replan(task, plan, plan_idx,
                                                   f"3 consecutive failures at step {plan_idx}")
                    if new_plan and new_plan.get("steps"):
                        plan = new_plan
                        steps = plan["steps"]
                        plan_idx = 0
                        action_fail_streak = 0
                        yield {"type": "step", "data": f"📋 Replanned: {len(steps)} steps"}
                        continue
                    plan_idx += 1  # Skip failed step
                else:
                    plan_idx += 1  # Skip failed step

            # Health check on timeout or sustained failures
            timed_out = result.state_hint == "TIMEOUT" if result else False
            if target_app and (timed_out or action_fail_streak >= 3):
                try:
                    recovered, events = await self._check_health_and_recover(
                        target_app, target_exe, step_num)
                    for evt in events:
                        yield evt
                    if recovered:
                        action_fail_streak = 0
                except Exception as e:
                    self._log_event("health_check_error", str(e), step_num)

            # Focus recovery: if target app lost focus, switch back
            if target_app and action_name not in ("open_app", "open_website", "close_app"):
                active_win = _get_active_window()
                target_lower = target_app.lower()
                active_lower = active_win.lower()
                if target_lower not in active_lower and active_lower not in target_lower:
                    self._log_event("focus_stolen", f"'{active_win}' stole focus from '{target_app}'", step_num)
                    yield {"type": "warning", "data": f"⚡ Focus stolen by '{active_win}' — switching back"}
                    try:
                        refocus = await asyncio.to_thread(
                            execute_action, "focus_app", {"window_title": target_app})
                        if not refocus.ok:
                            import pyautogui
                            pyautogui.hotkey('alt', 'tab')
                            await asyncio.sleep(0.5)
                    except Exception:
                        pass

            # Screenshot after non-trivial actions
            SKIP_SCREENSHOT = {"paint_pencil", "paint_fill_tool", "paint_color",
                                "paint_fill_style", "paint_outline_style", "wait",
                                "press_key", "focus_app", "paint_shape_tool"}
            if action_name not in SKIP_SCREENSHOT or not result.ok:
                img = result.screenshot if result else None
                if not img:
                    img = _screenshot_b64()
                if img:
                    yield {"type": "artifact", "data": {
                        "type": "screenshot", "value": img,
                        "label": f"Step {step_num}: {action_name}"
                    }}

            # ── Universal vision checkpoint ──
            # Every 15 steps, verify things are on track (works for any app)
            if step_num % self._checkpoint_interval == 0 and step_num > 5:
                check = await asyncio.to_thread(
                    _ask_screen,
                    f"Task: '{task[:80]}'. Look at the screen. "
                    f"Is progress being made toward this task? "
                    f"Is anything wrong (wrong app, wrong screen, errors, blank canvas, "
                    f"invisible drawing, wrong colors)? Answer briefly.",
                    0.75)
                self._log_event("vision_checkpoint", check[:300], step_num)
                yield {"type": "step", "data": f"🔍 Check: {check[:100]}"}

                # If something is clearly wrong, trigger a replan
                check_lower = check.lower()
                if any(kw in check_lower for kw in ["wrong", "error", "blank", "nothing", "invisible",
                                                      "empty canvas", "not visible", "incorrect"]):
                    if replan_count < 2:
                        replan_count += 1
                        yield {"type": "warning", "data": f"⚠ Vision detected issue: {check[:80]}. Replanning..."}
                        new_plan = await self._replan(task, plan, plan_idx,
                                                       f"Vision checkpoint: {check[:200]}")
                        if new_plan and new_plan.get("steps"):
                            plan = new_plan
                            steps = plan["steps"]
                            plan_idx = 0
                            yield {"type": "step", "data": f"📋 Replanned: {len(steps)} steps"}
                            continue

            # Pre-save verification: before saving, check the result looks right
            if action_name == "save_file" and result.ok:
                verify = await asyncio.to_thread(
                    _ask_screen,
                    f"The task was: '{task[:80]}'. The file was just saved. "
                    f"Does the result on screen look like a reasonable attempt at this task? "
                    f"Is the drawing/work visible and recognizable? YES or NO with brief reason.",
                    0.75)
                self._log_event("pre_save_verify", verify[:300], step_num)
                yield {"type": "step", "data": f"🔍 Save check: {verify[:100]}"}

            # Urgent save reminder
            remaining = MAX_STEPS - step_num
            if remaining <= 5 and action_name != "save_file":
                yield {"type": "warning", "data": f"⚠ ONLY {remaining} STEPS LEFT — SAVE NOW"}

        # Max steps reached
        self._log_event("max_steps", f"Reached {MAX_STEPS} steps", step_num)
        self._flush_log(task, "partial")
        yield {"type": "warning", "data": f"Reached {MAX_STEPS} step limit."}
        yield {"type": "done", "data": f"Task incomplete after {MAX_STEPS} steps."}

    async def _get_local_decision(self, plan: dict, plan_idx: int,
                                    step_num: int) -> dict | None:
        """Ask local LLM for the next action based on the plan."""
        steps = plan.get("steps", [])
        if plan_idx >= len(steps):
            return {"action": "DONE", "params": {"summary": "All plan steps completed"}}

        current_step = steps[plan_idx]
        # For simple plan steps, just return them directly — no LLM call needed
        # This saves ~2-3s per step for straightforward actions
        action = current_step.get("action", "")
        params = current_step.get("params", {})

        # Direct execution for unambiguous plan steps (skip local LLM entirely)
        if action in ACTION_REGISTRY or action in ("DONE", "FAIL"):
            return current_step

        # Only call local LLM if the step needs interpretation
        context = (
            f"PLAN: {json.dumps(steps[max(0,plan_idx-1):plan_idx+3], default=str)}\n"
            f"CURRENT STEP INDEX: {plan_idx} (of {len(steps)})\n"
            f"STEP TO EXECUTE: {json.dumps(current_step, default=str)}\n"
            f"Output the action JSON."
        )
        messages = [{"role": "user", "content": context}]
        raw = await asyncio.to_thread(self._call_local_llm, LOCAL_EXECUTOR_SYSTEM, messages)
        if raw:
            return self._parse_llm_response(raw)
        return current_step  # Fallback to plan step

    async def _replan(self, task: str, old_plan: dict, failed_at: int,
                       reason: str) -> dict | None:
        """Ask remote Claude to create a new plan after failure."""
        action_catalog = get_action_catalog()
        system = (PLANNER_SYSTEM
                  .replace("{action_catalog}", action_catalog)
                  .replace("{max_steps}", str(MAX_STEPS)))

        screen_state = _ask_screen("What is currently on screen? Describe the canvas state.")
        completed = old_plan.get("steps", [])[:failed_at]

        messages = [{"role": "user", "content": (
            f"TASK: {task}\n\n"
            f"SCREEN NOW: {screen_state}\n"
            f"COMPLETED STEPS: {json.dumps(completed[:10], default=str)}\n"
            f"FAILED AT STEP {failed_at}: {reason}\n\n"
            "Create a NEW plan to finish the task from the current state. "
            "Do NOT repeat already-completed steps. Return ONLY the JSON plan."
        )}]
        try:
            raw = await asyncio.to_thread(
                self._call_remote_llm, system, messages,
                max_tokens=4096, timeout=90.0, tier=TIER_SMART)
            if raw:
                parsed = self._parse_llm_response(raw)
                if parsed and "steps" in parsed:
                    return parsed
        except Exception as e:
            self._log_event("replan_error", str(e))
        return None

    async def _execute_remote_only(self, task: str, screen_state: str, action_catalog: str,
                                     target_app: str | None, target_exe: str | None) -> AsyncIterator[dict]:
        """Remote-only execution mode for reactive tasks (games, browsing, general desktop)."""
        save_threshold = MAX_STEPS - 5
        system = (STEP_SYSTEM_PROMPT
                  .replace("{action_catalog}", action_catalog)
                  .replace("{max_steps}", str(MAX_STEPS))
                  .replace("{save_threshold}", str(save_threshold)))

        # Inject app-specific knowledge if available
        from kb.apps import AppDB
        app_db = AppDB()
        app_context = ""
        if target_app:
            app_context = app_db.format_context(target_app)
            if not app_context:
                # Try progressively shorter names and aliases
                candidates = [target_app, target_exe or ""]
                # Add first word of target_app (e.g. "Solitaire" from "Solitaire & Casual Games")
                candidates.append(target_app.split()[0] if target_app else "")
                candidates.append(task.split()[0] if task else "")
                for alias in candidates:
                    if alias:
                        app_context = app_db.format_context(alias)
                        if app_context:
                            break
        # Also check task keywords for app names
        if not app_context:
            for app_name in app_db.list_apps():
                if app_name.lower() in task.lower():
                    app_context = app_db.format_context(app_name)
                    if app_context:
                        break
        if app_context:
            system += app_context
            self._log_event("app_knowledge", f"Loaded: {app_context[:100]}")

        messages = [{"role": "user", "content": (
            f"TASK: {task}\n\nCURRENT SCREEN: {screen_state}\n\n"
            f"ACTIVE WINDOW: {_get_active_window()}\n\nWhat is your first action?"
        )}]

        step = 0
        consecutive_fails = 0
        action_fail_streak = 0
        last_screen_hash = _screenshot_hash()
        recent_actions = []  # Track last N actions for loop detection

        # ── Execute startup_sequence if the app has one ──
        # This runs deterministic steps (launch, fullscreen, new game) before the LLM takes over
        app_profile = None
        if target_app:
            app_profile = app_db.get(target_app)
            if not app_profile.get("startup_sequence"):
                # Try first word of target_app (e.g. "Solitaire" from "Solitaire & Casual Games")
                first_word = target_app.split()[0]
                app_profile = app_db.get(first_word)
            if not app_profile.get("startup_sequence"):
                # Try all known apps for substring match
                for name in app_db.list_apps():
                    if (target_app.lower().split()[0] in name.lower() or
                        name.lower().split()[0] in target_app.lower()):
                        app_profile = app_db.get(name)
                        if app_profile.get("startup_sequence"):
                            break

        if app_profile and app_profile.get("startup_sequence"):
            seq = app_profile["startup_sequence"]
            yield {"type": "step", "data": f"🚀 Running startup sequence ({len(seq)} steps)..."}
            self._log_event("startup_sequence", f"{len(seq)} steps", 0)
            for i, seq_step in enumerate(seq):
                if self._stopped:
                    self._flush_log(task, "stopped")
                    yield {"type": "done", "data": "Task stopped."}
                    return
                action = seq_step.get("action", "")
                params = seq_step.get("params", {})
                note = seq_step.get("note", action)
                yield {"type": "status", "data": f"Startup {i+1}/{len(seq)}: {note}"}
                result = await self._execute_with_timeout(action, params)
                self._log_event("startup_step", {
                    "action": action, "ok": result.ok, "note": note,
                    "output": result.output[:100] if result.ok else result.error[:100],
                }, 0)
                if not result.ok:
                    yield {"type": "warning", "data": f"Startup step failed: {note} — continuing"}

            if self._stopped:
                self._flush_log(task, "stopped")
                yield {"type": "done", "data": "Task stopped."}
                return

            yield {"type": "step", "data": "✓ Startup sequence complete"}
            # Update the first message to tell the LLM the app is already open
            messages = [{"role": "user", "content": (
                f"TASK: {task}\n\n"
                f"The app has been launched and set up (fullscreen, new game started).\n"
                f"ACTIVE WINDOW: {_get_active_window()}\n\n"
                f"The app is ready. What is your first gameplay action?"
            )}]

        while step < MAX_STEPS:
            if self._stopped:
                self._flush_log(task, "stopped")
                yield {"type": "done", "data": "Task stopped."}
                return

            step += 1
            # Use FAST tier for routine reactive steps, SMART for first step (needs more context)
            step_tier = TIER_SMART if step <= 1 else TIER_FAST
            tier_label = "smart" if step_tier == TIER_SMART else "fast"
            yield {"type": "status", "data": f"Step {step}/{MAX_STEPS} — Thinking ({tier_label})..."}

            llm_start = _time.time()
            try:
                raw = await asyncio.to_thread(
                    self._call_remote_llm, system, messages, tier=step_tier)
            except Exception as e:
                self._log_event("error", f"LLM error: {e}", step)
                self._flush_log(task, "failed")
                yield {"type": "error", "data": f"LLM error: {e}"}
                return
            llm_elapsed = _time.time() - llm_start

            if raw is None:
                self._flush_log(task, "failed")
                yield {"type": "error", "data": "LLM failed after retries."}
                return

            self._log_event("llm_response", raw[:500], step)
            decision = self._parse_llm_response(raw)
            if not decision:
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content":
                    'Reply with ONLY JSON: {"thinking": "...", "action": "name", "params": {...}}'})
                consecutive_fails += 1
                # Escalate to SMART after 2 parse failures from FAST
                if consecutive_fails == 2 and step_tier == TIER_FAST:
                    self._log_event("escalate", "Haiku struggling, escalating to Opus", step)
                    yield {"type": "warning", "data": "Escalating to Opus for better reasoning..."}
                    step_tier = TIER_SMART
                elif consecutive_fails >= 4:
                    self._flush_log(task, "failed")
                    yield {"type": "error", "data": "LLM not returning valid JSON."}
                    return
                continue

            consecutive_fails = 0
            action_name = decision.get("action", "")
            params = decision.get("params", {})
            thinking = decision.get("thinking", "")

            if thinking:
                yield {"type": "step", "data": f"💭 {thinking[:150]}"}
            self._log_event("decision", {"action": action_name, "params": params}, step)

            if action_name == "DONE":
                summary = params.get("summary", "Task completed")
                # Validate: check real actions AND verify with vision
                real_actions = sum(1 for a in recent_actions
                                   if a.split(":")[0] not in ("look", "screenshot", "wait",
                                                                "focus_app", "press_key",
                                                                "scroll_page", "handle_unexpected"))
                if real_actions < 5 and step > 5:
                    self._log_event("premature_done", f"Only {real_actions} real actions", step)
                    yield {"type": "warning", "data": f"⚠ Rejecting premature DONE — only {real_actions} real actions taken"}
                    messages.append({"role": "assistant", "content": raw})
                    messages.append({"role": "user", "content":
                        "You declared DONE but haven't actually completed the task. "
                        f"You've only made {real_actions} real interactions. "
                        "Keep going — the task is NOT complete."})
                    continue

                # Vision verification: actually check if the task looks complete
                verify = _ask_screen(
                    f"The agent claims this task is done: '{task[:80]}'. "
                    f"Does the screen show the task was actually completed? Answer YES or NO honestly.",
                    scale=0.75)
                if "no" in verify.lower() and "yes" not in verify.lower():
                    self._log_event("done_rejected_vision", verify[:200], step)
                    yield {"type": "warning", "data": f"⚠ Vision says NOT complete: {verify[:80]}"}
                    messages.append({"role": "assistant", "content": raw})
                    messages.append({"role": "user", "content":
                        f"Vision verification says the task is NOT complete: {verify[:200]}. "
                        "Keep working."})
                    continue

                img = _screenshot_b64()
                if img:
                    yield {"type": "artifact", "data": {"type": "screenshot", "value": img,
                                                         "label": "Final result"}}
                self._flush_log(task, "completed")
                yield {"type": "done", "data": f"Done: {summary}"}
                return

            if action_name == "FAIL":
                self._flush_log(task, "failed")
                yield {"type": "error", "data": f"Agent gave up: {params.get('reason', '?')}"}
                return

            yield {"type": "status", "data": f"Step {step}/{MAX_STEPS} — {action_name}..."}
            result = await self._execute_with_timeout(action_name, params)

            self._log_event("action_result", {
                "action": action_name, "ok": result.ok,
                "output": result.output[:200],
            }, step)

            if result.ok:
                yield {"type": "step", "data": f"✓ {result.output[:150]}"}
                action_fail_streak = 0
            else:
                yield {"type": "warning", "data": f"✗ {action_name}: {result.error[:150]}"}
                action_fail_streak += 1

            # Health check
            if target_app and (result.state_hint == "TIMEOUT" or action_fail_streak >= 3):
                try:
                    recovered, events = await self._check_health_and_recover(
                        target_app, target_exe, step)
                    for evt in events:
                        yield evt
                    if recovered:
                        action_fail_streak = 0
                except Exception:
                    pass

            # Auto-detect and dismiss popups after app-opening or navigation actions
            AUTO_POPUP_CHECK_ACTIONS = {"open_app", "focus_app", "open_website", "click", "click_web_element"}
            if action_name in AUTO_POPUP_CHECK_ACTIONS and result.ok:
                popup_result = await asyncio.to_thread(
                    execute_action, "handle_unexpected", {})
                if popup_result.ok and "handled" in popup_result.output.lower():
                    self._log_event("auto_popup", popup_result.output[:200], step)
                    yield {"type": "step", "data": f"🔔 {popup_result.output[:100]}"}

            # Focus recovery: if target app lost focus, switch back
            if target_app and action_name not in ("open_app", "open_website", "close_app"):
                active_win = _get_active_window()
                target_lower = target_app.lower()
                active_lower = active_win.lower()
                if target_lower not in active_lower and active_lower not in target_lower:
                    # Something stole focus — switch back
                    self._log_event("focus_stolen", f"Expected '{target_app}', got '{active_win}'", step)
                    yield {"type": "warning", "data": f"⚡ Focus stolen by '{active_win}' — switching back to {target_app}"}
                    try:
                        refocus = await asyncio.to_thread(
                            execute_action, "focus_app", {"window_title": target_app})
                        if refocus.ok:
                            yield {"type": "step", "data": f"✓ Refocused {target_app}"}
                        else:
                            # Alt+Tab as fallback
                            import pyautogui
                            pyautogui.hotkey('alt', 'tab')
                            await asyncio.sleep(0.5)
                    except Exception:
                        pass

            # Screenshot
            SKIP = {"paint_pencil", "paint_fill_tool", "paint_color",
                     "paint_fill_style", "paint_outline_style", "wait", "press_key", "focus_app"}
            if action_name not in SKIP or not result.ok:
                img = result.screenshot or _screenshot_b64()
                if img:
                    yield {"type": "artifact", "data": {
                        "type": "screenshot", "value": img,
                        "label": f"Step {step}: {action_name}"
                    }}

            # Loop detection: if the same action+params repeats 3+ times, force a different approach
            action_sig = f"{action_name}:{json.dumps(params, sort_keys=True)[:100]}"
            recent_actions.append(action_sig)
            if len(recent_actions) > 8:
                recent_actions = recent_actions[-8:]
            # Check for loops (same action 3+ times in last 6)
            if len(recent_actions) >= 3:
                last_few = recent_actions[-6:]
                most_common = max(set(last_few), key=last_few.count)
                if last_few.count(most_common) >= 3:
                    self._log_event("loop_detected", most_common, step)
                    yield {"type": "warning", "data": f"🔄 Loop detected: {action_name} repeated 3+ times. Forcing new approach."}
                    # Escalate to SMART and inject loop-breaking instruction
                    step_tier = TIER_SMART
                    messages.append({"role": "assistant", "content": raw})
                    messages.append({"role": "user", "content":
                        f"LOOP DETECTED: You've repeated '{action_name}' 3+ times with no progress. "
                        f"This approach is NOT working. Try something COMPLETELY DIFFERENT. "
                        f"If you're stuck on a menu, press Escape and try a keyboard shortcut instead. "
                        f"If you can't find a button, try a different approach entirely."})
                    continue

            # ── Universal vision checkpoint (every 15 steps) ──
            if step % self._checkpoint_interval == 0 and step > 5:
                check = await asyncio.to_thread(
                    _ask_screen,
                    f"Task: '{task[:80]}'. Is progress being made? "
                    f"Is anything wrong (wrong app, errors, blank screen, invisible work)? Brief answer.",
                    0.75)
                self._log_event("vision_checkpoint", check[:300], step)
                feedback_extra = f"\n🔍 VISION CHECK: {check[:200]}"
            else:
                feedback_extra = ""

            # Build feedback
            active_win = _get_active_window()
            feedback = f"{'OK' if result.ok else 'FAIL'}: {(result.output if result.ok else result.error)[:200]}"
            if result.state_hint:
                feedback += f" | {result.state_hint[:100]}"
            feedback += f" | window={active_win}"

            remaining = MAX_STEPS - step
            if remaining <= 10:
                feedback += f"\n⚠ ONLY {remaining} STEPS LEFT — SAVE NOW."

            feedback += feedback_extra
            feedback += "\nNext?"
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": feedback})

            # Trim conversation
            if llm_elapsed > 20 and len(messages) > 8:
                messages = messages[:1] + messages[-6:]
            elif len(messages) > CONVERSATION_MAX:
                messages = messages[:1] + messages[-CONVERSATION_KEEP:]

        self._flush_log(task, "partial")
        yield {"type": "warning", "data": f"Reached {MAX_STEPS} step limit."}
        yield {"type": "done", "data": f"Task incomplete after {MAX_STEPS} steps."}
