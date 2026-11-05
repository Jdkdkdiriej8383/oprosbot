"""Configuration module using pydantic-settings.

Loads environment variables from .env file or environment with type validation.
"""

from typing import Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings with pydantic-settings validation."""

    BOT_TOKEN: str = Field(
        ...,
        description="Telegram Bot API token obtained from @BotFather"
    )
    DATABASE_URL: str = Field(
        default="sqlite+aiosqlite:///oprosbot.db",
        description="Async SQLAlchemy database connection string"
    )
    BOT_USERNAME: Optional[str] = Field(
        default=None,
        description="Telegram bot username without @ (detected automatically if omitted)"
    )
    TIMEZONE: str = Field(
        default="UTC",
        description="Default timezone for timestamps and scheduling"
    )
    LOG_LEVEL: str = Field(
        default="INFO",
        description="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)"
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


# Global settings instance (lazy loaded or initialized when required)
def get_settings() -> Settings:
    return Settings()
