"""
API endpoints for querying Helm task run history.
Provides observability into what Helm did, what scripts it ran,
what errors occurred, and what the screen looked like at each step.

Endpoints:
  GET /api/runs              — list recent runs with full details (from DB)
  GET /api/runs/last         — shortcut for the most recent run
  GET /api/runs/last/scripts — just the scripts from the last run
  GET /api/runs/:id          — get a specific run with all artifacts
  GET /api/logs              — list recent run logs (from JSON files)
  GET /api/logs/last         — the most recent log file with full event timeline
  GET /api/logs/:filename    — get a specific log file
"""

import os
import json
import logging
from pathlib import Path
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from db.database import AsyncSessionLocal
from tasks.manager import TaskManager

logger = logging.getLogger(__name__)
router = APIRouter()

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")


def _format_run(run, artifacts=None):
    """Format a TaskRun + artifacts into a JSON-friendly dict."""
    result = {
        "id": run.id,
        "task_id": run.task_id,
        "status": run.status,
        "input": run.input,
        "output": run.output,
        "error": run.error,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }
    if run.started_at and run.finished_at:
        result["duration_s"] = round((run.finished_at - run.started_at).total_seconds(), 1)

    if artifacts is not None:
        result["scripts"] = []
        result["screenshots"] = []
        result["urls"] = []
        result["steps"] = []

        for a in artifacts:
            if a.type == "text":
                result["scripts"].append({
                    "label": a.label or "Script",
                    "value": a.value,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                })
            elif a.type == "screenshot":
                # Don't include base64 data by default — too large
                result["screenshots"].append({
                    "id": a.id,
                    "label": a.label or "Screenshot",
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                })
            elif a.type == "url":
                result["urls"].append(a.value)
            else:
                result["steps"].append({
                    "type": a.type,
                    "label": a.label,
                    "value": a.value[:500] if a.value else None,
                })

    return result


@router.get("/runs")
async def list_runs(
    limit: int = Query(default=10, ge=1, le=100),
    status: str = Query(default=None, description="Filter by status: completed, failed, running"),
    include_scripts: bool = Query(default=True, description="Include script text in response"),
):
    """List recent task runs with full details."""
    async with AsyncSessionLocal() as db:
        manager = TaskManager(db)
        runs = await manager.list_runs(limit=limit)

        results = []
        for run in runs:
            if status and run.status != status:
                continue
            artifacts = await manager.get_artifacts(run.id) if include_scripts else None
            results.append(_format_run(run, artifacts))

    return results


@router.get("/runs/last")
async def last_run(include_scripts: bool = Query(default=True)):
    """Get the most recent task run with full details."""
    async with AsyncSessionLocal() as db:
        manager = TaskManager(db)
        runs = await manager.list_runs(limit=1)
        if not runs:
            return JSONResponse({"error": "No runs found"}, status_code=404)
        run = runs[0]
        artifacts = await manager.get_artifacts(run.id) if include_scripts else None
        return _format_run(run, artifacts)


@router.get("/runs/last/scripts")
async def last_run_scripts():
    """Get just the scripts from the most recent run — quick debugging view."""
    async with AsyncSessionLocal() as db:
        manager = TaskManager(db)
        runs = await manager.list_runs(limit=1)
        if not runs:
            return JSONResponse({"error": "No runs found"}, status_code=404)
        run = runs[0]
        artifacts = await manager.get_artifacts(run.id)

        scripts = []
        for a in artifacts:
            if a.type == "text":
                scripts.append({
                    "label": a.label or "Script",
                    "value": a.value,
                })

        return {
            "run_id": run.id,
            "status": run.status,
            "input": run.input,
            "error": run.error,
            "output": run.output,
            "scripts": scripts,
        }


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    """Get a specific run by ID with all artifacts."""
    async with AsyncSessionLocal() as db:
        manager = TaskManager(db)
        run = await manager.get_run(run_id)
        if not run:
            return JSONResponse({"error": "Run not found"}, status_code=404)
        artifacts = await manager.get_artifacts(run.id)
        return _format_run(run, artifacts)


@router.get("/runs/{run_id}/screenshot/{artifact_id}")
async def get_screenshot(run_id: str, artifact_id: str):
    """Get a specific screenshot artifact (base64 image data)."""
    async with AsyncSessionLocal() as db:
        manager = TaskManager(db)
        artifacts = await manager.get_artifacts(run_id)
        for a in artifacts:
            if a.id == artifact_id and a.type == "screenshot":
                return {"id": a.id, "label": a.label, "image": a.value}
        return JSONResponse({"error": "Screenshot not found"}, status_code=404)


# ── JSON Log File Endpoints ──────────────────────────────────────────────────
# These query the structured JSON log files written by ScriptExecutor._flush_log()
# Each log file contains the full event timeline for a single task run.

@router.get("/logs")
async def list_logs(limit: int = Query(default=10, ge=1, le=50)):
    """List recent run log files with summary info (no full event data)."""
    if not os.path.isdir(LOG_DIR):
        return []

    files = sorted(Path(LOG_DIR).glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    results = []
    for f in files[:limit]:
        try:
            with open(f, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
            results.append({
                "filename": f.name,
                "task": data.get("task", ""),
                "status": data.get("status", ""),
                "started": data.get("started"),
                "finished": data.get("finished"),
                "event_count": len(data.get("events", [])),
            })
        except Exception:
            results.append({"filename": f.name, "error": "Could not parse"})

    return results


@router.get("/logs/last")
async def last_log():
    """Get the most recent log file with full event timeline."""
    if not os.path.isdir(LOG_DIR):
        return JSONResponse({"error": "No logs directory"}, status_code=404)

    files = sorted(Path(LOG_DIR).glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        return JSONResponse({"error": "No log files found"}, status_code=404)

    try:
        with open(files[0], 'r', encoding='utf-8') as f:
            data = json.load(f)
        data["filename"] = files[0].name
        return data
    except Exception as e:
        return JSONResponse({"error": f"Could not read log: {e}"}, status_code=500)


@router.get("/logs/{filename}")
async def get_log(filename: str):
    """Get a specific log file by filename."""
    # Sanitize filename to prevent path traversal
    safe_name = os.path.basename(filename)
    filepath = os.path.join(LOG_DIR, safe_name)

    if not os.path.isfile(filepath):
        return JSONResponse({"error": "Log file not found"}, status_code=404)

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        data["filename"] = safe_name
        return data
    except Exception as e:
        return JSONResponse({"error": f"Could not read log: {e}"}, status_code=500)
