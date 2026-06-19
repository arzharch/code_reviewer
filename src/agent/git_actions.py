import subprocess
import requests
from typing import List, Optional
from src.agent.state import RiskAssessment, Proposal
from src.common.config import settings

class GitActionsService:
    """
    Handles interacting with the source control system (GitHub).
    """

    @staticmethod
    def post_pr_comment(repo_url: str, pr_number: int, comment: str) -> bool:
        """
        Posts a comment to a GitHub PR via the API.
        """
        token = settings.github_token.get_secret_value() if settings.github_token else None
        if not token:
            print("No GitHub token configured; skipping PR comment.")
            return False
            
        # Very naive repo extraction from github url
        # e.g. https://github.com/owner/repo
        repo_path = repo_url.replace("https://github.com/", "").replace(".git", "")
        
        url = f"https://api.github.com/repos/{repo_path}/issues/{pr_number}/comments"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json"
        }
        
        response = requests.post(url, json={"body": comment}, headers=headers)
        if response.status_code == 201:
            return True
        else:
            print(f"Failed to post comment: {response.text}")
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
            messages = ["Autonomous Code Review Fixes\\n"]
            for p in proposals:
                messages.append(f"- Fixed {p.finding_id}: {p.description}")
            commit_message = "\\n".join(messages)
            
            subprocess.run(["git", "commit", "-m", commit_message], cwd=repo_path, check=True, capture_output=True)
            
            # Push (assuming remote is already configured and authenticated via SSH/Token)
            subprocess.run(["git", "push", "origin", branch_name], cwd=repo_path, check=True, capture_output=True)
            
            return True
        except subprocess.CalledProcessError as e:
            print(f"Failed to commit/push: {e.stderr}")
            return False
