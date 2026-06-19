import subprocess
from pathlib import Path
from src.agent.state import ProjectProfile

class IngestionService:
    """
    Handles fetching diffs and detecting project parameters (language, framework, test commands).
    """

    @staticmethod
    def get_local_diff(repo_path: str, staged: bool = False) -> str:
        """
        Fetches the git diff from a local directory.
        """
        cmd = ["git", "diff"]
        if staged:
            cmd.append("--staged")
        
        result = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Git diff failed: {result.stderr}")
        return result.stdout

    @staticmethod
    def detect_project_profile(repo_path: str) -> ProjectProfile:
        """
        Detects the project's primary language, framework, and essential commands
        by looking at lockfiles and configurations.
        """
        base_dir = Path(repo_path)
        sources = []
        confidence = 0.0
        
        primary_language = "unknown"
        package_manager = "unknown"
        test_command = "echo 'No test command detected'"
        lint_command = None
        build_command = None
        framework = None

        # Python Detection
        if (base_dir / "pyproject.toml").exists() or (base_dir / "requirements.txt").exists():
            primary_language = "python"
            if (base_dir / "pyproject.toml").exists():
                sources.append("pyproject.toml")
                # Very basic package manager assumption, real implementation would parse the TOML
                if (base_dir / "poetry.lock").exists():
                    package_manager = "poetry"
                    test_command = "poetry run pytest"
                    sources.append("poetry.lock")
                    confidence += 0.4
                elif (base_dir / "uv.lock").exists():
                    package_manager = "uv"
                    test_command = "uv run pytest"
                    sources.append("uv.lock")
                    confidence += 0.4
                else:
                    package_manager = "pip"
                    test_command = "pytest"
                    confidence += 0.2
            
            # Framework detection
            if (base_dir / "manage.py").exists():
                framework = "django"
                test_command = f"{package_manager} run python manage.py test" if package_manager in ["poetry", "uv"] else "python manage.py test"
                sources.append("manage.py")
                confidence += 0.3
            elif (base_dir / "main.py").exists():
                # Checking for FastAPI roughly
                framework = "fastapi"
                sources.append("main.py")
                confidence += 0.2

            lint_command = "ruff check ."

        # Node.js Detection
        elif (base_dir / "package.json").exists():
            primary_language = "javascript/typescript"
            sources.append("package.json")
            if (base_dir / "package-lock.json").exists():
                package_manager = "npm"
                test_command = "npm test"
                sources.append("package-lock.json")
                confidence += 0.4
            elif (base_dir / "yarn.lock").exists():
                package_manager = "yarn"
                test_command = "yarn test"
                sources.append("yarn.lock")
                confidence += 0.4
            elif (base_dir / "pnpm-lock.yaml").exists():
                package_manager = "pnpm"
                test_command = "pnpm test"
                sources.append("pnpm-lock.yaml")
                confidence += 0.4

            lint_command = f"{package_manager} run lint"

        # Cap confidence
        confidence = min(1.0, confidence)

        return ProjectProfile(
            primary_language=primary_language,
            framework=framework,
            package_manager=package_manager,
            test_command=test_command,
            lint_command=lint_command,
            build_command=build_command,
            detection_confidence=confidence,
            detection_sources=sources
        )
