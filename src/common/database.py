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

from .models import Run, Finding, AuditLog
import uuid
from typing import Dict, Any, Optional

async def log_run(run_id: str, repo: str, status: str, surface: str = "github_app", pr_number: Optional[int] = None, profile: Optional[Dict[str, Any]] = None):
    async with get_db_session() as session:
        # Check if exists
        from sqlalchemy import select
        result = await session.execute(select(Run).where(Run.id == uuid.UUID(run_id)))
        run = result.scalar_one_or_none()
        
        if run:
            run.status = status
            if profile:
                run.profile = profile
        else:
            run = Run(
                id=uuid.UUID(run_id),
                repo=repo,
                pr_number=pr_number,
                surface=surface,
                status=status,
                profile=profile or {}
            )
            session.add(run)

async def write_audit_log(run_id: str, event_type: str, payload: Dict[str, Any]):
    async with get_db_session() as session:
        log_entry = AuditLog(
            run_id=uuid.UUID(run_id),
            event_type=event_type,
            payload=payload
        )
        session.add(log_entry)
