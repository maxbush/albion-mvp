"""PR6: system_settings (персистентный kill switch) + /metrics."""
import pytest

from src.db.migrations import init_db
from src.db.repository import (
    ScheduledActionRepository, SystemSettingsRepository,
)


async def _init_tmp_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    await init_db("albion.db")
    return "albion.db"


# ── system_settings ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_system_settings_roundtrip(tmp_path, monkeypatch):
    db = await _init_tmp_db(tmp_path, monkeypatch)
    repo = SystemSettingsRepository(db)
    assert await repo.get("k") is None
    await repo.set("k", "1")
    assert await repo.get("k") == "1"
    await repo.set("k", "2")
    assert await repo.get("k") == "2"  # upsert, не дубли


@pytest.mark.asyncio
async def test_kill_switch_persists_across_restart(tmp_path, monkeypatch):
    """set → запись в system_settings; load → восстановление после 'рестарта'."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    import src.bot.handlers as H

    await H.set_kill_switch_level(0)
    assert H.get_kill_switch_level() == 0
    assert await SystemSettingsRepository(db).get("kill_switch_level") == "0"

    # 'Рестарт': модульное значение сброшено к дефолту, загрузка читает БД
    H._kill_switch_level = 2
    await H.load_kill_switch_level()
    assert H.get_kill_switch_level() == 0

    H._kill_switch_level = 2  # гигиена для других тестов


# ── /metrics ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_collect_metrics_format(tmp_path, monkeypatch):
    await _init_tmp_db(tmp_path, monkeypatch)
    from src.db.repository import DeadLetterQueueRepository
    await ScheduledActionRepository().create(
        workflow_id=1, execute_at="2020-01-01T00:00:00+00:00",
        action="a", payload={})
    dlq = DeadLetterQueueRepository()
    await dlq.put("t", "e", {}, "boom")

    from src.services.metrics import collect_metrics
    text = await collect_metrics("albion.db")
    assert "albion_dlq_size 1" in text
    assert "albion_scheduled_actions_total{status=\"pending\"} 1" in text
    assert "albion_scheduled_pending_lag_seconds" in text
    assert "albion_kill_switch_level 2" in text
    # exposition: каждая метрика — HELP/TYPE до значения
    assert "# HELP albion_dlq_size" in text


def test_metrics_endpoint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from fastapi.testclient import TestClient
    from src.api.webhook import app
    with TestClient(app) as c:
        r = c.get("/metrics")
    assert r.status_code == 200
    assert "albion_dlq_size" in r.text
