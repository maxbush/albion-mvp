from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parent.parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: str = "test_token"
    telegram_webhook_secret: str = "test_secret"
    telegram_webhook_url: str | None = None

    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    database_url: str = "sqlite+aiosqlite:///./albion.db"

    app_name: str = "ALBION MVP"
    log_level: str = "INFO"

    llm_model: str = "anthropic/claude-3-haiku"
    llm_cheap_model: str = "openai/gpt-4o-mini"

    # Demo mode: создаёт тестовых пользователей и демо-уведомления при старте
    # В проде выключить: ALBION_DEMO_MODE=false
    albion_demo_mode: bool = True


settings = Settings()
