"""
The review workflow.

Topology (plan-and-execute, not a single linear pass):

    ingest_and_detect → static_analysis → plan → draft_fixes
        → Send(one branch per proposal) → verify_proposal ⟲ repair
        → collect → [replan ⟲ draft_fixes]
        → risk_score → aggregate_and_decide

`verify_proposal` is the compiled per-proposal sandbox subgraph from
`proposal_graph`, fanned out with `Send` so each proposal is applied and tested
on its own tree with its own retry budget.
"""
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Tuple

from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langgraph.types import Send
from pydantic import BaseModel

from src.agent.analysis import LLMAnalysisService, StaticAnalysisService
from src.agent.git_actions import GitActionsService
from src.agent.ingestion import IngestionService
from src.agent.proposal_graph import build_proposal_graph
from src.agent.risk_engine import RiskEngine
from src.agent.state import (
    AgentState,
    Finding,
    PlanItem,
    ProjectProfile,
    Proposal,
    RiskAssessment,
    TestResult,
)
from src.common.config import settings
from src.common.logging import get_logger, instrument_node
from src.execution_plane.sandbox import SandboxRuntime, apply_patches

logger = get_logger("agent")

SEVERITY_RANK = {"critical": 0, "error": 1, "warning": 2, "info": 3}


class FixPlan(BaseModel):
    """Planner output: which findings to fix, in what order, and how."""
    items: List[PlanItem]


# --- Nodes ------------------------------------------------------------------

def ingest_and_detect(state: AgentState) -> Dict[str, Any]:
    """Detects framework and test command, and resolves the changed files."""
    job = state["job"]
    repo = job.repo
    profile = IngestionService.detect_project_profile(repo)

    if job.evaluate_entire_codebase:
        diff_files = IngestionService.get_all_tracked_files(repo)
    else:
        diff_text = job.raw_diff or IngestionService.get_local_diff(repo)
        # Extremely basic diff parsing to get modified files
        diff_files = [line.split(" b/")[1] for line in diff_text.splitlines() if line.startswith("diff --git a/")]

    logger.info("ingest.done", extra={"language": profile.primary_language,
                                      "test_command": profile.test_command,
                                      "diff_files": len(diff_files)})
    return {"profile": profile, "diff_files": diff_files, "status": "running"}


def static_analysis(state: AgentState) -> Dict[str, Any]:
    """Runs the static analysers in parallel, then the LLM semantic pass."""
    repo = state["job"].repo
    diff_files = state.get("diff_files", [])

    static_service = StaticAnalysisService(repo_path=repo, diff_files=diff_files)

    # ruff / bandit / mypy are independent subprocesses: fan out, fan in.
    findings: List[Finding] = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(static_service.run_ruff, diff_files),
            pool.submit(static_service.run_bandit, diff_files),
            pool.submit(static_service.run_mypy, diff_files),
        ]
        for future in futures:
            try:
                findings.extend(future.result())
            except Exception:
                logger.exception("static_analysis.tool_failed")

    semantic_service = LLMAnalysisService(
        repo_path=repo,
        diff_files=diff_files,
        token_limit=state["job"].token_limit,
    )
    findings.extend(semantic_service.run_semantic_analysis())

    logger.info("static_analysis.done", extra={"findings": len(findings),
                                               "tokens_used": semantic_service.tokens_used})
    return {"findings": findings, "tokens_used": semantic_service.tokens_used}


