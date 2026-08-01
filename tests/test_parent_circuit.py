"""Round 6 — родительский контур, /lessons и i18n (по UX-аудиту).

П1: персональная отмена — только свои занятия, occurrence-aware, с подтверждением.
П4: /lessons — «Мои занятия» с перевыдачей постоянных ссылок на комнату.
П3: i18n-слой (tr/lang_of) + язык по роли при регистрации.
Сводка: build_morning_digest_text occurrence-aware + идемпотентный запуск авто-рассылки.
"""

import json

import pytest

from src.config import settings


# ── Фейки (тот же паттерн, что в test_round4_ux.py) ─────────────────

class FakeUser:
    def __init__(self, id, username=None, full_name="T"):
        self.id = id
        self.username = username
        self.full_name = full_name


class FakeMessage:
    def __init__(self):
        self.replies = []  # [(text, kwargs)]

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw))


class FakeChat:
    def __init__(self, id=1):
        self.id = id
        self.sent = []

    async def send_message(self, text, **kw):
        self.sent.append((text, kw))


class FakeQuery:
    def __init__(self, data, user):
        self.data = data
        self.from_user = user
        self.edits = []
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kw):
        self.edits.append((text, kw))


class FakeBot:
    def __init__(self):
        self.menus = []

    async def set_my_commands(self, commands, scope=None):
        self.menus.append((getattr(scope, "chat_id", None),
                           [(c.command, c.description) for c in commands]))


class FakeUpdate:
    def __init__(self, user, chat_id=1):
        self.effective_user = user
        self.effective_chat = FakeChat(chat_id)
        self.message = FakeMessage()
        self.callback_query = None


class FakeContext:
    def __init__(self, args=None, bot=None):
        self.args = args or []
        self.bot = bot or FakeBot()


async def _init_tmp_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    return "albion.db"


def _all_buttons(markup):
    """InlineKeyboardMarkup → плоский список кнопок."""
    return [b for row in (markup.inline_keyboard or []) for b in row]


async def _seed_personal_schedule(db=None):
    """Два своих класса (perma сегодня+завтра) и один чужой. Возвращает даты."""
    from src.db.repository import MeritHubClassRepository, MeritHubEnrollmentRepository
    from src.utils.recurrence import mh_weekday, org_now
    from datetime import timedelta

    crepo = MeritHubClassRepository(db) if db else MeritHubClassRepository()
    erepo = MeritHubEnrollmentRepository(db) if db else MeritHubEnrollmentRepository()
    today = org_now().date()
    tomorrow = today + timedelta(days=1)
    wd_today, wd_tomorrow = mh_weekday(today), mh_weekday(tomorrow)

    await crepo.upsert(
        "C20", title="Physics", class_type="perma",
        schedule_days=json.dumps([wd_today]),
        start_time=f"{today.isoformat()}T23:59:00+00:00",
        participant_link="plink20")
    await crepo.upsert(
        "C21", title="Maths", class_type="perma",
        schedule_days=json.dumps([wd_tomorrow]),
        start_time=f"{today.isoformat()}T23:58:00+00:00",
        participant_link="plink21")
    await crepo.upsert(
        "C22", title="Foreign", class_type="perma",
        schedule_days=json.dumps([wd_today, wd_tomorrow]),
        start_time=f"{today.isoformat()}T23:57:00+00:00",
        participant_link="plink22")
    await erepo.add("C20", "mh_s01", client_user_id="s01",
                    parent_telegram_id="555", student_name="Sofia")
    await erepo.add("C21", "mh_s02", client_user_id="s02",
                    parent_telegram_id="555", student_name="Max")
    await erepo.add("C22", "mh_s03", client_user_id="s03",
                    parent_telegram_id="777", student_name="Чужой")
    return today, tomorrow


