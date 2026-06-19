# Project 2: Autonomous Code Review & Refactoring Agent

## Goal
Build a production-grade autonomous agent that ingests pull requests or local code diffs, identifies bugs/anti-patterns, evaluates code quality, and securely proposes or applies refactored code. The system is built with a production-first mindset from day one, emphasizing security, robustness, idempotency, and network reliability.

## Architecture & Technology Stack
* **Framework:** Python with LangGraph (Plan-and-Execute pattern).
* **LLM Provider:** OpenAI (`gpt-4o` or `gpt-4o-mini` for lightweight tasks).
* **State Persistence:** Postgres (LangGraph checkpointer) with fail-proof configurations.
* **Code Quality Tools:** Ruff (linting), MyPy (type checking), Bandit (security scanning), and Radon/AST tools to deeply understand code semantics rather than just compiling.
* **Input Mechanism:** 
  * *GitHub App:* Ingests `pull_request` webhooks, fetching only the modified files (diffs).
  * *CLI:* Reads local `--staged` changes or branch diffs.

## Production Requirements
From the first line of code, this project will incorporate:
* **Security & Sandboxing:** Untrusted code execution is strictly isolated.
* **Middleware & Rate Limiting:** API gateways for handling webhook bursts and protecting against rate limit exhaustion.
* **Network Robustness:** Built-in retries for network failures, idempotency keys for webhooks to prevent duplicate runs, and a durable queue decoupled from the execution plane.
* **Fail-proof Database:** Connection pooling, durable Postgres checkpointer to recover LangGraph state on pod/node failure.
* **Commit Strategy:** Frequent, granular commits tracking what is being setup (e.g., "first commit", "restructuring", "add checkpointer", "setup postgres").

---

## Development Sprints

### Sprint 1: Production Foundation & Infrastructure setup
**Focus:** Establishing the robust skeleton of the service.
* **File Structuring:** Set up the Python project structure optimized for microservices (Control Plane & Execution Plane).
* **Database Setup:** Initialize Postgres for the LangGraph checkpointer, including migrations, connection pooling (e.g., asyncpg + PgBouncer logic), and fail-over considerations.
* **API Middleware:** Implement the webhook receiver (FastAPI) with idempotency (handling duplicate GitHub delivery IDs), payload signature verification (HMAC), and rate limiting.
* **Task Queuing:** Implement a job queue to decouple webhook ingestion from long-running agent execution, ensuring network drops don't lose jobs.

### Sprint 2: Core LangGraph Agent Engine
**Focus:** The AI orchestrator and state management.
* **Agent State Schema:** Define `ReviewJob`, `Finding`, `Proposal`, and `RiskAssessment` Pydantic models.
* **Checkpointer Integration:** Wire up the Postgres checkpointer to LangGraph to ensure resumability if the agent worker crashes.
* **LLM Integration:** Integrate OpenAI (`gpt-4o` / `gpt-4o-mini`) for the `plan_fixes` node.
* **Risk Engine:** Build the deterministic risk scoring function (evaluating file criticality, blast radius, test results).

### Sprint 3: Code Ingestion & Deep Analysis
**Focus:** Understanding what changed and evaluating its quality.
* **Diff Parsing:** Implement logic to fetch and parse unified diffs from GitHub PRs or local git trees. The agent will only focus on changed lines and their surrounding context.
* **Static & Semantic Analysis:** Integrate Ruff, MyPy, and Bandit. Implement an LLM-assisted "Project Understanding" step to read configuration files (`pyproject.toml`, `package.json`) and grasp the project's purpose and architecture before proposing changes.

### Sprint 4: Sandboxed Execution & Testing
**Focus:** Safe execution of untrusted code.
* **Sandbox Runtime:** Implement the environment for `apply_in_sandbox_and_test`.
* **Patch Applier:** Apply proposed diffs securely to the ephemeral checkout.
* **Test Runner:** Execute the repository's test suite and capture coverage/pass/fail metrics without risking the host machine.

### Sprint 5: Action & Output Handling
**Focus:** Closing the loop with the user.
* **Auto-Commit Logic:** Implement the git tool to securely commit and push low-risk fixes back to the PR branch using short-lived tokens.
* **Escalation & Commenting:** For high-risk findings, post PR review comments with suggestions and a neutral check-run status.
* **Audit Logging:** Finalize the Postgres audit log table to guarantee tracing of why the bot made specific decisions.