def plan(state: AgentState) -> Dict[str, Any]:
    """
    Chooses which findings are worth fixing and how, before any diff exists.
    This is the 'plan' half of plan-and-execute: it runs once up front and is
    revisited by `replan` when verification fails.
    """
    findings = state.get("findings", [])
    if not findings:
        return {"plan": [], "pending_finding_ids": []}

    ranked = sorted(findings, key=lambda f: SEVERITY_RANK.get(f.severity, 9))[:20]

    api_key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
    if not api_key:
        logger.error("plan.no_api_key")
        return {"status": "failed", "plan": [], "pending_finding_ids": []}

    llm = ChatOpenAI(
        api_key=api_key,  # type: ignore
        model=settings.openai_model,
        temperature=0.0,
    ).with_structured_output(FixPlan)

    prompt = ChatPromptTemplate.from_messages([
        ("system",
         "You are the planner for an autonomous code reviewer. Given static and semantic "
         "findings, decide which are worth fixing automatically and describe the approach "
         "for each in one sentence. Skip findings that are stylistic noise, ambiguous, or "
         "that require product decisions - set skip=true with a short skip_reason. "
         "Order by priority, 1 = highest. Use the exact finding ids given."),
        ("human", "Project profile: {profile}\n\nFindings:\n{findings}"),
    ])

    try:
        result = (prompt | llm).invoke({
            "profile": state["profile"],
            "findings": "\n".join(
                f"- id={f.id} severity={f.severity} tool={f.tool} file={f.file} :: {f.description}"
                for f in ranked
            ),
        })
        items = [item for item in result.items if any(item.finding_id == f.id for f in ranked)]
    except Exception:
        logger.exception("plan.llm_failed")
        # Fall back to a deterministic plan so a planner outage does not
        # silently drop the whole review.
        items = [PlanItem(finding_id=f.id, approach="Fix as reported.", priority=SEVERITY_RANK.get(f.severity, 9))
                 for f in ranked]

    actionable = [item for item in sorted(items, key=lambda i: i.priority) if not item.skip]
    logger.info("plan.done", extra={"planned": len(actionable), "skipped": len(items) - len(actionable)})

    return {"plan": items, "pending_finding_ids": [item.finding_id for item in actionable]}


def draft_fixes(state: AgentState) -> Dict[str, Any]:
    """Drafts a diff for every plan item scheduled this round."""
    api_key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
    if not api_key:
        return {"status": "failed"}

    pending = set(state.get("pending_finding_ids") or [])
    plan_items = {item.finding_id: item for item in state.get("plan", [])}
    findings = {f.id: f for f in state.get("findings", [])}

    llm = ChatOpenAI(
        api_key=api_key,  # type: ignore
        model=settings.openai_model,
        temperature=0.0,
    ).with_structured_output(Proposal)

    prompt = ChatPromptTemplate.from_messages([
        ("system",
         "You are an expert autonomous software engineer. Draft a patch for the given finding, "
         "following the planned approach. Return the patch as a unified diff (--- / +++ / @@ hunks) "
         "with correct paths relative to the repository root. No prose, no markdown fences. "
         "Set confidence honestly between 0 and 1."),
        ("human",
         "Project profile: {profile}\n\nFinding: {finding}\n\nPlanned approach: {approach}\n\n"
         "Current content of {file}:\n{content}"),
    ])

    proposals: List[Proposal] = []
    for finding_id in pending:
        finding = findings.get(finding_id)
        if not finding:
            continue
        item = plan_items.get(finding_id)
        try:
            proposal = (prompt | llm).invoke({
                "profile": state["profile"],
                "finding": finding,
                "approach": item.approach if item else "Fix as reported.",
                "file": finding.file,
                "content": IngestionService.read_file(state["job"].repo, finding.file),
            })
            proposal.finding_id = finding_id
            proposals.append(proposal)
        except Exception:
            logger.exception("draft_fixes.failed", extra={"finding_id": finding_id})

    drafted = [p.finding_id for p in proposals]
    logger.info("draft_fixes.done", extra={"drafted": len(drafted), "requested": len(pending)})
    return {"proposals": proposals, "pending_finding_ids": drafted, "status": "running"}


def fan_out_proposals(state: AgentState):
    """Sends each pending proposal into its own sandbox subgraph branch."""
    pending = set(state.get("pending_finding_ids") or [])
    findings = {f.id: f for f in state.get("findings", [])}
    proposals = [p for p in state.get("proposals", []) if p.finding_id in pending]

    if not proposals:
        return "collect"

    return [
        Send("verify_proposal", {
            "job": state["job"],
            "profile": state["profile"],
            "proposal": proposal,
            "finding": findings.get(proposal.finding_id),
            "attempt": 0,
        })
        for proposal in proposals
    ]


def latest_result_per_proposal(state: AgentState) -> Dict[str, TestResult]:
    """Last verification outcome for each proposal, across all repair attempts."""
    latest: Dict[str, TestResult] = {}
    for result in state.get("test_results", []) or []:
        if result.proposal_id:
            current = latest.get(result.proposal_id)
            if current is None or result.attempt >= current.attempt:
                latest[result.proposal_id] = result
    return latest


