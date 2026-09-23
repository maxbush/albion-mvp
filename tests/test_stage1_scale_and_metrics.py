"""Тесты Этапа 1: Масштабирование под 100+ тьюторов, миграции, мониторинг."""

import aiosqlite
import pytest
from starlette.testclient import TestClient

from src.api.webhook import app
from src.db.migrations import init_db
from src.db.repository import (
    MeritHubClassRepository, MeritHubContactRepository,
    MeritHubStudentRepository, UserRepository,
)
from scripts.migrate_real_data import migrate_data


@pytest.mark.asyncio
async def test_schema_migrations_and_indexes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db_file = "albion.db"
    await init_db(db_file)

    async with aiosqlite.connect(db_file) as db:
        # Проверяем наличие таблицы версий
        cursor = await db.execute("SELECT version, name FROM schema_migrations")
        rows = await cursor.fetchall()
        assert len(rows) >= 1
        assert rows[0][0] == 1

        # Проверяем наличие составных индексов
        idx_cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='index'")
        indexes = {r[0] for r in await idx_cursor.fetchall()}
        assert "idx_classes_tutor_start" in indexes
        assert "idx_enroll_parent_role" in indexes
        assert "idx_contacts_phone" in indexes
        assert "idx_incidents_status_type" in indexes


@pytest.mark.asyncio
async def test_metrics_and_health_endpoints(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db_file = "albion.db"
    await init_db(db_file)

    # Добавляем тьютора и студентов
    await MeritHubContactRepository(db_file).upsert("t1", name="Tutor 1", role="tutor")
    await MeritHubStudentRepository(db_file).upsert("s1", name="Student 1", role="student")
    await MeritHubClassRepository(db_file).upsert(
        "c1", title="Perma class", class_type="perma", start_time="2026-08-01T10:00:00+01:00"
    )

    client = TestClient(app)

    # 1. Health
    h_resp = client.get("/health")
    assert h_resp.status_code == 200
    assert h_resp.json()["status"] == "ok"
    assert h_resp.json()["database"] == "connected"

    # 2. Metrics
    m_resp = client.get("/metrics")
    assert m_resp.status_code == 200
    metrics = m_resp.json()
    assert metrics["tutors_total"] >= 1
    assert metrics["students_total"] >= 1
    assert metrics["classes_perma"] >= 1


@pytest.mark.asyncio
async def test_real_data_migration_script(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db_file = "albion_real.db"
    res = await migrate_data(db_file)
    assert res["coordinators"] == 4
    assert res["students"] == 18

    # Проверяем, что повторный запуск идемпотентичен (не дублирует)
    res2 = await migrate_data(db_file)
    assert res2["students"] == 18

    s_repo = MeritHubStudentRepository(db_file)
    students = await s_repo._fetchall("SELECT name, timezone FROM merithub_students")
    assert len(students) == 18
    names = {s["name"] for s in students}
    assert "Ernest Mezheritsky" in names
    assert "Alexandra Mironova" in names
