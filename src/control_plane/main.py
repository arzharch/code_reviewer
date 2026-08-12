from fastapi import FastAPI, Request, BackgroundTasks
from .middleware import GitHubSignatureMiddleware, IdempotencyMiddleware, RateLimitMiddleware
from src.common.config import settings
from src.agent.state import ReviewJob
from src.agent.agent import get_compiled_graph
from src.agent.checkpointer import get_checkpointer

app = FastAPI(
    title="Autonomous Code Reviewer - Control Plane",
    description="API Gateway for ingesting webhooks and routing to the execution plane",
    version="0.1.0"
)

# Apply middlewares
# Note: Middleware order matters. Outer to inner: RateLimit -> Idempotency -> Signature
app.add_middleware(RateLimitMiddleware, rate_limit=100, window=60)
app.add_middleware(IdempotencyMiddleware)
if settings.github_webhook_secret:
    app.add_middleware(
        GitHubSignatureMiddleware, 
        secret=settings.github_webhook_secret.get_secret_value()
    )

import tempfile
import shutil
from src.agent.git_actions import GitActionsService

async def run_agent_workflow(job: ReviewJob, payload: dict):
    """
    Background task that executes the LangGraph agent workflow for the incoming PR/push.
    """
    temp_dir = None
    try:
        # Clone repo and fetch diff
        repo_full_name = getattr(job, "repo_full_name", None)
        if not repo_full_name or not job.pr_number:
            print("Missing repo name or PR number, aborting workflow.")
            return
            
        print(f"Preparing workspace for {repo_full_name} PR #{job.pr_number}")
        
        # Post initial status comment
        GitActionsService.post_pr_comment(
            repo_full_name=repo_full_name,
            pr_number=job.pr_number,
            comment="🤖 **Autonomous Code Reviewer**: I've received your pull request and am currently analyzing the code. I'll report back shortly!",
            installation_id=job.installation_id
        )
        
        temp_dir, diff_content, head_sha, base_sha = GitActionsService.clone_and_prep_pr(
            repo_full_name=repo_full_name,
            pr_number=job.pr_number,
            clone_url=job.repo,
            installation_id=job.installation_id
        )
        
        # Update job to point to local temp workspace and add diff
        job.repo = temp_dir
        job.commit_sha = head_sha
        job.raw_diff = diff_content
        # This workspace is an agent-owned clone with an authenticated remote,
        # so the agent is allowed to commit and push to the PR branch from it.
        job.workspace_is_clone = True
        
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
            
            print(f"Starting agent workflow for job {job.id}")
            final_state = await graph.ainvoke(initial_state, config=config)
            print(f"Agent workflow completed for job {job.id}. Final status: {final_state.get('status')}")
    except Exception as e:
        print(f"Agent workflow failed for job {job.id}: {e}")
    finally:
        # Clean up the temporary workspace
        if temp_dir:
            print(f"Cleaning up workspace {temp_dir}")
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
            token_limit=10000  # Default safety limit for GitHub PRs to save credits
        )

        background_tasks.add_task(run_agent_workflow, job, payload)
        return {"status": "queued", "job_id": job.id}
        
    return {"status": "ignored", "reason": "Not a pull_request event"}

@app.get("/health")
async def health_check():
    """
    Basic health check endpoint for orchestration/KEDA.
    """
    return {"status": "healthy"}