# ── П1: персональная отмена ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_upcoming_lessons_personal_and_occurrence_aware(tmp_path, monkeypatch):
    """upcoming_lessons_for_parent: только свои, серии развёрнуты в даты."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.workflows.cancellation import upcoming_lessons_for_parent

    today, tomorrow = await _seed_personal_schedule(db)
    lessons = await upcoming_lessons_for_parent("555")

    by_class = {l["class_id"]: l for l in lessons}
    assert set(by_class) == {"C20", "C21"}           # чужого C22 нет
    assert by_class["C20"]["date"] in (today.isoformat(), tomorrow.isoformat())
    assert by_class["C21"]["date"] == tomorrow.isoformat()  # серия завтрашнего дня
    assert by_class["C20"]["student_name"] == "Sofia"
    assert "23:59" in by_class["C20"]["label"]       # читаемая подпись
    # Родитель без зачислений — пусто, а не все классы организации
    assert await upcoming_lessons_for_parent("999") == []


@pytest.mark.asyncio
async def test_upcoming_lessons_skips_started_today(tmp_path, monkeypatch):
    """Занятие сегодняшнего дня, которое уже началось, не предлагается к отмене."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.db.repository import MeritHubClassRepository, MeritHubEnrollmentRepository
    from src.utils.recurrence import mh_weekday, org_now
    from src.workflows.cancellation import upcoming_lessons_for_parent

    today = org_now().date()
    crepo = MeritHubClassRepository(db)
    await crepo.upsert(
        "C30", title="Early", class_type="perma",
        schedule_days=json.dumps([mh_weekday(today)]),
        start_time=f"{today.isoformat()}T00:00:00+00:00",  # 00:00 уже прошло
        end_date=today.isoformat())                        # серия кончается сегодня
    await MeritHubEnrollmentRepository(db).add(
        "C30", "mh_s01", client_user_id="s01",
        parent_telegram_id="555", student_name="Sofia")

    lessons = await upcoming_lessons_for_parent("555")
    assert lessons == []  # сегодняшнее (уже начавшееся) пропущено; других нет


@pytest.mark.asyncio
async def test_cancel_lesson_command_shows_personal_buttons(tmp_path, monkeypatch):
    """Без аргументов /cancel_lesson даёт кнопки своих занятий, а не запрос ID."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import cmd_cancel_lesson
    from src.db.repository import UserRepository

    await UserRepository(db).create("555", "parent", "Родитель")
    await _seed_personal_schedule(db)

    upd = FakeUpdate(FakeUser(555))
    await cmd_cancel_lesson(upd, FakeContext([]))

    text, kw = upd.message.replies[-1]
    assert "Какое занятие отменяем" in text
    assert "напишите координатору" in text
    btns = _all_buttons(kw["reply_markup"])
    assert any(b.callback_data.startswith("cancel_class:C20:") for b in btns)
    assert any(b.callback_data.startswith("cancel_class:C21:") for b in btns)
    assert not any("C22" in (b.callback_data or "") for b in btns)


@pytest.mark.asyncio
async def test_cancel_abort_keeps_lesson(tmp_path, monkeypatch):
    """«◀️ Не надо» на шаге подтверждения — занятие остаётся, события нет."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import handle_callback
    from src.db.repository import MeritHubClassRepository
    from src.events.bus import bus
    from src.events.types import EventTypes

    await MeritHubClassRepository(db).upsert("C9", title="Math", start_time="2099-07-31T15:00:00+00:00")

    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.LESSON_CANCELLED, cap)
    try:
        # Отказ на шаге подтверждения
        upd = FakeUpdate(FakeUser(555))
        upd.callback_query = FakeQuery("cancel_x", upd.effective_user)
        await handle_callback(upd, FakeContext())
    finally:
        bus.unsubscribe(EventTypes.LESSON_CANCELLED, cap)

    assert not captured
    edit = upd.callback_query.edits[-1][0]
    assert "остаётся в расписании" in edit


# ── П4: /lessons — «Мои занятия» ─────────────────────────────────────

