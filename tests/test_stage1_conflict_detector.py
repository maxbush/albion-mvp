"""Тесты Этапа 1: Контроль пересечений занятий у репетитора (perma и oneTime)."""

import json
import pytest

from src.utils.conflict_detector import intervals_overlap, check_tutor_conflicts
from src.db.repository import MeritHubClassRepository


def test_intervals_overlap():
    # 15:00-16:00 (900..960) и 15:30-16:30 (930..990) -> перехлест 30 мин
    overlap, mins = intervals_overlap(900, 60, 930, 60)
    assert overlap is True
    assert mins == 30

    # Стык в стык: 15:00-16:00 (900..960) и 16:00-17:00 (960..1020) -> нет перехлеста
    overlap, mins = intervals_overlap(900, 60, 960, 60)
    assert overlap is False
    assert mins == 0

    # Полное вложение: 14:00-17:00 (840..1020) и 15:00-16:00 (900..960) -> 60 мин
    overlap, mins = intervals_overlap(840, 180, 900, 60)
    assert overlap is True
    assert mins == 60


@pytest.mark.asyncio
async def test_perma_vs_perma_conflict(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")

    crepo = MeritHubClassRepository("albion.db")
    # Существующая серия: пн (1) и ср (3), 15:00, 60 мин
    await crepo.upsert(
        "C_p1",
        title="Maths with Dan",
        tutor_client_user_id="tutor_dan",
        class_type="perma",
        schedule_days=json.dumps([1, 3]),
        start_time="2026-08-01T15:00:00+01:00",
        duration=60,
    )

    # 1. Новая серия: тот же репетитор, ср (3) и пт (5), 15:30 (пересекается со средой 15:00-16:00)
    conflicts = await check_tutor_conflicts(
        tutor_client_user_id="tutor_dan",
        class_type="perma",
        start_time_hhmm="15:30",
        duration_min=60,
        schedule_days=[3, 5],
        db_path="albion.db",
    )
    assert len(conflicts) == 1
    assert conflicts[0].conflicting_class_id == "C_p1"
    assert conflicts[0].overlap_minutes == 30

    # 2. Новая серия: тот же репетитор, но другие дни (вт=2, чт=4) -> нет конфликта
    no_conflicts = await check_tutor_conflicts(
        tutor_client_user_id="tutor_dan",
        class_type="perma",
        start_time_hhmm="15:30",
        duration_min=60,
        schedule_days=[2, 4],
        db_path="albion.db",
    )
    assert len(no_conflicts) == 0

    # 3. Другой репетитор в то же время -> нет конфликта
    other_tutor = await check_tutor_conflicts(
        tutor_client_user_id="tutor_anna",
        class_type="perma",
        start_time_hhmm="15:00",
        duration_min=60,
        schedule_days=[1, 3],
        db_path="albion.db",
    )
    assert len(other_tutor) == 0


@pytest.mark.asyncio
async def test_perma_vs_onetime_conflict(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")

    crepo = MeritHubClassRepository("albion.db")
    # Разовый урок: понедельник 2026-08-03, 14:00-15:30 (90 мин)
    await crepo.upsert(
        "C_one",
        title="Consultation",
        tutor_client_user_id="tutor_dan",
        class_type="oneTime",
        start_time="2026-08-03T14:00:00+01:00",
        duration=90,
    )

    # Новая регулярная серия по понедельникам (1) в 15:00 (60 мин) -> перехлест 15:00-15:30
    conflicts = await check_tutor_conflicts(
        tutor_client_user_id="tutor_dan",
        class_type="perma",
        start_time_hhmm="15:00",
        duration_min=60,
        schedule_days=[1],
        start_date="2026-08-01",
        db_path="albion.db",
    )
    assert len(conflicts) == 1
    assert conflicts[0].conflicting_class_id == "C_one"
    assert conflicts[0].overlap_minutes == 30
