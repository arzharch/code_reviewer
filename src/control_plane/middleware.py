import hmac
import hashlib
import json
from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
import redis.asyncio as redis
import os

# Initialize Redis connection for rate limiting and idempotency
redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
redis_client = redis.from_url(redis_url)

class GitHubSignatureMiddleware(BaseHTTPMiddleware):
    """
    Middleware to verify GitHub HMAC signature.
    """
    def __init__(self, app, secret: str):
        super().__init__(app)
        self.secret = secret.encode("utf-8") if secret else None

    async def dispatch(self, request: Request, call_next):
        # We only verify signature on specific webhook paths
        if request.url.path == "/webhook" and request.method == "POST":
            if not self.secret:
                return JSONResponse(status_code=500, content={"detail": "Webhook secret not configured"})

            signature_header = request.headers.get("x-hub-signature-256")
            if not signature_header:
                return JSONResponse(status_code=401, content={"detail": "Missing X-Hub-Signature-256"})

            # Read the body and store it back so the route can still read it
            body = await request.body()
            
            # Reconstruct the body so downstream handlers can consume it
            async def receive():
                return {"type": "http.request", "body": body}
            request._receive = receive

            expected_signature = "sha256=" + hmac.new(
                self.secret, body, hashlib.sha256
            ).hexdigest()

            if not hmac.compare_digest(signature_header, expected_signature):
                return JSONResponse(status_code=401, content={"detail": "Invalid signature"})

        response = await call_next(request)
        return response

class IdempotencyMiddleware(BaseHTTPMiddleware):
    """
    Middleware to deduplicate GitHub Delivery IDs to prevent redundant processing.
    """
    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/webhook" and request.method == "POST":
            delivery_id = request.headers.get("x-github-delivery")
            if delivery_id:
                # Check if this delivery ID was already processed
                is_duplicate = await redis_client.get(f"webhook_delivery:{delivery_id}")
                if is_duplicate:
                    return JSONResponse(status_code=202, content={"detail": "Webhook already processed"})
                
                # Mark as processed with a TTL (e.g., 24 hours)
                await redis_client.setex(f"webhook_delivery:{delivery_id}", 86400, "1")
                
        response = await call_next(request)
        return response

class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Simple Token Bucket rate limiter using Redis for the API Gateway.
    """
    def __init__(self, app, rate_limit: int = 100, window: int = 60):
        super().__init__(app)
        self.rate_limit = rate_limit
        self.window = window

    async def dispatch(self, request: Request, call_next):
        # Rate limit based on IP (or Installation ID if authenticated)
        client_ip = request.client.host if request.client else "unknown"
        key = f"rate_limit:{client_ip}"
        
        current_count = await redis_client.incr(key)
        if current_count == 1:
            await redis_client.expire(key, self.window)
            
        if current_count > self.rate_limit:
            return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})
            
        response = await call_next(request)
        return response
