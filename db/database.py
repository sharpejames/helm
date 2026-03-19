from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from db.models import Base

engine = None
AsyncSessionLocal = None

async def init_db(config: dict):
    global engine, AsyncSessionLocal
    db_path = config['db']['path']
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
