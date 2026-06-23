from typing import Dict, Any, List, Optional
from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from src.agent.state import AgentState, Proposal, TestResult, ProjectProfile
from src.agent.risk_engine import RiskEngine
from src.common.config import settings
from src.agent.ingestion import IngestionService
from src.agent.analysis import StaticAnalysisService, LLMAnalysisService
from src.execution_plane.sandbox import SandboxRuntime
from src.agent.git_actions import GitActionsService

# --- Stub Tool / Node Functions for Sprint 2 ---

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

def apply_in_sandbox_and_test(state: AgentState) -> Dict[str, Any]:
    """Applies patches and runs tests securely."""
    repo = state["job"].repo
    profile = state["profile"]
    proposals = state.get("proposals", [])
    
    sandbox = SandboxRuntime(repo)
    try:
        sandbox.setup()
        applied = sandbox.apply_proposals(proposals)
        
        # If applied successfully, run tests
        if applied:
            test_result = sandbox.run_tests(profile)
        else:
            test_result = TestResult(
                passed=False,
                coverage_percent=0.0,
                output="Failed to apply one or more patches to the repository."
            )
            
        return {"test_results": [test_result]}
    finally:
        sandbox.teardown()

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
    
    assessments = []
    for prop in proposals:
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

def aggregate_and_decide(state: AgentState) -> Dict[str, Any]:
    """Decides to auto-commit or escalate based on risk assessments."""
    assessments = state.get("risk_assessments", [])
    proposals = state.get("proposals", [])
    job = state["job"]
    
    # Check if any assessment requires escalation
    should_escalate = any(a.decision == "escalate" for a in assessments)
    
    # Determine the repo name to use for comments
    repo_name_for_api = getattr(job, "repo_full_name", None) or job.repo

    if should_escalate:
        # Post comment on PR with the findings and proposals
        comment_lines = [
            "## 🤖 Autonomous Code Review Escalation",
            "",
            "I found issues but the risk of auto-committing is too high. Please review my proposals:",
            ""
        ]
        
        for idx, p in enumerate(proposals):
            comment_lines.append(f"### {p.finding_id}")
            comment_lines.append(f"{p.description}")
            comment_lines.append("")
            
            # Format diff nicely, handling potential markdown code blocks from the LLM
            diff_text = p.diff.strip()
            if diff_text.startswith("```"):
                comment_lines.append(diff_text)
            else:
                comment_lines.append("```diff")
                comment_lines.append(diff_text)
                comment_lines.append("```")
                
            comment_lines.append("")
            comment_lines.append("---")
            comment_lines.append("")
            
        comment = "\n".join(comment_lines)
        
        if job.pr_number and repo_name_for_api and not str(repo_name_for_api).startswith("C:"):
            GitActionsService.post_pr_comment(repo_name_for_api, job.pr_number, comment, job.installation_id)
        return {"status": "escalated"}
    else:
        # All safe to auto-commit
        if job.branch:
            # We assume the sandbox has successfully verified, but we need to commit
            # directly to the local clone, since sandbox is ephemeral.
            # In production, we'd apply the patches to the repo local clone, then push.
            sandbox = SandboxRuntime(job.repo)
            sandbox.setup()
            try:
                sandbox.apply_proposals(proposals)
                GitActionsService.commit_and_push(sandbox.sandbox_dir, proposals, job.branch)
            finally:
                sandbox.teardown()
            
            # Post success comment
            comment = "## 🤖 Autonomous Code Review Success\n\nI have successfully applied and tested fixes for the discovered issues. The commits have been pushed to your branch."
            if job.pr_number and repo_name_for_api and not str(repo_name_for_api).startswith("C:"):
                GitActionsService.post_pr_comment(repo_name_for_api, job.pr_number, comment, job.installation_id)
                
        return {"status": "auto_committed"}

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
