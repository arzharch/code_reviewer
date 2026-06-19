# Autonomous code review & refactoring agent — production architecture

**Status:** Design baseline (v1)
**Scope:** GitHub App (PR-triggered) + CLI (local), shared core engine
**Autonomy model:** Fully autonomous for low-risk fixes; mandatory human escalation for everything else

---

## 1. Overview

This system reviews code automatically, understands the framework and test setup of the repo it's working in, proposes and applies refactors/bug fixes, and decides for itself — using a deterministic risk model, not vibes — whether a fix is safe to commit on its own or needs a human to look at it.

It ships on two surfaces that share one engine:

- **GitHub App** — installed on a repo/org, triggered by PR webhooks (`opened`, `synchronize`, `ready_for_review`). This is the CodeRabbit-shaped surface: PR comments, inline suggestions, check runs, and — when authorized — direct commits to the PR branch.
- **CLI** — run locally against a working tree, a diff, or `--staged` changes. Same engine, same risk model, faster feedback loop, no network round-trip required for the review itself (LLM calls aside).

Both surfaces produce the same internal object — a `ReviewJob` — and hand it to the same LangGraph agent. The agent doesn't know or care which surface invoked it.

### 1.1 Non-goals (v1)

- Not a general-purpose coding assistant / chat interface — it reacts to diffs, it doesn't take open-ended instructions mid-PR (that's a v2 "talk to the bot in PR comments" feature).
- Not multi-repo cross-cutting refactors in one pass — each `ReviewJob` is scoped to one PR or one local diff.
- Not a replacement for human code review on anything architecturally significant — by design, the risk gate routes those to a human.

### 1.2 Success metrics

| Metric | Target | Why it matters |
|---|---|---|
| False-positive rate on findings | < 10% | Above this, developers start ignoring the bot entirely |
| Revert rate on auto-committed fixes | < 1% | This is the number that decides whether "fully autonomous" was the right call |
| Time-to-first-comment on a PR | < 3 min (p95) | Has to beat human review latency to be worth using |
| Escalation precision (escalated findings a human agrees were risky) | > 80% | Low precision here means the risk model is over-escalating and autonomy isn't paying for itself |
| Sandbox escape / containment incidents | 0 | Non-negotiable — see §9 |

---

## 2. High-level architecture

The system is split into two infrastructure planes connected by a queue. This split is the single most important infra decision in this design, so it's worth stating the reasoning up front before the diagram of boxes:

- PR webhook traffic is **bursty and cheap to absorb** — a serverless control plane (fast cold start, scale-to-zero, pay-per-invocation) is the right shape for it.
- Actually **executing a PR's code and test suite is the dangerous part** — it's running arbitrary, untrusted, third-party code on your infrastructure. That needs strong isolation (microVM, not just a container namespace), needs to support multi-minute runs with per-language runtime images, and needs tight resource and network controls. Standard FaaS platforms aren't built for that threat model or those run times.

So: control plane handles intake and routing; a queue decouples it from the execution plane; the execution plane is where the actual agent runs, inside hardened sandboxes, and that's the only place untrusted code ever executes.

```
 GitHub App (webhook)        CLI (local)
        │                         │
        └───────────┬─────────────┘
                     ▼
            Control plane (serverless)
        API gateway · webhook verification
        auth & installation tokens · job queue
                     │
                     ▼
        Execution plane (Kubernetes + microVM sandbox)
        framework detection → static analysis →
        LangGraph agent (plan → patch → test) → risk gate
                     │
         ┌───────────┴───────────┐
         ▼                       ▼
   Auto-commit              Escalate to human
   (low risk,                (high risk, or any
    tests green)              failed test run)
         │                       │
         └───────────┬───────────┘
                      ▼
                   GitHub
         (commit / PR comment / check run)
```

### 2.1 Control plane (recommended: serverless)

