import argparse
import asyncio
import sys
import os
from src.common.config import settings
from src.agent.state import ReviewJob
from src.agent.agent import get_compiled_graph
from src.agent.checkpointer import get_checkpointer

async def run_cli():
    parser = argparse.ArgumentParser(description="Autonomous Code Review CLI")
    parser.add_argument("--repo", default=".", help="Path to the repository to evaluate")
    parser.add_argument("--full", action="store_true", help="Evaluate the entire codebase instead of just diffs")
    parser.add_argument("--tokens", type=int, default=None, help="Token limit for LLM analysis")
    args = parser.parse_args()

    # Override to the cheapest model for testing as requested
    settings.openai_model = "gpt-4o-mini"
    
    # Ensure repo is absolute path
    repo_path = os.path.abspath(args.repo)

    job = ReviewJob(
        repo=repo_path,
        evaluate_entire_codebase=args.full,
        token_limit=args.tokens
    )

    print(f"Starting code review for {repo_path}")
    if args.full:
        print(f"Mode: Full Codebase Evaluation")
    if args.tokens:
        print(f"Token Limit: {args.tokens}")

    try:
        async with get_checkpointer() as checkpointer:
            graph = get_compiled_graph(checkpointer=checkpointer)
            config = {"configurable": {"thread_id": job.id}}
            initial_state = {
                "job": job,
                "findings": [],
                "proposals": [],
                "test_results": [],
                "risk_assessments": [],
                "unapplied": {}
            }
            
            print("Invoking agent workflow...")
            final_state = await graph.ainvoke(initial_state, config=config)
            
            print("\n=== Analysis Complete ===")
            print(f"Status: {final_state.get('status')}")
            
            findings = final_state.get('findings', [])
            if not findings:
                print("\nFindings: No issues found. The codebase looks great!")
            else:
                print(f"\nFindings ({len(findings)}):")
                for f in findings:
                    # Some findings might be dicts if not fully validated back to BaseModel in final state
                    if isinstance(f, dict):
                        print(f" - [{f.get('severity', '').upper()}] {f.get('file')}:{f.get('line_range', [0])[0]} ({f.get('tool')}): {f.get('description')}")
                    else:
                        print(f" - [{f.severity.upper()}] {f.file}:{f.line_range[0]} ({f.tool}): {f.description}")
            
            proposals = final_state.get('proposals', [])
            print(f"\nProposals ({len(proposals)}):")
            for p in proposals:
                if isinstance(p, dict):
                    print(f" - Fix for {p.get('finding_id')}: {p.get('description')}")
                else:
                    print(f" - Fix for {p.finding_id}: {p.description}")

    except Exception as e:
        print(f"Error running agent workflow: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

def main():
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_cli())

if __name__ == "__main__":
    main()
