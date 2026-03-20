import asyncio
import ast
import os
import re
import sys
import json
import tempfile
import logging
import urllib.parse
import requests
from datetime import datetime, timezone
from typing import AsyncIterator
from agent.models import LLMClient
from agent.prompts import SCRIPT_SYSTEM, SCRIPT_FIX_SYSTEM
from kb import KnowledgeBase
from kb.apps import AppDB

logger = logging.getLogger(__name__)

CLAWMETHEUS_URL = "http://127.0.0.1:7331"
MAX_RETRIES = 2
MAX_SYNTAX_FIXES = 3
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")

# Apps we know about — used to detect which app a task involves
KNOWN_APPS = ["paint", "notepad", "chrome", "word", "excel", "photoshop",
              "firefox", "edge", "explorer", "terminal", "powershell", "vscode"]

class ScriptExecutor:
    def __init__(self, llm: LLMClient, config: dict = None):
        self.llm = llm
        self.kb = KnowledgeBase()
        self.app_db = AppDB()
        self._current_proc: asyncio.subprocess.Process | None = None
        self._stopped = False
        self._last_task: str | None = None
        self._last_script: str | None = None
        self._last_run_id: str | None = None
        self._last_kb_id: str | None = None
        self._run_log: list[dict] = []  # accumulated events for current run
        os.makedirs(LOG_DIR, exist_ok=True)

        # Resolve task_runner.py path — lives in Helm's root directory
        # task_runner.py is the orchestration layer that generated scripts import
        helm_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self._task_runner_path = helm_root.replace("\\", "/")
        tr_file = os.path.join(helm_root, "task_runner.py")
        if not os.path.isfile(tr_file):
            logger.warning(f"task_runner.py not found at {helm_root}")
        else:
            logger.info(f"task_runner.py path: {helm_root}")

    def _log_event(self, event_type: str, data: str | dict, attempt: int = 0):
        """Accumulate a structured log event for the current run."""
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            "attempt": attempt,
            "data": data if isinstance(data, str) else json.dumps(data),
        }
        self._run_log.append(entry)
        logger.info(f"[run] {event_type}: {str(data)[:200]}")

    def _flush_log(self, task: str, status: str):
        """Write accumulated run log to disk as a JSON file."""
        if not self._run_log:
            return
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            slug = re.sub(r'[^a-z0-9]+', '_', task.lower()[:40]).strip('_')
            filename = f"{ts}_{slug}_{status}.json"
            filepath = os.path.join(LOG_DIR, filename)
            log_data = {
                "task": task,
                "status": status,
                "started": self._run_log[0]["ts"] if self._run_log else None,
                "finished": datetime.now(timezone.utc).isoformat(),
                "events": self._run_log,
            }
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(log_data, f, indent=2, ensure_ascii=False)
            logger.info(f"Run log saved: {filepath}")
        except Exception as e:
            logger.error(f"Failed to save run log: {e}")
        finally:
            self._run_log = []

    def _clawmetheus_running(self) -> bool:
        try:
            r = requests.get(f"{CLAWMETHEUS_URL}/status", timeout=3)
            return r.status_code == 200
        except Exception:
            return False

    def _detect_apps(self, task: str) -> list[str]:
        """Detect which apps a task involves from the task description."""
        task_lower = task.lower()
        detected = []
        for app in KNOWN_APPS:
            if app in task_lower:
                detected.append(app.capitalize())
        # Special cases
        if "grok" in task_lower or "x.com" in task_lower or "twitter" in task_lower:
            if "Chrome" not in detected:
                detected.append("Chrome")
        if "mspaint" in task_lower and "Paint" not in detected:
            detected.append("Paint")
        if "draw" in task_lower or "sketch" in task_lower or "canvas" in task_lower:
            if "Paint" not in detected:
                detected.append("Paint")
        return detected

    @property
    def IMPORT_PREAMBLE(self) -> str:
        """Import preamble that MUST be at the top of every generated script."""
        return (
            "import sys, time, requests, base64, os, math, pyautogui\n"
            f"sys.path.insert(0, r'{self._task_runner_path}')\n"
            "from task_runner import *\n"
            "from datetime import datetime\n"
            "\n"
            "# Screen size helper — never hardcode 1920x1080\n"
            "def get_screen_size():\n"
            "    return pyautogui.size()\n"
        )

    def _extract_script(self, text: str) -> str:
        # Try regex first — handle both \n and \r\n line endings
        m = re.search(r'```(?:python)?\s*\r?\n(.*?)```', text, re.DOTALL)
        if m:
            return m.group(1).strip()
        # Fallback: strip fence lines manually
        lines = text.strip().splitlines()
        if lines and lines[0].strip().startswith('```'):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith('```'):
            lines = lines[:-1]
        return '\n'.join(lines).strip()

    def _ensure_imports(self, script: str) -> str:
        """Ensure the script has the task_runner import preamble. Prepend if missing."""
        if not script.strip():
            return script
        if "from task_runner import" in script:
            return script
        logger.warning("Script missing task_runner imports — prepending preamble")
        return self.IMPORT_PREAMBLE + "\n" + script

    # Patterns that indicate flood fill usage — BANNED
    _FLOOD_FILL_PATTERNS = [
        r'\buse_fill\s*\(',
        r'find_tool\s*\(\s*["\']Fill with color["\']',
        r'find_tool\s*\(\s*["\']Fill["\']',
        r'find_tool\s*\(\s*["\']Bucket["\']',
    ]

    def _strip_flood_fill(self, script: str) -> str:
        """Remove flood fill calls from generated scripts. Returns cleaned script."""
        import re as _re
        original = script
        for pattern in self._FLOOD_FILL_PATTERNS:
            if _re.search(pattern, script, _re.IGNORECASE):
                logger.warning(f"FLOOD FILL DETECTED in script — stripping: {pattern}")
                # Comment out the offending lines instead of removing them
                lines = script.split('\n')
                new_lines = []
                for line in lines:
                    if _re.search(pattern, line, _re.IGNORECASE):
                        new_lines.append(f"# STRIPPED (flood fill banned): {line.strip()}")
                        new_lines.append(f"print('[WARNING] Flood fill call was stripped — use shape tools instead')")
                    else:
                        new_lines.append(line)
                script = '\n'.join(new_lines)
        if script != original:
            logger.info("Flood fill calls were stripped from script")
        return script

    def _check_syntax(self, script: str) -> str | None:
        """
        Check script for syntax errors using ast.parse().
        Returns None if valid, or the error message if invalid.
        """
        try:
            ast.parse(script)
            return None
        except SyntaxError as e:
            return f"SyntaxError at line {e.lineno}: {e.msg}"

    def _fix_syntax(self, script: str, error: str) -> str | None:
        """Ask LLM to fix a syntax error. Lightweight — no screen context needed."""
        prompt = f"""This script has a syntax error. Fix ONLY the syntax error, don't change logic.

Error: {error}

```python
{script}
```

Return the corrected script in a ```python block."""
        try:
            raw = self.llm.complete(SCRIPT_FIX_SYSTEM, [{"role": "user", "content": prompt}])
            return self._extract_script(raw)
        except Exception as e:
            logger.error(f"Syntax fix failed: {e}")
            return None

    def _screenshot_b64(self) -> str | None:
        try:
            r = requests.get(f"{CLAWMETHEUS_URL}/screenshot/base64?scale=0.5", timeout=10).json()
            return r.get("image")
        except Exception:
            return None

    def _ask_screen(self, question: str) -> str:
        """Ask Gemini about current screen via Clawmetheus /ask (takes fresh screenshot internally)."""
        try:
            q = urllib.parse.quote(question)
            r = requests.get(f"{CLAWMETHEUS_URL}/ask?q={q}&scale=0.5", timeout=60).json()
            return r.get("answer", "unknown")
        except Exception as e:
            logger.warning(f"Screen ask failed: {e}")
            return "unknown"

    def _fix_script(self, task: str, failed_script: str, error: str, screen_state: str = "") -> str | None:
        prompt = f"""Original task: {task}

Current screen state: {screen_state or 'unknown'}

Failed script:
```python
{failed_script}
```

Error / output:
{error[:1500]}

Write a corrected script that fixes this specific failure."""
        try:
            raw = self.llm.complete(SCRIPT_FIX_SYSTEM, [{"role": "user", "content": prompt}])
            script = self._extract_script(raw)
            if script:
                script = self._ensure_imports(script)
                script = self._strip_flood_fill(script)
            return script
        except Exception as e:
            logger.error(f"Fix script failed: {e}")
            return None

    async def _blocker_monitor(self, stop_event: asyncio.Event) -> None:
        """
        Background task: runs while a script executes.
        Periodically checks for unexpected modals/dialogs and dismisses them
        using keyboard shortcuts first (Escape → Alt+F4 → Ctrl+W),
        then mouse on Cancel/X only — never OK/Yes without context.
        """
        CHECK_INTERVAL = 15.0  # seconds — Gemini free tier is 15 RPM; don't burn quota on monitoring

        while not stop_event.is_set():
            await asyncio.sleep(CHECK_INTERVAL)
            if stop_event.is_set():
                break
            try:
                answer = self._ask_screen(
                    "Is there a modal dialog, popup, alert, or unexpected window "
                    "blocking the main application right now? Answer yes or no only."
                )
                if "yes" not in answer.lower():
                    continue

                logger.info("Blocker monitor: modal detected — attempting keyboard dismiss")

                # Try keyboard shortcuts: Escape → Alt+F4 → Ctrl+W
                dismissed = False
                for keys in [["escape"], ["alt", "f4"], ["ctrl", "w"]]:
                    requests.post(f"{CLAWMETHEUS_URL}/action",
                                  json={"type": "key", "keys": keys}, timeout=5)
                    await asyncio.sleep(0.8)
                    check = self._ask_screen(
                        "Is there still a modal dialog or popup blocking the screen? yes or no"
                    )
                    if "yes" not in check.lower():
                        logger.info(f"Blocker dismissed via {'+'.join(keys)}")
                        dismissed = True
                        break

                if not dismissed:
                    # Fall back to mouse — ask for Cancel/X coords, never OK/Yes
                    coords_answer = self._ask_screen(
                        "There is a modal dialog on screen. Identify the safest button to dismiss it. "
                        "Prefer: Cancel, Close, X, No, Don't Save. Avoid: OK, Yes, Save, Confirm. "
                        "Give ONLY the pixel coordinates as x,y (screenshot is at 0.5 scale)."
                    )
                    m = re.search(r'(\d+)\s*,\s*(\d+)', coords_answer)
                    if m:
                        x, y = int(m.group(1)) * 2, int(m.group(2)) * 2  # scale up
                        requests.post(f"{CLAWMETHEUS_URL}/action",
                                      json={"type": "click", "x": x, "y": y}, timeout=5)
                        logger.info(f"Blocker: clicked dismiss button at ({x},{y})")

            except Exception as e:
                logger.debug(f"Blocker monitor non-fatal error: {e}")

    async def _run_script(self, script: str) -> dict:
        tmp = tempfile.NamedTemporaryFile(
            mode='w', suffix='.py', delete=False, encoding='utf-8'
        )
        try:
            tmp.write(script)
            tmp.close()
            proc = await asyncio.create_subprocess_exec(
                sys.executable, tmp.name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, 'PYTHONUTF8': '1'}
            )
            self._current_proc = proc

            # Run background blocker monitor alongside the script
            stop_monitor = asyncio.Event()
            monitor_task = asyncio.create_task(self._blocker_monitor(stop_monitor))

            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
            except asyncio.TimeoutError:
                # Capture any partial output before killing
                partial_out = ""
                try:
                    # Read whatever is available in the pipe
                    if proc.stdout:
                        partial_data = await asyncio.wait_for(proc.stdout.read(65536), timeout=2)
                        partial_out = partial_data.decode('utf-8', errors='replace').strip()
                except Exception:
                    pass
                proc.kill()
                return {"success": False, "output": partial_out, "error": "Script timed out after 300s"}
            except asyncio.CancelledError:
                proc.kill()
                return {"success": False, "output": "", "error": "Task stopped by user"}
            finally:
                self._current_proc = None
                stop_monitor.set()
                monitor_task.cancel()
                try:
                    await monitor_task
                except asyncio.CancelledError:
                    pass

            if self._stopped:
                return {"success": False, "output": "", "error": "Task stopped by user"}

            out = stdout.decode('utf-8', errors='replace').strip()
            err = stderr.decode('utf-8', errors='replace').strip()
            return {"success": proc.returncode == 0, "output": out, "error": err}
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass

    def stop(self):
        """Stop the current task. Kills the running script subprocess."""
        self._stopped = True
        if self._current_proc:
            try:
                self._current_proc.kill()
            except Exception:
                pass
            return True
        return False

    async def stream_task(self, task: str) -> AsyncIterator[dict]:
        # Reset stop flag for new task
        self._stopped = False
        self._current_proc = None
        self._last_task = task
        self._last_script = None
        self._last_run_id = None
        self._task_incomplete = False
        self._run_log = []

        self._log_event("task_start", task)

        # 1. Check Clawmetheus
        if not self._clawmetheus_running():
            self._log_event("error", "Clawmetheus not running")
            self._flush_log(task, "error")
            yield {"type": "error", "data": "Clawmetheus not running. Start it: cd workspace/clawmetheus && .\\start.ps1"}
            return

        # 2. PRE-EXECUTION: screenshot + screen state check
        yield {"type": "status", "data": "Checking current screen state..."}
        screen_state = self._ask_screen(
            "What application is currently in focus and visible on screen? "
            "List all open windows you can see. Be specific."
        )
        self._log_event("screen_state", screen_state)
        yield {"type": "step", "data": f"Screen: {screen_state}"}

        # 3. Search KB for similar proven scripts
        kb_examples = self.kb.format_examples(task, limit=2)
        if kb_examples:
            self._log_event("kb_match", f"Found {len(self.kb.search(task, limit=2))} similar scripts")
            yield {"type": "step", "data": f"Found {len(self.kb.search(task, limit=2))} similar proven scripts in KB"}

        # 3b. Load app-specific knowledge
        detected_apps = self._detect_apps(task)
        app_context = ""
        for app_name in detected_apps:
            ctx = self.app_db.format_context(app_name)
            if ctx:
                app_context += ctx
                yield {"type": "step", "data": f"Loaded knowledge profile for {app_name}"}

        # 4. Generate script with screen context + KB examples + app knowledge
        yield {"type": "status", "data": "Writing automation script..."}
        context = f"{task}\n\n[Current screen state: {screen_state}]"
        if app_context:
            context += app_context
        if kb_examples:
            context += kb_examples

        self._log_event("llm_request", {"prompt_length": len(context)})

        try:
            system_prompt = SCRIPT_SYSTEM.replace("{task_runner_path}", self._task_runner_path)
            raw = self.llm.complete(system_prompt, [{"role": "user", "content": context}])
        except Exception as e:
            self._log_event("error", f"LLM error: {e}")
            self._flush_log(task, "failed")
            yield {"type": "error", "data": f"LLM error: {e}"}
            return

        script = self._extract_script(raw)

        # 4b. Catch empty script — don't waste retries running nothing
        if not script.strip():
            self._log_event("error", f"LLM returned empty script. Raw response length: {len(raw)}")
            self._log_event("debug_raw_response", raw[:2000])
            self._flush_log(task, "failed")
            yield {"type": "error", "data": f"LLM returned no code. Raw response ({len(raw)} chars) had no ```python block. Check model output."}
            return

        # 4c. Ensure task_runner imports are present
        script = self._ensure_imports(script)

        # 4d. Strip any flood fill calls (LLM keeps generating them despite prompt rules)
        script = self._strip_flood_fill(script)

        # 5. Syntax pre-check — fix syntax errors before burning a real attempt
        for syntax_attempt in range(MAX_SYNTAX_FIXES):
            syntax_err = self._check_syntax(script)
            if not syntax_err:
                break
            self._log_event("syntax_error", syntax_err, attempt=syntax_attempt)
            yield {"type": "warning", "data": f"Syntax error (auto-fixing): {syntax_err}"}
            fixed = self._fix_syntax(script, syntax_err)
            if not fixed:
                break
            script = fixed
            yield {"type": "step", "data": f"Syntax fix attempt {syntax_attempt + 1}/{MAX_SYNTAX_FIXES}"}
        else:
            # Still has syntax errors after all fix attempts
            syntax_err = self._check_syntax(script)
            if syntax_err:
                self._log_event("error", f"Unfixable syntax error: {syntax_err}")
                self._flush_log(task, "failed")
                yield {"type": "error", "data": f"Could not fix syntax error: {syntax_err}"}
                return

        logger.info(f"Generated script ({len(script)} chars)")
        self._last_script = script
        self._log_event("script_generated", {"length": len(script), "attempt": 1})
        yield {"type": "artifact", "data": {"type": "text", "value": script, "label": "Script"}}

        # 4. Execute with retry loop
        last_result = None
        for attempt in range(MAX_RETRIES + 1):
            # Check stop between retries
            if self._stopped:
                self._log_event("stopped", "User requested stop")
                self._flush_log(task, "stopped")
                yield {"type": "warning", "data": "Stopped by user."}
                yield {"type": "done", "data": "Task stopped."}
                return

            if attempt == 0:
                yield {"type": "status", "data": "Executing..."}
            else:
                yield {"type": "status", "data": f"Retrying with corrected script (attempt {attempt+1}/{MAX_RETRIES+1})..."}

            self._log_event("execute_start", f"attempt {attempt+1}", attempt=attempt)
            result = await self._run_script(script)
            last_result = result

            # Check stop after script execution
            if self._stopped:
                self._log_event("stopped", "User requested stop")
                self._flush_log(task, "stopped")
                yield {"type": "warning", "data": "Stopped by user."}
                yield {"type": "done", "data": "Task stopped."}
                return

            # Log script output
            self._log_event("execute_result", {
                "success": result["success"],
                "output": result["output"][:2000] if result["output"] else "",
                "error": result["error"][:2000] if result["error"] else "",
            }, attempt=attempt)

            # Stream stdout progress
            if result["output"]:
                for line in result["output"].splitlines():
                    if line.strip():
                        yield {"type": "step", "data": line}

            # POST-EXECUTION: screenshot + verify
            img = self._screenshot_b64()
            if img:
                yield {"type": "artifact", "data": {
                    "type": "screenshot",
                    "value": img,
                    "label": f"Result (attempt {attempt+1})"
                }}

            if result["success"]:
                # CODE-BASED completion checks FIRST (more reliable than vision)
                script_output = result.get("output", "")
                task_lower = task.lower()
                incomplete_signs = []

                if "save" in task_lower and "Saved:" not in script_output and "SUCCESS" not in script_output:
                    incomplete_signs.append("save step may not have completed (no save confirmation in output)")
                if ("grok" in task_lower or "upload" in task_lower):
                    if "Prompt submitted to Grok" not in script_output:
                        incomplete_signs.append("Grok prompt was never submitted (missing 'Prompt submitted to Grok' in output)")
                    if "timed out" in script_output.lower() or "not found" in script_output.lower()[-300:]:
                        incomplete_signs.append("Grok interaction had errors (timeout or element not found)")
                    if "attachment to Grok failed" in script_output:
                        incomplete_signs.append("File attachment to Grok failed")

                # IMAGE VALIDATION: Check saved images aren't blank/destroyed
                if ("draw" in task_lower or "paint" in task_lower or "picture" in task_lower):
                    saved_files = re.findall(r'(?:SUCCESS|Saved|saved)[:\s]+([A-Z]:\\[^\s\r\n]+\.png)', script_output)
                    if not saved_files:
                        # Also try to find file paths mentioned with validate_image
                        saved_files = re.findall(r'validate_image.*?PASSED[:\s]+([A-Z]:\\[^\s\r\n]+\.png)', script_output)
                    for fpath in saved_files:
                        fpath = fpath.strip().rstrip(')')
                        if os.path.exists(fpath):
                            fsize = os.path.getsize(fpath)
                            if fsize < 5000:
                                incomplete_signs.append(f"Saved image is too small ({fsize} bytes) — likely blank or flood-filled: {fpath}")
                            elif "validation failed" in script_output.lower():
                                incomplete_signs.append(f"Image validation failed for {fpath}")
                    if "validation failed" in script_output.lower() and not saved_files:
                        incomplete_signs.append("Image validation failed (drawing may be blank or destroyed)")

                if incomplete_signs:
                    warning = "Task INCOMPLETE: " + "; ".join(incomplete_signs)
                    self._log_event("incomplete", warning, attempt=attempt)
                    yield {"type": "warning", "data": warning}
                    # Treat as failure if we have retries left
                    if attempt < MAX_RETRIES:
                        result["success"] = False
                        error_text = warning
                        yield {"type": "status", "data": "Analyzing incomplete execution..."}
                        fixed = self._fix_script(task, script, error_text, "Task incomplete — see error for details")
                        if fixed:
                            for sx in range(MAX_SYNTAX_FIXES):
                                sx_err = self._check_syntax(fixed)
                                if not sx_err:
                                    break
                                fixed2 = self._fix_syntax(fixed, sx_err)
                                if not fixed2:
                                    break
                                fixed = fixed2
                            script = fixed
                            self._last_script = script
                            self._log_event("script_fixed", {"length": len(script)}, attempt=attempt+1)
                            yield {"type": "artifact", "data": {"type": "text", "value": script, "label": f"Fixed script (attempt {attempt+2})"}}
                            continue  # retry with fixed script
                    else:
                        self._task_incomplete = True
                        yield {"type": "warning", "data": "No retries left — task did NOT complete successfully"}

                # Vision verification (secondary — only if code checks passed)
                verify_prompt = f"The task was: '{task[:200]}'. "
                verify_prompt += "Check these specific things:\n"
                verify_prompt += "1. What is currently on screen?\n"
                verify_prompt += "2. If the task involved SAVING a file — was a file actually saved? (look for save confirmation or the file path in the script output)\n"
                verify_prompt += "3. If the task involved uploading to a WEBSITE (like Grok) — is that website showing a response or the uploaded content?\n"
                verify_prompt += "4. Did ALL parts of the task complete, or did it stop partway through?\n"
                verify_prompt += "Answer ONLY: COMPLETE or INCOMPLETE. If INCOMPLETE, say what's missing. Do NOT say COMPLETE if any part is missing."
                verify = self._ask_screen(verify_prompt)
                self._log_event("verification", verify, attempt=attempt)
                yield {"type": "step", "data": f"Verification: {verify}"}

                if "incomplete" in verify.lower() and not incomplete_signs:
                    warning = f"Vision check says incomplete: {verify[:200]}"
                    self._log_event("incomplete", warning, attempt=attempt)
                    yield {"type": "warning", "data": warning}
                    if attempt < MAX_RETRIES:
                        result["success"] = False
                        error_text = warning
                        yield {"type": "status", "data": "Analyzing incomplete execution..."}
                        fixed = self._fix_script(task, script, error_text, verify)
                        if fixed:
                            for sx in range(MAX_SYNTAX_FIXES):
                                sx_err = self._check_syntax(fixed)
                                if not sx_err:
                                    break
                                fixed2 = self._fix_syntax(fixed, sx_err)
                                if not fixed2:
                                    break
                                fixed = fixed2
                            script = fixed
                            self._last_script = script
                            self._log_event("script_fixed", {"length": len(script)}, attempt=attempt+1)
                            yield {"type": "artifact", "data": {"type": "text", "value": script, "label": f"Fixed script (attempt {attempt+2})"}}
                            continue  # retry with fixed script
                    else:
                        self._task_incomplete = True
                        yield {"type": "warning", "data": "No retries left — task did NOT complete successfully"}

                break

            # Failed — get screen state for fix context
            screen_now = self._ask_screen(
                "What went wrong? What is on screen right now? "
                "Which application is in focus?"
            )
            error_text = result["error"] or result["output"] or "Unknown error"
            self._log_event("attempt_failed", {
                "error": error_text[:500],
                "screen": screen_now[:500],
            }, attempt=attempt)
            yield {"type": "warning", "data": f"Attempt {attempt+1} failed: {error_text[:200]}"}
            yield {"type": "step", "data": f"Screen after failure: {screen_now}"}

            if attempt < MAX_RETRIES:
                yield {"type": "status", "data": "Analyzing failure and generating fix..."}
                fixed = self._fix_script(task, script, error_text, screen_now)
                if not fixed:
                    self._log_event("error", "Could not generate fix script")
                    self._flush_log(task, "failed")
                    yield {"type": "error", "data": "Could not generate a fix."}
                    return
                # Syntax-check the fix before using it
                for sx in range(MAX_SYNTAX_FIXES):
                    sx_err = self._check_syntax(fixed)
                    if not sx_err:
                        break
                    yield {"type": "warning", "data": f"Fix has syntax error (auto-fixing): {sx_err}"}
                    fixed2 = self._fix_syntax(fixed, sx_err)
                    if not fixed2:
                        break
                    fixed = fixed2
                script = fixed
                self._last_script = script
                self._log_event("script_fixed", {"length": len(script)}, attempt=attempt+1)
                yield {"type": "artifact", "data": {"type": "text", "value": script, "label": f"Fixed script (attempt {attempt+2})"}}
            else:
                self._log_event("error", f"Failed after {MAX_RETRIES+1} attempts: {error_text[:500]}")
                self._flush_log(task, "failed")
                yield {"type": "error", "data": f"Failed after {MAX_RETRIES+1} attempts:\n{error_text[:500]}"}
                # Save failed script to KB with negative rating for learning
                if self._last_script:
                    try:
                        app = detected_apps[0] if detected_apps else ""
                        tags = [w for w in task.lower().split() if len(w) > 3 and w.isalpha()][:5]
                        kb_id = self.kb.save(
                            task=task, script=self._last_script,
                            tags=tags, app=app, run_id=self._last_run_id,
                        )
                        self.kb.rate(kb_id, thumbs_up=False)  # auto thumbs-down
                        self._last_kb_id = kb_id
                        yield {"type": "feedback_request", "data": {
                            "id": kb_id, "task": task, "success": False,
                            "message": "Task failed. What went wrong? Your feedback helps Helm learn."
                        }}
                    except Exception:
                        pass
                return

        # 5. Extract URLs from output
        if last_result and last_result["output"]:
            urls = re.findall(r'https?://\S+', last_result["output"])
            for url in urls:
                yield {"type": "artifact", "data": {"type": "url", "value": url.rstrip('.,)'), "label": "URL"}}

        # 6. Save successful script to KB
        final_status = "completed" if (last_result and last_result["success"] and not getattr(self, '_task_incomplete', False)) else "partial"
        if last_result and last_result["success"] and not getattr(self, '_task_incomplete', False) and self._last_script:
            try:
                app = detected_apps[0] if detected_apps else ""
                tags = [w for w in task.lower().split() if len(w) > 3 and w.isalpha()][:5]
                if app:
                    tags.append(app.lower())

                kb_id = self.kb.save(
                    task=task, script=self._last_script,
                    tags=tags, app=app, run_id=self._last_run_id,
                )
                self._last_kb_id = kb_id
                yield {"type": "feedback_request", "data": {
                    "id": kb_id, "task": task, "success": True,
                    "message": "How'd it do? Your feedback helps Helm learn."
                }}
                logger.info(f"Saved to KB: {kb_id}")
            except Exception as e:
                logger.warning(f"KB save failed: {e}")

        summary = f"Done: {task}"
        if last_result and last_result["output"]:
            summary += f"\n{last_result['output'][:300]}"
        self._log_event("task_done", {"status": final_status, "summary": summary[:500]})
        self._flush_log(task, final_status)
        yield {"type": "done", "data": summary}
