# Autonomous Code Review Agent

**An AI-powered, fully autonomous code reviewer that understands your framework, runs your tests, and writes safe fixes.**

This project is an advanced code review system built as a LangGraph agent. It doesn't just leave comments on PRs—it provisions an isolated microVM sandbox, detects the project's framework, runs static analysis, generates patches, and **actually runs your test suite** to verify its fixes. Using a deterministic risk model, it autonomously commits low-risk fixes directly to the PR branch and escalates high-risk changes to human developers.

---

## 1. System Design

The architecture is split into a **Control Plane** for cheap, fast webhook intake, and an **Execution Plane** for secure, sandboxed code execution.

![System Design Diagram](./docs/images/system-design.png)

- **Control Plane (Serverless):** Handles bursty GitHub webhook traffic (`opened`, `synchronize`), validates HMAC signatures, mints scoped installation tokens, and enqueues jobs. 
- **Execution Plane (Kubernetes):** Pulls from the job queue, provisions ephemeral **microVM sandboxes** (using gVisor/Firecracker), and runs the LangGraph agent.
- **Risk Engine:** A deterministic gate that scores fixes based on test outcomes, blast radius, and file criticality to decide if a commit is safe for full autonomy.

## 2. Agentic Architecture (LangGraph Topology)

The system moves beyond a single-trajectory ReAct loop to a resilient, resumable **plan-and-execute** model.

![Agentic Architecture Diagram](./docs/images/agentic-architecture.png)

1. **Ingest & Detect:** Automatically detects the primary language, framework, test command, and linting rules by parsing lockfiles (`package-lock.json`, `pyproject.toml`) and CI configs.
2. **Plan Fixes:** A Large Language Model drafts scoped proposals for static analysis findings.
3. **Per-Proposal Sandbox Subgraph:** For each proposal, a fresh microVM is spun up. The agent applies the patch and runs the project's *actual* test suite. If tests fail, it receives a bounded retry budget to fix its own code.
4. **Aggregate & Decide:** All findings are evaluated by the deterministic risk model to separate them into an auto-commit batch and an escalate batch.

## 3. Process Flow

How the system behaves from the moment a developer opens a Pull Request:

![Process Flow Diagram](./docs/images/process-flow.png)

1. A developer pushes code; GitHub fires a webhook to the Control Plane.
2. The Execution Plane spins up an isolated sandbox to protect infrastructure from untrusted PR code.
3. The agent drafts patches and tests them against the PR's test suite.
4. **The hard gate:** If the test suite fails or the blast radius is too large, it stops and requests human review. If tests are green and the risk score is low, it commits the fix directly to the branch.
