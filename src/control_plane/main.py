import shutil

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from src.agent.agent import get_compiled_graph
from src.agent.checkpointer import get_checkpointer
from src.agent.git_actions import GitActionsService
from src.agent.state import ReviewJob
from src.common.config import settings
from src.common.logging import configure_logging, get_logger, job_context

from .middleware import GitHubSignatureMiddleware, IdempotencyMiddleware, RateLimitMiddleware

configure_logging(level=settings.log_level, fmt=settings.log_format)
logger = get_logger("control_plane")

app = FastAPI(
    title="Autonomous Code Reviewer - Control Plane",
    description="API Gateway for ingesting webhooks and routing to the execution plane",
    version="0.1.0"
)

# Middleware order: Starlette runs the LAST added first, so signature
# verification (added last) is the outermost layer and rejects forged
# payloads before they consume the rate-limit budget or an idempotency key.
app.add_middleware(RateLimitMiddleware, rate_limit=100, window=60)
app.add_middleware(IdempotencyMiddleware)
if settings.github_webhook_secret:
    app.add_middleware(
        GitHubSignatureMiddleware,
        secret=settings.github_webhook_secret.get_secret_value()
    )
else:
    logger.warning("webhook.signature_verification_disabled",
                   extra={"reason": "GITHUB_WEBHOOK_SECRET is not set"})


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


async def run_agent_workflow(job: ReviewJob, payload: dict):
    """
    Background task that executes the LangGraph agent workflow for the incoming PR.
    """
    temp_dir = None
    with job_context(job.id):
        try:
            repo_full_name = job.repo_full_name
            if not repo_full_name or not job.pr_number:
                logger.error("workflow.missing_pr_context")
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
            # This workspace is an agent-owned clone with an authenticated remote,
            # so the agent is allowed to commit and push to the PR branch from it.
            job.workspace_is_clone = True

            async with get_checkpointer() as checkpointer:
                graph = get_compiled_graph(checkpointer=checkpointer)
                config = {"configurable": {"thread_id": job.id}}

                final_state = await graph.ainvoke(_initial_state(job), config=config)
                logger.info("workflow.done", extra={"final_status": final_state.get("status")})
        except Exception:
            logger.exception("workflow.failed")
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
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)


@app.post("/webhook")
async def handle_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()

    action = payload.get("action")
    if "pull_request" in payload:
        if action not in ["opened", "synchronize", "reopened"]:
            return {"status": "ignored", "reason": f"Action {action} ignored to save credits"}

        pr_number = payload["pull_request"].get("number")
        branch = payload["pull_request"]["head"].get("ref")
        repo_url = payload.get("repository", {}).get("clone_url", "")
        repo_full_name = payload.get("repository", {}).get("full_name")
        installation_id = payload.get("installation", {}).get("id")

        if not repo_url or not repo_full_name:
            return {"status": "ignored", "reason": "No repository info in payload"}

        job = ReviewJob(
            repo=repo_url,  # Initially the URL, will be replaced with local path
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            branch=branch,
            installation_id=installation_id,
            token_limit=settings.token_budget
        )

        logger.info("webhook.accepted", extra={"job_id": job.id, "repo": repo_full_name,
                                               "pr": pr_number, "action": action})
        background_tasks.add_task(run_agent_workflow, job, payload)
        return {"status": "accepted", "job_id": job.id}

    return {"status": "ignored", "reason": "Not a pull_request event"}


@app.get("/health")
async def health_check():
    """
    Basic health check endpoint for orchestration/KEDA.
    """
    return {"status": "healthy"}


@app.get("/jobs/{job_id}")
async def job_status(job_id: str):
    """
    Reads a review's persisted checkpoint: where it got to, and what it found.
    This is what makes a stalled or crashed run inspectable after the fact.
    """
    async with get_checkpointer() as checkpointer:
        graph = get_compiled_graph(checkpointer=checkpointer)
        snapshot = await graph.aget_state({"configurable": {"thread_id": job_id}})

    if not snapshot or not snapshot.created_at:
        raise HTTPException(status_code=404, detail=f"No checkpoint found for job {job_id}")

    values = snapshot.values or {}
    return {
        "job_id": job_id,
        "status": values.get("status"),
        "next_nodes": list(snapshot.next or []),
        "checkpointed_at": snapshot.created_at,
        "findings": len(values.get("findings", []) or []),
        "proposals": len(values.get("proposals", []) or []),
        "verified": len(values.get("test_results", []) or []),
        "unapplied": values.get("unapplied", {}) or {},
        "retries_used": values.get("retries_used", {}) or {},
        "replan_rounds": values.get("replan_rounds", 0),
        "tokens_used": values.get("tokens_used", 0),
    }


@app.post("/jobs/{job_id}/resume")
async def resume_job(job_id: str):
    """
    Resumes a review from its last checkpoint. Invoking with `None` replays the
    graph from where it stopped instead of restarting the whole analysis.
    """
    with job_context(job_id):
        async with get_checkpointer() as checkpointer:
            graph = get_compiled_graph(checkpointer=checkpointer)
            config = {"configurable": {"thread_id": job_id}}
            snapshot = await graph.aget_state(config)

            if not snapshot or not snapshot.created_at:
                raise HTTPException(status_code=404, detail=f"No checkpoint found for job {job_id}")
            if not snapshot.next:
                return {"job_id": job_id, "status": (snapshot.values or {}).get("status"),
                        "resumed": False, "reason": "Job already finished"}

            job = (snapshot.values or {}).get("job")
            workspace_gone = job is not None and not GitActionsService.workspace_exists(job.repo)
            if workspace_gone:
                raise HTTPException(
                    status_code=409,
                    detail="The job's workspace no longer exists; re-run the review instead of resuming."
                )

            logger.info("job.resume", extra={"job_id": job_id, "from_nodes": list(snapshot.next)})
            final_state = await graph.ainvoke(None, config=config)

    return {"job_id": job_id, "resumed": True, "status": final_state.get("status")}
