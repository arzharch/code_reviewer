from typing import List, Dict, Literal, Tuple, Optional
from pydantic import BaseModel, Field
from typing_extensions import TypedDict
import uuid

class ProjectProfile(BaseModel):
    primary_language: str
    framework: Optional[str] = None
    package_manager: str
    test_command: str
    lint_command: Optional[str] = None
    build_command: Optional[str] = None
    monorepo: bool = False
    affected_packages: List[str] = []
    detection_confidence: float
    detection_sources: List[str]

class Finding(BaseModel):
    id: str
    tool: str
    file: str
    line_range: Tuple[int, int]
    severity: Literal["info", "warning", "error", "critical"]
    category: str
    description: str

class Proposal(BaseModel):
    finding_id: str
    diff: str
    description: str
    rationale: str
    confidence: float

class TestResult(BaseModel):
    __test__ = False  # not a pytest test class
    proposal_id: Optional[str] = None
    passed: bool
    output: str
    duration_s: float = 0.0
    coverage_percent: float = 0.0

class RiskAssessment(BaseModel):
    proposal_id: str
    score: float
    signals: Dict[str, float]
    decision: Literal["auto_commit", "escalate"]
    reasons: List[str]

class ReviewJob(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    repo: str
    repo_full_name: Optional[str] = None
    pr_number: Optional[int] = None
    commit_sha: Optional[str] = None
    branch: Optional[str] = None
    evaluate_entire_codebase: bool = False
    token_limit: Optional[int] = None
    installation_id: Optional[int] = None
    raw_diff: Optional[str] = None
    # True once `repo` points at an agent-owned clone with an authenticated
    # remote. Only then may the agent commit and push. Local CLI runs stay
    # False so the agent never writes to a developer's checkout.
    workspace_is_clone: bool = False

class AgentState(TypedDict):
    job: ReviewJob
    profile: ProjectProfile
    diff_files: List[str]
    findings: List[Finding]
    proposals: List[Proposal]
    test_results: List[TestResult]
    risk_assessments: List[RiskAssessment]
    retries_used: Dict[str, int]
    # finding_id -> why its patch could not be applied
    unapplied: Dict[str, str]
    status: Literal[
        "running",
        "awaiting_human",
        "completed",
        "failed",
        "escalated",
        "auto_committed",
        "partially_committed",
        "no_action",
    ]
