import asyncio
import logging
from typing import AsyncIterator
from pipeline.action import ActionPipeline, BaseAction, Artifact, ActionResult
from core.screen import screen, Coords
from core.input import input_ctrl
from agent.planner import Planner
from agent.models import LLMClient

logger = logging.getLogger(__name__)

# --- Concrete Actions ---

class ClickAction(BaseAction):
    def __init__(self, target: str, double: bool = False, expected: str = "element clicked"):
        self.target = target
        self.double = double
        self.expected_result = expected
    async def execute(self, coords: Coords = None) -> bool:
        if coords:
            input_ctrl.click(coords, double=self.double)
            return True
        return False

class TypeAction(BaseAction):
    def __init__(self, text: str, expected: str = "text typed"):
        self.target = None
        self.text = text
        self.expected_result = expected
    async def execute(self, coords: Coords = None) -> bool:
        input_ctrl.type_text(self.text)
        return True

class HotkeyAction(BaseAction):
    def __init__(self, keys: list, expected: str = "shortcut executed"):
        self.target = None
        self.keys = keys
        self.expected_result = expected
    async def execute(self, coords: Coords = None) -> bool:
        input_ctrl.hotkey(*self.keys)
        return True

class NavigateAction(BaseAction):
    def __init__(self, url: str):
        self.target = None
        self.url = url
        self.expected_result = f"browser opened {url}"
    async def execute(self, coords: Coords = None) -> bool:
        # Open URL like a human: Win+R → "chrome <url>" → Enter
        # Never use webbrowser.open() — that's an API call
        input_ctrl.hotkey("win", "r")
        await asyncio.sleep(0.7)
        input_ctrl.type_text(f"chrome {self.url}")
        await asyncio.sleep(0.3)
        input_ctrl.press("enter")
        await asyncio.sleep(3.0)
        return True

class ScrollAction(BaseAction):
    def __init__(self, target: str, direction: str = "down", clicks: int = 3):
        self.target = target
        self.direction = direction
        self.clicks = clicks if direction == "down" else -clicks
        self.expected_result = f"scrolled {direction}"
    async def execute(self, coords: Coords = None) -> bool:
        if coords:
            input_ctrl.scroll(coords, self.clicks)
            return True
        return False

class WaitAction(BaseAction):
    def __init__(self, duration_ms: int = 1000):
        self.target = None
        self.duration_ms = duration_ms
        self.expected_result = "waited"
    async def execute(self, coords: Coords = None) -> bool:
        await asyncio.sleep(self.duration_ms / 1000)
        return True

def step_to_action(step: dict) -> BaseAction:
    t = step.get('action', '')
    if t == 'click':
        return ClickAction(step['target'], step.get('double', False), step.get('expected', 'clicked'))
    elif t == 'type':
        return TypeAction(step['text'], step.get('expected', 'typed'))
    elif t == 'hotkey':
        return HotkeyAction(step['keys'], step.get('expected', 'shortcut'))
    elif t == 'navigate':
        return NavigateAction(step['url'])
    elif t == 'scroll':
        return ScrollAction(step.get('target', 'page'), step.get('direction', 'down'), step.get('clicks', 3))
    elif t == 'wait':
        return WaitAction(step.get('duration_ms', 1000))
    raise ValueError(f"Unknown action: {t}")

# --- Executor ---

class Executor:
    def __init__(self, pipeline: ActionPipeline, planner: Planner):
        self.pipeline = pipeline
        self.planner = planner

    async def stream_task(self, task: str) -> AsyncIterator[dict]:
        yield {"type": "status", "data": "Planning..."}
        steps = self.planner.plan(task)
        if not steps:
            yield {"type": "error", "data": "Could not plan steps for this task."}
            return

        yield {"type": "status", "data": f"Executing {len(steps)} steps"}
        all_artifacts: list[Artifact] = []
        replan_count = 0
        MAX_REPLANS = 2

        i = 0
        while i < len(steps):
            step = steps[i]
            label = f"{step.get('action')} {step.get('target', step.get('url', step.get('text', '')))[:40]}"
            yield {"type": "step", "data": f"Step {i+1}/{len(steps)}: {label}"}

            if step.get('action') == 'extract':
                shot = screen.capture()
                target = step.get('target', 'current state')
                artifact_type = step.get('artifact_type', 'text')
                if artifact_type == 'url':
                    url = self.pipeline.vision.extract_url(shot)
                    if url:
                        all_artifacts.append(Artifact(type='url', value=url, label='URL', step=i))
                        yield {"type": "artifact", "data": {"type": "url", "value": url, "label": "URL"}}
                else:
                    text = self.pipeline.vision.ask(shot, f"Extract: {target}")
                    all_artifacts.append(Artifact(type='text', value=text, label=target, step=i))
                    yield {"type": "artifact", "data": {"type": "text", "value": text, "label": target}}
                i += 1
                continue

            try:
                action = step_to_action(step)
            except ValueError as e:
                yield {"type": "warning", "data": str(e)}
                i += 1
                continue

            result = await self.pipeline.execute(action)

            for a in result.artifacts:
                all_artifacts.append(a)
                if a.type != 'screenshot':
                    yield {"type": "artifact", "data": {"type": a.type, "value": a.value, "label": a.label}}
                else:
                    yield {"type": "artifact", "data": {"type": "screenshot", "value": a.value, "label": a.label}}

            if not result.success:
                replan_count += 1
                if replan_count > MAX_REPLANS:
                    yield {"type": "error", "data": f"Step {i+1} failed after {MAX_REPLANS} replan attempts. Stopping."}
                    return
                yield {"type": "warning", "data": f"Step {i+1} failed, replanning ({replan_count}/{MAX_REPLANS})..."}
                shot = screen.capture()
                state = self.pipeline.vision.check_state(shot)
                new_steps = self.planner.replan(task, step, state.get('description', ''))
                if new_steps:
                    steps = new_steps
                    i = 0
                    continue
                else:
                    yield {"type": "error", "data": f"Failed at step {i+1} and could not replan."}
                    return

            i += 1

        summary = self._summarize(task, all_artifacts)
        yield {"type": "done", "data": summary, "artifacts": [{"type": a.type, "value": a.value if a.type != "screenshot" else None, "label": a.label} for a in all_artifacts]}

    def _summarize(self, task: str, artifacts: list[Artifact]) -> str:
        urls = [a.value for a in artifacts if a.type == 'url']
        files = [a.value for a in artifacts if a.type == 'file']
        parts = [f"Done: {task}"]
        if urls:
            parts.append("URLs: " + ", ".join(urls))
        if files:
            parts.append("Files: " + ", ".join(files))
        return "\n".join(parts)
