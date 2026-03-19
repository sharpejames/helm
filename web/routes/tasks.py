import logging
from fastapi import APIRouter, Request, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from db.database import get_db
from tasks.manager import TaskManager
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()

class TaskCreate(BaseModel):
    name: str
    description: str = ""
    type: str = "manual"
    schedule: str = None

class TaskUpdate(BaseModel):
    name: str = None
    description: str = None
    schedule: str = None
    enabled: bool = None

@router.get("/tasks")
async def list_tasks(db: AsyncSession = Depends(get_db)):
    manager = TaskManager(db)
    tasks = await manager.list_tasks()
    return [{"id": t.id, "name": t.name, "description": t.description, "type": t.type, 
             "schedule": t.schedule, "enabled": t.enabled, "created_at": t.created_at.isoformat()}
            for t in tasks]

@router.post("/tasks")
async def create_task(task: TaskCreate, db: AsyncSession = Depends(get_db)):
    manager = TaskManager(db)
    t = await manager.create_task(task.name, task.description, task.type, task.schedule)
    return {"id": t.id, "name": t.name, "description": t.description, "type": t.type, 
            "schedule": t.schedule, "enabled": t.enabled}

@router.get("/tasks/{task_id}")
async def get_task(task_id: str, db: AsyncSession = Depends(get_db)):
    manager = TaskManager(db)
    task = await manager.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    
    runs = await manager.list_runs(task_id=task_id, limit=10)
    runs_data = []
    for run in runs:
        artifacts = await manager.get_artifacts(run.id)
        runs_data.append({
            "id": run.id,
            "status": run.status,
            "input": run.input,
            "output": run.output,
            "error": run.error,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "artifacts": [{"type": a.type, "label": a.label, "value": a.value if a.type != "screenshot" else None} 
                         for a in artifacts]
        })
    
    return {
        "id": task.id,
        "name": task.name,
        "description": task.description,
        "type": task.type,
        "schedule": task.schedule,
        "enabled": task.enabled,
        "created_at": task.created_at.isoformat(),
        "runs": runs_data
    }

@router.put("/tasks/{task_id}")
async def update_task(task_id: str, update: TaskUpdate, db: AsyncSession = Depends(get_db)):
    manager = TaskManager(db)
    updates = {k: v for k, v in update.dict().items() if v is not None}
    task = await manager.update_task(task_id, **updates)
    if not task:
        raise HTTPException(404, "Task not found")
    return {"id": task.id, "name": task.name, "enabled": task.enabled}

@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str, db: AsyncSession = Depends(get_db)):
    manager = TaskManager(db)
    ok = await manager.delete_task(task_id)
    if not ok:
        raise HTTPException(404, "Task not found")
    return {"deleted": True}

@router.post("/tasks/{task_id}/run")
async def run_task(task_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    manager = TaskManager(db)
    task = await manager.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    
    # Create a run and execute
    run = await manager.create_run(task_id, task.description)
    # TODO: Execute task in background
    return {"run_id": run.id, "status": "pending"}

@router.get("/runs/{run_id}")
async def get_run(run_id: str, db: AsyncSession = Depends(get_db)):
    manager = TaskManager(db)
    run = await manager.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    
    artifacts = await manager.get_artifacts(run_id)
    return {
        "id": run.id,
        "task_id": run.task_id,
        "status": run.status,
        "input": run.input,
        "output": run.output,
        "error": run.error,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "artifacts": [{"id": a.id, "type": a.type, "value": a.value, "label": a.label, "step": a.step}
                     for a in artifacts]
    }
