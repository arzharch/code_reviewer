from contextlib import asynccontextmanager
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
import asyncpg
from src.common.config import settings

@asynccontextmanager
async def get_checkpointer():
    """
    Context manager to yield an AsyncPostgresSaver checkpointer for LangGraph.
    Uses asyncpg directly as required by langgraph-checkpoint-postgres.
    """
    # The checkpointer expects an asyncpg connection pool
    async with asyncpg.create_pool(settings.database_url, min_size=1, max_size=5) as pool:
        checkpointer = AsyncPostgresSaver(pool)
        
        # Ensure the underlying checkpointer tables are created (checkpoints, checkpoint_writes, etc.)
        await checkpointer.setup()
        
        yield checkpointer
