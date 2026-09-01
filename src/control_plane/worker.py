import shutil
from arq.connections import RedisSettings

from src.agent.agent import get_compiled_graph
from src.agent.checkpointer import get_checkpointer
from src.agent.git_actions import GitActionsService
from src.agent.state import ReviewJob
from src.common.config import settings
from src.common.logging import configure_logging, get_logger, job_context
from src.common.database import log_run

configure_logging(level=settings.log_level, fmt=settings.log_format)
logger = get_logger("worker")

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

async def run_agent_workflow(ctx: dict, job_dict: dict, payload: dict):
    """
    ARQ Task that executes the LangGraph agent workflow for the incoming PR.
    """
    job = ReviewJob(**job_dict)
    temp_dir = None
    
    with job_context(job.id):
        # Log to postgres that the run has started
        await log_run(run_id=job.id, repo=job.repo_full_name or job.repo, status="running", pr_number=job.pr_number)
        
        try:
            repo_full_name = job.repo_full_name
            if not repo_full_name or not job.pr_number:
                logger.error("workflow.missing_pr_context")
                await log_run(run_id=job.id, repo=job.repo, status="failed", pr_number=job.pr_number)
                return

            logger.info("workflow.start", extra={"repo": repo_full_name, "pr": job.pr_number})

            GitActionsService.post_pr_comment(
                repo_full_name=repo_full_name,
                pr_number=job.pr_number,
                comment=(
                    "🤖 **Autonomous Code Reviewer**: I've received your pull request and am "
                    f"analyzing it now. Job `{job.id}`."
                ),
                installation_id=job.installation_id
            )

            temp_dir, diff_content, head_sha, base_sha = GitActionsService.clone_and_prep_pr(
                repo_full_name=repo_full_name,
                pr_number=job.pr_number,
                clone_url=job.repo,
                installation_id=job.installation_id
            )

            job.repo = temp_dir
            job.commit_sha = head_sha
            job.raw_diff = diff_content
            job.workspace_is_clone = True

            async with get_checkpointer() as checkpointer:
                graph = get_compiled_graph(checkpointer=checkpointer)
                config = {"configurable": {"thread_id": job.id}}

                final_state = await graph.ainvoke(_initial_state(job), config=config)
                final_status = final_state.get("status", "completed")
                logger.info("workflow.done", extra={"final_status": final_status})
                await log_run(run_id=job.id, repo=repo_full_name, status=final_status, pr_number=job.pr_number)
                
        except Exception as e:
            logger.exception("workflow.failed")
            await log_run(run_id=job.id, repo=job.repo_full_name or job.repo, status="failed", pr_number=job.pr_number)
            if job.repo_full_name and job.pr_number:
                GitActionsService.post_pr_comment(
                    repo_full_name=job.repo_full_name,
                    pr_number=job.pr_number,
                    comment=(
                        "🤖 **Autonomous Code Reviewer**: this review failed partway through. "
                        f"Job `{job.id}` can be resumed once the cause is fixed."
                    ),
                    installation_id=job.installation_id
                )
            raise e
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

# Parse Redis URL
# e.g. redis://localhost:6379 -> host=localhost, port=6379
# We will use from_url for ARQ RedisSettings if possible, or construct it.
redis_settings = RedisSettings.from_dsn(settings.redis_url)

class WorkerSettings:
    """
    Configuration for the ARQ worker.
    Run via `arq src.control_plane.worker.WorkerSettings`
    """
    functions = [run_agent_workflow]
    redis_settings = redis_settings
    max_jobs = 10
    job_timeout = 3600 # 1 hour max
