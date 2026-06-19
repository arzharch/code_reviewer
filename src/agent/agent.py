from typing import Dict, Any, List
from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from src.agent.state import AgentState, ProjectProfile, Proposal
from src.agent.risk_engine import RiskEngine
from src.common.config import settings

# --- Stub Tool / Node Functions for Sprint 2 ---

def ingest_and_detect(state: AgentState) -> Dict[str, Any]:
    """Detects framework and tests commands."""
    return {"status": "running"}

def static_analysis(state: AgentState) -> Dict[str, Any]:
    """Runs local static analysis tools (Ruff, MyPy, Bandit) and populates findings."""
    return {"status": "running"}

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
    return {"status": "running"}

def risk_score(state: AgentState) -> Dict[str, Any]:
    """Evaluates the risk of the proposals based on test results and metrics."""
    engine = RiskEngine(auto_commit_threshold=0.15)
    # TODO: loop over proposals and test results to calculate RiskAssessments
    return {"status": "running"}

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
