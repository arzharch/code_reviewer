import subprocess
import json
import os
from typing import List, Optional
from pathlib import Path
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from src.agent.state import Finding
from src.common.config import settings

class StaticAnalysisService:
    """
    Wraps local static analysis tools (Ruff, MyPy, Bandit) to generate Findings.
    """

    def __init__(self, repo_path: str, diff_files: Optional[List[str]] = None):
        self.repo_path = repo_path
        self.diff_files = diff_files or []

    def run_ruff(self, diff_files: Optional[List[str]] = None) -> List[Finding]:
        """
        Runs Ruff (linter) on the given files and parses the JSON output into Findings.
        """
        target_files = diff_files or self.diff_files
        cmd = ["ruff", "check", "--output-format", "json"]
        if target_files:
            cmd.extend(target_files)
        else:
            cmd.append(".")

        try:
            result = subprocess.run(cmd, cwd=self.repo_path, capture_output=True, text=True)
        except FileNotFoundError:
            print("Ruff not found. Skipping static analysis with ruff.")
            return []
        
        findings = []
        
        try:
            # Ruff might exit with 1 if there are violations, so we ignore returncode
            # if we have valid JSON stdout
            if result.stdout.strip():
                issues = json.loads(result.stdout)
                for issue in issues:
                    file_path = issue.get("filename", "")
                    # Convert absolute path to relative if possible
                    if file_path.startswith(self.repo_path):
                        file_path = os.path.relpath(file_path, self.repo_path)
                        
                    findings.append(Finding(
                        id=f"ruff_{issue.get('code', 'unknown')}_{hash(file_path + str(issue.get('location', {}).get('row')))}",
                        tool="ruff",
                        file=file_path,
                        line_range=(issue.get("location", {}).get("row", 0), issue.get("end_location", {}).get("row", 0)),
                        severity="warning", # Ruff defaults
                        category="lint",
                        description=f"{issue.get('code', '')}: {issue.get('message', '')}"
                    ))
        except json.JSONDecodeError:
            pass # Handle parsing error gracefully
            
        return findings

    def run_bandit(self, diff_files: Optional[List[str]] = None) -> List[Finding]:
        """
        Runs Bandit (security scanner) and parses output.
        """
        cmd = ["bandit", "-f", "json", "-r", "."]
        # In a real setup, we would filter to diff_files if provided
        
        try:
            result = subprocess.run(cmd, cwd=self.repo_path, capture_output=True, text=True)
        except FileNotFoundError:
            print("Bandit not found. Skipping security scan.")
            return []
            
        findings = []
        
        try:
            if result.stdout.strip():
                data = json.loads(result.stdout)
                for issue in data.get("results", []):
                    findings.append(Finding(
                        id=f"bandit_{issue.get('test_id', 'unknown')}_{hash(issue.get('filename') + str(issue.get('line_number')))}",
                        tool="bandit",
                        file=issue.get("filename", "").replace(self.repo_path + "/", ""),
                        line_range=(issue.get("line_number", 0), issue.get("line_number", 0)),
                        severity=issue.get("issue_severity", "LOW").lower(),
                        category="security",
                        description=issue.get("issue_text", "")
                    ))
        except json.JSONDecodeError:
            pass

        return findings

    def run_mypy(self, diff_files: Optional[List[str]] = None) -> List[Finding]:
        """
        Runs MyPy (type checker) and parses output.
        """
        # A simple non-json output parsing for mypy
        cmd = ["mypy", ".", "--show-error-codes"]
        try:
            result = subprocess.run(cmd, cwd=self.repo_path, capture_output=True, text=True)
        except FileNotFoundError:
            print("MyPy not found. Skipping type check.")
            return []
            
        findings = []
        
        if result.stdout.strip():
            for line in result.stdout.splitlines():
                parts = line.split(":")
                if len(parts) >= 4 and "error" in line:
                    file_path = parts[0].strip()
                    try:
                        line_num = int(parts[1].strip())
                        description = ":".join(parts[3:]).strip()
                        findings.append(Finding(
                            id=f"mypy_err_{hash(file_path + str(line_num))}",
                            tool="mypy",
                            file=file_path,
                            line_range=(line_num, line_num),
                            severity="error",
                            category="type",
                            description=description
                        ))
                    except ValueError:
                        pass
        return findings

    def run_all(self, diff_files: Optional[List[str]] = None) -> List[Finding]:
        """
        Aggregates findings from all configured tools.
        """
        findings = []
        findings.extend(self.run_ruff(diff_files))
        findings.extend(self.run_bandit(diff_files))
        findings.extend(self.run_mypy(diff_files))
        return findings

