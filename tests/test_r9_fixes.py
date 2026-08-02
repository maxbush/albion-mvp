"""Round 9 — total audit fixes (MASTER_PLAN v6).

Проверяем:
1) R9-1: LIKE-коллизия incident_id — resolve_absence(5) НЕ должен отменять
   workflow инцидента 55; точные поиски по parent_telegram_id.
"""

import json
import sqlite3

import pytest

from src.db.repository import (
    IncidentRepository,
    WorkflowRepository,
    ScheduledActionRepository,
    UserRepository,
)
from src.workflows.engine import engine
from src.workflows.absence import AbsenceWorkflow
from src.events.bus import bus
from src.events.types import Event, EventTypes


@pytest.mark.asyncio
async def test_r9_1_resolve_absence_does_not_cancel_other_incident_workflow(tmp_path, monkeypatch):
    """R9-1: инциденты 5 и 55 — resolve_absence(5) отменяет ТОЛЬКО свой workflow."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    engine.repo = WorkflowRepository("albion.db")
    engine.scheduler = ScheduledActionRepository("albion.db")

    con = sqlite3.connect("albion.db")
    con.execute("INSERT INTO incidents (id, lesson_ref, type, status) VALUES (5, 'L5', 'absence', 'pending')")
    con.execute("INSERT INTO incidents (id, lesson_ref, type, status) VALUES (55, 'L55', 'absence', 'pending')")
    con.commit()
    con.close()

    w5 = await engine.start_workflow("absence_notification", {
        "incident_id": 5, "parent_telegram_id": "777",
        "student_name": "Миша", "lesson_ref": "L5"})
    w55 = await engine.start_workflow("absence_notification", {
        "incident_id": 55, "parent_telegram_id": "999",
        "student_name": "Катя", "lesson_ref": "L55"})

    wf = AbsenceWorkflow("albion.db")
    await wf.resolve_absence(5, "777", resolution="parent_ok")

    repo = WorkflowRepository("albion.db")
    assert (await repo.get(w5))["state"] == "cancelled"
    # КРИТИЧНО: чужой workflow не тронут (раньше LIKE-подстрока матчила 55)
    assert (await repo.get(w55))["state"] == "running"
    assert (await IncidentRepository("albion.db").get(55))["status"] == "pending"


@pytest.mark.asyncio
async def test_r9_1_parent_reply_uses_correct_workflow(tmp_path, monkeypatch):
    """R9-1: notify_coordinators_parent_reply берёт данные СВОЕГО workflow (не 55)."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    engine.repo = WorkflowRepository("albion.db")
    engine.scheduler = ScheduledActionRepository("albion.db")

    con = sqlite3.connect("albion.db")
    con.execute("INSERT INTO incidents (id, lesson_ref, type, status) VALUES (5, 'L5', 'absence', 'pending')")
    con.execute("INSERT INTO incidents (id, lesson_ref, type, status) VALUES (55, 'L55', 'absence', 'pending')")
    con.commit()
    con.close()
    await UserRepository("albion.db").create("999", "coordinator", "Координатор")

    await engine.start_workflow("absence_notification", {
        "incident_id": 5, "parent_telegram_id": "777",
        "student_name": "Миша", "lesson_ref": "L5"})
    await engine.start_workflow("absence_notification", {
        "incident_id": 55, "parent_telegram_id": "999",
        "student_name": "Катя", "lesson_ref": "L55"})

    captured = []
    async def cap(ev):
        captured.append(ev.data)
    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        wf = AbsenceWorkflow("albion.db")
        await wf.notify_coordinators_parent_reply(5, "ok", parent_telegram_id="777")
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)

    coord_msgs = [d["message"] for d in captured if d.get("telegram_id") == "999"]
    assert coord_msgs, "координатор должен получить уведомление"
    text = coord_msgs[0]
    assert "Миша" in text and "Инцидент #5" in text
    assert "Катя" not in text and "#55" not in text  # данные чужого workflow не утекли


