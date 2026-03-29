"""
web/routes/skills.py — Skill Builder API.

Skills are verified step-by-step action sequences that Helm can execute reliably.
Each step is tested individually, then the full chain is tested.

Example skill: "Upload image to Grok"
  Step 1: open_website(url="https://x.com/i/grok")
  Step 2: wait(ms=5000)
  Step 3: look("Where is the text input?") → get coordinates
  Step 4: click(x, y) on the text input
  Step 5: type_text("prompt")
  ...
"""

import logging
import asyncio
import json
from fastapi import APIRouter
from agent.actions import execute_action, _ask_screen, _get_active_window, _screenshot_b64

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/skills/test-step")
async def test_step(body: dict):
    """Test a single action step. Returns the result so user can verify."""
    action = body.get("action", "")
    params = body.get("params", {})

    if not action:
        return {"error": "No action specified"}

    # Execute the action
    result = await asyncio.to_thread(execute_action, action, params)

    # Take a screenshot after
    screenshot = _screenshot_b64()

    # Get active window
    active_window = _get_active_window()

    return {
        "action": action,
        "params": params,
        "ok": result.ok,
        "output": result.output,
        "error": result.error,
        "state_hint": result.state_hint,
        "active_window": active_window,
        "screenshot": screenshot,
    }


@router.post("/skills/test-vision")
async def test_vision(body: dict):
    """Ask the vision model a question about the current screen."""
    question = body.get("question", "What is on screen?")
    scale = body.get("scale", 0.75)

    answer = await asyncio.to_thread(_ask_screen, question, scale)
    screenshot = _screenshot_b64()

    return {
        "question": question,
        "answer": answer,
        "screenshot": screenshot,
    }


@router.post("/skills/test-chain")
async def test_chain(body: dict):
    """Test a chain of steps in sequence. Pauses between steps if pause=true."""
    steps = body.get("steps", [])
    results = []

    for i, step in enumerate(steps):
        action = step.get("action", "")
        params = step.get("params", {})

        if not action:
            results.append({"step": i, "error": "No action"})
            continue

        result = await asyncio.to_thread(execute_action, action, params)
        screenshot = _screenshot_b64()

        results.append({
            "step": i,
            "action": action,
            "params": params,
            "ok": result.ok,
            "output": result.output[:200],
            "error": result.error[:200] if result.error else "",
            "screenshot": screenshot,
        })

        # Stop on failure if stop_on_fail is set
        if not result.ok and step.get("stop_on_fail", True):
            break

    return {"results": results, "completed": len(results)}
