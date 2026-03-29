"""
Helm Status Overlay — Always-on-top transparent status window.

Shows what Helm is doing in real-time without stealing focus.
Hides automatically before screenshots so it doesn't appear in vision.

Usage: py overlay.py
  - Runs as a separate process alongside Helm
  - Polls /api/chat SSE stream for status updates
  - Press Escape or click X to minimize, click tray to restore
"""

import tkinter as tk
import threading
import time
import requests
import json
import sys
import logging

logger = logging.getLogger(__name__)

HELM_URL = "http://localhost:8765"
POLL_INTERVAL = 0.5  # seconds


class HelmOverlay:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Helm")
        self.root.attributes('-topmost', True)
        self.root.attributes('-alpha', 0.85)
        self.root.overrideredirect(True)  # No title bar

        # Position: bottom-right corner
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        w, h = 400, 120
        x = screen_w - w - 20
        y = screen_h - h - 60  # Above taskbar
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        # Dark background
        self.root.configure(bg='#1a1a2e')

        # Header
        header = tk.Frame(self.root, bg='#16213e', height=28)
        header.pack(fill='x')
        header.pack_propagate(False)

        tk.Label(header, text="⚓ Helm", font=('Segoe UI', 10, 'bold'),
                 fg='#e0e0e0', bg='#16213e').pack(side='left', padx=8)

        self.status_dot = tk.Label(header, text="●", font=('Segoe UI', 10),
                                    fg='#4ade80', bg='#16213e')
        self.status_dot.pack(side='left', padx=4)

        # Minimize button
        tk.Button(header, text="—", font=('Segoe UI', 9), fg='#888',
                  bg='#16213e', bd=0, command=self.minimize,
                  activebackground='#2a2a4e').pack(side='right', padx=4)

        # Status text
        self.status_var = tk.StringVar(value="Ready")
        self.status_label = tk.Label(self.root, textvariable=self.status_var,
                                      font=('Segoe UI', 9), fg='#a0a0c0',
                                      bg='#1a1a2e', anchor='w', wraplength=380,
                                      justify='left')
        self.status_label.pack(fill='x', padx=10, pady=(6, 2))

        # Detail text
        self.detail_var = tk.StringVar(value="")
        self.detail_label = tk.Label(self.root, textvariable=self.detail_var,
                                      font=('Segoe UI', 8), fg='#666680',
                                      bg='#1a1a2e', anchor='w', wraplength=380,
                                      justify='left')
        self.detail_label.pack(fill='x', padx=10, pady=(0, 6))

        # Make draggable
        header.bind('<Button-1>', self._start_drag)
        header.bind('<B1-Motion>', self._on_drag)

        # Escape to minimize
        self.root.bind('<Escape>', lambda e: self.minimize())

        # Track visibility for screenshot hiding
        self._visible = True
        self._drag_x = 0
        self._drag_y = 0

        # Start polling thread
        self._running = True
        self._poll_thread = threading.Thread(target=self._poll_status, daemon=True)
        self._poll_thread.start()

    def _start_drag(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _on_drag(self, event):
        x = self.root.winfo_x() + event.x - self._drag_x
        y = self.root.winfo_y() + event.y - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    def minimize(self):
        self._visible = False
        self.root.withdraw()
        # Show again after 3 seconds or when task completes
        threading.Timer(3.0, self._check_restore).start()

    def _check_restore(self):
        if not self._visible:
            self.root.after(0, self.restore)

    def restore(self):
        self._visible = True
        self.root.deiconify()

    def hide_for_screenshot(self):
        """Hide overlay temporarily so it doesn't appear in screenshots."""
        if self._visible:
            self.root.withdraw()
            time.sleep(0.05)

    def show_after_screenshot(self):
        """Restore overlay after screenshot."""
        if self._visible:
            self.root.after(0, self.root.deiconify)

    def update_status(self, text, detail="", color=None):
        """Update the overlay text. Thread-safe."""
        def _update():
            self.status_var.set(text[:100])
            self.detail_var.set(detail[:150])
            if color:
                self.status_dot.configure(fg=color)
        self.root.after(0, _update)

    def _poll_status(self):
        """Poll Helm API for status updates."""
        while self._running:
            try:
                r = requests.get(f"{HELM_URL}/health", timeout=2)
                if r.status_code == 200:
                    self.update_status("Connected", "Waiting for task...", '#4ade80')
                else:
                    self.update_status("Helm not responding", "", '#ef4444')
            except Exception:
                self.update_status("Connecting to Helm...", f"{HELM_URL}", '#fbbf24')
            time.sleep(POLL_INTERVAL * 4)

    def run(self):
        """Start the overlay main loop."""
        self.root.mainloop()
        self._running = False


# Global overlay instance for screenshot hiding
_overlay: HelmOverlay = None


def get_overlay() -> HelmOverlay:
    global _overlay
    return _overlay


if __name__ == "__main__":
    overlay = HelmOverlay()
    _overlay = overlay
    print("Helm overlay running. Press Escape to minimize.")
    overlay.run()
