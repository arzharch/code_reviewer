import subprocess
import json
import os
from typing import List, Tuple
from pathlib import Path
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from src.agent.state import Finding
from src.common.config import settings

class StaticAnalysisService:
    """
    Wraps local static analysis tools (Ruff, MyPy, Bandit) to generate Findings.
    """

    @staticmethod
    def run_ruff(repo_path: str, diff_files: List[str] = None) -> List[Finding]:
        """
        Runs Ruff (linter) on the given files and parses the JSON output into Findings.
        """
        cmd = ["ruff", "check", "--output-format", "json"]
        if diff_files:
            cmd.extend(diff_files)
        else:
            cmd.append(".")

        result = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True)
        findings = []
        
        try:
            # Ruff might exit with 1 if there are violations, so we ignore returncode
            # if we have valid JSON stdout
            if result.stdout.strip():
                issues = json.loads(result.stdout)
                for issue in issues:
                    file_path = issue.get("filename", "")
                    # Convert absolute path to relative if possible
                    if file_path.startswith(repo_path):
                        file_path = os.path.relpath(file_path, repo_path)
                        
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

    @staticmethod
    def run_bandit(repo_path: str, diff_files: List[str] = None) -> List[Finding]:
        """
        Runs Bandit (security scanner) and parses output.
        """
        cmd = ["bandit", "-f", "json", "-r", "."]
        # In a real setup, we would filter to diff_files if provided
        
        result = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True)
        findings = []
        
        try:
            if result.stdout.strip():
                data = json.loads(result.stdout)
                for issue in data.get("results", []):
                    findings.append(Finding(
                        id=f"bandit_{issue.get('test_id', 'unknown')}_{hash(issue.get('filename') + str(issue.get('line_number')))}",
                        tool="bandit",
                        file=issue.get("filename", "").replace(repo_path + "/", ""),
                        line_range=(issue.get("line_number", 0), issue.get("line_number", 0)),
                        severity=issue.get("issue_severity", "LOW").lower(),
                        category="security",
                        description=issue.get("issue_text", "")
                    ))
        except json.JSONDecodeError:
            pass

        return findings

    @staticmethod
    def run_mypy(repo_path: str, diff_files: List[str] = None) -> List[Finding]:
        """
        Runs MyPy (type checker) and parses output.
        """
        # A simple non-json output parsing for mypy
        cmd = ["mypy", ".", "--show-error-codes"]
        result = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True)
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

    @staticmethod
    def run_all(repo_path: str, diff_files: List[str] = None) -> List[Finding]:
        """
        Aggregates findings from all configured tools.
        """
        findings = []
        findings.extend(StaticAnalysisService.run_ruff(repo_path, diff_files))
        findings.extend(StaticAnalysisService.run_bandit(repo_path, diff_files))
        findings.extend(StaticAnalysisService.run_mypy(repo_path, diff_files))
        return findings

class LLMAnalysisService:
    """
    Uses an LLM to read critical files and provide deep semantic understanding
    and identify architectural anti-patterns that static linters miss.
    """
    
    @staticmethod
    def run_semantic_analysis(repo_path: str, diff_files: List[str] = None) -> List[Finding]:
        api_key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
        if not api_key or not diff_files:
            return []

        llm = ChatOpenAI(
            api_key=api_key,
            model=settings.openai_model,
            temperature=0.0
        )

        prompt = ChatPromptTemplate.from_messages([
            ("system", "You are an expert software architect. Analyze the following code snippet for deep semantic bugs, security flaws, or architectural anti-patterns. If you find any, return a JSON array of objects with 'description', 'severity' (warning/error/critical), and 'line_number'. If none, return an empty array []"),
            ("human", "File: {file_path}\\n\\nCode:\\n{code}")
        ])

        findings = []
        for file_path in diff_files:
            full_path = Path(repo_path) / file_path
            if full_path.exists() and full_path.is_file() and full_path.suffix in [".py", ".ts", ".js", ".go", ".rs"]:
                try:
                    code_content = full_path.read_text(encoding="utf-8")
                    # Naive chunking or limit for demo purposes
                    if len(code_content) > 10000:
                        code_content = code_content[:10000]
                        
                    chain = prompt | llm
                    response = chain.invoke({"file_path": file_path, "code": code_content})
                    
                    try:
                        # Very naive parse of the LLM response
                        data = json.loads(response.content)
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
