from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import SecretStr
from typing import Optional

class Settings(BaseSettings):
    """
    Application configuration managed by Pydantic.
    Loads variables from the environment and/or a .env file.
    """

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5433/agent_db"

    # Redis
    redis_url: str = "redis://localhost:6379"

    # OpenAI
    openai_api_key: Optional[SecretStr] = None
    openai_model: str = "gpt-4o"
    openai_analysis_model: str = "gpt-4o-mini"

    # GitHub App
    github_app_id: Optional[str] = None
    github_private_key: Optional[SecretStr] = None
    github_webhook_secret: Optional[SecretStr] = None
    github_token: Optional[SecretStr] = None

    # Agent behaviour
    # Risk score at or below which a proposal may be committed without a human.
    auto_commit_threshold: float = 0.15
    # Self-correction attempts per proposal inside the sandbox subgraph.
    max_repair_attempts: int = 2
    # How many times the whole plan may be revised after verification failures.
    max_replan_rounds: int = 1
    # Hard ceiling on estimated LLM tokens per review. Enforced without prompting.
    token_budget: int = 120_000
    # Wall-clock limit for the project's test command inside the sandbox.
    test_timeout_seconds: int = 300

    # Observability
    log_level: str = "INFO"
    log_format: str = "json"  # "json" for containers, "plain" for local CLI
    # Set LANGSMITH_API_KEY / LANGSMITH_TRACING in the environment to get
    # per-node LLM traces; LangChain picks them up automatically.
    langsmith_project: str = "autonomous-code-reviewer"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

# Global settings instance
settings = Settings()
