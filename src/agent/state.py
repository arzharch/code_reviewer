import operator
import uuid
from typing import Annotated, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field
from typing_extensions import TypedDict


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

class PlanItem(BaseModel):
    """One unit of work chosen by the planner before any diff is drafted."""
    finding_id: str
    approach: str
    priority: int = 5
    skip: bool = False
    skip_reason: Optional[str] = None

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
    attempt: int = 0

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


# --- Reducers ---------------------------------------------------------------
# Proposals are verified in parallel, one branch per proposal, so every channel
# those branches write to needs an explicit merge strategy.

def upsert_proposals(left: Optional[List[Proposal]], right: Optional[List[Proposal]]) -> List[Proposal]:
    """Last write per finding_id wins, so a repaired diff replaces its original."""
    merged: Dict[str, Proposal] = {p.finding_id: p for p in (left or [])}
    for proposal in right or []:
        merged[proposal.finding_id] = proposal
    return list(merged.values())


def merge_counters(left: Optional[Dict[str, int]], right: Optional[Dict[str, int]]) -> Dict[str, int]:
    return {**(left or {}), **(right or {})}


def merge_failures(left: Optional[Dict[str, str]], right: Optional[Dict[str, str]]) -> Dict[str, str]:
    """
    Merges patch-failure reasons. An empty reason clears the entry, which is how
    a successful repair retracts an earlier failure.
    """
    merged = {**(left or {})}
    for finding_id, reason in (right or {}).items():
        if reason:
            merged[finding_id] = reason
        else:
            merged.pop(finding_id, None)
    return merged


def replace(left, right):
    """Plain last-write-wins for channels written by a single branch."""
    return right if right is not None else left


class ProposalOutput(TypedDict, total=False):
    """The only channels the per-proposal subgraph writes back to the parent."""
    proposals: Annotated[List[Proposal], upsert_proposals]
    test_results: Annotated[List[TestResult], operator.add]
    unapplied: Annotated[Dict[str, str], merge_failures]
    retries_used: Annotated[Dict[str, int], merge_counters]


class ProposalState(TypedDict, total=False):
    """State of one proposal being applied, tested and repaired in isolation."""
    job: ReviewJob
    profile: ProjectProfile
    proposal: Proposal
    finding: Optional[Finding]
    attempt: int
    last_error: str
    proposals: Annotated[List[Proposal], upsert_proposals]
    test_results: Annotated[List[TestResult], operator.add]
    unapplied: Annotated[Dict[str, str], merge_failures]
    retries_used: Annotated[Dict[str, int], merge_counters]


class AgentState(TypedDict, total=False):
    job: ReviewJob
    profile: ProjectProfile
    diff_files: List[str]
    findings: List[Finding]
    plan: List[PlanItem]
    # finding_ids drafted and verified in the current round
    pending_finding_ids: Annotated[List[str], replace]
    proposals: Annotated[List[Proposal], upsert_proposals]
    test_results: Annotated[List[TestResult], operator.add]
    unapplied: Annotated[Dict[str, str], merge_failures]
    retries_used: Annotated[Dict[str, int], merge_counters]
    risk_assessments: List[RiskAssessment]
    replan_rounds: int
    tokens_used: int
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
