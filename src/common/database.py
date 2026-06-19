from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base
from contextlib import asynccontextmanager
import os

# Base class for SQLAlchemy models
Base = declarative_base()

# Retrieve the database URL from environment variables, default to a local postgres instance
# We use asyncpg for async connection pooling
DATABASE_URL = os.getenv(
    "DATABASE_URL", 
    "postgresql+asyncpg://postgres:postgres@localhost:5432/agent_db"
)

# Create an asynchronous engine with connection pooling
# pool_size and max_overflow help make the DB robust under bursty load
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True  # Checks connection liveness before using
)

# Async session factory
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False
)

@asynccontextmanager
async def get_db_session():
    """
    Dependency to get an async database session.
    Ensures safe commit/rollback and cleanup.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