@pytest.mark.asyncio
async def test_lessons_parent_lists_occurrences_and_links(tmp_path, monkeypatch):
    """Родитель: свои занятия + кнопки-ссылки на комнату (participant_link)."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import cmd_lessons
    from src.db.repository import UserRepository

    await UserRepository(db).create("555", "parent", "Родитель")
    _, tomorrow = await _seed_personal_schedule(db)

    upd = FakeUpdate(FakeUser(555))
    await cmd_lessons(upd, FakeContext([]))

    text, kw = upd.message.replies[-1]
    assert "Sofia" in text and "Max" in text
    assert "Чужой" not in text
    btns = _all_buttons(kw["reply_markup"])
    urls = [b.url for b in btns if b.url]
    assert any("plink20" in u for u in urls) and any("plink21" in u for u in urls)
    assert not any("plink22" in u for u in urls)          # чужая ссылка недоступна
    assert "постоянные" in text                            # подсказка про перевыдачу


@pytest.mark.asyncio
async def test_lessons_tutor_en_and_host_link(tmp_path, monkeypatch):
    """Тьютор: английский интерфейс (язык по роли) и host_link."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import cmd_lessons
    from src.db.repository import (
        MeritHubClassRepository, MeritHubContactRepository,
        MeritHubEnrollmentRepository, UserRepository,
    )
    from src.utils.recurrence import mh_weekday, org_now
    from datetime import timedelta

    await UserRepository(db).create("777", "tutor", "Daniel", language="en")
    await MeritHubContactRepository(db).upsert(
        "t01", telegram_id="777", role="tutor", name="Daniel John")
    tomorrow = org_now().date() + timedelta(days=1)
    await MeritHubClassRepository(db).upsert(
        "C40", title="Eng", class_type="perma",
        schedule_days=json.dumps([mh_weekday(tomorrow)]),
        start_time=f"{tomorrow.isoformat()}T15:00:00+00:00",
        tutor_client_user_id="t01", host_link="hlink40")
    await MeritHubEnrollmentRepository(db).add(
        "C40", "mh_s04", client_user_id="s04", student_name="Roman")

    upd = FakeUpdate(FakeUser(777))
    await cmd_lessons(upd, FakeContext([]))

    text, kw = upd.message.replies[-1]
    assert "Your upcoming lessons" in text           # EN (язык тьютора)
    assert "Roman" in text and "15:00" in text
    btns = _all_buttons(kw["reply_markup"])
    assert any("hlink40" in (b.url or "") for b in btns)   # host-ссылка тьютора


@pytest.mark.asyncio
async def test_lessons_empty_honest_message(tmp_path, monkeypatch):
    """Нет занятий → честное пустое состояние без обещаний магии."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import cmd_lessons
    from src.db.repository import UserRepository

    await UserRepository(db).create("555", "parent", "Родитель")
    upd = FakeUpdate(FakeUser(555))
    await cmd_lessons(upd, FakeContext([]))

    text = upd.message.replies[-1][0]
    assert "не вижу" in text


# ── П3: i18n ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_i18n_tr_fallbacks_and_lang_of(tmp_path, monkeypatch):
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.db.repository import UserRepository
    from src.utils.i18n import lang_of, tr

    await UserRepository(db).create("42", "tutor", "Daniel", language="en")
    await UserRepository(db).create("43", "parent", "Анна")  # язык по умолчанию ru

    assert await lang_of("42") == "en"
    assert await lang_of("43") == "ru"
    assert await lang_of("404") == "ru"                       # незнакомец → дефолт

    assert tr("tutor_btn_ready", "en") == "✅ Ready"
    assert tr("tutor_btn_ready", "de") == "✅ Готов(а)"       # неизвестный язык → ru
    assert tr("tutor_btn_ready") == "✅ Готов(а)"             # без языка → ru
    assert tr("no_such_key") == "no_such_key"                 # нет ключа → сам ключ
    # Форматирование и устойчивость к лишним/отсутствующим параметрам
    assert "Daniel" in tr("tutor_cancelled", "en", subject="Daniel — Eng", reason="x")
    # Без параметров шаблон возвращается как есть (сырые плейсхолдеры заметны при отладке)
    assert "{subject}" in tr("tutor_cancelled", "en")


@pytest.mark.asyncio
async def test_register_tutor_sets_en_language(tmp_path, monkeypatch):
    """Выбор роли при регистрации выставляет язык: tutor → en, иначе ru."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import handle_callback
    from src.db.repository import UserRepository

    upd = FakeUpdate(FakeUser(501, full_name="Daniel"))
    upd.callback_query = FakeQuery("register_tutor", upd.effective_user)
    await handle_callback(upd, FakeContext())
    row = await UserRepository(db).get_by_telegram_id("501")
    assert row["language"] == "en"

    upd2 = FakeUpdate(FakeUser(502, full_name="Анна"))
    upd2.callback_query = FakeQuery("register_parent", upd2.effective_user)
    await handle_callback(upd2, FakeContext())
    row2 = await UserRepository(db).get_by_telegram_id("502")
    assert row2["language"] == "ru"