| Component | Role | Suggested tech |
|---|---|---|
| Webhook receiver | Verifies GitHub HMAC signature, deduplicates delivery IDs, enqueues job | Cloud Run / Lambda behind API Gateway |
| Auth service | Mints/refreshes GitHub App installation tokens (short-lived, repo-scoped) | Same service, backed by a secrets manager |
| Job queue | Decouples bursty webhook intake from execution plane capacity | SQS / Cloud Tasks / Pub/Sub |
| CLI API | Same intake path, minus the webhook signature step — auth via local device token | Same Cloud Run service, different route |

### 2.2 Execution plane (recommended: Kubernetes + microVM isolation)

| Component | Role |
|---|---|
| Job dispatcher | Pulls from the queue, claims a job, spins up an isolated worker pod |
| Sandbox runtime | gVisor or Firecracker-isolated container per job — no shared kernel with the host, no shared filesystem across jobs |
| LangGraph agent worker | Runs the plan-and-execute graph (§6) inside the sandbox |
| Risk engine | Deterministic scoring service, called by the graph, not itself an LLM call (§7) |
| Result reporter | Posts back to GitHub (Checks API, PR comments, commits) or to the CLI's local output |

Autoscaling: **KEDA**, scaling worker pod count on queue depth rather than CPU — job arrival is bursty (a flurry of PRs after a merge train) and CPU-based scaling reacts too late for that pattern.

### 2.3 Why not pick one infra style for everything

- **All-serverless** falls down on the execution plane: most FaaS platforms cap execution time (minutes) and don't give you control over the sandbox's isolation technology, network egress, or the ability to run a matrix of language runtimes (a Python test suite needs a very different image than a Rust one).
- **All-Kubernetes** wastes money and adds latency on the control plane: webhook intake is spiky and mostly idle, and keeping pods warm 24/7 to shave a few hundred ms off webhook ACK time isn't worth the bill.

The queue in the middle means each plane can be re-architected independently later without touching the other.

---

## 3. Framework & language detection subsystem

This runs first, before any LLM call, and its output (`ProjectProfile`) is injected into every downstream prompt and tool call. Getting this wrong means the agent runs `pytest` on a Jest repo, or proposes a fix that doesn't match the project's actual lint config.

**Detection signals, in priority order:**

1. Lockfiles / manifests: `package-lock.json` / `pnpm-lock.yaml` (Node), `pyproject.toml` / `poetry.lock` (Python), `go.mod` (Go), `Cargo.toml` (Rust), `pom.xml` / `build.gradle` (JVM).
2. Framework-specific config: `next.config.js`, `nuxt.config.ts`, `manage.py` (Django), `artisan` (Laravel).
3. Test runner config: `pytest.ini`/`tox.ini`, `jest.config.*`, `vitest.config.*`, `go test` convention, `Cargo.toml` `[dev-dependencies]`.
4. Lint/format config: `.eslintrc*`, `ruff.toml`, `.golangci.yml`.
5. CI config as ground truth, when present: parse `.github/workflows/*.yml` for the actual commands CI runs — this is the highest-confidence signal because it's what the team already trusts.

```python
class ProjectProfile(BaseModel):
    primary_language: str
    framework: str | None
    package_manager: str            # npm, pnpm, poetry, cargo, go-modules...
    test_command: str               # resolved, e.g. "pnpm test -- --coverage"
    lint_command: str | None
    build_command: str | None
    monorepo: bool
    affected_packages: list[str]    # for monorepos, which sub-packages the diff touches
    detection_confidence: float
    detection_sources: list[str]    # what files/signals led to this profile
```

If `detection_confidence` is below a threshold, the agent runs in **suggest-only mode for that job regardless of the configured autonomy level** — you do not want to guess a test command and then trust its exit code for an auto-commit decision.

---

## 4. The agent: LangGraph design

### 4.1 Plan-and-execute, not pure ReAct

A single-trajectory ReAct loop is a fine fit for "answer a question with tools." It's a poor fit here because:

- The job needs to **survive interruption** — a sandbox can be killed and rescheduled mid-test-run; you need to resume from a checkpoint, not restart an LLM conversation from scratch.
- **Escalation is a first-class outcome**, not an error path — the graph needs an explicit state that means "stop here, a human takes over," and that's naturally a node/edge in a graph, awkward to express as "the ReAct loop decided to give up."
- You want an **explicit plan** (which findings get fixed, in what order, with what retry budget) that's inspectable and auditable independent of the LLM's reasoning trace.

