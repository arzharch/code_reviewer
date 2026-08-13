import argparse
import asyncio
import os
import sys

from src.agent.agent import get_compiled_graph
from src.agent.checkpointer import get_checkpointer
from src.agent.state import ReviewJob
from src.common.config import settings
from src.common.logging import configure_logging, get_logger, job_context

logger = get_logger("cli")


def _initial_state(job: ReviewJob) -> dict:
    return {
        "job": job,
        "findings": [],
        "plan": [],
        "pending_finding_ids": [],
        "proposals": [],
        "test_results": [],
        "risk_assessments": [],
        "unapplied": {},
        "retries_used": {},
        "replan_rounds": 0,
        "tokens_used": 0,
        "status": "running",
    }


async def run_cli():
    parser = argparse.ArgumentParser(description="Autonomous Code Review CLI")
    parser.add_argument("--repo", default=".", help="Path to the repository to evaluate")
    parser.add_argument("--full", action="store_true", help="Evaluate the entire codebase instead of just diffs")
    parser.add_argument("--tokens", type=int, default=None, help="Token budget for LLM analysis")
    parser.add_argument("--resume", metavar="JOB_ID", default=None,
                        help="Resume a previous run from its Postgres checkpoint")
    parser.add_argument("--log-format", choices=["plain", "json"], default="plain")
    parser.add_argument("--log-level", default=settings.log_level)
    args = parser.parse_args()

    configure_logging(level=args.log_level, fmt=args.log_format)

    repo_path = os.path.abspath(args.repo)

    try:
        async with get_checkpointer() as checkpointer:
            graph = get_compiled_graph(checkpointer=checkpointer)

            if args.resume:
                config = {"configurable": {"thread_id": args.resume}}
                snapshot = await graph.aget_state(config)
                if not snapshot or not snapshot.created_at:
                    print(f"No checkpoint found for job {args.resume}")
                    sys.exit(1)
                if not snapshot.next:
                    print(f"Job {args.resume} already finished with status "
                          f"{(snapshot.values or {}).get('status')}")
                    return
                with job_context(args.resume):
                    logger.info("cli.resume", extra={"from_nodes": list(snapshot.next)})
                    final_state = await graph.ainvoke(None, config=config)
            else:
                job = ReviewJob(
                    repo=repo_path,
                    evaluate_entire_codebase=args.full,
                    token_limit=args.tokens,
                )
                with job_context(job.id):
                    logger.info("cli.start", extra={"repo": repo_path, "full": args.full,
                                                    "token_limit": args.tokens})
                    print(f"Job id: {job.id}  (resume with --resume {job.id})")
                    final_state = await graph.ainvoke(
                        _initial_state(job), config={"configurable": {"thread_id": job.id}}
                    )

            _print_report(final_state)

    except Exception as e:
        logger.exception("cli.failed")
        print(f"Error running agent workflow: {e}")
        sys.exit(1)


def _print_report(final_state: dict):
    print("\n=== Analysis Complete ===")
    print(f"Status: {final_state.get('status')}")

    findings = final_state.get("findings", []) or []
    if not findings:
        print("\nFindings: No issues found. The codebase looks great!")
    else:
        print(f"\nFindings ({len(findings)}):")
        for f in findings:
            if isinstance(f, dict):
                print(f" - [{f.get('severity', '').upper()}] {f.get('file')}:{f.get('line_range', [0])[0]} "
                      f"({f.get('tool')}): {f.get('description')}")
            else:
                print(f" - [{f.severity.upper()}] {f.file}:{f.line_range[0]} ({f.tool}): {f.description}")

    proposals = final_state.get("proposals", []) or []
    assessments = {a.proposal_id if not isinstance(a, dict) else a.get("proposal_id"): a
                   for a in (final_state.get("risk_assessments", []) or [])}
    retries = final_state.get("retries_used", {}) or {}

    print(f"\nProposals ({len(proposals)}):")
    for p in proposals:
        finding_id = p.get("finding_id") if isinstance(p, dict) else p.finding_id
        description = p.get("description") if isinstance(p, dict) else p.description
        assessment = assessments.get(finding_id)
        if assessment is not None:
            decision = assessment.get("decision") if isinstance(assessment, dict) else assessment.decision
            score = assessment.get("score") if isinstance(assessment, dict) else assessment.score
            verdict = f"{decision} (risk {score:.2f})"
        else:
            verdict = "not scored"
        repairs = retries.get(finding_id, 0)
        suffix = f", {repairs} repair attempt(s)" if repairs else ""
        print(f" - {finding_id}: {description} -> {verdict}{suffix}")


def main():
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_cli())


if __name__ == "__main__":
    main()
