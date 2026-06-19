from typing import List, Dict, Literal, Tuple, Optional, Any
from pydantic import BaseModel
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
    rationale: str
    confidence: float

class TestResult(BaseModel):
    proposal_id: str
    passed: bool
    output_excerpt: str
    duration_s: float
    coverage_delta: Optional[float] = None

class RiskAssessment(BaseModel):
    proposal_id: str
    score: float
    signals: Dict[str, float]
    decision: Literal["auto_commit", "escalate"]
    reasons: List[str]

class ReviewJob(BaseModel):
    job_id: str = str(uuid.uuid4())
    repo: str
    pr_number: Optional[int] = None
    commit_sha: Optional[str] = None

class AgentState(TypedDict):
    job: ReviewJob
    profile: ProjectProfile
    findings: List[Finding]
    proposals: List[Proposal]
    test_results: List[TestResult]
    risk_assessments: List[RiskAssessment]
    retries_used: Dict[str, int]
    status: Literal["running", "awaiting_human", "completed", "failed"]
