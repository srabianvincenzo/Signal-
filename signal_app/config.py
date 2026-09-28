"""Runtime configuration, loaded from environment variables and an optional .env file.

API keys live only in the environment. Nothing in the codebase hardcodes a credential.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///signal.db"
    github_token: str | None = None
    signal_user_agent: str = "signal-research"


@lru_cache
def get_settings() -> Settings:
    return Settings()