### 4.2 State schema

```python
class Finding(BaseModel):
    id: str
    tool: str                      # which static analyzer / detector produced it
    file: str
    line_range: tuple[int, int]
    severity: Literal["info", "warning", "error", "critical"]
    category: str                  # "bug", "security", "style", "perf", "anti-pattern"
    description: str

class Proposal(BaseModel):
    finding_id: str
    diff: str                      # unified diff, scoped to this one fix
    rationale: str
    confidence: float              # model's self-reported confidence, calibrated against eval set

class TestResult(BaseModel):
    proposal_id: str
    passed: bool
    output_excerpt: str
    duration_s: float
    coverage_delta: float | None

class RiskAssessment(BaseModel):
    proposal_id: str
    score: float                   # 0-1, higher = riskier
    signals: dict[str, float]      # individual signal contributions, for auditability
    decision: Literal["auto_commit", "escalate"]
    reasons: list[str]             # human-readable, shown in the escalation comment

class AgentState(TypedDict):
    job: ReviewJob
    profile: ProjectProfile
    findings: list[Finding]
    proposals: list[Proposal]
    test_results: list[TestResult]
    risk_assessments: list[RiskAssessment]
    retries_used: dict[str, int]   # per-finding retry counter
    status: Literal["running", "awaiting_human", "completed", "failed"]
```

### 4.3 Graph topology

```
ingest_and_detect
      │
      ▼
static_analysis  (parallel tool calls: linters, AST checks, security scanners,
      │            dependency audit — fan-out/fan-in, not sequential)
      ▼
plan_fixes        (LLM: for each finding above a severity threshold, draft a
      │            scoped Proposal; explicitly skips findings it has low
      │            confidence about rather than guessing)
      ▼
┌─────────────────────────────────────────────┐
│  per-proposal subgraph (parallel, one per   │
│  finding, isolated sandbox each):           │
│                                              │
│   apply_in_sandbox → run_tests               │
│         │                  │                 │
│         │            pass ─┘→ risk_score      │
│         │                       │             │
│         └─ fail → (retry ≤2) ──┘             │
│                       │                       │
│                  exhausted → mark escalate    │
└─────────────────────────────────────────────┘
      ▼
aggregate_and_decide   (collects all risk_assessments;
      │                 splits into auto_commit batch and escalate batch)
      ▼
   ┌────────────┴────────────┐
   ▼                         ▼
commit_low_risk        open_review_request
(git commit, push,     (PR comment / inline suggestion
 check run = success)   + Checks API "neutral", awaiting human)
```

Each finding gets its **own sandboxed apply-and-test cycle**, run in parallel where the sandbox pool allows it. A failing test triggers a bounded retry (the plan node gets the test failure output and redrafts that one proposal) — capped at 2 retries, after which that finding is automatically routed to escalation regardless of its risk score. **A failing test suite can never be the reason a risk score looks artificially low** — `passed: false` short-circuits straight to escalation, full stop, before the risk model even runs.

### 4.4 Checkpointing & persistence

LangGraph's checkpointer is backed by Postgres (not in-memory), keyed by `job_id`. This buys:

- Resumability if a worker pod is killed mid-job (Kubernetes preemption, spot instance reclaim, deploy rollout).
- A durable audit trail of every state transition — required for §12 (auditability of autonomous commits).
- The ability to replay a job's exact trajectory when debugging a bad auto-commit after the fact.

---

## 5. Risk classification engine

This is the part that actually makes "fully autonomous" defensible rather than reckless. It is a **deterministic scoring function**, not an LLM judgment call — the LLM's confidence is one input signal among several, never the sole gate.

### 5.1 Signals

