# Autonomous Code Review Agent - Interview Q&A Deep Dive

This document is designed to help you prepare for technical interviews regarding the Autonomous Code Review Agent. It mimics a real-world system design and architecture interview, complete with edge cases, failure modes, and follow-up questions.

---

## 1. Architecture & Scaling

**Q: Can you walk me through the high-level architecture of your Code Review Agent?**
**A:** The system is split into two primary components: the **Control Plane** and the **Execution Plane**, connected by a **Redis (ARQ)** task queue. 
1. The **Control Plane** is a FastAPI service that listens for GitHub webhooks (e.g., when a PR is opened). It verifies the webhook signature, creates a job record in PostgreSQL, and enqueues the job into Redis.
2. The **Execution Plane** consists of LangGraph worker nodes. When a job is picked up, the worker fetches the code, runs static analysis (Ruff, Bandit, MyPy) in parallel, and asks an LLM to evaluate the issues. 
3. If a fix is proposed, the worker spins up a secure Docker container using **gVisor** (`runsc`) to apply the patch and run the project's test suite. Based on the test results and a deterministic risk score, the agent decides whether to auto-commit the fix or escalate to a human.

**Follow-up: What happens if 1,000 PRs are opened simultaneously? How does your system scale?**
**A:** Because we decoupled the webhook ingestion from the execution, the FastAPI Control Plane can easily absorb the burst of HTTP requests. It simply drops 1,000 jobs into the Redis queue.
For the Execution Plane, we use **KEDA (Kubernetes Event-driven Autoscaling)**. KEDA monitors the length of the Redis `arq:queue`. As the queue grows, KEDA dynamically provisions more worker pods to process the backlog in parallel.

**Follow-up: If KEDA scales up to 100 workers, won't they overwhelm your PostgreSQL database?**
**A:** To prevent database exhaustion, the workers use **connection pooling** via `asyncpg` with a strict `pool_size` and `max_overflow`. Additionally, state checkpointing in LangGraph is highly optimized. We only write to Postgres at the end of discrete graph nodes, rather than holding long-running transactions open during LLM inference or test execution.

---

## 2. Security & Untrusted Code Execution (The gVisor Sandbox)

**Q: Your agent actually runs the test suite of the PR it is reviewing. Pull requests can contain malicious code. How do you prevent a malicious PR from stealing your API keys or breaking out of the container?**
**A:** This is handled through strict sandbox isolation at the **Execution Plane**:
1. **No Secrets in Sandbox:** The environment variables (like `OPENAI_API_KEY` or `DATABASE_URL`) exist in the parent Python worker but are explicitly **stripped** from the subprocess environment before the test container is launched.
2. **Network Isolation:** We run the container with `--network=none`, meaning even if the untrusted code attempts to exfiltrate data, there is no network interface available to do so.
3. **Kernel Isolation (gVisor):** Standard Docker containers share the host's kernel, making them vulnerable to kernel exploits. We use the **gVisor runtime (`runsc`)**, which provides a user-space kernel proxy. It intercepts system calls, preventing untrusted code from touching the actual host kernel.

**Follow-up: What if the malicious PR contains a "Fork Bomb" (`:(){ :|:& };:`) or an infinite `while True` loop to exhaust your server's resources?**
**A:** A fork bomb would try to exhaust PIDs and memory. We mitigate this using Docker's resource limits (e.g., `--cpus="1.0"`, `--memory="512m"`, and `--pids-limit="100"`). Furthermore, the Python `subprocess.run` that invokes the sandbox has a hard **timeout (e.g., 300 seconds)**. If the test suite doesn't finish in 5 minutes, a `subprocess.TimeoutExpired` exception is caught, the container is killed, and the LLM receives a "Tests timed out" failure, which forces it to escalate the fix.

---

## 3. Resilience & State Management (LangGraph Edge Cases)

**Q: AI agents can take minutes to run. What happens if a Kubernetes node crashes or the worker pod is evicted right in the middle of generating a fix?**
**A:** We use **LangGraph's AsyncPostgresSaver** for checkpointing. The agent's workflow is modeled as a state machine. Every time a node transitions (e.g., from `analyze_code` to `propose_fixes`), the entire state is checkpointed to PostgreSQL.
If a worker crashes, the ARQ job will timeout and retry. When the new worker picks up the job, it doesn't start from scratch. We pass the `thread_id` (the job ID) to LangGraph, and it automatically resumes from the exact node where it left off, skipping the expensive LLM calls that already succeeded.

