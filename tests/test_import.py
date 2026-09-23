"""PR7: scripts/import_data.py — CSV-импорт данных клиента."""
import pytest

from src.db.migrations import init_db
from src.db.repository import (
    MeritHubClassRepository, MeritHubContactRepository,
    MeritHubEnrollmentRepository, MeritHubStudentRepository,
)


async def _init_tmp_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    await init_db("albion.db")
    return "albion.db"


def _csv(tmp_path, name, rows):
    import csv as _csv_mod
    p = tmp_path / name
    with open(p, "w", newline="", encoding="utf-8") as f:
        _csv_mod.writer(f).writerows(rows)
    return str(p)


@pytest.mark.asyncio
async def test_import_students(tmp_path, monkeypatch):
    from scripts.import_data import run
    db = await _init_tmp_db(tmp_path, monkeypatch)
    path = _csv(tmp_path, "s.csv", [
        ["client_user_id", "name", "email", "parent_telegram_id", "phone",
         "timezone", "country"],
        ["p1", "Мама Миши", "m@x.com", "555", "+7900", "Europe/London", "GB"],
        ["p2", "Папа без контакта", "", "", "", "", ""],
    ])
    rc = await run(path, "students", db, dry_run=False)
    assert rc == 0
    st = await MeritHubStudentRepository(db)._fetchone(
        "SELECT * FROM merithub_students WHERE client_user_id='p1'")
    assert st["name"] == "Мама Миши" and st["parent_telegram_id"] == "555"
    ct = await MeritHubContactRepository(db)._fetchone(
        "SELECT * FROM merithub_contacts WHERE client_user_id='p1'")
    assert ct["phone"] == "+7900"


@pytest.mark.asyncio
async def test_import_warns_missing_contact(tmp_path, monkeypatch, capsys):
    from scripts.import_data import run
    db = await _init_tmp_db(tmp_path, monkeypatch)
    path = _csv(tmp_path, "s.csv", [
        ["client_user_id", "name"],
        ["p1", "Без контактов"],
    ])
    await run(path, "students", db, dry_run=True)
    out = capsys.readouterr().out
    assert "нет TG и телефона" in out
    # dry-run ничего не пишет
    assert await MeritHubStudentRepository(db)._fetchone(
        "SELECT * FROM merithub_students WHERE client_user_id='p1'") is None


@pytest.mark.asyncio
async def test_import_classes_day_names(tmp_path, monkeypatch):
    from scripts.import_data import run, _days
    db = await _init_tmp_db(tmp_path, monkeypatch)
    assert _days("пн, ср") == "[1, 3]"
    assert _days("1,3") == "[1, 3]"
    assert _days("") is None
    path = _csv(tmp_path, "c.csv", [
        ["class_id", "title", "class_type", "schedule_days", "start_time",
         "duration", "tutor_client_user_id", "end_date"],
        ["C1", "Математика", "perma", "пн,ср", "2026-09-01T15:00", "60", "t1", ""],
        ["C2", "Разовое", "one", "", "2026-10-01T18:00", "45", "t1", ""],
    ])
    await run(path, "classes", db, dry_run=False)
    c = await MeritHubClassRepository(db).get("C1")
    assert c["class_type"] == "perma" and c["schedule_days"] == "[1, 3]"
    assert c["duration"] == 60
    c2 = await MeritHubClassRepository(db).get("C2")
    assert c2["class_type"] == "oneTime"


@pytest.mark.asyncio
async def test_import_enrollments_and_errors(tmp_path, monkeypatch, capsys):
    from scripts.import_data import run
    db = await _init_tmp_db(tmp_path, monkeypatch)
    path = _csv(tmp_path, "e.csv", [
        ["class_id", "client_user_id", "merithub_user_id",
         "parent_telegram_id", "student_name", "role"],
        ["C1", "p1", "usr_847", "555", "Миша", "student"],
        ["", "p2", "", "", "", ""],  # нет class_id → ошибка
    ])
    rc = await run(path, "enrollments", db, dry_run=False)
    assert rc == 1
    out = capsys.readouterr().out
    assert "ошибок 1" in out
    enr = await MeritHubEnrollmentRepository(db).list_by_class("C1")
    assert len(enr) == 1 and enr[0]["client_user_id"] == "p1"


@pytest.mark.asyncio
async def test_idempotent_rerun(tmp_path, monkeypatch):
    from scripts.import_data import run
    db = await _init_tmp_db(tmp_path, monkeypatch)
    path = _csv(tmp_path, "s.csv", [
        ["client_user_id", "name", "phone"], ["p1", "Мама", "+1"]])
    await run(path, "students", db, dry_run=False)
    path2 = _csv(tmp_path, "s2.csv", [
        ["client_user_id", "name", "phone"], ["p1", "Мама В.", "+1"]])
    await run(path2, "students", db, dry_run=False)
    rows = await MeritHubStudentRepository(db)._fetchall(
        "SELECT * FROM merithub_students WHERE client_user_id='p1'")
    assert len(rows) == 1 and rows[0]["name"] == "Мама В."


def test_template():
    from scripts.import_data import TEMPLATES
    assert "client_user_id" in TEMPLATES["students"]
    assert "schedule_days" in TEMPLATES["classes"]
