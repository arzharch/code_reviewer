from sqlalchemy import Column, String, Float, Boolean, Integer, ForeignKey, DateTime, JSON, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
import uuid
from .database import Base

class Installation(Base):
    """
    Represents a GitHub App installation for an organization or user.
    """
    __tablename__ = "installations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    github_install_id = Column(Integer, unique=True, nullable=False)
    org_or_user = Column(String, nullable=False)
    autonomy_mode = Column(String, nullable=False, default="shadow")  # shadow | suggest_only | gated_auto | full_auto
    auto_commit_threshold = Column(Float, nullable=False, default=0.15)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

class Run(Base):
    """
    Represents an invocation of the agent for a PR or local diff.
    """
    __tablename__ = "runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    installation_id = Column(UUID(as_uuid=True), ForeignKey("installations.id"), nullable=True)
    repo = Column(String, nullable=False)
    pr_number = Column(Integer, nullable=True) # Null for CLI
    surface = Column(String, nullable=False)   # 'github_app' | 'cli'
    status = Column(String, nullable=False)    # running | awaiting_human | completed | failed
    profile = Column(JSON, nullable=False)     # ProjectProfile snapshot
    started_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)

class Finding(Base):
    """
    Static analysis or AST-based issues found in the code.
    """
    __tablename__ = "findings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id = Column(UUID(as_uuid=True), ForeignKey("runs.id"), nullable=False)
    tool = Column(String, nullable=False)
    file = Column(String, nullable=False)
    severity = Column(String, nullable=False)
    category = Column(String, nullable=False)
    description = Column(Text, nullable=False)

class Proposal(Base):
    """
    A proposed diff to fix a finding.
    """
    __tablename__ = "proposals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    finding_id = Column(UUID(as_uuid=True), ForeignKey("findings.id"), nullable=False)
    diff = Column(Text, nullable=False)
    rationale = Column(Text, nullable=False)
    model_confidence = Column(Float, nullable=True)
    test_passed = Column(Boolean, nullable=True)
    test_output_excerpt = Column(Text, nullable=True)
    risk_score = Column(Float, nullable=True)
    risk_signals = Column(JSON, nullable=True)
    decision = Column(String, nullable=True) # auto_commit | escalate
    committed_sha = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

class AuditLog(Base):
    """
    Compliance-grade audit log for all agent actions.
    """
    __tablename__ = "audit_log"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id = Column(UUID(as_uuid=True), ForeignKey("runs.id"), nullable=False)
    event_type = Column(String, nullable=False) # state_transition | tool_call | decision | commit
    payload = Column(JSON, nullable=False)
    occurred_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
