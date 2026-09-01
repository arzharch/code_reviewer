"""
Per-proposal sandbox subgraph.

Each proposal is applied and tested on its own throwaway tree, so one bad patch
cannot mask or poison another. When the tests fail the proposal gets a bounded
number of repair attempts: the failure output is fed back to the model, which
redrafts the diff, and the cycle repeats until it passes or the budget runs out.

    apply_and_test ──passed──────────────► END
          ▲                │
          │            failed & budget left
          └──── repair ◄───┘
"""
from typing import Any, Dict

from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from src.agent.state import Proposal, ProposalOutput, ProposalState, TestResult
from src.common.config import settings
from src.common.logging import get_logger, instrument_node
from src.execution_plane.sandbox import SandboxRuntime

logger = get_logger("agent.proposal")

REPAIR_PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     "You are an expert software engineer repairing your own failed patch. "
     "You previously proposed a diff; applying it or running the test suite failed. "
     "Study the error and produce a corrected unified diff. "
     "Return ONLY a valid unified diff (--- / +++ / @@ hunks) against the current "
     "file contents. Do not include prose or markdown fences."),
    ("human",
     "Project profile: {profile}\n\n"
     "Original finding id: {finding_id}\n"
     "Previous diff:\n{diff}\n\n"
     "Failure output (truncated):\n{error}\n\n"
     "Attempt {attempt} of {max_attempts}."),
])


def apply_and_test(state: ProposalState) -> Dict[str, Any]:
    """Applies this one proposal to a fresh sandbox and runs the test suite."""
    job = state["job"]
    proposal = state["proposal"]
    attempt = state.get("attempt", 0)

    sandbox = SandboxRuntime(job.repo)
    try:
        sandbox.setup()
        apply_result = sandbox.apply_proposals([proposal])

        if proposal.finding_id in apply_result.failed:
            reason = apply_result.failed[proposal.finding_id]
            logger.warning("proposal.apply_failed",
                           extra={"finding_id": proposal.finding_id, "attempt": attempt, "reason": reason})
            return {
                "test_results": [TestResult(
                    proposal_id=proposal.finding_id,
                    passed=False,
                    output=f"Patch did not apply: {reason}",
                    attempt=attempt,
                )],
                "unapplied": {proposal.finding_id: reason},
                "last_error": reason,
            }

        result = sandbox.run_tests(state["profile"])
        result.proposal_id = proposal.finding_id
        result.attempt = attempt

        logger.info("proposal.tested",
                    extra={"finding_id": proposal.finding_id, "attempt": attempt, "passed": result.passed})

        return {
            "test_results": [result],
            # Applying succeeded, so retract any earlier apply failure.
            "unapplied": {proposal.finding_id: ""},
            "last_error": "" if result.passed else result.output[-4000:],
        }
    finally:
        sandbox.teardown()


def repair(state: ProposalState) -> Dict[str, Any]:
    """Feeds the failure back to the model and redrafts the diff."""
    proposal = state["proposal"]
    attempt = state.get("attempt", 0) + 1

    api_key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
    if not api_key:
        return {"attempt": attempt}

    llm = ChatOpenAI(
        api_key=api_key,  # type: ignore
        model=settings.openai_model,
        temperature=0.0,
    ).with_structured_output(Proposal)

    try:
        revised = (REPAIR_PROMPT | llm).invoke({
            "profile": state["profile"],
            "finding_id": proposal.finding_id,
            "diff": proposal.diff,
            "error": (state.get("last_error") or "")[:4000],
            "attempt": attempt,
            "max_attempts": settings.max_repair_attempts,
        })
    except Exception as e:
        logger.warning("proposal.repair_failed",
                       extra={"finding_id": proposal.finding_id, "attempt": attempt, "reason": str(e)})
        return {"attempt": attempt}

    # Keep the original finding id so the proposal stays traceable.
    revised.finding_id = proposal.finding_id
    logger.info("proposal.repaired", extra={"finding_id": proposal.finding_id, "attempt": attempt})

    return {
        "proposal": revised,
        "proposals": [revised],
        "attempt": attempt,
        "retries_used": {proposal.finding_id: attempt},
    }


def should_repair(state: ProposalState) -> str:
    """Routes back into repair while the retry budget allows it."""
    results = state.get("test_results") or []
    if results and results[-1].passed:
        return END
    if state.get("attempt", 0) >= settings.max_repair_attempts:
        logger.info("proposal.budget_exhausted",
                    extra={"finding_id": state["proposal"].finding_id,
                           "attempts": state.get("attempt", 0)})
        return END
    return "repair"


def build_proposal_graph():
    """Compiles the apply → test → repair loop for a single proposal."""
    graph = StateGraph(ProposalState, output_schema=ProposalOutput)
    graph.add_node("apply_and_test", instrument_node("apply_and_test", apply_and_test))
    graph.add_node("repair", instrument_node("repair", repair))

    graph.add_edge(START, "apply_and_test")
    graph.add_conditional_edges("apply_and_test", should_repair, {"repair": "repair", END: END})
    graph.add_edge("repair", "apply_and_test")

    return graph.compile()
