"""
agent/agent_executor.py — Hybrid streaming agent executor for Helm.

Architecture:
1. ScreenMonitor (Win32, ~10ms) — tracks foreground window, open windows, modals
2. Gemini Vision (~300ms) — visual understanding at key moments
3. UIA Scene Graph (~50ms) — element IDs for clicking
4. LLM Decision — gets all three, picks one action

Vision is called: at task start, after state changes, every 5 steps, when stuck.
"""

import asyncio
import threading
import time
import base64
import logging
import requests
import urllib.parse
import sys
import os
import importlib.util as _ilu

logger = logging.getLogger(__name__)

# Import v2 modules using importlib to avoid name collision with helm/agent/
V2_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "v2")
sys.path.insert(0, V2_DIR)

def _load(name, path):
    spec = _ilu.spec_from_file_location(name, path)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# Load scene_graph and actions first (no internal v2 deps)
_scene_graph = _load("scene_graph", os.path.join(V2_DIR, "scene_graph.py"))
_actions = _load("v2_actions", os.path.join(V2_DIR, "agent", "actions.py"))

# Patch into sys.modules so v2 executor/decision internal imports resolve
sys.modules["scene_graph"] = _scene_graph
sys.modules.setdefault("agent", type(sys)("agent"))
sys.modules["agent.actions"] = _actions

# Now load modules that depend on scene_graph and agent.actions
_executor = _load("v2_executor", os.path.join(V2_DIR, "agent", "executor.py"))
_decision = _load("v2_decision", os.path.join(V2_DIR, "agent", "decision.py"))
_perception = _load("v2_perception", os.path.join(V2_DIR, "perception", "engine.py"))

SceneGraph = _scene_graph.SceneGraph
Action = _actions.Action
DoneAction = _actions.DoneAction
FailAction = _actions.FailAction
parse_action = _actions.parse_action
ActionExecutor = _executor.ActionExecutor
LLMDecisionMaker = _decision.LLMDecisionMaker
PerceptionEngine = _perception.PerceptionEngine

# Local imports
from agent.screen_monitor import ScreenMonitor

CLAWMETHEUS_URL = "http://127.0.0.1:7331"
MAX_STEPS = 50
STUCK_THRESHOLD = 3
VISION_INTERVAL = 5  # call vision every N steps