def collect(state: AgentState) -> Dict[str, Any]:
    """Fan-in point: records which proposals are still failing after repairs."""
    latest = latest_result_per_proposal(state)
    unapplied = state.get("unapplied", {}) or {}

    failing = [pid for pid, result in latest.items() if not result.passed]
    failing.extend(fid for fid in unapplied if fid not in failing)

    logger.info("collect.done", extra={"verified": len(latest), "failing": len(failing)})
    return {"pending_finding_ids": failing}


def route_after_collect(state: AgentState) -> str:
    """Revises the plan once if anything is still failing, then moves on."""
    pending = state.get("pending_finding_ids") or []
    if pending and state.get("replan_rounds", 0) < settings.max_replan_rounds:
        return "replan"
    return "risk_score"


def replan(state: AgentState) -> Dict[str, Any]:
    """
    Revises the approach for proposals that exhausted their repair budget.
    Failure output is fed back so the second attempt is not a blind retry.
    """
    pending = state.get("pending_finding_ids") or []
    latest = latest_result_per_proposal(state)
    plan_items = {item.finding_id: item for item in state.get("plan", [])}

    api_key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
    if not api_key:
        return {"replan_rounds": state.get("replan_rounds", 0) + 1}

    llm = ChatOpenAI(
        api_key=api_key,  # type: ignore
        model=settings.openai_model,
        temperature=0.0,
    ).with_structured_output(FixPlan)

    prompt = ChatPromptTemplate.from_messages([
        ("system",
         "You are replanning fixes that failed verification. For each finding, either propose a "
         "materially different approach, or set skip=true with a skip_reason if it should be left "
         "to a human. Do not repeat the approach that already failed."),
        ("human", "Failures:\n{failures}"),
    ])

    failures = "\n".join(
        f"- id={fid} approach={plan_items[fid].approach if fid in plan_items else 'n/a'} "
        f"error={(latest[fid].output if fid in latest else 'patch did not apply')[:600]}"
        for fid in pending
    )

    try:
        result = (prompt | llm).invoke({"failures": failures})
        revised = {item.finding_id: item for item in result.items}
    except Exception:
        logger.exception("replan.llm_failed")
        revised = {}

    new_plan = [revised.get(item.finding_id, item) for item in state.get("plan", [])]
    retry_ids = [fid for fid in pending if not revised.get(fid, plan_items.get(fid, PlanItem(finding_id=fid, approach=""))).skip]

    logger.info("replan.done", extra={"retrying": len(retry_ids), "dropped": len(pending) - len(retry_ids)})
    return {
        "plan": new_plan,
        "pending_finding_ids": retry_ids,
        "replan_rounds": state.get("replan_rounds", 0) + 1,
    }


def risk_score(state: AgentState) -> Dict[str, Any]:
    """Scores each proposal against its own verification result."""
    engine = RiskEngine(auto_commit_threshold=settings.auto_commit_threshold)
    proposals = state.get("proposals", [])
    findings = state.get("findings", [])
    diff_files = state.get("diff_files", [])
    unapplied = state.get("unapplied", {}) or {}
    latest = latest_result_per_proposal(state)

    blast_radius = min(1.0, len(diff_files) / 10.0) if diff_files else 0.1

    assessments = []
    for prop in proposals:
        # A patch that would not even apply can never be auto-committed.
        if prop.finding_id in unapplied:
            assessments.append(RiskAssessment(
                proposal_id=prop.finding_id,
                score=1.0,
                signals={"patch_applied": 0.0},
                decision="escalate",
                reasons=[f"Patch could not be applied: {unapplied[prop.finding_id]}"],
            ))
            continue

        test_result = latest.get(prop.finding_id)
        if test_result is None:
            assessments.append(RiskAssessment(
                proposal_id=prop.finding_id,
                score=1.0,
                signals={"verified": 0.0},
                decision="escalate",
                reasons=["Proposal was never verified in a sandbox."],
            ))
            continue

        finding = next((f for f in findings if f.id == prop.finding_id), None)
        file_criticality = 0.5
        semantic_risk = 0.0
        static_severity = 0.0

        if finding:
            file_name = finding.file.lower()
            if "setup.py" in file_name or "main.py" in file_name or ".env" in file_name:
                file_criticality = 0.9
            elif "test" in file_name:
                file_criticality = 0.1

            if finding.severity == "critical":
                static_severity = 1.0
                semantic_risk = 1.0
            elif finding.severity == "error":
                static_severity = 0.8
                semantic_risk = 0.8
            elif finding.severity == "warning":
                static_severity = 0.4
                semantic_risk = 0.4

        assessment = engine.calculate_risk(
            proposal=prop,
            test_result=test_result,
            file_criticality_score=file_criticality,
            blast_radius_normalized=blast_radius,
            static_analysis_severity_normalized=static_severity,
            historical_revert_rate=0.0,
            semantic_risk_flag=semantic_risk,
        )
        # Every repair attempt spent is evidence the fix was not obvious.
        attempts = (state.get("retries_used", {}) or {}).get(prop.finding_id, 0)
        if attempts and assessment.decision == "auto_commit":
            assessment.reasons.append(f"Needed {attempts} repair attempt(s) before passing.")
        assessments.append(assessment)

    auto = sum(1 for a in assessments if a.decision == "auto_commit")
    logger.info("risk_score.done", extra={"auto_commit": auto, "escalate": len(assessments) - auto})
    return {"risk_assessments": assessments}


