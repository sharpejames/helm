import json
import re
import logging
from agent.models import LLMClient
from agent.prompts import PLANNER_SYSTEM, REPLANNER_SYSTEM

logger = logging.getLogger(__name__)

class Planner:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def plan(self, task: str) -> list[dict]:
        response = self.llm.complete(PLANNER_SYSTEM, [{"role": "user", "content": task}])
        return self._parse(response)

    def replan(self, task: str, failed_step: dict, screen_desc: str) -> list[dict]:
        content = f"Original task: {task}\nFailed step: {json.dumps(failed_step)}\nCurrent screen: {screen_desc}\nProvide revised steps."
        response = self.llm.complete(REPLANNER_SYSTEM, [{"role": "user", "content": content}])
        return self._parse(response)

    def _parse(self, text: str) -> list[dict]:
        text = re.sub(r'```(?:json)?\s*', '', text).strip().rstrip('`').strip()
        try:
            steps = json.loads(text)
            if isinstance(steps, list):
                return steps
        except Exception:
            m = re.search(r'\[.*\]', text, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group())
                except Exception:
                    pass
        logger.error(f"Could not parse steps: {text[:200]}")
        return []
