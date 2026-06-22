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
app.add_middleware(
    GitHubSignatureMiddleware, 
    secret=settings.github_webhook_secret.get_secret_value() if settings.github_webhook_secret else ""
)

async def run_agent_workflow(job: ReviewJob, payload: dict):
    """
    Background task that executes the LangGraph agent workflow for the incoming PR/push.
    """
    async with get_checkpointer() as checkpointer:
        graph = get_compiled_graph(checkpointer=checkpointer)
        # Unique thread ID per job for checkpointer
        config = {"configurable": {"thread_id": job.id}}
        
        # Initial state for the graph
        initial_state = {
            "job": job,
            "findings": [],
            "proposals": [],
            "test_results": [],
            "risk_assessments": []
        }
        
        try:
            # We use ainvoke for asynchronous execution of the graph
            print(f"Starting agent workflow for job {job.id}")
            final_state = await graph.ainvoke(initial_state, config=config)
            print(f"Agent workflow completed for job {job.id}. Final status: {final_state.get('status')}")
        except Exception as e:
            print(f"Agent workflow failed for job {job.id}: {e}")

@app.post("/webhook")
async def handle_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    
    # Example parsing for GitHub PRs
    pr_number = None
    branch = None
    repo_url = payload.get("repository", {}).get("clone_url", "")
    
    if "pull_request" in payload:
        pr_number = payload["pull_request"].get("number")
        branch = payload["pull_request"]["head"].get("ref")
        
    if not repo_url:
        return {"status": "ignored", "reason": "No repository URL in payload"}

    job = ReviewJob(
        repo=repo_url,
        pr_number=pr_number,
        branch=branch
    )

    # Queue the background task to execute the graph
    background_tasks.add_task(run_agent_workflow, job, payload)
    
    return {"status": "queued", "job_id": job.id}

@app.get("/health")
async def health_check():
    """
    Basic health check endpoint for orchestration/KEDA.
    """
    return {"status": "healthy"}

@app.get("/unsafe_query")
async def unsafe_query(user_id: str):
    """
    Vulnerable endpoint for testing LLM analysis.
    """
    # Simulate executing a raw SQL query with an f-string (SQL Injection)
    query = f"SELECT * FROM users WHERE id = '{user_id}'"
    return {"query_executed": query}
