from typing import Dict, Any, List, Optional, Tuple
from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from src.agent.state import AgentState, Proposal, RiskAssessment, TestResult, ProjectProfile
from src.agent.risk_engine import RiskEngine
from src.common.config import settings
from src.agent.ingestion import IngestionService
from src.agent.analysis import StaticAnalysisService, LLMAnalysisService
from src.execution_plane.sandbox import SandboxRuntime, apply_patches
from src.agent.git_actions import GitActionsService

# --- Graph node functions ---

def ingest_and_detect(state: AgentState) -> Dict[str, Any]:
    """Detects framework and tests commands."""
    job = state["job"]
    repo = job.repo
    profile = IngestionService.detect_project_profile(repo)
    
    if job.evaluate_entire_codebase:
        diff_files = IngestionService.get_all_tracked_files(repo)
    else:
        if job.raw_diff:
            diff_text = job.raw_diff
        else:
            # For local CLI jobs we just use local diff
            diff_text = IngestionService.get_local_diff(repo)
        
        # Extremely basic diff parsing to get modified files
        diff_files = [line.split(" b/")[1] for line in diff_text.splitlines() if line.startswith("diff --git a/")]
        
    return {"profile": profile, "diff_files": diff_files}

def static_analysis(state: AgentState) -> Dict[str, Any]:
    """Runs local static analysis tools (Ruff, MyPy, Bandit) and populates findings."""
    repo = state["job"].repo
    diff_files = state.get("diff_files", [])
    
    static_service = StaticAnalysisService(repo_path=repo, diff_files=diff_files)
    findings = static_service.run_all()
    
    # Also run LLM semantic analysis
    semantic_service = LLMAnalysisService(
        repo_path=repo, 
        diff_files=diff_files, 
        token_limit=state["job"].token_limit
    )
    semantic_findings = semantic_service.run_semantic_analysis()
    findings.extend(semantic_findings)
    
    return {"findings": findings}



def plan_fixes(state: AgentState) -> Dict[str, Any]:
    """Uses LLM to draft Proposals for each Finding."""
    
    # Check if OpenAI API key is set
    api_key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
    if not api_key:
        return {"status": "failed"}

    llm = ChatOpenAI(
        api_key=api_key, # type: ignore
        model=settings.openai_model,
        temperature=0.0
    ).with_structured_output(Proposal)

    prompt = ChatPromptTemplate.from_messages([
        ("system", "You are an expert autonomous software engineer. Draft a code diff Proposal to fix the given Finding in the provided ProjectProfile. Be precise and confident."),
        ("human", "Project Profile: {profile}\\n\\nFinding to Fix: {finding}")
    ])

    proposals = []
    
    # Iterate over all findings to generate proposals
    for finding in state.get("findings", []):
        try:
            chain = prompt | llm
            # This generates a Proposal object
            proposal = chain.invoke({"profile": state["profile"], "finding": finding})
            proposals.append(proposal)
        except Exception as e:
            # Handle LLM failures gracefully (e.g., skip finding, add to retries, etc)
            print(f"Error generating proposal for finding {finding.id}: {e}")

    return {"proposals": proposals, "status": "running"}

def verify_proposals(repo: str, profile: ProjectProfile, proposals: List[Proposal]) -> Tuple[TestResult, Dict[str, str]]:
    """
    Applies a set of proposals to a throwaway copy of `repo` and runs the
    project's test suite against the patched tree.
    Returns the test result plus the proposals that would not apply.
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

def apply_in_sandbox_and_test(state: AgentState) -> Dict[str, Any]:
    """Applies patches to a disposable tree and runs the project's tests."""
    repo = state["job"].repo
    profile = state["profile"]
    proposals = state.get("proposals", [])

    test_result, unapplied = verify_proposals(repo, profile, proposals)
    return {"test_results": [test_result], "unapplied": unapplied}

def risk_score(state: AgentState) -> Dict[str, Any]:
    """Evaluates the risk of the proposals based on test results and metrics."""
    engine = RiskEngine(auto_commit_threshold=0.15)
    proposals = state.get("proposals", [])
    test_results = state.get("test_results", [])
    findings = state.get("findings", [])
    diff_files = state.get("diff_files", [])
    
    # We only have one aggregated test result for the whole branch right now
    combined_test_result = test_results[-1] if test_results else TestResult(passed=False, coverage_percent=0.0, output="No tests run")
    
    blast_radius = min(1.0, len(diff_files) / 10.0) if diff_files else 0.1
    
    unapplied = state.get("unapplied", {}) or {}

    assessments = []
    for prop in proposals:
        # A patch that would not even apply can never be auto-committed.
        if prop.finding_id in unapplied:
            assessments.append(RiskAssessment(
                proposal_id=prop.finding_id,
                score=1.0,
                signals={"patch_applied": 0.0},
                decision="escalate",
                reasons=[f"Patch could not be applied: {unapplied[prop.finding_id]}"]
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
            test_result=combined_test_result,
            file_criticality_score=file_criticality,
            blast_radius_normalized=blast_radius,
            static_analysis_severity_normalized=static_severity,
            historical_revert_rate=0.0,
            semantic_risk_flag=semantic_risk
        )
        assessments.append(assessment)
        
    return {"risk_assessments": assessments}

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
    reasons = {a.proposal_id: a.reasons for a in assessments}

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
        # The earlier test run covered every proposal at once. The batch we are
        # about to push is a subset, so verify that exact subset on its own.
        test_result, unapplied = verify_proposals(job.repo, state["profile"], auto)

        if unapplied:
            for finding_id, why in unapplied.items():
                reasons.setdefault(finding_id, []).append(f"Patch failed to apply on re-verification: {why}")
            escalate.extend([p for p in auto if p.finding_id in unapplied])
            auto = [p for p in auto if p.finding_id not in unapplied]

        if auto and not test_result.passed:
            for p in auto:
                reasons.setdefault(p.finding_id, []).append("Test suite failed when this batch was applied alone.")
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

    return {"status": status}

# --- Graph Construction ---

def create_agent_graph() -> StateGraph:
    workflow = StateGraph(AgentState)

    # Add nodes
    workflow.add_node("ingest_and_detect", ingest_and_detect)
    workflow.add_node("static_analysis", static_analysis)
    workflow.add_node("plan_fixes", plan_fixes)
    workflow.add_node("apply_in_sandbox_and_test", apply_in_sandbox_and_test)
    workflow.add_node("risk_score", risk_score)
    workflow.add_node("aggregate_and_decide", aggregate_and_decide)

    # Build edges (linear for now; will become parallel/conditional later)
    workflow.set_entry_point("ingest_and_detect")
    workflow.add_edge("ingest_and_detect", "static_analysis")
    workflow.add_edge("static_analysis", "plan_fixes")
    workflow.add_edge("plan_fixes", "apply_in_sandbox_and_test")
    workflow.add_edge("apply_in_sandbox_and_test", "risk_score")
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