def verify_proposals(repo: str, profile: ProjectProfile, proposals: List[Proposal]) -> Tuple[TestResult, Dict[str, str]]:
    """
    Applies a set of proposals together to a throwaway copy of `repo` and runs
    the test suite. Used to re-verify the exact batch that is about to be
    pushed, which is not the same tree any single proposal was tested on.
    """
    sandbox = SandboxRuntime(repo)
    try:
        sandbox.setup()
        apply_result = sandbox.apply_proposals(proposals)

        if not apply_result.applied:
            return TestResult(
                passed=False,
                coverage_percent=0.0,
                output="No proposal produced an applicable patch."
            ), apply_result.failed

        test_result = sandbox.run_tests(profile)
        return test_result, apply_result.failed
    finally:
        sandbox.teardown()


def _format_proposal_section(proposals: List[Proposal], reasons: Dict[str, List[str]]) -> List[str]:
    """Renders proposals as markdown for a PR comment."""
    lines: List[str] = []
    for p in proposals:
        lines.append(f"### {p.finding_id}")
        lines.append(p.description)
        why = reasons.get(p.finding_id) or []
        if why:
            lines.append("")
            lines.append(f"_Why not auto-committed: {' '.join(why)}_")
        lines.append("")

        diff_text = p.diff.strip()
        if diff_text.startswith("```"):
            lines.append(diff_text)
        else:
            lines.append("```diff")
            lines.append(diff_text)
            lines.append("```")

        lines.extend(["", "---", ""])
    return lines


