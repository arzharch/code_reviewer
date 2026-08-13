import subprocess
import shlex
import tempfile
import os
import shutil
from typing import Dict, List, Optional
from pydantic import BaseModel
from src.agent.state import Proposal, TestResult, ProjectProfile
from src.common.config import settings

class SandboxExecutionError(Exception):
    pass

# Environment variables that are safe to expose to untrusted code from a PR.
# Everything else (API keys, GitHub App private key, DB/Redis DSNs) is stripped.
_ENV_ALLOWLIST = (
    "PATH",
    "LANG",
    "LC_ALL",
    "TZ",
    # Windows needs these or CreateProcess / socket init fails
    "SYSTEMROOT",
    "SystemRoot",
    "COMSPEC",
    "ComSpec",
    "TEMP",
    "TMP",
    "PATHEXT",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
)

def build_sandbox_env(home: str) -> dict:
    """
    Builds a minimal environment for subprocesses that execute untrusted
    repository code. The parent process holds OPENAI_API_KEY,
    GITHUB_PRIVATE_KEY and DB credentials; none of them are inherited.
    """
    env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
    env["HOME"] = home
    env["USERPROFILE"] = home
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Never let git block on a credential/auth prompt inside the sandbox.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["GCM_INTERACTIVE"] = "never"
    return env

class ApplyResult(BaseModel):
    """Outcome of applying a batch of proposals to one working tree."""
    applied: List[str] = []            # finding_ids that landed
    failed: Dict[str, str] = {}        # finding_id -> reason

    @property
    def all_applied(self) -> bool:
        return not self.failed


def normalize_diff(raw: str) -> Optional[str]:
    """
    Turns an LLM-authored diff into something `git apply` can consume, or
    returns None if it is not a unified diff at all.

    The model routinely wraps diffs in markdown fences, and sometimes emits
    prose 'before/after' snippets instead of a patch. Those must be rejected
    here rather than discovered at apply time.
    """
    if not raw:
        return None

    text = raw.strip()

    # Strip a surrounding markdown fence (```diff ... ``` / ```patch ... ```)
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]
        while lines and lines[-1].strip() == "":
            lines.pop()
        if lines and lines[-1].strip().startswith("```"):
            lines.pop()
        text = "\n".join(lines).strip()

    # A usable patch needs file headers and at least one hunk.
    has_headers = "--- " in text and "+++ " in text
    has_hunk = "@@" in text
    if not (has_headers and has_hunk):
        return None

    return text.replace("\r\n", "\n").rstrip("\n") + "\n"


def apply_patches(repo_path: str, proposals: List[Proposal], env: Optional[dict] = None) -> ApplyResult:
    """
    Applies proposals to `repo_path` one at a time, reporting per-proposal
    outcomes. Patch files are written outside the tree so they can never be
    picked up by a later `git add .`.
    """
    env = env or build_sandbox_env(repo_path)
    result = ApplyResult()

    # `git apply` needs a repository. A real clone already is one; a plain
    # copy is not, so bootstrap a throwaway one.
    if not os.path.isdir(os.path.join(repo_path, ".git")):
        subprocess.run(["git", "init"], cwd=repo_path, capture_output=True, env=env)
        subprocess.run(["git", "add", "."], cwd=repo_path, capture_output=True, env=env)

    for proposal in proposals:
        diff = normalize_diff(proposal.diff)
        if diff is None:
            result.failed[proposal.finding_id] = "proposal did not contain a unified diff"
            continue

        fd, patch_path = tempfile.mkstemp(prefix="proposal_", suffix=".diff")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(diff)

            last_error = ""
            for strip_level in ("-p1", "-p0"):
                proc = subprocess.run(
                    ["git", "apply", "--recount", strip_level, patch_path],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    env=env
                )
                if proc.returncode == 0:
                    result.applied.append(proposal.finding_id)
                    break
                last_error = (proc.stderr or proc.stdout).strip()
            else:
                result.failed[proposal.finding_id] = last_error or "git apply failed"
        except Exception as e:
            result.failed[proposal.finding_id] = f"error applying patch: {e}"
        finally:
            if os.path.exists(patch_path):
                os.remove(patch_path)

    return result


