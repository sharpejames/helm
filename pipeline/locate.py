import asyncio
import logging
from dataclasses import dataclass
from core.screen import screen, Coords
from core.input import input_ctrl

logger = logging.getLogger(__name__)

@dataclass
class LocateResult:
    coords: Coords
    attempts: int
    confidence: float

class TargetNotFoundError(Exception):
    pass

class LocateLoop:
    def __init__(self, vision, config: dict):
        self.vision = vision
        self.max_attempts = config['pipeline'].get('locate_max_attempts', 8)
        self.damping = config['pipeline'].get('locate_damping', 0.75)

    async def find(self, target: str) -> LocateResult:
        screenshot = screen.capture()
        est_x, est_y = self.vision.estimate_location(screenshot, target)

        if est_x == 0 and est_y == 0:
            raise TargetNotFoundError(f"No initial estimate for: {target}")

        input_ctrl.move_to(Coords(est_x, est_y))
        await asyncio.sleep(0.1)

        for attempt in range(self.max_attempts):
            screenshot = screen.capture()
            cursor = screen.get_cursor()
            result = self.vision.check_cursor(screenshot, cursor.x, cursor.y, target)

            on_target = result.get('on_target', False)
            confidence = result.get('confidence', 0.0)
            logger.debug(f"Locate {attempt+1}/{self.max_attempts}: on_target={on_target} conf={confidence:.2f}")

            if on_target and confidence >= 0.75:
                return LocateResult(coords=cursor, attempts=attempt + 1, confidence=confidence)

            dx = result.get('dx', 0)
            dy = result.get('dy', 0)
            if dx == 0 and dy == 0:
                break

            damping = self.damping ** attempt
            w, h = screen.get_size()
            new_x = max(0, min(w - 1, cursor.x + int(dx * damping)))
            new_y = max(0, min(h - 1, cursor.y + int(dy * damping)))
            input_ctrl.move_to(Coords(new_x, new_y))
            await asyncio.sleep(0.1)

        # Final check at lower confidence threshold
        screenshot = screen.capture()
        cursor = screen.get_cursor()
        result = self.vision.check_cursor(screenshot, cursor.x, cursor.y, target)
        if result.get('on_target') and result.get('confidence', 0) >= 0.5:
            return LocateResult(coords=cursor, attempts=self.max_attempts, confidence=result.get('confidence', 0.5))

        raise TargetNotFoundError(f"Could not locate '{target}' after {self.max_attempts} attempts")
