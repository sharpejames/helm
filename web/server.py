import logging
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path
from agent.models import LLMClient
from agent.script_executor import ScriptExecutor
from db.database import get_db

logger = logging.getLogger(__name__)

def create_app(config: dict, scheduler) -> FastAPI:
    app = FastAPI(title="Helm", version="2.0.0")
    
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    # ScriptExecutor: generates complete Python scripts using task_runner.py
    # This is the proven approach — one LLM call generates a full script
    llm = LLMClient(config)
    executor = ScriptExecutor(llm)

    # Store in app state
    app.state.config = config
    app.state.scheduler = scheduler
    app.state.executor = executor
    
    # Mount routes
    from web.routes import chat, tasks, settings, runs
    app.include_router(chat.router, prefix="/api")
    app.include_router(tasks.router, prefix="/api")
    app.include_router(settings.router, prefix="/api")
    app.include_router(runs.router, prefix="/api")
    
    # Serve static files
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
    
    @app.get("/health")
    async def health():
        return {"status": "ok"}
    
    return app