@pytest.mark.asyncio
async def test_r9_1_find_active_incident_exact_tg(tmp_path, monkeypatch):
    """R9-1: поиск по parent_telegram_id точен (777 не находит workflow с 7777)."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    engine.repo = WorkflowRepository("albion.db")
    engine.scheduler = ScheduledActionRepository("albion.db")

    await IncidentRepository("albion.db").create(lesson_ref="A", type="absence", status="pending")
    i2 = await IncidentRepository("albion.db").create(lesson_ref="B", type="absence", status="pending")
    await engine.start_workflow("absence_notification", {
        "incident_id": 1, "parent_telegram_id": "777", "student_name": "А"})
    await engine.start_workflow("absence_notification", {
        "incident_id": i2, "parent_telegram_id": "7777", "student_name": "Б"})

    wf = AbsenceWorkflow("albion.db")
    hit = await wf.find_active_incident_for_parent("777")
    assert hit is not None
    inc_id, data = hit
    assert inc_id == 1 and data["student_name"] == "А"
    assert data["parent_telegram_id"] == "777"


@pytest.mark.asyncio
async def test_r9_1_find_by_json_works_for_strings_and_ints(tmp_path, monkeypatch):
    """R9-1: хелпер find_by_json корректен для int и str полей."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    repo = WorkflowRepository("albion.db")

    w1 = await repo.create("t", "running", {"incident_id": 5, "class_id": "C9"})
    w2 = await repo.create("t", "running", {"incident_id": 55, "class_id": "C10"})

    assert [r["id"] for r in await repo.find_by_json("incident_id", 5)] == [w1]
    assert [r["id"] for r in await repo.find_by_json("incident_id", 55)] == [w2]
    assert [r["id"] for r in await repo.find_by_json("class_id", "C9")] == [w1]
    assert [r["id"] for r in await repo.find_by_json("class_id", "C1")] == []
    assert [r["id"] for r in await repo.find_by_json(
        "incident_id", 5, state="cancelled")] == []
    assert [r["id"] for r in await repo.find_by_json(
        "incident_id", 5, state="running")] == [w1]


# ── R9-13: notify_late_detail — actor-aware ────────────────────────────

@pytest.mark.asyncio
async def test_r9_13_late_detail_parent_says_student_late(tmp_path, monkeypatch):
    """R9-13: родитель жмёт «Опоздаю» → координатору «Ученик опоздает на N мин»,
    а НЕ «Репетитор задержится» (прежняя ложь)."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    await UserRepository("albion.db").create("999", "coordinator", "Координатор")

    from src.workflows.lesson_ops import LessonOpsWorkflow
    wid = await WorkflowRepository("albion.db").create("prelesson_parent", "running", {
        "class_id": "C9",
        "actor_type": "parent",
        "actor_telegram_id": "777",
        "student_name": "Миша",
        "tutor_name": "Анна",
        "start_time": "2099-08-02T15:00:00+00:00",
    })

    captured = []
    async def cap(ev):
        captured.append(ev.data)
    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        await LessonOpsWorkflow("albion.db").notify_late_detail(wid, "15")
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)

    msgs = [d["message"] for d in captured if d.get("telegram_id") == "999"]
    assert msgs, "координатор должен получить уведомление"
    text = msgs[0]
    assert "Уточнение по опозданию ученика" in text
    assert "Ученик: Миша опоздает на 15 мин" in text
    assert "задержится" not in text


@pytest.mark.asyncio
async def test_r9_13_late_detail_tutor_says_tutor_late(tmp_path, monkeypatch):
    """R9-13: репетитор жмёт «Опоздаю» → координатору «Репетитор задержится на N мин»."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    await UserRepository("albion.db").create("999", "coordinator", "Координатор")

    from src.workflows.lesson_ops import LessonOpsWorkflow
    wid = await WorkflowRepository("albion.db").create("prelesson_tutor", "running", {
        "class_id": "C9",
        "actor_type": "tutor",
        "actor_telegram_id": "555",
        "tutor_name": "Анна",
        "student_names": ["Миша"],
        "start_time": "2099-08-02T15:00:00+00:00",
    })

    captured = []
    async def cap(ev):
        captured.append(ev.data)
    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        await LessonOpsWorkflow("albion.db").notify_late_detail(wid, "30+")
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)

    msgs = [d["message"] for d in captured if d.get("telegram_id") == "999"]
    text = msgs[0]
    assert "Уточнение по опозданию репетитора" in text
    assert "Репетитор: Анна задержится на 30+ мин" in text
    assert "опоздает" not in text
