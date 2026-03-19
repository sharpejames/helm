import asyncio
import logging
from core.screen import screen, Coords
from core.input import input_ctrl

logger = logging.getLogger(__name__)

class BlockerError(Exception):
    pass

# Keyboard shortcuts tried in order — safest first.
# Escape dismisses most dialogs. Alt+F4 closes the active window/modal.
# Ctrl+W closes browser tabs/documents. All are standard human shortcuts.
DISMISS_SHORTCUTS = [
    ("escape",),
    ("alt", "f4"),
    ("ctrl", "w"),
]

class BlockerHandler:
    def __init__(self, vision, config: dict):
        self.vision = vision
        self.max_attempts = config['pipeline'].get('blocker_max_attempts', 3)

    async def check_and_handle(self, screenshot: bytes) -> bool:
        """
        Check for blockers and dismiss them.
        Returns True if a blocker was found and handled.
        Raises BlockerError if it cannot be dismissed.
        """
        for attempt in range(self.max_attempts):
            result = self.vision.check_blockers(screenshot)
            if not result.get('has_blocker'):
                return False

            logger.info(f"Blocker detected: {result.get('type', 'unknown')} (attempt {attempt+1}/{self.max_attempts})")

            # Step 1: keyboard shortcuts first — reliable, no coordinate guessing
            if await self._try_keyboard_dismiss():
                return True

            # Step 2: mouse fallback — prefer Cancel/X, avoid OK/Yes
            logger.info("Keyboard dismiss failed, trying mouse")
            if await self._try_mouse_dismiss(result):
                return True

            await asyncio.sleep(0.5)
            screenshot = screen.capture()

        raise BlockerError(f"Could not dismiss blocker after {self.max_attempts} attempts")

    async def _try_keyboard_dismiss(self) -> bool:
        """Try DISMISS_SHORTCUTS in order. Returns True if blocker gone."""
        for keys in DISMISS_SHORTCUTS:
            input_ctrl.hotkey(*keys)
            await asyncio.sleep(0.6)
            shot = screen.capture()
            result = self.vision.check_blockers(shot)
            if not result.get('has_blocker'):
                logger.info(f"Blocker dismissed via keyboard: {'+'.join(keys)}")
                return True
        return False

    async def _try_mouse_dismiss(self, blocker_result: dict) -> bool:
        """
        Click to dismiss. Prefers Cancel/Close/X over OK/Yes.
        Only clicks OK as a last resort when it's the only option.
        """
        action = blocker_result.get('dismiss_action', 'none')
        x = blocker_result.get('dismiss_x')
        y = blocker_result.get('dismiss_y')

        if action in ('close_button', 'click_outside'):
            if x and y:
                input_ctrl.click(Coords(int(x), int(y)))
            else:
                input_ctrl.press('escape')
        elif action == 'escape_key':
            input_ctrl.press('escape')
        elif action == 'accept_button':
            # OK/Yes — only click if coordinates provided and no safer option exists
            logger.warning("Blocker dismiss_action is accept — clicking OK as last resort")
            if x and y:
                input_ctrl.click(Coords(int(x), int(y)))
            else:
                input_ctrl.press('escape')
        else:
            input_ctrl.press('escape')

        await asyncio.sleep(0.5)
        shot = screen.capture()
        result = self.vision.check_blockers(shot)
        dismissed = not result.get('has_blocker')
        if dismissed:
            logger.info("Blocker dismissed via mouse click")
        return dismissed
