from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base
from contextlib import asynccontextmanager

from .config import settings

# Base class for SQLAlchemy models
Base = declarative_base()

# Retrieve the database URL from settings
# We use asyncpg for async connection pooling
DATABASE_URL = settings.database_url

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