class AgentExecutor:
    """
    Hybrid streaming agent executor.
    Combines cheap local monitoring + Gemini vision + UIA scene graph.
    """

    def __init__(self, config: dict):
        self.config = config
        llm_cfg = config.get("llm", {})
        cm_cfg = config.get("clawmetheus", {})
        vis_cfg = config.get("vision", {})
        self.cm_url = cm_cfg.get("url", CLAWMETHEUS_URL)

        # Perception engine (UIA only — fast, for element IDs)
        self.perception = PerceptionEngine(
            enable_yolo=False, enable_ocr=False, enable_opencv=False
        )

        # Action executor (sends actions to Clawmetheus)
        self.action_executor = ActionExecutor(base_url=self.cm_url)

        # LLM decision maker
        self.decision_maker = LLMDecisionMaker(
            api_url=llm_cfg.get("base_url", "http://localhost:8000"),
            api_key=llm_cfg.get("api_key", "skippy-kiro-local-2026"),
            model=llm_cfg.get("model", "claude-opus-4.6"),
        )

        # Screen monitor (Win32, ~10ms per check)
        self.monitor = ScreenMonitor()

        # Stop event
        self._stop_event: threading.Event | None = None

    @property
    def stop_event(self) -> threading.Event | None:
        return self._stop_event

    def _clawmetheus_running(self) -> bool:
        try:
            r = requests.get(f"{self.cm_url}/status", timeout=3)
            return r.status_code == 200
        except Exception:
            return False

    def _screenshot_b64(self) -> str | None:
        try:
            r = requests.get(f"{self.cm_url}/screenshot/base64?scale=0.5", timeout=10).json()
            return r.get("image")
        except Exception:
            return None

    def _vision_describe(self, context: str = "") -> str:
        """Ask Gemini to describe what's on screen. ~300ms."""
        try:
            q = f"Describe what you see on screen. What app is in focus? What is the current state? {context}"
            encoded = urllib.parse.quote(q)
            r = requests.get(f"{self.cm_url}/ask?q={encoded}&scale=0.5", timeout=30).json()
            return r.get("answer", "Vision unavailable")
        except Exception as e:
            logger.warning(f"Vision failed: {e}")
            return f"Vision error: {e}"

    def _perceive(self, app_filter: str = "") -> SceneGraph:
        if app_filter:
            return self.perception.perceive_fast(app_filter)
        return self.perception.perceive_full(include_screenshot=False)

    def _should_use_vision(self, step_num: int, changes: list, stuck_count: int) -> bool:
        """Decide whether to call Gemini vision this step."""
        if step_num == 1:
            return True  # always on first step
        if changes:
            return True  # state changed (new window, focus change)
        if stuck_count >= 2:
            return True  # agent seems stuck
        if step_num % VISION_INTERVAL == 0:
            return True  # periodic check
        return False

    async def stream_task(self, task: str):
        """Execute a task with hybrid perception. Yields SSE event dicts."""
        self._stop_event = threading.Event()

        if not self._clawmetheus_running():
            yield {"type": "error", "data": "Clawmetheus not running. Start it: cd workspace/clawmetheus && .\\start.ps1"}
            return

        yield {"type": "status", "data": "Starting task..."}

        # Initial screenshot
        img = self._screenshot_b64()
        if img:
            yield {"type": "artifact", "data": {"type": "screenshot", "value": img, "label": "Initial screen"}}

        step_num = 0
        stuck_count = 0
        last_action_type = None
        history = []
        last_vision = ""

        try:
            while step_num < MAX_STEPS:
                if self._stop_event.is_set():
                    yield {"type": "warning", "data": "Stopped by user."}
                    yield {"type": "done", "data": f"Stopped after {step_num} steps."}
                    return

                step_num += 1

                # 1. MONITOR — cheap local state check (~10ms)
                yield {"type": "status", "data": f"Step {step_num}/{MAX_STEPS} — Checking screen..."}
                screen_state, changes = await asyncio.to_thread(self.monitor.check)

                if changes:
                    for c in changes:
                        yield {"type": "step", "data": f"🔄 {c}"}

                # Check for unexpected modal
                modal = await asyncio.to_thread(self.monitor.detect_modal)
                if modal:
                    yield {"type": "step", "data": f"⚠ Modal detected: {modal}"}

                if self._stop_event.is_set():
                    yield {"type": "done", "data": f"Stopped after {step_num} steps."}
                    return

                # 2. VISION — Gemini visual check (when needed, ~300ms)
                use_vision = self._should_use_vision(step_num, changes, stuck_count)
                if use_vision:
                    yield {"type": "status", "data": f"Step {step_num}/{MAX_STEPS} — Looking at screen..."}
                    context = f"Task: {task[:100]}. Step {step_num}."
                    if modal:
                        context += f" There appears to be a modal dialog: {modal}"
                    last_vision = await asyncio.to_thread(self._vision_describe, context)
                    yield {"type": "step", "data": f"👁 {last_vision[:150]}"}

                if self._stop_event.is_set():
                    yield {"type": "done", "data": f"Stopped after {step_num} steps."}
                    return

                # 3. PERCEIVE — UIA scene graph (~50ms)
                yield {"type": "status", "data": f"Step {step_num}/{MAX_STEPS} — Reading UI elements..."}
                try:
                    scene = await asyncio.to_thread(self._perceive)
                except Exception as e:
                    yield {"type": "warning", "data": f"Perception failed: {e}"}
                    scene = SceneGraph(elements=[], sources_used=["none"], perception_ms=0)

                if self._stop_event.is_set():
                    yield {"type": "done", "data": f"Stopped after {step_num} steps."}
                    return

                # 4. DECIDE — LLM picks one action
                yield {"type": "status", "data": f"Step {step_num}/{MAX_STEPS} — Deciding..."}

                # Build enhanced context for the decision maker
                enhanced_context = self._build_context(
                    task, scene, screen_state, last_vision, modal, history
                )

                try:
                    action = await asyncio.to_thread(
                        self.decision_maker.decide, enhanced_context, scene, history
                    )
                except Exception as e:
                    yield {"type": "error", "data": f"Decision failed: {e}"}
                    return

                reasoning = action.reasoning[:150] if action.reasoning else ""
                yield {"type": "step", "data": f"🎯 {action.action_type}: {reasoning}"}

                # Handle done/fail
                if isinstance(action, DoneAction):
                    img = self._screenshot_b64()
                    if img:
                        yield {"type": "artifact", "data": {"type": "screenshot", "value": img, "label": "Final result"}}
                    yield {"type": "done", "data": f"{'✓' if action.success else '✗'} {action.summary}"}
                    return

                if isinstance(action, FailAction):
                    img = self._screenshot_b64()
                    if img:
                        yield {"type": "artifact", "data": {"type": "screenshot", "value": img, "label": "Failed state"}}
                    yield {"type": "error", "data": f"Agent gave up: {action.error}"}
                    return

                if self._stop_event.is_set():
                    yield {"type": "done", "data": f"Stopped after {step_num} steps."}
                    return

                # 5. ACT — execute the action
                yield {"type": "status", "data": f"Step {step_num}/{MAX_STEPS} — Executing {action.action_type}..."}
                try:
                    exec_result = await asyncio.to_thread(
                        self.action_executor.execute, action, scene
                    )
                except Exception as e:
                    exec_result = {"ok": False, "error": str(e), "action_type": action.action_type}

                ok = exec_result.get("ok", False)
                details = exec_result.get("details", "")
                if ok:
                    yield {"type": "step", "data": f"✓ {details}"}
                else:
                    yield {"type": "warning", "data": f"✗ {details}: {exec_result.get('error', '')}"}

                # Brief pause for UI to update
                await asyncio.sleep(0.5)

                # 6. VERIFY — screenshot after action
                img = self._screenshot_b64()
                if img:
                    yield {"type": "artifact", "data": {
                        "type": "screenshot", "value": img,
                        "label": f"Step {step_num}: {action.action_type}"
                    }}

                # Record history
                history.append(_StepRecord(step_num, action, exec_result, ok))

                # Stuck detection
                if action.action_type == last_action_type and not ok:
                    stuck_count += 1
                    if stuck_count >= STUCK_THRESHOLD:
                        yield {"type": "error", "data": f"Stuck: {action.action_type} failed {stuck_count}x. Aborting."}
                        return
                else:
                    stuck_count = 0
                    last_action_type = action.action_type

            # Max steps reached
            yield {"type": "warning", "data": f"Reached {MAX_STEPS} step limit."}
            img = self._screenshot_b64()
            if img:
                yield {"type": "artifact", "data": {"type": "screenshot", "value": img, "label": "Final state"}}
            yield {"type": "done", "data": f"Task incomplete after {MAX_STEPS} steps."}

        except Exception as e:
            logger.error(f"Agent loop error: {e}", exc_info=True)
            yield {"type": "error", "data": f"Fatal error: {e}"}
        finally:
            self._stop_event = None

    def _build_context(self, task, scene, screen_state, vision_desc, modal, history):
        """Build enhanced task context string for the LLM decision maker."""
        parts = [task]
        parts.append(f"\n[SCREEN STATE]\n{screen_state.summary()}")
        if vision_desc:
            parts.append(f"\n[VISUAL DESCRIPTION]\n{vision_desc}")
        if modal:
            parts.append(f"\n[MODAL DETECTED] A dialog '{modal}' may be blocking. Deal with it first.")
        return "\n".join(parts)

    def stop(self):
        """Stop the current task immediately."""
        if self._stop_event:
            self._stop_event.set()
            return True
        return False


class _StepRecord:
    """Lightweight step record for LLM history context."""
    def __init__(self, step_num, action, execution_result, success):
        self.step_num = step_num
        self.action = action
        self.execution_result = execution_result
        self.success = success
