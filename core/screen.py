import mss
import base64
import io
from dataclasses import dataclass
from PIL import Image

@dataclass
class Coords:
    x: int
    y: int

    def __add__(self, other: "Coords") -> "Coords":
        return Coords(self.x + other.x, self.y + other.y)

class Screen:
    def capture(self) -> bytes:
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            shot = sct.grab(monitor)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()

    def capture_base64(self) -> str:
        return base64.b64encode(self.capture()).decode()

    def get_cursor(self) -> Coords:
        import pyautogui
        x, y = pyautogui.position()
        return Coords(x, y)

    def get_size(self) -> tuple[int, int]:
        import pyautogui
        return pyautogui.size()

screen = Screen()
