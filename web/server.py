import logging
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path
from datetime import datetime
from agent.models import LLMClient
from agent.script_executor import ScriptExecutor
from agent.step_executor import StepExecutor
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
    
    llm = LLMClient(config)

    # Initialize local LLM for hybrid mode (if configured)
    from agent.models import init_llm
    init_llm(config)

    # Executor mode: "step" (new, step-by-step) or "script" (legacy, monolithic)
    executor_mode = config.get("executor", {}).get("mode", "step")
    if executor_mode == "script":
        logger.info("Using SCRIPT executor (legacy monolithic mode)")
        executor = ScriptExecutor(llm, config)
    else:
        logger.info("Using STEP executor (action-by-action mode)")
        executor = StepExecutor(llm, config)

    # Log startup info for debugging
    boot_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info(f"=== Helm server started at {boot_ts} ===")
    logger.info(f"Executor: {executor_mode}, MAX_STEPS={StepExecutor.__module__}")
    from agent.actions import ACTION_REGISTRY
    logger.info(f"Actions loaded: {len(ACTION_REGISTRY)} actions")
    from agent.models import get_local_llm, get_router
    local = get_local_llm()
    router = get_router()
    if router:
        logger.info(f"Model tiers: smart={router.smart_model}, fast={router.fast_model}, "
                     f"local={local.model if local else 'none'}")
    elif local:
        logger.info(f"Hybrid mode ACTIVE: local LLM = {local.model} at {local.base_url}")
    else:
        logger.info("Hybrid mode OFF: no local LLM configured (remote-only)")

    # Store in app state
    app.state.config = config
    app.state.scheduler = scheduler
    app.state.executor = executor
    
    # Mount routes
    from web.routes import chat, tasks, settings, runs, learn
    app.include_router(chat.router, prefix="/api")
    app.include_router(tasks.router, prefix="/api")
    app.include_router(settings.router, prefix="/api")
    app.include_router(runs.router, prefix="/api")
    app.include_router(learn.router, prefix="/api")

    @app.get("/health")
    async def health():
        return {"status": "ok"}
    
    # Serve static files (MUST be last — catches all unmatched routes)
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
    
    return app