class SandboxRuntime:
    """
    Applies patches and runs a repository's test command off the main tree.

    Applies patches locally, then runs a repository's test command securely 
    inside a gVisor Docker container (`--runtime=runsc`).

    Real isolation (gVisor `runsc`) prevents untrusted test suites from breaking
    out to the host network or executing kernel exploits.
    """

    def __init__(self, original_repo_path: str):
        self.original_repo_path = original_repo_path
        self.sandbox_dir = tempfile.mkdtemp(prefix="agent_sandbox_")
        self.env = build_sandbox_env(self.sandbox_dir)

    def setup(self):
        """Copies the repository to the sandbox directory."""
        # Using shutil.copytree to copy contents. Ignoring .git might be necessary if it's huge,
        # but we need it for git apply sometimes.
        try:
            # We copy everything inside the repo to sandbox_dir
            for item in os.listdir(self.original_repo_path):
                s = os.path.join(self.original_repo_path, item)
                d = os.path.join(self.sandbox_dir, item)
                if os.path.isdir(s):
                    # Exclude venv and big directories to save time/space
                    if item not in [".venv", "node_modules", ".git", "venv", "__pycache__"]:
                        shutil.copytree(s, d, symlinks=False, ignore=None)
                else:
                    shutil.copy2(s, d)
        except Exception as e:
            self.teardown()
            raise SandboxExecutionError(f"Failed to setup sandbox: {e}")

    def teardown(self):
        """Cleans up the sandbox directory."""
        if os.path.exists(self.sandbox_dir):
            shutil.rmtree(self.sandbox_dir, ignore_errors=True)

    def apply_proposals(self, proposals: List[Proposal]) -> ApplyResult:
        """
        Applies each proposal's diff to the sandbox tree.
        Patches are independent: one bad diff no longer discards the rest.
        """
        return apply_patches(self.sandbox_dir, proposals, env=self.env)

    def run_tests(self, profile: ProjectProfile) -> TestResult:
        """
        Runs the project's test command in the sandbox.
        """
        cmd = profile.test_command
        if not cmd or cmd == "echo 'No test command detected'":
            return TestResult(
                passed=False,
                coverage_percent=0.0,
                output="No valid test command found in project profile."
            )

        try:
            # We map the sandbox_dir into the container at /workspace
            # and run the test_command safely inside.
            # Using --network=none to prevent untrusted code from making outbound calls.
            # Using --runtime=runsc for gVisor isolation (if available on the host).
            # If runsc isn't installed locally, you can remove --runtime=runsc for local dev.
            
            docker_cmd = [
                "docker", "run", "--rm",
                "--network=none",
                "--runtime=runsc",
                "-v", f"{self.sandbox_dir}:/workspace",
                "-w", "/workspace",
                "autonomous-sandbox:latest",
                "bash", "-c", cmd
            ]
            
            result = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                timeout=settings.test_timeout_seconds
            )
            
            passed = (result.returncode == 0)
            
            # Naive coverage parsing for Python/pytest-cov
            coverage = 0.0
            for line in result.stdout.splitlines():
                if "TOTAL" in line and "%" in line:
                    parts = line.split()
                    for p in parts:
                        if "%" in p:
                            try:
                                coverage = float(p.replace("%", ""))
                            except ValueError:
                                pass
                                
            return TestResult(
                passed=passed,
                coverage_percent=coverage,
                output=result.stdout + "\\n" + result.stderr
            )
            
        except subprocess.TimeoutExpired:
            return TestResult(
                passed=False,
                coverage_percent=0.0,
                output="Tests timed out after 300 seconds."
            )
        except Exception as e:
            return TestResult(
                passed=False,
                coverage_percent=0.0,
                output=f"Error running tests: {e}"
            )