| Signal | What it measures | Why it matters |
|---|---|---|
| `file_criticality` | Is the touched file in a configured high-sensitivity path (`auth/`, `payments/`, `*.tf`, `migrations/`, CI config)? | Some files are wrong to touch autonomously no matter how small the diff |
| `blast_radius` | Lines changed, files changed, number of call sites affected (via static call graph) | Bigger surface area = more that can go wrong |
| `test_outcome` | Did the real test suite pass, and did coverage drop? | The single strongest empirical signal — and the only one that's a hard gate, not a weighted score |
| `static_analysis_severity` | Max severity of findings the fix addresses/introduces | A fix that itself trips a new linter warning is suspect |
| `model_confidence` | Calibrated self-reported confidence from the plan node, calibrated against a held-out eval set (raw LLM confidence is *not* trustworthy uncalibrated) | One input, weighted modestly — never decisive alone |
| `historical_revert_rate` | For this repo/file/category, what fraction of past auto-commits here were later reverted? | Lets the system learn caution per-repo over time |
| `semantic_risk_keywords` | Presence of auth, crypto, payment, PII-handling identifiers in the diff | Cheap, high-signal guard against autonomy in regulated-feeling code |

### 5.2 Scoring & thresholds

```
risk_score = (
    w1 * file_criticality +
    w2 * blast_radius_normalized +
    w3 * static_analysis_severity_normalized +
    w4 * (1 - model_confidence_calibrated) +
    w5 * historical_revert_rate +
    w6 * semantic_risk_flag
)

# hard gates — override the weighted score entirely
if not test_outcome.passed:               decision = ESCALATE
elif file_criticality == "protected_path": decision = ESCALATE
elif risk_score <= AUTO_COMMIT_THRESHOLD:  decision = AUTO_COMMIT
else:                                      decision = ESCALATE
```

`AUTO_COMMIT_THRESHOLD` starts conservative and is tuned per-repo from observed revert rates (§13) — a new installation starts in **shadow mode** (everything routes to escalation, regardless of score) for a configurable window so you can measure the model's real-world precision before trusting it with commits at all.

Every `RiskAssessment` stores its individual `signals` dict, not just the final number — this is what lets an escalation comment say *why* (e.g. "touches `payments/charge.py`, escalating regardless of confidence") instead of "risk score 0.71."

---

## 6. Tool catalog

| Tool | Purpose | Sandbox-bound? |
|---|---|---|
| `repo_reader` | Read files, list directory trees, fetch diff context | Yes |
| `ast_analyzer` | Language-specific AST parsing for structural checks (per-language: `ast`/`libcst` for Python, `ts-morph` for TS, `go/ast`) | Yes |
| `static_lint_runner` | Wraps the repo's own configured linter (not a generic one) | Yes |
| `security_scanner` | Wraps Semgrep / CodeQL-style rules for known-bad patterns | Yes |
| `dependency_audit` | Checks for known-vulnerable dependency versions | Yes |
| `patch_applier` | Applies a unified diff to the sandbox working tree | Yes |
| `test_runner` | Executes the repo's *actual* detected test command, captures structured output | Yes |
| `git_tool` | Branch, commit, push — only callable from `commit_low_risk` node, only inside the sandbox, with a short-lived scoped token | Yes |
| `github_api_tool` | Post comments, open check runs, request reviewers | No (control plane) |
| `repo_search` (optional RAG layer) | Semantic search over an embedded index of the repo for cross-file context | Yes |

Every sandbox-bound tool only ever touches the **one ephemeral checkout** created for that job — never a shared filesystem, never another job's workspace.

---

## 7. Sandboxing & security architecture

This is the highest-priority subsystem in the whole design. The product's entire premise is *executing untrusted, third-party code* (whatever's in the PR) *and giving it write access* (the proposed commit) — get this wrong and the blast radius isn't a bad refactor, it's a compromised build agent.

### 7.1 Threat model