**Follow-up: What if the webhook payload is delivered twice by GitHub? Do you review the code twice?**
**A:** No. The FastAPI Control Plane uses an **IdempotencyMiddleware**. We hash the payload or use the `X-GitHub-Delivery` header as an idempotency key and store it in Redis. If a duplicate delivery occurs, the middleware intercepts it and returns a `200 OK` immediately without enqueuing a duplicate job.

---

## 4. LLM Hallucinations & Patch Application

**Q: LLMs are notorious for outputting poorly formatted text. What if the LLM outputs a diff that has missing context lines or is wrapped in markdown, and `git apply` rejects it?**
**A:** We treat LLM output as highly untrusted data. 
First, our `normalize_diff()` function aggressively cleans the output—stripping markdown fences (```diff), fixing carriage returns, and validating that file headers (`---`, `+++`) exist.
Second, when we run `git apply`, we use the `--recount` flag and try multiple strictness levels (`-p1`, `-p0`). 
If `git apply` still fails, we don't crash. We mark that specific proposal as "failed to apply" and pass that error back into the state. The LangGraph topology has a `replan` node that feeds the `git apply` error back to the LLM, giving it a chance to correct its formatting.

**Follow-up: What if the LLM enters an infinite loop—it proposes a fix, the tests fail, it tries again, and fails again forever?**
**A:** To prevent endless cycles and massive OpenAI API bills, we implemented a **Retry Budget** in the LangGraph state. The state tracks `retries_used` per finding. If a finding exceeds its maximum retries (e.g., 3 attempts), the system explicitly routes that finding to the "Escalate" bucket. It stops trying to fix it autonomously and instead leaves a comment for the human reviewer.

---

## 5. Git and Branching Edge Cases

**Q: In CI pipelines, repositories are often cloned as "shallow clones" (fetch-depth: 1) to save time. If your agent is trying to apply patches or analyze diffs, how does it handle missing git history?**
**A:** If we try to push a commit to a shallow clone, git will reject it. Our `GitActionsService` detects shallow clones by checking if `.git/shallow` exists. If it does, we run `git fetch --unshallow` before attempting to commit and push the fixes.

**Follow-up: What if the human developer force-pushes a new commit to the PR branch while the agent is in the middle of testing a fix?**
**A:** When the agent finishes testing and attempts to `git push origin HEAD:refs/heads/{branch}`, GitHub will reject the push because the remote branch has diverged (since we don't use `--force`). We catch this `CalledProcessError`. Instead of silently failing, the agent's risk decider catches the push failure and updates the status to "Escalated", leaving a PR comment explaining that the fix couldn't be pushed due to upstream branch changes.

---

## 6. Project Detection & Execution

**Q: You mentioned the agent runs the project's test suite. Since every project uses different languages and frameworks, how does the LLM know which commands to execute to test the code?**
**A:** We actually **do not** let the LLM guess the test commands. Relying on an LLM to arbitrarily guess bash commands for every repository is a massive security risk and highly prone to hallucination. 
Instead, our system uses a deterministic `IngestionService`. When a PR is opened, the service scans the repository's root for known lockfiles and configuration signatures:
- If it sees `pyproject.toml` and `poetry.lock`, it knows the project is Python and the test command is `poetry run pytest`.
- If it sees `manage.py`, it categorizes the framework as `django` and sets the command to `python manage.py test`.
- If it sees `package.json` and `yarn.lock`, it sets the language to JavaScript/TypeScript and the command to `yarn test`.

This deterministic data is packaged into a `ProjectProfile` object. The `ProjectProfile` is passed into the LangGraph state. The LLM receives this profile as read-only context (so it knows what framework rules to apply when reviewing the code), and the Execution Plane (gVisor sandbox) blindly executes the `test_command` string provided by the `ProjectProfile`.

**Follow-up: What if the repository uses a custom build tool or script that isn't recognized by your `IngestionService`?**
**A:** If no recognized configuration is found, the `ProjectProfile` defaults the test command to `echo 'No test command detected'`. When the sandbox executes this, the test results return a safe "passed/skipped" state but flag that no real tests were run. The deterministic risk decider sees that test coverage for the proposed patch is 0%, which automatically increases the risk score of the fix. If the score crosses the threshold, the agent escalates the fix to a human rather than blindly auto-committing untested code.
