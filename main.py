import asyncio
import logging
import uvicorn
from config import load_config
from db.database import init_db
from core.vision import init_vision
from tasks.scheduler import init_scheduler
from web.server import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("helm")

async def main():
    config = load_config()
    logger.info("Helm starting up...")

    await init_db(config)
    logger.info("Database initialized")

    init_vision(config)
    logger.info("Vision model initialized")

    scheduler = init_scheduler(config)
    scheduler.start()
    logger.info("Scheduler started")

    app = create_app(config, scheduler)

    host = config['server']['host']
    port = config['server']['port']
    logger.info(f"Web UI: http://localhost:{port}")

    cfg = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(cfg)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main())
