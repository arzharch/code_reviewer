# Kubernetes and KEDA setup (From Zero)

If you're new to Kubernetes (K8s), this guide explains from scratch what it is, why we use it for the Autonomous Code Reviewer, and how our specific deployment works.

## 1. What is Kubernetes?

Kubernetes is a system for running and managing containerized applications (like Docker containers) across multiple machines. Instead of you manually running `docker run` on a server, you tell Kubernetes: *"I want 2 copies of my API running, and if one crashes, restart it."* Kubernetes handles the scheduling, networking, and scaling automatically.

### Key K8s Concepts Used in This Project:
- **Pod:** The smallest unit in Kubernetes. A pod usually runs one container (e.g., our API container or our Worker container).
- **Deployment:** A blueprint for creating Pods. It ensures that a specified number of identical Pods are always running.
- **Service (Svc):** A stable network endpoint. Pod IP addresses change all the time as they are destroyed and recreated, but a Service gives you a fixed name (like `redis-svc`) to connect to them.
- **Namespace:** A virtual cluster. We put all our resources into the `code-reviewer` namespace to keep them separated from other things running on the cluster.
- **Secret:** A secure way to store sensitive info (like API keys) instead of hardcoding them in code.

## 2. Why does the Code Review Agent need Kubernetes?

When a pull request arrives, the webhook is quick, but analyzing the code, making LLM calls, and running test suites takes time. If you receive 10 PRs at once, a single server might crash or take hours to process them sequentially.

Kubernetes solves this:
1. **The Control Plane (API)** is deployed as a standard web service that always stays awake (2 replicas) to receive webhooks instantly.
2. **The Execution Plane (Worker)** scales based on demand. If there are 10 jobs in the queue, Kubernetes automatically spins up 10 Worker Pods to process them in parallel!

## 3. What is KEDA and ARQ?

We use **ARQ** (Async Redis Queue) in Python to handle our background tasks. When the webhook receives a PR, it enqueues a job to Redis.

```python
# src/control_plane/main.py
@app.post("/webhook")
async def github_webhook(request: Request, bg_tasks: BackgroundTasks):
    redis = await create_pool(RedisSettings())
    await redis.enqueue_job("process_pr_job", pr_data)
    return {"status": "enqueued"}
```

Our workers are configured to listen to this queue:

```python
# src/control_plane/worker.py
class WorkerSettings:
    functions = [process_pr_job]
    redis_settings = RedisSettings(host='redis-svc', port=6379)
```

However, if 100 jobs are enqueued, we need more workers. **KEDA (Kubernetes Event-Driven Autoscaling)** acts as a bridge. We give KEDA a `ScaledObject` that tells it to scale our Worker Deployment based on the Redis list length:

```yaml
# k8s/keda-scaledobject.yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: worker-scaler
  namespace: code-reviewer
spec:
  scaleTargetRef:
    name: worker-deployment
  minReplicaCount: 0  # Scales to zero when idle to save money!
  maxReplicaCount: 10 # Caps at 10 to avoid exploding the budget
  triggers:
  - type: redis
    metadata:
      address: redis://redis-svc.code-reviewer.svc.cluster.local:6379
      listName: arq:queue
      listLength: "1" # Add 1 pod for every 1 job in the queue
```

## 4. gVisor Security: The Crown Jewel

When the worker pod tests a PR, it doesn't test it directly on the worker. That would be incredibly dangerous—an LLM or malicious user could write code that deletes files or steals environment variables. 

Instead, the Worker Pod spins up a temporary Docker container using the **gVisor (`runsc`)** runtime. gVisor provides a fake Linux kernel to the container. If the PR contains malicious code, it hits the fake kernel and is completely contained.

Here is the Python snippet showing how the worker provisions this ultimate zero-trust sandbox:

```python
# src/execution_plane/sandbox.py
def execute_tests_in_sandbox(self, command: str):
    # We use docker run with --runtime=runsc to use the gVisor secure kernel
    # --network=none ensures the unverified code cannot exfiltrate data to the internet
    docker_cmd = [
        "docker", "run", "--rm",
        "--runtime=runsc",
        "--network=none",
        "--memory=512m",
        "-v", f"{self.workspace}:/workspace",
        "-w", "/workspace",
        "python:3.12-slim",
        "sh", "-c", command
    ]
    
    result = subprocess.run(
        docker_cmd, 
        capture_output=True, 
        text=True, 
        timeout=300
    )
    return result.stdout, result.returncode == 0
```

To allow the worker to run Docker commands, we deploy it in privileged mode with access to the host's Docker socket:

```yaml
# k8s/worker-deployment.yaml
      containers:
      - name: worker
        image: code-reviewer-worker:latest
        volumeMounts:
        - name: docker-sock
          mountPath: /var/run/docker.sock
      volumes:
      - name: docker-sock
        hostPath:
          path: /var/run/docker.sock
```

By combining ARQ for distributed task management, KEDA for reactive queue-based scaling, and gVisor for kernel-level sandboxing, the system achieves a highly scalable and flawlessly secure pipeline for autonomous AI code execution!
