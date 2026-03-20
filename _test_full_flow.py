"""
Full flow test: open Paint, select red, draw circle, save, validate.
Run: py _test_full_flow.py
"""
import sys, time, math, os, requests, base64, io
sys.path.insert(0, '.')
from task_runner import (
    open_app, kill_app, ensure_foreground, ensure_maximized,
    new_canvas, get_canvas_bounds, select_color, use_pencil,
    draw_circle, app_save, validate_image, get_active_window,
    wait_ms, click, _screen_size, wait_for_clear, _action
)
from datetime import datetime
from PIL import Image

BASE = "http://127.0.0.1:7331"

def take_screenshot():
    sr = requests.get(f"{BASE}/screenshot/base64?scale=1.0", timeout=10).json()
    return Image.open(io.BytesIO(base64.b64decode(sr['image'])))

print("=" * 60)
print("FULL FLOW TEST: Red circle in Paint")
print("=" * 60)

# 0. Verify clawmetheus is running
try:
    requests.get(f"{BASE}/screenshot/base64?scale=0.5", timeout=5)
    print("[OK] Clawmetheus is running")
except Exception:
    print("[FAIL] Clawmetheus not running at", BASE)
    sys.exit(1)

# 1. Open Paint fresh
print("\n[1] Opening Paint...")
kill_app("Paint")
time.sleep(2)
open_app("mspaint", wait_title="Paint", wait_secs=8)
ensure_foreground("Paint")
ensure_maximized("Paint")
wait_for_clear("Paint")
active = get_active_window()
print(f"    Active window: {active}")
assert "paint" in active.lower(), f"Paint not in foreground! Got: {active}"

# 2. New canvas
print("\n[2] New canvas...")
new_canvas()
wait_ms(800)

# 3. Get canvas bounds
print("\n[3] Getting canvas bounds...")
cl, ct, cr, cb = get_canvas_bounds()
canvas_w, canvas_h = cr - cl, cb - ct
cx, cy = (cl + cr) // 2, (ct + cb) // 2
_sw, _sh = _screen_size()
print(f"    Canvas: ({cl},{ct}) -> ({cr},{cb}) = {canvas_w}x{canvas_h}px")
print(f"    Screen: {_sw}x{_sh}, Center: ({cx},{cy})")
assert canvas_w > 50 and canvas_h > 50, f"Canvas too small: {canvas_w}x{canvas_h}"

# 4. Select red color
print("\n[4] Selecting red color...")
select_color("red")
wait_ms(500)

# 5. Re-verify Paint foreground
print("\n[5] Re-verifying Paint foreground...")
ensure_foreground("Paint")
wait_ms(300)

# 6. Select pencil tool
print("\n[6] Selecting pencil...")
use_pencil()
wait_ms(500)

# 7. Take BEFORE screenshot
print("\n[7] Taking before screenshot...")
img_before = take_screenshot()

# 8. Draw circle
radius = min(canvas_w, canvas_h) // 4
print(f"\n[8] Drawing circle at ({cx},{cy}) r={radius}...")
result = draw_circle(cx, cy, radius)
print(f"    Result: ok={result.get('ok')}")
wait_ms(1000)

# 9. Compare before/after
print("\n[9] Comparing before/after...")
img_after = take_screenshot()
changed = 0
for i in range(200):
    angle = 2 * math.pi * i / 200
    px = int(cx + radius * math.cos(angle))
    py = int(cy + radius * math.sin(angle))
    if 0 <= px < img_before.width and 0 <= py < img_before.height:
        if img_before.getpixel((px, py)) != img_after.getpixel((px, py)):
            changed += 1
print(f"    Pixels changed along circle: {changed}/200")

if changed > 20:
    print("    DRAWING SUCCESS — circle is visible on screen")
else:
    print("    *** DRAWING FAILED — circle not visible ***")
    print("    Check that clawmetheus was restarted after the mouse_event fix.")
    sys.exit(1)

# 10. Save
ts = datetime.now().strftime("%Y%m%d_%H%M%S")
filepath = rf"C:\Users\sharp\Pictures\redcircle_test_{ts}.png"
print(f"\n[10] Saving to {filepath}...")
app_save(filepath, "Paint")

# 11. Validate
print("\n[11] Validating image...")
if os.path.exists(filepath):
    fsize = os.path.getsize(filepath)
    print(f"    File: {fsize} bytes")
    ok, reason = validate_image(filepath, "a red circle on canvas")
    print(f"    validate_image: ok={ok}, reason={reason}")
    if ok:
        print("\n" + "=" * 60)
        print("TEST PASSED")
        print("=" * 60)
    else:
        print(f"\n    TEST FAILED: {reason}")
else:
    print("    File not saved!")
