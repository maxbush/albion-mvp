import logging
from pathlib import Path
from urllib.parse import urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parent.parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: str = "test_token"
    telegram_webhook_secret: str = "test_secret"
    telegram_webhook_url: str | None = None
    # R9-3: локальный приёмник апдейтов для webhook-режима (бот слушает этот
    # порт/путь, Telegram POSTит на него). url_path берётся из WEBHOOK_URL.
    telegram_webhook_host: str = "0.0.0.0"
    telegram_webhook_port: int = 8443

    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    database_url: str = "sqlite+aiosqlite:///./albion.db"

    app_name: str = "ALBION MVP"
    log_level: str = "INFO"

    llm_model: str = "deepseek/deepseek-v4-flash"
    llm_cheap_model: str = "deepseek/deepseek-v4-flash"

    # MeritHub API (OAuth2 + JWT). Без client_id+secret фабрика возвращает mock.
    merithub_client_id: str | None = None
    merithub_client_secret: str | None = None
    merithub_service_host: str = "https://serviceaccount1.meritgraph.com"
    merithub_class_host: str = "https://class1.meritgraph.com"
    merithub_live_host: str = "https://live.merithub.com"
    merithub_timeout: float = 15.0
    merithub_log_payload: bool = False

    # MeritHub webhooks (push-модель): секрет опционален. Если MeritHub пришлёт
    # подпись/токен в известном заголовке — проверим. Если подписи нет, webhook
    # всё равно примем и сохраним payload для анализа/обработки.
    merithub_webhook_secret: str | None = None
    merithub_webhook_port: int = 8000
    merithub_webhook_path: str = "/merithub/webhook"

    # WhatsApp Business Cloud API (этап 1). Без token+phone_number_id —
    # mock-режим: отправитель создаётся, но в боте НЕ регистрируется
    # (иначе реальные уведомления уходили бы в mock-никуда).
    whatsapp_token: str | None = None               # system user token
    whatsapp_phone_number_id: str | None = None
    whatsapp_business_account_id: str | None = None
    whatsapp_app_secret: str | None = None          # X-Hub-Signature-256 verify
    whatsapp_verify_token: str | None = None        # GET webhook challenge
    whatsapp_webhook_path: str = "/whatsapp/webhook"
    whatsapp_graph_version: str = "v21.0"

    # Провайдер WhatsApp-транспорта: "meta" (Cloud API напрямую) или
    # "twilio" (BSP — клиент изначально планировал Twilio, см. ALBION_CONTEXT).
    whatsapp_provider: str = "meta"
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    # Номер отправителя в формате Twilio: "whatsapp:+14155238886" (sandbox).
    twilio_whatsapp_from: str = "whatsapp:+14155238886"
    twilio_webhook_path: str = "/twilio/whatsapp"

    # Владельцы/админы пилота — TG ID через запятую (узнать свой: /whoami в боте).
    # Эти аккаунты могут раздавать роли командой /role.
    albion_admin_telegram_ids: str = ""

    # Тайминги сценария неявки (в минутах). Для живого демо поставьте 1.
    albion_notify_parent_delay_min: int = 5
    albion_escalate_delay_min: int = 15

    # Напоминания/контроль перед уроком.
    albion_prelesson_reminder_min: int = 15
    albion_class_live_grace_min: int = 5

    # Частота тика планировщика. Для пилота удобно 5с, для обычного режима 30с.
    albion_scheduler_interval_sec: int = 30

    # Бесплатное окно отмены/переноса (часов до занятия). Меньше — платно:
    # родителю показывается предупреждение до подтверждения.
    albion_cancel_free_hours: int = 24
    albion_reschedule_free_hours: int = 24

    # Пилот: имя тестового ученика для сценария /pilot_absent.
    albion_pilot_student_name: str = "Пилотный ученик"

    # Demo mode: создаёт тестовых пользователей и демо-уведомления при старте
    # В проде выключить: ALBION_DEMO_MODE=false
    albion_demo_mode: bool = False

    # Каноническая зона расписания ОРГАНИЗАЦИИ (решение владельца, H4 / P4.1):
    # schedule_class создаётся в этой зоне; наивные даты без offset трактуются
    # в ней же. Зоны учеников/репетиторов — только для dual-time display,
    # на создание класса они не влияют. Единая точка правды — НЕ хардкодить.
    albion_org_timezone: str = "Europe/London"

    @property
    def merithub_use_real(self) -> bool:
        """True, если заданы MeritHub CLIENT_ID + CLIENT_SECRET (Vendor Agnostic switch)."""
        return bool(self.merithub_client_id and self.merithub_client_secret)

    @property
    def whatsapp_use_real(self) -> bool:
        """True, если у выбранного провайдера заданы креды."""
        if self.whatsapp_provider == "twilio":
            return self.twilio_use_real
        return bool(self.whatsapp_token and self.whatsapp_phone_number_id)

    @property
    def twilio_use_real(self) -> bool:
        """True, если заданы TWILIO_ACCOUNT_SID + AUTH_TOKEN."""
        return bool(self.twilio_account_sid and self.twilio_auth_token)

    def org_zone(self):
        """ZoneInfo канонической зоны организации.

        При невалидном ALBION_ORG_TIMEZONE — fallback на Europe/London с warning
        (лучше деградировать предсказуемо, чем уронить планирование занятий)."""
        from zoneinfo import ZoneInfo
        try:
            return ZoneInfo(self.albion_org_timezone)
        except Exception:
            logger.warning(
                "Invalid ALBION_ORG_TIMEZONE=%r — falling back to Europe/London",
                self.albion_org_timezone,
            )
            return ZoneInfo("Europe/London")

    @property
    def database_path(self) -> str:
        """Локальный путь к sqlite-файлу из DATABASE_URL."""
        parsed = urlparse(self.database_url)
        if parsed.scheme == "sqlite+aiosqlite":
            path = parsed.path.lstrip("/")
            return path or "albion.db"
        return "albion.db"

    @property
    def db_dsn(self) -> str:
        """DSN хранилища: postgres:// URL для PG-деплоя, иначе путь sqlite-файла."""
        if self.database_url.startswith(("postgres://", "postgresql://", "postgresql+asyncpg://")):
            return self.database_url
        return self.database_path


settings = Settings()
