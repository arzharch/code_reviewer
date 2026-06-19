from fastapi import FastAPI, Request
from .middleware import GitHubSignatureMiddleware, IdempotencyMiddleware, RateLimitMiddleware
import os

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
    secret=os.getenv("GITHUB_WEBHOOK_SECRET", "")
)

@app.post("/webhook")
async def github_webhook(request: Request):
    """
    Intake endpoint for GitHub App webhooks.
    """
    event = request.headers.get("x-github-event")
    body = await request.json()
    
    # Simple routing based on event type
    if event == "pull_request":
        action = body.get("action")
        if action in ["opened", "synchronize", "ready_for_review"]:
            # TODO: Enqueue the task to the asynchronous job queue
            # job_queue.enqueue("review_pr", body)
            return {"status": "enqueued", "action": action}
            
    return {"status": "ignored"}

@app.get("/health")
async def health_check():
    """
    Basic health check endpoint for orchestration/KEDA.
    """
    return {"status": "healthy"}
