"""Round 3 (MASTER_PLAN v3) — E2E-проверки P0-фиксов.

P0.1: _format_dual_time — суффикс [+Nч к London] реально считается
P0.2: class_live_check на отдельном workflow — алерт «не перешёл в live» достижим
P0.5: naive start_time трактуется как Europe/London (не UTC)
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.workflows.lesson_ops import _format_dual_time, _parse_dt, _schedule_at


# ── P0.1: dual-time diff ────────────────────────────────────────────

def test_p01_dual_time_shows_positive_offset():
    """Лето: London=UTC+1 (BST), Almaty=UTC+5 → суффикс [+4ч к London]."""
    result = _format_dual_time("2026-07-28T15:00:00+00:00", "Asia/Almaty")
    assert "16:00 (London)" in result
    assert "20:00 (ваше время, Asia/Almaty)" in result
    # ГЛАВНОЕ: суффикс с разницей поясов реально показывается (bug: всегда был 0).
    assert "[+4ч к London]" in result


def test_p01_dual_time_shows_negative_offset():
    """New York летом UTC-4, London UTC+1 → [-5ч к London]."""
    result = _format_dual_time("2026-07-28T15:00:00+00:00", "America/New_York")
    assert "16:00 (London)" in result
    assert "[-5ч к London]" in result


def test_p01_dual_time_no_suffix_when_same_offset():
    """Europe/Paris летом UTC+2, London UTC+1 → [+1ч к London]."""
    result = _format_dual_time("2026-07-28T15:00:00+00:00", "Europe/Paris")
    assert "[+1ч к London]" in result


def test_p01_dual_time_winter_offsets():
    """Зимой London=UTC+0, Almaty=UTC+5 → [+5ч к London]."""
    result = _format_dual_time("2026-01-28T15:00:00+00:00", "Asia/Almaty")
    assert "15:00 (London)" in result
    assert "[+5ч к London]" in result


# ── P0.5: naive start_time → Europe/London (канон) ──────────────────

def test_p05_parse_dt_naive_is_london_not_utc():
    """Naive вход '...T15:00:00' летом = 15:00 BST, т.е. 14:00 UTC."""
    dt = _parse_dt("2026-07-28T15:00:00")
    assert dt.tzinfo is not None
    as_utc = dt.astimezone(timezone.utc)
    assert (as_utc.hour, as_utc.minute) == (14, 0)
    # London-время — исходные 15:00
    from zoneinfo import ZoneInfo
    assert dt.astimezone(ZoneInfo("Europe/London")).hour == 15


def test_p05_parse_dt_aware_untouched():
    """Aware вход не меняется."""
    dt = _parse_dt("2026-07-28T15:00:00+00:00")
    assert dt.utcoffset() == __import__("datetime").timedelta(0)
    as_utc = dt.astimezone(timezone.utc)
    assert as_utc.hour == 15


def test_p05_schedule_at_converts_naive_london_to_utc():
    """_schedule_at возвращает UTC-строку: наивные 15:00 London летом → 14:00Z."""
    # Дату берём заведомо в будущем, чтобы не сработал fallback.
    target = datetime(2099, 7, 28, 15, 0, 0)
    out = _schedule_at(target)
    out_dt = datetime.fromisoformat(out)
    assert out_dt.tzinfo is not None
    out_utc = out_dt.astimezone(timezone.utc)
    assert out_utc.hour == 14  # BST = UTC+1


# ── P0.2: class_live_check живёт на отдельном workflow ──────────────

@pytest.mark.asyncio
async def test_p02_live_check_survives_tutor_response(tmp_path, monkeypatch):
    """E2E: tutor нажал «Урок начался» → live-check НЕ отменён →
    при отсутствии статуса lv координатор получает алерт о не переходе в live.
    Раньше live-check отменялся вместе с tutor_start_check workflow (мёртвая ветка)."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")

    from src.db.repository import (
        ScheduledActionRepository, UserRepository, WorkflowRepository,
    )
    from src.events.bus import bus
    from src.events.types import EventTypes
    from src.workflows.lesson_ops import LessonOpsWorkflow

    await UserRepository("albion.db").create("coord_1", "coordinator", "Координатор")
    await UserRepository("albion.db").create("tutor_tg", "tutor", "Репетитор")
    await UserRepository("albion.db").create("parent_tg", "parent", "Родитель")

    ops = LessonOpsWorkflow("albion.db")
    start_iso = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    await ops.schedule_class_coordination(
        class_id="C_TEST",
        start_time=start_iso,
        tutor_name="Репетитор",
        tutor_telegram_id="tutor_tg",
        student_rows=[{
            "name": "Ученик", "client_user_id": "s01",
            "parent_telegram_id": "parent_tg", "timezone": "Europe/London",
        }],
    )

    sched = ScheduledActionRepository("albion.db")
    actions = await sched._fetchall("SELECT * FROM scheduled_actions")
    by_action = {a["action"]: a for a in actions}
    # Фикс: live-check на ОТДЕЛЬНОМ workflow (раньше — общий с tutor_start_check).
    assert by_action["class_live_check"]["workflow_id"] != by_action["tutor_start_check"]["workflow_id"]

    live_wid = by_action["class_live_check"]["workflow_id"]
    start_wid = by_action["tutor_start_check"]["workflow_id"]

    captured = []
    async def cap(ev):
        captured.append(ev.data)
    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        # Репетитор отвечает «Урок начался» на start-check.
        await ops.record_checkin_response(start_wid, actor_tg="tutor_tg", action="class_started")

        # tutor_start workflow завершён, а live-check action остался PENDING
        start_wf = await WorkflowRepository("albion.db").get(start_wid)
        assert start_wf["state"] == "completed"
        live_action = await sched._fetchone(
            "SELECT * FROM scheduled_actions WHERE id=?", (by_action["class_live_check"]["id"],))
        assert live_action["status"] == "pending", "live-check не должен отменяться ответом репетитора!"

        captured.clear()
        # Симулируем тик live-check: статуса lv нет → алерт координатору.
        await ops._check_class_live(live_wid)
        alerts = [d for d in captured if "не перешёл в live" in (d.get("message") or "")]
        assert alerts, "ожидался алерт «урок не перешёл в live»"
        assert "подтвердил" in alerts[0]["message"]  # контекст: репетитор подтвердил старт
        assert alerts[0]["telegram_id"] == "coord_1"

        # Live-check workflow завершён после алерта.
        live_wf = await WorkflowRepository("albion.db").get(live_wid)
        assert live_wf["state"] == "completed"
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)


