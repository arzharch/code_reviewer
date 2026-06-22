from contextlib import asynccontextmanager
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from src.common.config import settings

@asynccontextmanager
async def get_checkpointer():
    """
    Context manager to yield an AsyncPostgresSaver checkpointer for LangGraph.
    """
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    async with AsyncPostgresSaver.from_conn_string(dsn) as checkpointer:
        await checkpointer.setup()
        yield checkpointer
