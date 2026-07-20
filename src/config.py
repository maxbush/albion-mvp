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

    # MeritHub API (OAuth2 + JWT). Без client_id+secret фабрика возвращает mock.
    merithub_client_id: str | None = None
    merithub_client_secret: str | None = None
    merithub_service_host: str = "https://serviceaccount1.meritgraph.com"
    merithub_class_host: str = "https://class1.meritgraph.com"
    merithub_live_host: str = "https://live.merithub.com"
    merithub_timeout: float = 15.0

    # MeritHub webhooks (push-модель): секрет генерируется на странице интеграций
    # MeritHub («Generate API Secret»). Им проверяем подлинность входящих событий.
    # Без секрета эндпоинт отвечает 503 (открытого приёмника не будет).
    merithub_webhook_secret: str | None = None
    merithub_webhook_port: int = 8000
    merithub_webhook_path: str = "/merithub/webhook"

    # Владельцы/админы пилота — TG ID через запятую (узнать свой: /whoami в боте).
    # Эти аккаунты могут раздавать роли командой /role.
    albion_admin_telegram_ids: str = ""

    # Тайминги сценария неявки (в минутах). Для живого демо поставьте 1.
    albion_notify_parent_delay_min: int = 5
    albion_escalate_delay_min: int = 15

    # Пилот: имя тестового ученика для сценария /pilot_absent.
    albion_pilot_student_name: str = "Пилотный ученик"

    # Demo mode: создаёт тестовых пользователей и демо-уведомления при старте
    # В проде выключить: ALBION_DEMO_MODE=false
    albion_demo_mode: bool = False

    @property
    def merithub_use_real(self) -> bool:
        """True, если заданы MeritHub CLIENT_ID + CLIENT_SECRET (Vendor Agnostic switch)."""
        return bool(self.merithub_client_id and self.merithub_client_secret)


settings = Settings()
