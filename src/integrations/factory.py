"""Фабрика интеграций — Vendor Agnostic switch (принцип из Видения ALBION).

Возвращает реальный клиент, если в .env заданы credentials, иначе — mock.
Workflow не знают, с чем работают: любую интеграцию/модель можно заменить,
не переписывая бизнес-логику.

Реальный клиент — singleton (lru_cache), чтобы токен и кеш переживали
несколько вызовов в рамках одного процесса.
"""

import logging
from functools import lru_cache

from src.config import settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _real_merithub_service(
    client_id: str,
    client_secret: str,
    service_host: str,
    class_host: str,
    live_host: str,
    timeout: float,
):
    from src.integrations.merithub_client import MeritHubClient
    logger.info("MeritHub: REAL client created (client_id=%s)", client_id)
    return MeritHubClient(
        client_id=client_id,
        client_secret=client_secret,
        service_host=service_host,
        class_host=class_host,
        live_host=live_host,
        timeout=timeout,
    )


def get_merithub_service():
    """Реальный MeritHubClient (OAuth2+JWT), если заданы CLIENT_ID+SECRET, иначе mock."""
    if settings.merithub_use_real:
        return _real_merithub_service(
            settings.merithub_client_id,
            settings.merithub_client_secret,
            settings.merithub_service_host,
            settings.merithub_class_host,
            settings.merithub_live_host,
            settings.merithub_timeout,
        )
    if settings.merithub_client_secret and not settings.merithub_client_id:
        logger.warning("MeritHub: CLIENT_SECRET задан, но нет CLIENT_ID — использую mock.")
    from src.integrations.merithub_mock import MockMeritHubService
    return MockMeritHubService()


def get_airtable_service():
    """Реальный Airtable — следующий этап. Пока mock (тот же интерфейс)."""
    from src.integrations.airtable_mock import MockAirtableService
    return MockAirtableService()