class LLMAnalysisService:
    """
    Uses an LLM to read critical files and provide deep semantic understanding
    and identify architectural anti-patterns that static linters miss.
    """
    
    def __init__(self, repo_path: str, diff_files: Optional[List[str]] = None, token_limit: Optional[int] = None):
        self.repo_path = repo_path
        self.diff_files = diff_files or []
        self.token_limit = token_limit
        self.original_token_limit = token_limit
        self.llm = ChatOpenAI(
            model="gpt-4o-mini",
            api_key=settings.openai_api_key.get_secret_value() if settings.openai_api_key else None, # type: ignore
            temperature=0
        )

    def run_semantic_analysis(self) -> List[Finding]:
        if not self.diff_files:
            return []

        prompt = ChatPromptTemplate.from_messages([
            ("system", "You are an expert software architect. Analyze the following code snippet for deep semantic bugs, security flaws, or architectural anti-patterns. If you find any, return a JSON array of objects with 'description', 'severity' (warning/error/critical), and 'line_number'. If none, return an empty array []"),
            ("human", "File: {file_path}\n\nCode:\n{code}")
        ])

        findings = []
        total_tokens_used = 0

        for file_path in self.diff_files:
            full_path = Path(self.repo_path) / file_path
            if full_path.exists() and full_path.is_file() and full_path.suffix in [".py", ".ts", ".js", ".go", ".rs"]:
                try:
                    code_content = full_path.read_text(encoding="utf-8")
                    # Naive chunking or limit for demo purposes
                    if len(code_content) > 10000:
                        code_content = code_content[:10000]
                        
                    estimated_tokens = len(code_content) // 4
                    if self.token_limit and self.original_token_limit and (total_tokens_used + estimated_tokens) > self.token_limit:
                        ans = input(f"\n[LLM Limit] Token limit of {self.token_limit} reached (used ~{total_tokens_used}). Continue scanning for another {self.original_token_limit} tokens? (y/n): ")
                        if ans.strip().lower() != 'y':
                            print(f"Stopping LLM analysis. Returning {len(findings)} findings gathered so far.")
                            break
                        else:
                            # Bump limit by the original amount
                            self.token_limit += self.original_token_limit
                        
                    chain = prompt | self.llm
                    response = chain.invoke({"file_path": file_path, "code": code_content})
                    total_tokens_used += estimated_tokens
                    
                    try:
                        # Very naive parse of the LLM response
                        content = response.content.strip()
                        if content.startswith("```json"):
                            content = content[7:-3].strip()
                        elif content.startswith("```"):
                            content = content[3:-3].strip()
                        
                        data = json.loads(content)
                        for issue in data:
                            findings.append(Finding(
                                id=f"llm_ast_{hash(file_path + str(issue.get('line_number')))}",
                                tool="llm_semantic_analysis",
                                file=file_path,
                                line_range=(issue.get("line_number", 0), issue.get("line_number", 0)),
                                severity=issue.get("severity", "warning"),
                                category="architecture",
                                description=issue.get("description", "")
                            ))
                    except json.JSONDecodeError:
                        pass
                except Exception as e:
                    print(f"Failed semantic analysis for {file_path}: {e}")
                    
        return findings
