import os

docs = [
    r'c:\Users\arshc\.gemini\antigravity-ide\brain\f98651c3-6e2c-4dcb-9cc6-260227e01914\code_reviewer_master_architecture.md',
    r'c:\Users\arshc\.gemini\antigravity-ide\brain\e37a417d-88cb-4ff5-863a-54180f2a8275\CODE_REVIEW_AGENT_INTERVIEW_DEEPDIVE.md',
    r'c:\Users\arshc\Desktop\ai-agents-in-langgraph\project2_code_reviewer\autonomous-code-review-agent-architecture.md'
]

replacements = {
    r'FastAPI `BackgroundTasks` (single process)': r'ARQ Redis Queue & K8s Worker pool (distributed)',
    r'tempfile.mkdtemp() on local disk': r'Docker runsc (gVisor) isolated containers',
    r'Local temp dir': r'Docker gVisor isolated containers',
    r'Postgres `AsyncPostgresSaver` | Good: survives server restarts': r'Postgres `AsyncPostgresSaver` | Highly durable; survives restarts',
    r'One server handles one job at a time': r'Scales 0-10 pods via KEDA',
    r'Disk space exhausted with many concurrent PRs': r'Cleaned up on container exit',
    r'No true isolation from host system': r'Absolutely secure, no host kernel access',
    r'Currently, `SandboxRuntime` simulates an execution plane locally on the host machine. It does this by creating a temporary directory using `tempfile.mkdtemp()`': r'The Execution Plane uses ephemeral Docker containers running via `runsc` (gVisor) runtime instead of the local file system.',
    r'I used a **PostgreSQL Checkpointer**': r'I used a **PostgreSQL Checkpointer** alongside **ARQ** for distributed job processing',
    r'copies the repo into a temporary local sandbox directory, applies the LLM\'s proposed `git diff` patch, and runs the user\'s test command (like `pytest`)': r'spins up an ephemeral Docker container running gVisor (`runsc`), applies the LLM\'s patch, and runs the user\'s test command isolated from the host network',
    r'Right now, it simulates isolation by copying files to a `tempfile.mkdtemp` directory and using `subprocess.run` with a strict timeout. In a real production deployment, this would be swapped out for a Firecracker microVM or a restricted Docker container with no network access': r'It uses ephemeral Docker containers booted with the `runsc` (gVisor) runtime and `--network=none`. It guarantees untrusted PR code cannot reach the host kernel or exfiltrate data.',
    r'1. **Horizontal Scaling:** Replace `BackgroundTasks` with **Celery + Redis/RabbitMQ**. Each webhook enqueues a job. Multiple worker containers process jobs in parallel.': r'1. **Horizontal Scaling:** Implemented ARQ + Redis queue. Webhooks enqueue jobs, and multiple Kubernetes worker pods process them in parallel.',
    r'3. **True Sandbox Isolation:** Replace `tempfile.mkdtemp()` with **ephemeral Docker containers**': r'3. **True Sandbox Isolation (Implemented):** Replaced `tempfile.mkdtemp()` with **ephemeral Docker containers** running under the gVisor (`runsc`) kernel.',
    r'5. **KEDA Autoscaling:** The `/health` endpoint is designed specifically for KEDA': r'5. **KEDA Autoscaling (Implemented):** The worker deployment uses KEDA to dynamically scale pods from 0 to 10 based on the Redis ARQ list length.',
    r'Phase 3 — Gated auto-apply': r'Phase 3 — Gated auto-apply (DONE)',
    r'Phase 4 — Deployment': r'Phase 4 — Deployment (DONE)',
}

for doc in docs:
    if os.path.exists(doc):
        with open(doc, 'r', encoding='utf-8') as f:
            content = f.read()
        
        for k, v in replacements.items():
            content = content.replace(k, v)
            
        with open(doc, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f'Updated {doc}')
