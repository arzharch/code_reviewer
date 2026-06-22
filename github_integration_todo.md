# GitHub Connection Readiness - TODOs

The current codebase has critical architectural flaws regarding GitHub webhook integration that must be resolved before it can be deployed or used with a live GitHub App.

## 1. Remote vs. Local Repository Flaw (Critical)
When a GitHub webhook hits `src/control_plane/main.py`, it passes the remote `clone_url` (e.g., `https://github.com/owner/repo.git`) to the `ReviewJob` model.
However, the entire Execution Plane (e.g., `SandboxRuntime`, `IngestionService`) expects `repo` to be a **local filesystem path**:
- `IngestionService.get_local_diff` runs `subprocess.run(["git", "diff"], cwd=repo_path)`. This fails with a remote URL.
- `SandboxRuntime.setup` tries to copy the directory using `os.listdir(self.original_repo_path)`. This will crash with a `FileNotFoundError`.

**Action Item:** Implement a secure cloning mechanism (e.g., in a temporary directory) when a webhook is received, and pass that local path to the LangGraph workflow instead of the GitHub URL.

## 2. GitHub App Authentication
The `.env.example` provides variables for a GitHub App (`GITHUB_APP_ID`, `GITHUB_PRIVATE_KEY`), but `src/agent/git_actions.py` expects a static `settings.github_token`.
- GitHub Apps authenticate by generating a short-lived JSON Web Token (JWT) using their private key, which is then exchanged for an installation access token.
- The current code lacks the logic to perform this exchange, meaning API requests (to post PR comments or push code) will fail with `401 Unauthorized`.

**Action Item:** Update `GitActionsService` (or introduce a new auth service) to dynamically exchange the `GITHUB_PRIVATE_KEY` for an installation token before calling the GitHub API.