def aggregate_and_decide(state: AgentState) -> Dict[str, Any]:
    """
    Splits proposals into an auto-commit batch and an escalation batch,
    re-verifies the auto-commit batch on its own, then pushes it to the PR
    branch. Anything that fails at any point falls back to escalation.
    """
    assessments = state.get("risk_assessments", [])
    proposals = state.get("proposals", [])
    job = state["job"]

    by_id = {a.proposal_id: a for a in assessments}
    reasons = {a.proposal_id: list(a.reasons) for a in assessments}

    auto = [p for p in proposals if by_id.get(p.finding_id) and by_id[p.finding_id].decision == "auto_commit"]
    escalate = [p for p in proposals if p not in auto]

    committed: List[Proposal] = []

    if auto and not (job.workspace_is_clone and job.branch):
        # No agent-owned clone (local CLI run): suggest only, never write.
        for p in auto:
            reasons.setdefault(p.finding_id, []).append("No writable PR workspace; suggesting instead of committing.")
        escalate.extend(auto)
        auto = []

    if auto:
        # Each proposal was tested alone. The batch about to be pushed is a
        # different tree, so verify that exact combination before pushing.
        test_result, unapplied = verify_proposals(job.repo, state["profile"], auto)

        if unapplied:
            for finding_id, why in unapplied.items():
                reasons.setdefault(finding_id, []).append(f"Patch failed to apply on re-verification: {why}")
            escalate.extend([p for p in auto if p.finding_id in unapplied])
            auto = [p for p in auto if p.finding_id not in unapplied]

        if auto and not test_result.passed:
            for p in auto:
                reasons.setdefault(p.finding_id, []).append("Test suite failed when this batch was applied together.")
            escalate.extend(auto)
            auto = []

        if auto:
            apply_result = apply_patches(job.repo, auto)
            for finding_id, why in apply_result.failed.items():
                reasons.setdefault(finding_id, []).append(f"Patch failed to apply to the PR clone: {why}")
            escalate.extend([p for p in auto if p.finding_id in apply_result.failed])
            landed = [p for p in auto if p.finding_id in apply_result.applied]

            if landed and GitActionsService.commit_and_push(job.repo, landed, job.branch):
                committed = landed
            else:
                for p in landed:
                    reasons.setdefault(p.finding_id, []).append("Commit or push to the PR branch failed.")
                escalate.extend(landed)

    # --- Report back on the PR -------------------------------------------
    comment_lines: List[str] = []

    if committed:
        comment_lines.extend([
            "## 🤖 Autonomous Code Review — fixes pushed",
            "",
            f"I applied {len(committed)} low-risk fix(es), verified them against your test suite, "
            "and pushed them to this branch.",
            ""
        ])
        for p in committed:
            comment_lines.append(f"- **{p.finding_id}**: {p.description}")
        comment_lines.extend(["", "---", ""])

    if escalate:
        comment_lines.extend([
            "## 🤖 Autonomous Code Review — needs a human",
            "",
            "These changes were too risky for me to commit. Proposals below:",
            ""
        ])
        comment_lines.extend(_format_proposal_section(escalate, reasons))

    repo_name_for_api = job.repo_full_name
    if comment_lines and job.pr_number and repo_name_for_api:
        GitActionsService.post_pr_comment(
            repo_name_for_api, job.pr_number, "\n".join(comment_lines), job.installation_id
        )

    if committed and escalate:
        status = "partially_committed"
    elif committed:
        status = "auto_committed"
    elif escalate:
        status = "escalated"
    else:
        status = "no_action"

    logger.info("decide.done", extra={"status": status,
                                      "committed": len(committed),
                                      "escalated": len(escalate)})
    return {"status": status}


# --- Graph Construction -----------------------------------------------------

def create_agent_graph() -> StateGraph:
    workflow = StateGraph(AgentState)

    workflow.add_node("ingest_and_detect", instrument_node("ingest_and_detect", ingest_and_detect))
    workflow.add_node("static_analysis", instrument_node("static_analysis", static_analysis))
    workflow.add_node("plan", instrument_node("plan", plan))
    workflow.add_node("draft_fixes", instrument_node("draft_fixes", draft_fixes))
    # The per-proposal sandbox subgraph, fanned out one branch per proposal.
    workflow.add_node("verify_proposal", build_proposal_graph())
    workflow.add_node("collect", instrument_node("collect", collect))
    workflow.add_node("replan", instrument_node("replan", replan))
    workflow.add_node("risk_score", instrument_node("risk_score", risk_score))
    workflow.add_node("aggregate_and_decide", instrument_node("aggregate_and_decide", aggregate_and_decide))

    workflow.set_entry_point("ingest_and_detect")
    workflow.add_edge("ingest_and_detect", "static_analysis")
    workflow.add_edge("static_analysis", "plan")
    workflow.add_edge("plan", "draft_fixes")
    workflow.add_conditional_edges("draft_fixes", fan_out_proposals, ["verify_proposal", "collect"])
    workflow.add_edge("verify_proposal", "collect")
    workflow.add_conditional_edges("collect", route_after_collect, ["replan", "risk_score"])
    workflow.add_edge("replan", "draft_fixes")
    workflow.add_edge("risk_score", "aggregate_and_decide")
    workflow.add_edge("aggregate_and_decide", END)

    return workflow

def get_compiled_graph(checkpointer=None):
    """
    Returns the compiled LangGraph agent, optionally with a Postgres checkpointer.
    """
    workflow = create_agent_graph()
    if checkpointer:
        return workflow.compile(checkpointer=checkpointer)
    return workflow.compile()
