import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)
_scheduler: AsyncIOScheduler = None

def init_scheduler(config: dict) -> AsyncIOScheduler:
    global _scheduler
    tz = config.get('scheduler', {}).get('timezone', 'UTC')
    _scheduler = AsyncIOScheduler(timezone=tz)
    return _scheduler

def get_scheduler() -> AsyncIOScheduler:
    return _scheduler

def add_cron_job(job_id: str, func, cron_expr: str, **kwargs):
    parts = cron_expr.split()
    if len(parts) != 5:
        raise ValueError(f"Invalid cron: {cron_expr}")
    minute, hour, day, month, dow = parts
    trigger = CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=dow)
    _scheduler.add_job(func, trigger, id=job_id, replace_existing=True, **kwargs)
    logger.info(f"Cron job {job_id}: {cron_expr}")

def add_interval_job(job_id: str, func, minutes: int = None, hours: int = None, **kwargs):
    trigger = IntervalTrigger(minutes=minutes, hours=hours)
    _scheduler.add_job(func, trigger, id=job_id, replace_existing=True, **kwargs)

def remove_job(job_id: str):
    try:
        _scheduler.remove_job(job_id)
    except Exception:
        pass

def list_jobs() -> list[dict]:
    return [{"id": j.id, "next_run": str(j.next_run_time), "trigger": str(j.trigger)}
            for j in _scheduler.get_jobs()]