# ── Авто-утренняя сводка ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_morning_digest_text_occurrence_aware(tmp_path, monkeypatch):
    """Сводка показывает perma-серии по дню недели и не показывает чужие даты."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.db.repository import MeritHubClassRepository, MeritHubEnrollmentRepository
    from src.utils.recurrence import mh_weekday, org_now
    from src.workflows.lesson_ops import build_morning_digest_text
    from datetime import timedelta

    today = org_now().date()
    tomorrow = today + timedelta(days=1)
    crepo = MeritHubClassRepository(db)
    await crepo.upsert(
        "C50", title="Physics", class_type="perma",
        schedule_days=json.dumps([mh_weekday(today)]),
        start_time=f"{today.isoformat()}T15:30:00+00:00")
    await crepo.upsert(
        "C51", title="Tomorrow", class_type="perma",
        schedule_days=json.dumps([mh_weekday(tomorrow)]),
        start_time=f"{tomorrow.isoformat()}T10:00:00+00:00")
    await MeritHubEnrollmentRepository(db).add(
        "C50", "mh_s01", client_user_id="s01", student_name="Sofia")

    text = await build_morning_digest_text()
    assert "Доброе утро" in text
    assert "🔁" in text and "Sofia" in text and "15:30" in text
    assert "Tomorrow" not in text                       # завтрашняя серия — не сегодня


@pytest.mark.asyncio
async def test_ensure_morning_digest_idempotent(tmp_path, monkeypatch):
    """Двойной вызов на старте → ровно одна запланированная сводка."""
    await _init_tmp_db(tmp_path, monkeypatch)
    from src.db.repository import ScheduledActionRepository
    from src.workflows.lesson_ops import MORNING_DIGEST_ACTION, ensure_morning_digest

    await ensure_morning_digest()
    await ensure_morning_digest()

    rows = await ScheduledActionRepository()._fetchall(
        "SELECT * FROM scheduled_actions WHERE action=? AND status='pending'",
        (MORNING_DIGEST_ACTION,))
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_morning_digest_tick_sends_and_reschedules(tmp_path, monkeypatch):
    """Тик scheduler'а: сводка уходит координаторам, задача пересоздаётся на след. день."""
    await _init_tmp_db(tmp_path, monkeypatch)
    from src.db.repository import (
        ScheduledActionRepository, UserRepository, WorkflowRepository,
    )
    from src.events.bus import bus
    from src.events.types import Event, EventTypes
    from src.workflows.lesson_ops import (
        MORNING_DIGEST_ACTION, LessonOpsWorkflow, _schedule_next_digest,
    )

    await UserRepository().create("coord_1", "coordinator", "Boss")
    aid = await _schedule_next_digest()
    action_row = await ScheduledActionRepository()._fetchone(
        "SELECT * FROM scheduled_actions WHERE id=?", (aid,))
    wid = action_row["workflow_id"]
    # Scheduler помечает действие исполненным ДО публикации тика (его работа,
    # не хендлера) — воспроизводим это, чтобы не зависеть от внутренней механики.
    await ScheduledActionRepository()._execute(
        "UPDATE scheduled_actions SET status='executed' WHERE id=?", (aid,))

    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        await LessonOpsWorkflow().handle_scheduler_tick(Event(EventTypes.SCHEDULER_TICK, {
            "action": MORNING_DIGEST_ACTION,
            "workflow_id": wid,
            "data": {"workflow_id": wid},
        }))
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)

    # Координатор получил сводку
    assert any(d.get("telegram_id") == "coord_1" and "Доброе утро" in d.get("message", "")
               for d in captured)
    # Старый workflow завершён, новая задача на завтра создана
    wf = await WorkflowRepository().get(wid)
    assert wf["state"] == "completed"
    rows = await ScheduledActionRepository()._fetchall(
        "SELECT * FROM scheduled_actions WHERE action=? AND status='pending'",
        (MORNING_DIGEST_ACTION,))
    assert len(rows) == 1 and rows[0]["workflow_id"] != wid
