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
    
    # GitHub App
    github_app_id: Optional[str] = None
    github_private_key: Optional[SecretStr] = None
    github_webhook_secret: Optional[SecretStr] = None
    github_token: Optional[SecretStr] = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

# Global settings instance
settings = Settings()
