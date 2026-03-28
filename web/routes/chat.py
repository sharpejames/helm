import logging
import json
from datetime import datetime
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse, JSONResponse
from db.database import AsyncSessionLocal
from tasks.manager import TaskManager
from kb import KnowledgeBase
from kb.apps import AppDB

logger = logging.getLogger(__name__)
router = APIRouter()
_kb = KnowledgeBase()
_app_db = AppDB()

@router.post("/chat")
async def chat(request: Request):
    body = await request.json()
    message = body.get("message", "")
    
    if not message:
        return {"error": "No message provided"}
    
    executor = request.app.state.executor

    # Create run record before streaming (own session, committed immediately)
    async with AsyncSessionLocal() as db:
        manager = TaskManager(db)
        run = await manager.create_run(None, message)
        await manager.update_run(run.id, status="running", started_at=datetime.utcnow())
        run_id = run.id

    async def event_stream():
        try:
            import asyncio
            last_event_time = asyncio.get_event_loop().time()

            async for event in executor.stream_task(message):
                yield f"data: {json.dumps(event)}\n\n"
                last_event_time = asyncio.get_event_loop().time()

                # Save artifacts and status updates in their own DB sessions
                try:
                    if event["type"] == "artifact":
                        async with AsyncSessionLocal() as db:
                            mgr = TaskManager(db)
                            ad = event["data"]
                            await mgr.save_artifact(
                                run_id, ad["type"], ad["value"],
                                ad.get("label", ""), 0
                            )

                    if event["type"] == "done":
                        async with AsyncSessionLocal() as db:
                            mgr = TaskManager(db)
                            await mgr.update_run(
                                run_id, status="completed",
                                output=event["data"],
                                finished_at=datetime.utcnow()
                            )

                    if event["type"] == "error":
                        async with AsyncSessionLocal() as db:
                            mgr = TaskManager(db)
                            await mgr.update_run(
                                run_id, status="failed",
                                error=event["data"],
                                finished_at=datetime.utcnow()
                            )
                except Exception as db_err:
                    logger.error(f"DB save error: {db_err}")

        except Exception as e:
            logger.error(f"Chat stream error: {e}", exc_info=True)
            try:
                async with AsyncSessionLocal() as db:
                    mgr = TaskManager(db)
                    await mgr.update_run(
                        run_id, status="failed",
                        error=str(e),
                        finished_at=datetime.utcnow()
                    )
            except Exception:
                pass
            yield f"data: {json.dumps({'type': 'error', 'data': str(e)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")

@router.post("/stop")
async def stop_task(request: Request):
    """Stop the currently running task."""
    executor = request.app.state.executor
    stopped = executor.stop()
    if stopped:
        logger.info("Task stop requested by user")
        return JSONResponse({"status": "stopped", "message": "Stop signal sent."})
    return JSONResponse({"status": "idle", "message": "No task running."})

@router.get("/chat/history")
async def chat_history(limit: int = 20):
    async with AsyncSessionLocal() as db:
        manager = TaskManager(db)
        runs = await manager.list_runs(limit=limit)

        result = []
        for run in runs:
            artifacts = await manager.get_artifacts(run.id)
            result.append({
                "id": run.id,
                "input": run.input,
                "output": run.output,
                "status": run.status,
                "error": run.error,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                "artifacts": [{"type": a.type, "value": a.value if a.type != "screenshot" else None, "label": a.label} for a in artifacts]
            })

    return result


# ── Knowledge Base endpoints ──────────────────────────────────────────────────

@router.post("/feedback")
async def feedback(request: Request):
    """Thumbs up/down on a KB entry with optional context."""
    body = await request.json()
    entry_id = body.get("id", "")
    thumbs_up = body.get("thumbs_up", True)
    context = body.get("context", "")

    if not entry_id:
        return JSONResponse({"error": "Missing id"}, status_code=400)

    # Rate in script KB
    found = _kb.rate(entry_id, thumbs_up=thumbs_up, context=context)

    if not found:
        return JSONResponse({"error": "Entry not found"}, status_code=404)

    entry = _kb.get(entry_id)

    # Also store feedback in the app knowledge DB
    if entry and entry.get("app") and context:
        _app_db.add_feedback(entry["app"], context, success=thumbs_up)

    # If rating drops below -1, remove from KB
    if entry and entry.get("rating", 0) < -1:
        _kb.remove(entry_id)
        return JSONResponse({"status": "removed", "id": entry_id,
                             "message": "Script removed from KB (too many thumbs down)"})

    return JSONResponse({
        "status": "rated",
        "id": entry_id,
        "rating": entry.get("rating", 0) if entry else 0,
    })


@router.get("/kb")
async def list_kb(limit: int = 50):
    """List all KB entries."""
    entries = _kb.list_all(limit=limit)
    return [
        {
            "id": e["id"],
            "task": e["task"],
            "app": e.get("app", ""),
            "tags": e.get("tags", []),
            "rating": e.get("rating", 0),
            "thumbs_up": e.get("thumbs_up", 0),
            "thumbs_down": e.get("thumbs_down", 0),
            "timestamp": e.get("timestamp", 0),
        }
        for e in entries
    ]


@router.delete("/kb/{entry_id}")
async def delete_kb_entry(entry_id: str):
    """Delete a KB entry."""
    if _kb.remove(entry_id):
        return JSONResponse({"status": "deleted", "id": entry_id})
    return JSONResponse({"error": "Entry not found"}, status_code=404)
