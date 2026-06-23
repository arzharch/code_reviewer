import subprocess
import requests
import tempfile
import os
from typing import List, Optional, Tuple
from src.agent.state import Proposal
from src.common.config import settings

class GitActionsService:
    """
    Handles interacting with the source control system (GitHub).
    """

    @staticmethod
    def get_token_string(installation_id: Optional[int] = None) -> str:
        """Helper to get the raw token string for git clone / requests."""
        if installation_id and settings.github_app_id and settings.github_private_key:
            import jwt
            import time
            
            payload = {
                "iat": int(time.time()) - 60,
                "exp": int(time.time()) + (10 * 60),
                "iss": str(settings.github_app_id)
            }
            
            encoded_jwt = jwt.encode(
                payload, 
                settings.github_private_key.get_secret_value(), 
                algorithm="RS256"
            )
            
            url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
            headers = {
                "Authorization": f"Bearer {encoded_jwt}",
                "Accept": "application/vnd.github.v3+json"
            }
            
            resp = requests.post(url, headers=headers)
            resp.raise_for_status()
            return resp.json()["token"]
            
        return settings.github_token.get_secret_value() if settings.github_token else ""

    @staticmethod
    def clone_and_prep_pr(repo_full_name: str, pr_number: int, clone_url: str, installation_id: Optional[int] = None) -> Tuple[str, str, str, str]:
        """
        Clones the PR head branch into a temporary directory and fetches the PR diff.
        Returns: (temp_dir_path, diff_content, head_sha, base_sha)
        """
        token = GitActionsService.get_token_string(installation_id)
        
        # 1. Fetch Diff via GitHub API manually to avoid PyGithub lazy loading bugs
        api_url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}"
        
        # Get the unified diff
        diff_headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.v3.diff"} if token else {"Accept": "application/vnd.github.v3.diff"}
        diff_resp = requests.get(api_url, headers=diff_headers)
        diff_resp.raise_for_status()
        diff_content = diff_resp.text
        
        # Get the PR JSON to extract shas
        json_headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.v3+json"} if token else {"Accept": "application/vnd.github.v3+json"}
        pr_resp = requests.get(api_url, headers=json_headers)
        pr_resp.raise_for_status()
        pr_data = pr_resp.json()
        
        head_sha = pr_data["head"]["sha"]
        head_ref = pr_data["head"]["ref"]
        base_sha = pr_data["base"]["sha"]
        
        # 2. Clone Repository securely
        auth_clone_url = clone_url.replace("https://", f"https://x-access-token:{token}@") if token else clone_url
        
        temp_dir = tempfile.mkdtemp(prefix="agent_workspace_")
        
        print(f"Cloning {repo_full_name} branch {head_ref} into {temp_dir}...")
        subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", head_ref, auth_clone_url, temp_dir],
            check=True, capture_output=True
        )
        
        return temp_dir, diff_content, head_sha, base_sha

    @staticmethod
    def post_pr_comment(repo_full_name: str, pr_number: int, comment: str, installation_id: Optional[int] = None) -> bool:
        """
        Posts a comment to a GitHub PR via the raw GitHub API.
        """
        try:
            token = GitActionsService.get_token_string(installation_id)
            if not token:
                print("No token available to post comment.")
                return False
                
            url = f"https://api.github.com/repos/{repo_full_name}/issues/{pr_number}/comments"
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3+json"
            }
            resp = requests.post(url, headers=headers, json={"body": comment})
            resp.raise_for_status()
            return True
        except Exception as e:
            print(f"Failed to post comment: {e}")
            return False

    @staticmethod
    def commit_and_push(repo_path: str, proposals: List[Proposal], branch_name: str) -> bool:
        """
        Commits applied patches directly to the repo and pushes to remote.
        This assumes the patches have already been applied to the working directory.
        """
        try:
            # Stage everything
            subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
            
            # Formulate commit message
            messages = ["Autonomous Code Review Fixes\n"]
            for p in proposals:
                messages.append(f"- Fixed {p.finding_id}: {p.description}")
            commit_message = "\n".join(messages)
            
            # Use basic config for commit if missing
            subprocess.run(["git", "config", "user.name", "Autonomous Reviewer"], cwd=repo_path, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "bot@autonomous-reviewer.local"], cwd=repo_path, check=True, capture_output=True)
            
            subprocess.run(["git", "commit", "-m", commit_message], cwd=repo_path, check=True, capture_output=True)
            
            # Push (assuming remote is already configured and authenticated via SSH/Token)
            subprocess.run(["git", "push", "origin", branch_name], cwd=repo_path, check=True, capture_output=True)
            
            return True
        except subprocess.CalledProcessError as e:
            print(f"Failed to commit/push: {e.stderr}")
            return False
