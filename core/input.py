import pyautogui
from core.screen import Coords

pyautogui.FAILSAFE = False  # Must be False — True causes cursor to get stuck at (0,0) and breaks all mouse actions
pyautogui.PAUSE = 0.05

class Input:
    def move_to(self, coords: Coords, duration: float = 0.2):
        pyautogui.moveTo(coords.x, coords.y, duration=duration)

    def click(self, coords: Coords = None, button: str = "left", double: bool = False):
        if coords:
            self.move_to(coords)
        if double:
            pyautogui.doubleClick(button=button)
        else:
            pyautogui.click(button=button)

    def right_click(self, coords: Coords = None):
        self.click(coords, button="right")

    def drag(self, start: Coords, end: Coords, duration: float = 0.5):
        pyautogui.moveTo(start.x, start.y)
        pyautogui.dragTo(end.x, end.y, duration=duration, button="left")

    def type_text(self, text: str, interval: float = 0.03):
        """Type via clipboard to handle special chars and speed."""
        import pyperclip
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")

    def hotkey(self, *keys):
        pyautogui.hotkey(*keys)

    def press(self, key: str):
        pyautogui.press(key)

    def scroll(self, coords: Coords, clicks: int):
        pyautogui.scroll(clicks, x=coords.x, y=coords.y)

input_ctrl = Input()
