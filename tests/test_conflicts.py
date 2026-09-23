"""PR5: контроль пересечений занятий у репетитора (жёсткий блок).

- find_conflicts: материализация через effective_dates — перенесённое
  занятие занимает НОВЫЙ слот, отменённое освобождает свой.
- /schedule: perma-кандидат с конфликтом → превью без кнопки создания;
  confirm перепроверяет (карточка могла устареть).
- /reschedule: целевой слот с пересечением → отказ, override не пишется.
"""
import pytest
from datetime import date, timedelta

from src.db.migrations import init_db
from src.db.repository import (
    MeritHubClassRepository, ScheduleOverrideRepository, UserRepository,
)
from src.services.conflicts import conflict_lines, find_conflicts
from tests.test_reschedule import (
    FakeContext, FakeUpdate, FakeUser, _perma,
)


async def _init_tmp_db(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    await init_db("albion.db")
    return "albion.db"


async def _add_class(row):
    await MeritHubClassRepository().upsert(**row)


def _future_monday() -> date:
    d = date.today() + timedelta(days=1)
    while d.weekday() != 0:
        d += timedelta(days=1)
    return d


def test_mh_day_code_mapping():
    """MH-код 1 (пн) ↔ weekday()+1 % 7 — sanity на внутреннюю раскладку дней."""
    from src.services.conflicts import find_conflicts  # noqa: F401
    mon = _future_monday()
    assert (mon.weekday() + 1) % 7 == 1


@pytest.mark.asyncio
async def test_perma_overlap_same_day(tmp_path, monkeypatch):
    await _init_tmp_db(monkeypatch, tmp_path)
    await _add_class(_perma("C1", days=[1, 3]))  # пн, ср 15:00, 60 мин
    hits = await find_conflicts("t1", {
        "ctype": "perma", "days": [1], "hhmm": "15:30", "duration": 60})
    assert hits and hits[0]["class_id"] == "C1"
    assert hits[0]["count"] >= 3  # каждый понедельник в горизонте


@pytest.mark.asyncio
async def test_adjacent_slots_no_conflict(tmp_path, monkeypatch):
    await _init_tmp_db(monkeypatch, tmp_path)
    await _add_class(_perma("C1", days=[1]))  # пн 15:00–16:00
    hits = await find_conflicts("t1", {
        "ctype": "perma", "days": [1], "hhmm": "16:00", "duration": 60})
    assert hits == []


@pytest.mark.asyncio
async def test_other_tutor_ignored(tmp_path, monkeypatch):
    await _init_tmp_db(monkeypatch, tmp_path)
    row = _perma("C1", days=[1])
    row["tutor_client_user_id"] = "T2"
    await _add_class(row)
    hits = await find_conflicts("t1", {
        "ctype": "perma", "days": [1], "hhmm": "15:00", "duration": 60})
    assert hits == []


@pytest.mark.asyncio
async def test_exclude_class_id(tmp_path, monkeypatch):
    await _init_tmp_db(monkeypatch, tmp_path)
    await _add_class(_perma("C1", days=[1]))
    hits = await find_conflicts("t1", {
        "ctype": "perma", "days": [1], "hhmm": "15:00", "duration": 60,
        "exclude_class_id": "C1"})
    assert hits == []


@pytest.mark.asyncio
async def test_moved_in_slot_conflicts(tmp_path, monkeypatch):
    """Перенесённое занятие занимает новый слот — конфликт по нему."""
    await _init_tmp_db(monkeypatch, tmp_path)
    mon = _future_monday()
    await _add_class(_perma("C1", days=[1]))  # пн 15:00
    await ScheduleOverrideRepository().add(
        "C1", mon.isoformat(), "moved",
        new_date=(mon + timedelta(days=1)).isoformat(), new_time="15:30")
    hits = await find_conflicts("t1", {
        "ctype": "one", "date": (mon + timedelta(days=1)).isoformat(),
        "hhmm": "16:00", "duration": 60})
    assert hits and hits[0]["class_id"] == "C1"


@pytest.mark.asyncio
async def test_cancelled_occurrence_frees_slot(tmp_path, monkeypatch):
    await _init_tmp_db(monkeypatch, tmp_path)
    mon = _future_monday()
    await _add_class(_perma("C1", days=[1]))
    await ScheduleOverrideRepository().add("C1", mon.isoformat(), "cancelled")
    hits = await find_conflicts("t1", {
        "ctype": "one", "date": mon.isoformat(), "hhmm": "15:30", "duration": 60})
    assert hits == []


@pytest.mark.asyncio
async def test_partial_overlap_counts(tmp_path, monkeypatch):
    await _init_tmp_db(monkeypatch, tmp_path)
    mon = _future_monday()
    row = _perma("C1", days=[1])
    row["duration"] = 45  # пн 15:00–15:45
    await _add_class(row)
    hits = await find_conflicts("t1", {
        "ctype": "one", "date": mon.isoformat(), "hhmm": "15:40", "duration": 30})
    assert hits


def test_conflict_lines_render():
    lines = conflict_lines([{"class_id": "C1", "title": "Математика",
                             "count": 5,
                             "examples": ["2026-10-05 15:00",
                                          "2026-10-12 15:00",
                                          "2026-10-19 15:00"]}])
    assert "Математика" in lines[0] and "+2" in lines[0]


@pytest.mark.asyncio
async def test_preview_perma_conflict_blocks_create_button(tmp_path, monkeypatch):
    """Превью perma с пересечением → ⛔ и нет кнопки 'Создать занятие'."""
    await _init_tmp_db(monkeypatch, tmp_path)
    await _add_class(_perma("C1", days=[1]))
    from src.bot.wizard import _sched_view
    d = {"ctype": "perma", "days": [1], "hour": 15, "minute": 30,
         "duration": 60, "tutor_cuid": "t1", "tutor_name": "Реп",
         "students": []}
    text, markup = await _sched_view("preview", d)
    assert "⛔" in text
    cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "wz:sched:confirm" not in cbs


@pytest.mark.asyncio
async def test_preview_clean_perma_keeps_create(tmp_path, monkeypatch):
    await _init_tmp_db(monkeypatch, tmp_path)
    await _add_class(_perma("C1", days=[2]))  # вт — не пересекается с пн
    from src.bot.wizard import _sched_view
    d = {"ctype": "perma", "days": [1], "hour": 15, "minute": 0,
         "duration": 60, "tutor_cuid": "t1", "tutor_name": "Реп",
         "students": []}
    text, markup = await _sched_view("preview", d)
    assert "⛔" not in text
    cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "wz:sched:confirm" in cbs


@pytest.mark.asyncio
async def test_reschedule_blocked_on_conflict(tmp_path, monkeypatch):
    """/reschedule в занятый слот → отказ, override не пишется."""
    db = await _init_tmp_db(monkeypatch, tmp_path)
    await UserRepository(db).create("999", "coordinator", "К")
    mon = _future_monday()
    tue = mon + timedelta(days=1)
    await _add_class(_perma("C1", days=[1]))                # пн 15:00
    await _add_class(_perma("C2", days=[2], hhmm="15:30"))    # вт 15:30–16:30

    from src.bot.handlers import cmd_reschedule
    upd = FakeUpdate(FakeUser(999))
    await cmd_reschedule(upd, FakeContext(
        ["C1", str(mon), str(tue), "15:45"]))
    assert "пересекается" in upd.message.replies[-1]
    assert not await ScheduleOverrideRepository(db).get("C1", str(mon))
