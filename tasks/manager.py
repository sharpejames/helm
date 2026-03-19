import uuid
import logging
from datetime import datetime
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession
from db.models import Task, TaskRun, Artifact

logger = logging.getLogger(__name__)

class TaskManager:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_task(self, name: str, description: str = "", type: str = "manual", schedule: str = None) -> Task:
        task = Task(id=str(uuid.uuid4()), name=name, description=description, type=type, schedule=schedule)
        self.db.add(task)
        await self.db.commit()
        await self.db.refresh(task)
        return task

    async def get_task(self, task_id: str) -> Task | None:
        r = await self.db.execute(select(Task).where(Task.id == task_id))
        return r.scalar_one_or_none()

    async def list_tasks(self) -> list[Task]:
        r = await self.db.execute(select(Task).order_by(desc(Task.created_at)))
        return list(r.scalars().all())

    async def update_task(self, task_id: str, **kwargs) -> Task | None:
        task = await self.get_task(task_id)
        if not task:
            return None
        for k, v in kwargs.items():
            setattr(task, k, v)
        task.updated_at = datetime.utcnow()
        await self.db.commit()
        await self.db.refresh(task)
        return task

    async def delete_task(self, task_id: str) -> bool:
        task = await self.get_task(task_id)
        if not task:
            return False
        await self.db.delete(task)
        await self.db.commit()
        return True

    async def create_run(self, task_id: str | None, input_data: str) -> TaskRun:
        run = TaskRun(id=str(uuid.uuid4()), task_id=task_id, status="pending", input=input_data)
        self.db.add(run)
        await self.db.commit()
        await self.db.refresh(run)
        return run

    async def update_run(self, run_id: str, **kwargs) -> TaskRun | None:
        r = await self.db.execute(select(TaskRun).where(TaskRun.id == run_id))
        run = r.scalar_one_or_none()
        if not run:
            return None
        for k, v in kwargs.items():
            setattr(run, k, v)
        await self.db.commit()
        return run

    async def get_run(self, run_id: str) -> TaskRun | None:
        r = await self.db.execute(select(TaskRun).where(TaskRun.id == run_id))
        return r.scalar_one_or_none()

    async def list_runs(self, task_id: str = None, limit: int = 50) -> list[TaskRun]:
        q = select(TaskRun).order_by(desc(TaskRun.started_at)).limit(limit)
        if task_id:
            q = q.where(TaskRun.task_id == task_id)
        r = await self.db.execute(q)
        return list(r.scalars().all())

    async def save_artifact(self, run_id: str, type: str, value: str, label: str = "", step: int = 0) -> Artifact:
        a = Artifact(id=str(uuid.uuid4()), run_id=run_id, type=type, value=value, label=label, step=step)
        self.db.add(a)
        await self.db.commit()
        return a

    async def get_artifacts(self, run_id: str) -> list[Artifact]:
        r = await self.db.execute(select(Artifact).where(Artifact.run_id == run_id))
        return list(r.scalars().all())