| Threat | Mitigation |
|---|---|
| Malicious PR designed to escape the sandbox during test execution | gVisor/Firecracker microVM isolation (not just container namespaces) — separate kernel, not shared with host |
| Prompt injection via code comments/docstrings/README content telling the agent to "ignore previous instructions" or exfiltrate secrets | Treat all repo content as untrusted data, never instructions — system prompt explicitly partitions "instructions" (from the orchestrator) vs "data" (the repo); tool outputs are never re-interpreted as new instructions |
| Exfiltration of secrets/env vars during a malicious test run | No secrets mounted into the execution sandbox at all; the sandbox gets a scoped, short-lived git token only at the commit step, injected just-in-time and revoked immediately after |
| Network access from inside untrusted test execution (calling out to an attacker-controlled endpoint, downloading a second-stage payload) | Default-deny network policy inside the sandbox; allow-list only the package registries the detected `ProjectProfile` actually needs (npm/PyPI/crates mirrors), nothing else |
| Resource exhaustion (fork bombs, infinite loops in a "fix") | Hard CPU/memory/PID limits and execution timeouts per sandbox, enforced at the microVM level, not just in-process |
| Supply-chain risk in the agent's own tool dependencies | Pinned, hash-verified dependencies for the agent platform itself; separate, more trusted build pipeline than the thing reviewing untrusted code |
| Persistent cross-job state leakage | One ephemeral sandbox per job, destroyed after; no shared volumes, no warm-pool reuse of a sandbox across two different repos |

### 7.2 Isolation boundary, concretely

Every `apply_in_sandbox → run_tests` step happens inside a freshly provisioned microVM:

- Fresh checkout, fresh container image matched to the `ProjectProfile` (e.g. a Python 3.12 image with the repo's exact lockfile installed).
- No mounted credentials except the package-registry allow-list above.
- Killed and destroyed on completion or timeout — never reused.
- All file writes from the patch applier are diffed and logged before they're allowed to leave the sandbox as a `Proposal`; the agent never has unmediated write access to the real repo. The *only* path from sandbox to real repo is the explicit `commit_low_risk` node, using a token scoped to that one PR's branch.

---

## 8. Human-in-the-loop & escalation UX

### 8.1 GitHub surface

- **Low-risk, auto-committed:** a commit on the PR branch authored by the bot, with a Check Run summarizing what changed and why (linking the `RiskAssessment.reasons`), plus a PR comment with the diff and rationale. Always reversible — it's a normal git commit, not a force-push, and it's clearly attributed.
- **Escalated:** no write access used. A PR review comment with the proposed diff as a suggestion block, the finding's severity, and the specific risk signals that triggered escalation. A Check Run is opened in `neutral` state — not failing the build, but visibly flagging "agent found something, needs a look."
- **Audit visibility:** every decision, auto-commit or escalate, links to a permalink summary (hosted by the control plane) showing the full `AgentState` trace for that job — static analysis findings, the proposal, the sandboxed test run output, and the exact risk signal breakdown.

### 8.2 CLI surface

Same engine, synchronous: findings and proposed diffs print to the terminal; auto-apply only happens with an explicit local flag (e.g. `--apply` or `--apply-safe`), and even then only for findings under the same risk threshold — local autonomy doesn't get a different (looser) bar than the GitHub surface.

---

## 9. Data model & persistence

### 9.1 Core schema (Postgres)

```sql
create table installations (
    id              uuid primary key default gen_random_uuid(),
    github_install_id bigint unique not null,
    org_or_user     text not null,
    autonomy_mode   text not null default 'shadow',  -- shadow | suggest_only | gated_auto | full_auto
    auto_commit_threshold double precision not null default 0.15,
    created_at      timestamptz not null default now()
);

create table runs (
    id              uuid primary key default gen_random_uuid(),
    installation_id uuid references installations(id),
    repo            text not null,
    pr_number       int,                 -- null for CLI-originated runs
    surface         text not null,       -- 'github_app' | 'cli'
    status          text not null,       -- running | awaiting_human | completed | failed
    profile         jsonb not null,      -- ProjectProfile snapshot
    started_at      timestamptz not null default now(),
    completed_at    timestamptz
);

create table findings (
    id              uuid primary key default gen_random_uuid(),
    run_id          uuid references runs(id),
    tool            text not null,
    file            text not null,
    severity        text not null,
    category        text not null,
    description     text not null
);

create table proposals (
    id              uuid primary key default gen_random_uuid(),
    finding_id      uuid references findings(id),
    diff            text not null,
    rationale       text not null,
    model_confidence double precision,
    test_passed     boolean,
    test_output_excerpt text,
    risk_score      double precision,
    risk_signals    jsonb,
    decision        text,                -- auto_commit | escalate
    committed_sha   text,                -- set only if auto_commit
    created_at      timestamptz not null default now()
);

create table audit_log (
    id              uuid primary key default gen_random_uuid(),
    run_id          uuid references runs(id),
    event_type      text not null,       -- state_transition | tool_call | decision | commit
    payload         jsonb not null,
    occurred_at     timestamptz not null default now()
);
```

`audit_log` is append-only and is the canonical source of truth for "why did the bot commit this" — required both for debugging reverts and, realistically, for any customer who asks "what exactly did this thing do to our codebase."

### 9.2 Optional RAG layer

For cross-file context (does this function get called elsewhere in a way that changes the risk of a fix?), an embedding index over the repo, refreshed incrementally on each run, backed by a vector store (pgvector is sufficient at this scale — no need for a dedicated vector DB unless repo sizes get into the hundreds of thousands of files). This is a v1.x enhancement, not required for the initial autonomous-low-risk-fixes scope.

---

## 10. Observability & auditability

- **Tracing:** OpenTelemetry spans around every LangGraph node and tool call, exported to a trace backend (LangSmith works well specifically for the LLM-reasoning spans; standard OTel collector for the rest) — you want to see "where did this job spend its 90 seconds" and "what exactly did the plan node see when it drafted this proposal."
- **Structured logs:** every tool call logs inputs/outputs (redacted of any secrets) keyed by `run_id`.
- **Metrics to dashboard from day one:** false-positive rate, revert rate, escalation precision, p50/p95 job latency, sandbox failure rate, per-repo `auto_commit_threshold` drift over time.
- **Audit trail:** the `audit_log` table from §9 is the compliance-grade record — every autonomous commit must be traceable to the exact findings, proposal, test output, and risk signals that produced it, indefinitely, not just in a 30-day log retention window.

---

## 11. Multi-tenancy & secrets

- **Per-installation isolation:** each GitHub App installation gets its own scoped, short-lived installation access token (GitHub's standard model) — never a long-lived PAT, never a token broader than the installing org.
- **No cross-tenant context:** a sandbox provisioned for tenant A's job never has access to tenant B's cached data, embeddings, or credentials — separate ephemeral workspace per job, full stop, no shared warm pool across tenants even for performance.
- **Secret rotation:** installation tokens refreshed on GitHub's standard short TTL; package-registry allow-list credentials (if any are needed, e.g. private registries) stored in a secrets manager and injected just-in-time, never baked into images.
- **Autonomy mode is per-installation, not global** (see `installations.autonomy_mode` in §9) — a customer can run in `suggest_only` indefinitely if they never want auto-commits, regardless of the platform default.

---

## 12. Evaluation & testing strategy for the agent itself

Testing an autonomous agent needs a different harness than testing a normal service — you're not just checking "does the code run," you're checking "does the agent make the right call under risk."

- **Golden repo suite:** a curated set of repos (forks of real OSS projects) with seeded bugs of known severity and known correct fixes. CI runs the full pipeline against these on every agent-platform change and checks both fix quality *and* risk-decision correctness (did it correctly auto-commit the trivial ones and escalate the seeded "looks risky" ones?).
- **Regression harness:** every escalation a human overturns ("actually this was fine, you should have auto-committed it" or vice versa) becomes a new eval case — the eval set grows from real production disagreement, not just synthetic bugs.
- **Shadow mode rollout per installation:** new installations (and any meaningfully changed risk model) start in shadow mode — the agent makes its decision, logs it, but never actually commits or comments — for a fixed window, so you can measure real-world precision/recall against what a human reviewer would have wanted before trusting it with write access.
- **Canary repos:** internal/dogfood repos get any new agent-platform version first, with tighter monitoring, before it reaches the general installation base.

---

## 13. CI/CD for the agent platform itself

This is a normal software delivery pipeline — worth stating explicitly because "the product does autonomous deploys to other people's code" doesn't mean *this platform's own* releases should be autonomous:

- Standard CI: lint, unit tests, the golden-repo eval suite (§12) as a required gate.
- Staged rollout: canary repos → small % of installations → full fleet, with automatic rollback on regression in revert rate or false-positive rate.
- Schema migrations for the Postgres state store run through standard migration tooling, backward-compatible by default (the checkpointer needs old in-flight jobs to still resume correctly across a deploy).
- Sandbox base images (per language/framework) are built and scanned on their own pipeline, version-pinned, independent of the agent platform's release cadence.

---

## 14. Cost & scaling considerations

- **Execution plane is the dominant cost driver**, not the control plane — sandboxed test runs (especially for larger test suites) are the expensive part. KEDA scaling on queue depth, plus a hard concurrency cap per installation, prevents one noisy customer from starving everyone else's queue.
- **LLM call cost:** the plan node is the main LLM cost center; static analysis tools are non-LLM and cheap. Batching findings into fewer, well-scoped plan calls (rather than one LLM call per tiny finding) keeps this bounded.
- **Sandbox provisioning latency** is the main contributor to job latency at the p95 — a warm pool of pre-provisioned (but not yet job-assigned) microVMs, recycled between jobs *with full re-image between uses*, trades a bit of idle compute cost for materially better p95 latency.

---

## 15. Rollout & maturity roadmap

| Phase | Scope | Infra | Autonomy |
|---|---|---|---|
| 0 — Internal dogfood | CLI only, single language (e.g. Python), canary repos | Local Docker Compose | Suggest-only |
| 1 — Shadow GitHub App | PR webhook integration live, but no comments/commits posted — pure measurement | Local Docker Compose + tunnel (§15.1) | Shadow |
| 2 — Suggest-only GA | PR comments + inline suggestions, no autonomous writes, multi-framework detection live | Local Docker Compose + tunnel | Suggest-only |
| 3 — Gated auto-apply | Bot pushes to a bot-owned branch, opens its own PR, human merges | Local Docker Compose + tunnel | Gated |
| 4 — Deployment | Cut over from local/tunnel to real cloud infra: serverless control plane, Kubernetes execution plane, managed queue, real microVM sandbox isolation | Cloud (§2) | Same as Phase 3, now at scale |
| 5 — Full autonomy (opt-in per installation) | Direct commits to PR branches for sub-threshold risk scores, per §5 | Cloud | Full, per-installation toggle |
| 6 — Adaptive thresholds | Per-repo `auto_commit_threshold` tuned automatically from observed revert rate, with a human-reviewable change log | Cloud | Full, self-tuning |

Each phase gate is a measured threshold on the metrics in §1.2, not a calendar date. Moving to Phase 5 (full autonomy) without first proving low revert rates in Phase 3 defeats the entire point of the risk model — and moving to Phase 4 (cloud deployment) before exhausting what local dogfooding can teach you just means paying cloud bills to debug things Docker Compose would have caught for free.

### 15.1 Local development environment (Phases 0–3)

Phases 0–3 need zero cloud spend. The whole stack runs as a Docker Compose project on a dev box, with a tunnel exposing the webhook receiver to GitHub:

```yaml
# docker-compose.yml (illustrative)
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: agent
    volumes:
      - pgdata:/var/lib/postgresql/data

  redis:              # local stand-in for the production queue (SQS/Cloud Tasks) — same job contract
    image: redis:7

  control-plane:
    build: ./control-plane
    environment:
      DATABASE_URL: postgres://postgres@postgres/agent
      QUEUE_URL: redis://redis:6379
      GITHUB_APP_ID: ${GITHUB_APP_ID}
      GITHUB_PRIVATE_KEY: ${GITHUB_PRIVATE_KEY}
      GITHUB_WEBHOOK_SECRET: ${GITHUB_WEBHOOK_SECRET}
    ports:
      - "8080:8080"     # the tunnel forwards to this port
    depends_on: [postgres, redis]

  agent-worker:
    build: ./agent-worker
    environment:
      DATABASE_URL: postgres://postgres@postgres/agent
      QUEUE_URL: redis://redis:6379
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
    depends_on: [postgres, redis]
    # NOTE: plain container, not a microVM — see caveat below

volumes:
  pgdata:
```

**Exposing the webhook locally — pick one:**

- **ngrok** (`ngrok http 8080`) — fastest to set up, fine for solo dev sessions. Free tier gives a random URL that changes on every restart, so you re-register the GitHub App's webhook URL each session unless you pay for a reserved domain.
- **Cloudflare Tunnel** (`cloudflared tunnel --url http://localhost:8080`) — free persistent hostname if you already own a domain in Cloudflare, no URL rotation. Better choice once you're running real PR traffic across more than a single sitting, rather than a one-off demo.

Either way, the GitHub App's webhook URL points at the tunnel's public address, and the HMAC signature verification in `control-plane` runs exactly as it will in production (§2.1) — that's the one piece of the control plane that has to behave identically local and cloud, since it's the actual security boundary on inbound webhooks.

**Important caveat — this is not the production sandbox.** `agent-worker` here is a normal Docker container, not a gVisor/Firecracker microVM. That's fine for validating agent logic, the risk model, and the LangGraph graph itself — it is *not* a substitute for the isolation guarantees in §7. Phases 0–3 should only run against trusted canary/dogfood repos, never against arbitrary public PRs, until Phase 4 brings real sandbox isolation online.

### 15.2 Phase 4 cutover

Moving from local to cloud is an infrastructure swap, not a rewrite — the control plane and agent worker are the same codebases, retargeted at different infra:

| Local (Phases 0–3) | Cloud (Phase 4+) |
|---|---|
| Docker container (`agent-worker`) | gVisor/Firecracker microVM per job |
| Redis as queue | SQS / Cloud Tasks / Pub/Sub |
| ngrok / Cloudflare Tunnel | Real public endpoint behind API Gateway |
| `.env` file secrets | Secrets manager, just-in-time injection |
| Single Postgres container | Managed Postgres (same schema, §9) |
| Manual restart on crash | KEDA-driven autoscaling, automatic rescheduling |

Because the job contract (`ReviewJob` in, a commit or an escalation out) doesn't change between the two environments, this cutover is infra work, not agent-logic work — which is exactly why it's worth spending Phases 0–3 proving out the autonomy logic cheaply before paying for real sandbox infrastructure.

---

## 16. Tech stack summary

| Layer | Recommendation |
|---|---|
| Agent framework | LangGraph (plan-and-execute), Postgres-backed checkpointer |
| Control plane compute | Cloud Run / Lambda + API Gateway |
| Queue | SQS / Cloud Tasks / Pub/Sub |
| Execution plane compute | Kubernetes + KEDA autoscaling |
| Sandbox isolation | gVisor or Firecracker microVMs |
| Primary datastore | Postgres (runs, findings, proposals, audit log) |
| Optional vector store | pgvector |
| Tracing | OpenTelemetry + LangSmith (for LLM-specific spans) |
| Secrets | Cloud-native secrets manager (Secrets Manager / Secret Manager / Vault) |
| GitHub integration | GitHub App (Checks API, installation tokens, PR comments/suggestions) |
| Static analysis | Per-language native linters (wrapped, not replaced) + Semgrep for security patterns |

---

## 17. Key risks & open questions

- **Calibration of `model_confidence`** is the riskiest unsolved piece — raw LLM-reported confidence is well known to be poorly calibrated; this needs a held-out eval set and probably a small calibration model (e.g. isotonic regression over historical outcomes) before it's trustworthy as a risk signal, not just used raw.
- **Monorepo blast-radius computation** is harder than single-repo — "which packages does this change affect" needs real dependency-graph analysis, not just changed-file lists, or risk scores will systematically under-count blast radius in monorepos.
- **Prompt-injection surface area is large** because the entire input is untrusted code and text (comments, docstrings, README, commit messages) — this needs ongoing red-teaming, not a one-time mitigation; treat §7.1's injection mitigation as a living control, not a checkbox.
- **Defining "protected path" per repo** (which files always escalate regardless of score) needs a sane default plus easy customer configuration — getting this wrong in either direction (too narrow = real incidents, too broad = autonomy never pays off) is a product risk as much as a technical one.
