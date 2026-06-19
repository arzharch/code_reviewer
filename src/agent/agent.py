from typing import Dict, Any, List
from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from src.agent.state import AgentState, ProjectProfile, Proposal, TestResult, RiskAssessment
from src.agent.risk_engine import RiskEngine
from src.common.config import settings
from src.agent.ingestion import IngestionService
from src.agent.analysis import StaticAnalysisService, LLMAnalysisService
from src.execution_plane.sandbox import SandboxRuntime

# --- Stub Tool / Node Functions for Sprint 2 ---

def ingest_and_detect(state: AgentState) -> Dict[str, Any]:
    """Detects framework and tests commands."""
    repo = state["job"].repo
    profile = IngestionService.detect_project_profile(repo)
    # For local CLI jobs we just use local diff
    diff_text = IngestionService.get_local_diff(repo)
    # Extremely basic diff parsing to get modified files
    diff_files = [line.split(" b/")[1] for line in diff_text.splitlines() if line.startswith("diff --git a/")]
    return {"profile": profile, "diff_files": diff_files}

def static_analysis(state: AgentState) -> Dict[str, Any]:
    """Runs local static analysis tools (Ruff, MyPy, Bandit) and populates findings."""
    repo = state["job"].repo
    diff_files = state.get("diff_files", [])
    findings = StaticAnalysisService.run_all(repo, diff_files)
    
    # Also run LLM semantic analysis
    semantic_findings = LLMAnalysisService.run_semantic_analysis(repo, diff_files)
    findings.extend(semantic_findings)
    
    return {"findings": findings}

def plan_fixes(state: AgentState) -> Dict[str, Any]:
    """Uses LLM to draft Proposals for each Finding."""
    
    # Check if OpenAI API key is set
    api_key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
    if not api_key:
        return {"status": "failed"}

    llm = ChatOpenAI(
        api_key=api_key,
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
    
    # We only have one aggregated test result for the whole branch right now
    combined_test_result = test_results[-1] if test_results else TestResult(passed=False, coverage_percent=0.0, output="No tests run")
    
    assessments = []
    for prop in proposals:
        # Mocking some metrics that would normally be fetched from Git history or AST
        assessment = engine.calculate_risk(
            proposal=prop,
            test_result=combined_test_result,
            file_criticality_score=0.1,  # e.g., setup.py is 1.0, src is 0.5, docs is 0.0
            blast_radius_normalized=0.2, # % of files modified or dependent
            static_analysis_severity_normalized=0.0,
            historical_revert_rate=0.05,
            semantic_risk_flag=0.0
        )
        assessments.append(assessment)
        
    return {"risk_assessments": assessments}

def aggregate_and_decide(state: AgentState) -> Dict[str, Any]:
    """Decides to auto-commit or escalate based on risk assessments."""
    return {"status": "completed"}

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
