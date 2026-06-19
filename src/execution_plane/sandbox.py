import subprocess
import tempfile
import os
import shutil
from typing import List, Tuple, Optional
from src.agent.state import Proposal, TestResult, ProjectProfile
from src.common.config import settings

class SandboxExecutionError(Exception):
    pass

class SandboxRuntime:
    """
    Manages securely executing code, applying patches, and running tests.
    In a real production environment, this would spin up a gVisor container, 
    Firecracker microVM, or at minimum a Docker container.
    For this implementation, we simulate it by copying to a temp directory and running locally.
    """
    
    def __init__(self, original_repo_path: str):
        self.original_repo_path = original_repo_path
        self.sandbox_dir = tempfile.mkdtemp(prefix="agent_sandbox_")

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

    def apply_proposals(self, proposals: List[Proposal]) -> bool:
        """
        Iterates through proposals and applies the diffs/patches.
        Returns True if all applied successfully, False otherwise.
        """
        for proposal in proposals:
            if not proposal.diff:
                continue
                
            # Write diff to a temporary file
            patch_path = os.path.join(self.sandbox_dir, f"patch_{proposal.finding_id}.diff")
            with open(patch_path, "w", encoding="utf-8") as f:
                f.write(proposal.diff)

            # Apply patch using `git apply` or `patch`
            # Since we ignored .git to save time, `git apply` might fail if not a git repo,
            # so we use `patch -p1` or initialize a dummy git repo.
            try:
                # Initialize dummy git to use git apply, as it's more reliable for git diffs
                subprocess.run(["git", "init"], cwd=self.sandbox_dir, capture_output=True, check=True)
                subprocess.run(["git", "add", "."], cwd=self.sandbox_dir, capture_output=True)
                
                result = subprocess.run(
                    ["git", "apply", patch_path],
                    cwd=self.sandbox_dir,
                    capture_output=True,
                    text=True
                )
                if result.returncode != 0:
                    print(f"Failed to apply patch for {proposal.finding_id}: {result.stderr}")
                    return False
            except Exception as e:
                print(f"Error applying patch: {e}")
                return False
                
        return True

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
            # We use shell=True because test_command might be "uv run pytest" etc.
            result = subprocess.run(
                cmd,
                cwd=self.sandbox_dir,
                shell=True,
                capture_output=True,
                text=True,
                timeout=300 # 5 min timeout
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
