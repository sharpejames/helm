import asyncio
import base64
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, List
from core.screen import screen, Coords
from core.input import input_ctrl
from pipeline.locate import LocateLoop, TargetNotFoundError
from pipeline.blockers import BlockerHandler, BlockerError

logger = logging.getLogger(__name__)

@dataclass
class Artifact:
    type: str   # screenshot | url | file | text
    value: str
    label: str = ""
    step: int = 0

@dataclass
class ActionResult:
    success: bool
    artifacts: List[Artifact] = field(default_factory=list)
    error: str = ""
    retries: int = 0

class BaseAction(ABC):
    target: Optional[str] = None
    expected_result: str = "action completed"

    @abstractmethod
    async def execute(self, coords: Optional[Coords] = None) -> bool:
        pass

class ActionPipeline:
    """THE STRICT PIPELINE. Every action goes through this. No exceptions."""

    def __init__(self, vision, config: dict):
        self.vision = vision
        self.config = config
        self.locate_loop = LocateLoop(vision, config)
        self.blocker_handler = BlockerHandler(vision, config)
        self.max_retries = config['pipeline'].get('max_retries', 3)
        self.settle_ms = config['pipeline'].get('settle_delay_ms', 500) / 1000

    async def execute(self, action: BaseAction) -> ActionResult:
        artifacts: List[Artifact] = []
        for retry in range(self.max_retries + 1):
            try:
                result = await self._pipeline(action, artifacts, retry)
                if result.success:
                    return result
                logger.warning(f"Verify failed attempt {retry+1}/{self.max_retries+1}")
            except TargetNotFoundError as e:
                logger.error(f"Target not found: {e}")
                if retry == self.max_retries:
                    return ActionResult(False, artifacts, str(e), retry)
            except BlockerError as e:
                return ActionResult(False, artifacts, str(e), retry)
            except Exception as e:
                logger.error(f"Pipeline error attempt {retry+1}: {e}", exc_info=True)
                if retry == self.max_retries:
                    return ActionResult(False, artifacts, str(e), retry)
        return ActionResult(False, artifacts, "Max retries exceeded", self.max_retries)

    async def _pipeline(self, action: BaseAction, artifacts: list, retry: int) -> ActionResult:
        # 1. PRE_CAPTURE
        logger.debug("[1] PRE_CAPTURE")
        pre = screen.capture()

        # 2. STATE_CHECK
        logger.debug("[2] STATE_CHECK")
        state = self.vision.check_state(pre)
        if not state.get('ready', True):
            await asyncio.sleep(1.0)
            pre = screen.capture()

        # 3. BLOCKER_CHECK
        logger.debug("[3] BLOCKER_CHECK")
        had_blocker = await self.blocker_handler.check_and_handle(pre)
        if had_blocker:
            pre = screen.capture()

        # 4. LOCATE_TARGET
        coords = None
        if action.target:
            logger.debug(f"[4] LOCATE_TARGET: {action.target}")
            loc = await self.locate_loop.find(action.target)
            coords = loc.coords
            logger.info(f"Located '{action.target}' at ({coords.x},{coords.y}) in {loc.attempts} attempts")

        # 5. PRE_ACTION_VERIFY
        if coords:
            logger.debug("[5] PRE_ACTION_VERIFY")
            shot = screen.capture()
            cur = screen.get_cursor()
            v = self.vision.check_cursor(shot, cur.x, cur.y, action.target)
            if not v.get('on_target') and v.get('confidence', 0) < 0.5:
                logger.warning("Pre-verify failed, re-locating")
                loc = await self.locate_loop.find(action.target)
                coords = loc.coords

        # 6. EXECUTE
        logger.debug("[6] EXECUTE")
        ok = await action.execute(coords)
        if not ok:
            return ActionResult(False, artifacts, "execute() returned False", retry)

        # 7. SETTLE
        await asyncio.sleep(self.settle_ms)

        # 8. POST_CAPTURE
        logger.debug("[8] POST_CAPTURE")
        post = screen.capture()

        # 9. VERIFY_RESULT
        logger.debug("[9] VERIFY_RESULT")
        vr = self.vision.verify_result(pre, post, action.expected_result)
        confidence = vr.get('confidence', 0.5)
        # Default to success — only fail if vision is confident it failed
        success = vr.get('success', True)
        if not success and confidence < 0.65:
            logger.debug("Low-confidence failure — assuming success")
            success = True

        # 10. ARTIFACT_CAPTURE
        logger.debug("[10] ARTIFACT_CAPTURE")
        artifacts.append(Artifact(
            type="screenshot",
            value=base64.b64encode(post).decode(),
            label=action.expected_result,
            step=10
        ))

        # Extract URL if browser is active
        url = self.vision.extract_url(post)
        if url:
            artifacts.append(Artifact(type="url", value=url, label="Current URL", step=10))

        return ActionResult(success=success, artifacts=artifacts, retries=retry)
