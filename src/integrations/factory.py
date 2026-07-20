"""Фабрика интеграций — Vendor Agnostic switch (принцип из Видения ALBION).

Возвращает реальный клиент, если в .env заданы credentials, иначе — mock.
Workflow не знают, с чем работают: любую интеграцию/модель можно заменить,
не переписывая бизнес-логику.
"""

import logging

from src.config import settings

logger = logging.getLogger(__name__)


def get_merithub_service():
    """Реальный MeritHubClient (OAuth2+JWT), если заданы CLIENT_ID+SECRET, иначе mock."""
    if settings.merithub_use_real:
        from src.integrations.merithub_client import MeritHubClient
        logger.info("MeritHub: REAL client (client_id=%s)", settings.merithub_client_id)
        return MeritHubClient(
            client_id=settings.merithub_client_id,
            client_secret=settings.merithub_client_secret,
            service_host=settings.merithub_service_host,
            class_host=settings.merithub_class_host,
            live_host=settings.merithub_live_host,
            timeout=settings.merithub_timeout,
        )
    if settings.merithub_client_secret and not settings.merithub_client_id:
        logger.warning("MeritHub: CLIENT_SECRET задан, но нет CLIENT_ID — использую mock.")
    from src.integrations.merithub_mock import MockMeritHubService
    return MockMeritHubService()


def get_airtable_service():
    """Реальный Airtable — следующий этап. Пока mock (тот же интерфейс)."""
    from src.integrations.airtable_mock import MockAirtableService
    return MockAirtableService()
