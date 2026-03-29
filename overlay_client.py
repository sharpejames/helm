"""
Helm Overlay Client — Connects to Helm's SSE stream for real-time updates.
Runs the overlay window and updates it with task progress.

Usage: py overlay_client.py
"""

import tkinter as tk
import threading
import time
import requests
import json

HELM_URL = "http://localhost:8765"


class HelmOverlay:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Helm Status")
        self.root.attributes('-topmost', True)
        self.root.attributes('-alpha', 0.88)
        self.root.overrideredirect(True)

        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        w, h = 420, 100
        x = screen_w - w - 20
        y = screen_h - h - 60
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.configure(bg='#0f0f23')

        # Header bar
        header = tk.Frame(self.root, bg='#1a1a3e', height=26)
        header.pack(fill='x')
        header.pack_propagate(False)
        tk.Label(header, text="⚓ Helm", font=('Segoe UI', 9, 'bold'),
                 fg='#c0c0e0', bg='#1a1a3e').pack(side='left', padx=8)
        self.dot = tk.Label(header, text="●", font=('Segoe UI', 8),
                            fg='#4ade80', bg='#1a1a3e')
        self.dot.pack(side='left')
        self.step_label = tk.Label(header, text="", font=('Segoe UI', 8),
                                    fg='#666', bg='#1a1a3e')
        self.step_label.pack(side='right', padx=8)
        tk.Button(header, text="━", font=('Segoe UI', 7), fg='#555',
                  bg='#1a1a3e', bd=0, command=self.toggle_minimize,
                  activebackground='#2a2a4e', cursor='hand2').pack(side='right')

        # Status
        self.status = tk.StringVar(value="Connecting...")
        tk.Label(self.root, textvariable=self.status, font=('Segoe UI', 9),
                 fg='#a0a0d0', bg='#0f0f23', anchor='w',
                 wraplength=400, justify='left').pack(fill='x', padx=10, pady=(4, 0))

        # Detail
        self.detail = tk.StringVar(value="")
        tk.Label(self.root, textvariable=self.detail, font=('Segoe UI', 8),
                 fg='#555570', bg='#0f0f23', anchor='w',
                 wraplength=400, justify='left').pack(fill='x', padx=10, pady=(0, 4))

        # Draggable
        header.bind('<Button-1>', lambda e: setattr(self, '_dx', e.x) or setattr(self, '_dy', e.y))
        header.bind('<B1-Motion>', lambda e: self.root.geometry(
            f"+{self.root.winfo_x()+e.x-self._dx}+{self.root.winfo_y()+e.y-self._dy}"))
        self.root.bind('<Escape>', lambda e: self.toggle_minimize())

        self._visible = True
        self._minimized = False
        self._dx = self._dy = 0
        self._running = True
        self._task_active = False

        # Start status poller
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def toggle_minimize(self):
        if self._minimized:
            self.root.deiconify()
            self._minimized = False
        else:
            self.root.withdraw()
            self._minimized = True

    def set(self, status, detail="", color=None, step=""):
        def _u():
            self.status.set(status[:120])
            self.detail.set(detail[:150])
            if color:
                self.dot.configure(fg=color)
            self.step_label.configure(text=step)
        self.root.after(0, _u)

    def _poll_loop(self):
        """Poll Helm for status. When a task is running, show live updates."""
        while self._running:
            try:
                r = requests.get(f"{HELM_URL}/health", timeout=2)
                if r.status_code == 200:
                    if not self._task_active:
                        self.set("Ready", "Waiting for task...", '#4ade80')
                else:
                    self.set("Helm not responding", "", '#ef4444')
            except Exception:
                self.set("Connecting...", HELM_URL, '#fbbf24')
            time.sleep(2)

    def run(self):
        self.root.mainloop()
        self._running = False


if __name__ == "__main__":
    print("Starting Helm overlay...")
    print("Drag the header to move. Press Escape to minimize/restore.")
    overlay = HelmOverlay()
    overlay.run()