@pytest.mark.asyncio
async def test_p02_live_check_quiet_when_class_live(tmp_path, monkeypatch):
    """Если classStatus=lv уже пришёл — live-check завершается тихо, без алерта."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")

    from src.db.repository import (
        MeritHubClassStatusRepository, UserRepository, WorkflowRepository,
    )
    from src.events.bus import bus
    from src.events.types import EventTypes
    from src.workflows.lesson_ops import LessonOpsWorkflow

    await UserRepository("albion.db").create("coord_1", "coordinator", "Координатор")
    await MeritHubClassStatusRepository("albion.db").upsert("C_LIVE", "lv")

    ops = LessonOpsWorkflow("albion.db")
    start_iso = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    await ops.schedule_class_coordination(
        class_id="C_LIVE",
        start_time=start_iso,
        tutor_name="Репетитор",
        tutor_telegram_id="tutor_tg",
        student_rows=[],
    )
    live_row = await WorkflowRepository("albion.db")._fetchone(
        "SELECT * FROM workflow_instances WHERE workflow_type='class_live_check'")
    assert live_row, "live workflow должен существовать"

    captured = []
    async def cap(ev):
        captured.append(ev.data)
    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        await ops._check_class_live(live_row["id"])
        assert not captured, "при lv алерта быть не должно"
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    live_wf = await WorkflowRepository("albion.db").get(live_row["id"])
    assert live_wf["state"] == "completed"
