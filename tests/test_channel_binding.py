"""PR9 — привязка каналов: normalize_phone, авто-биндинг WA→contacts,
importer пишет 'wa:'-адрес для phone-only контактов."""

import pytest

from src.channels.inbound import canonical_recipient, normalize_phone
from src.db.repository import (
    MeritHubContactRepository,
    MeritHubStudentRepository,
    UserChannelRepository,
    UserRepository,
)


def test_normalize_phone():
    assert normalize_phone("+7 999 000-11-22") == "+79990001122"
    assert normalize_phone("89990001122") == "+79990001122"
    assert normalize_phone("whatsapp:+79990001122") == "+79990001122"
    assert normalize_phone("+44 20 7946 0958") == "+442079460958"
    assert normalize_phone("") == "" and normalize_phone(None) == ""


@pytest.mark.asyncio
async def test_autobind_wa_to_contact_with_tg(db):
    """WA-входящее с номера из contacts → бинд к users-аккаунту, preferred=TG."""
    crepo = MeritHubContactRepository(db)
    await crepo.upsert("c1", telegram_id="555", role="parent",
                       name="Мама", phone="+79990001122")
    uid = await UserRepository(db).create("555", "parent", "Мама")

    ref = await canonical_recipient("whatsapp", "+79990001122", db_path=db)
    assert ref == "555"  # предпочтительный канал — telegram

    chans = await UserChannelRepository(db).list_for_user(uid)
    by_ch = {c["channel"]: c for c in chans}
    assert by_ch["telegram"]["is_preferred"] == 1
    assert by_ch["whatsapp"]["address"] == "+79990001122"
    assert by_ch["whatsapp"]["is_preferred"] == 0


@pytest.mark.asyncio
async def test_autobind_no_users_row_stays_wa(db):
    """Контакт есть в contacts, но не зарегистрирован в боте → 'wa:' ref."""
    await MeritHubContactRepository(db).upsert(
        "c2", telegram_id="777", role="parent", phone="+7111")
    ref = await canonical_recipient("whatsapp", "+7111", db_path=db)
    assert ref == "wa:+7111"


@pytest.mark.asyncio
async def test_autobind_phone_only_contact_stays_wa(db):
    """У контакта только телефон (нет TG) — отвечаем в WA."""
    await MeritHubContactRepository(db).upsert("c3", role="parent",
                                               phone="+7222")
    ref = await canonical_recipient("whatsapp", "+7222", db_path=db)
    assert ref == "wa:+7222"


@pytest.mark.asyncio
async def test_unknown_wa_stays_anonymous(db):
    ref = await canonical_recipient("whatsapp", "+7333", db_path=db)
    assert ref == "wa:+7333"


# ── Импортер: 'wa:'-адрес для phone-only контактов ───────────────────

@pytest.mark.asyncio
async def test_import_phone_only_gets_wa_address(db, tmp_path):
    """Родитель без TG но с телефоном → parent_telegram_id='wa:+…' —
    исходящие уведомления уйдут в WhatsApp."""
    import csv as _csv
    path = tmp_path / "students.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f)
        w.writerow(["client_user_id", "name", "email", "parent_telegram_id",
                    "phone", "timezone", "country"])
        w.writerow(["s1", "Вася", "", "", "8 (999) 000-11-22", "", ""])
        w.writerow(["s2", "Петя", "", "12345", "", "", ""])
    from scripts.import_data import run
    assert await run(str(path), "students", db, dry_run=False) == 0

    s1 = await MeritHubStudentRepository(db).get_by_client_id("s1")
    s2 = await MeritHubStudentRepository(db).get_by_client_id("s2")
    assert s1["parent_telegram_id"] == "wa:+79990001122"
    assert s2["parent_telegram_id"] == "12345"

    c1 = await MeritHubContactRepository(db).get("s1")
    assert c1["phone"] == "+79990001122"


@pytest.mark.asyncio
async def test_import_enrollment_phone_parent(db, tmp_path):
    """В enrollments телефон в parent_telegram_id → 'wa:'-ref."""
    import csv as _csv
    path = tmp_path / "enr.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f)
        w.writerow(["class_id", "client_user_id", "merithub_user_id",
                    "parent_telegram_id", "student_name", "role"])
        w.writerow(["C1", "s1", "mh_usr_1", "+79990001122", "Вася", ""])
    from scripts.import_data import run
    assert await run(str(path), "enrollments", db, dry_run=False) == 0
    from src.db.repository import MeritHubEnrollmentRepository
    rows = await MeritHubEnrollmentRepository(db)._fetchall(
        "SELECT * FROM merithub_enrollments")
    assert rows[0]["parent_telegram_id"] == "wa:+79990001122"
