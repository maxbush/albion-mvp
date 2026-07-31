"""Round 4 — UX-работы U1–U7 (только существующие механики).

U1: меню команд «/» по роли (set_my_commands + BotCommandScopeChat).
"""

import pytest

from src.config import settings


class FakeUser:
    def __init__(self, id, username=None, full_name="T"):
        self.id = id
        self.username = username
        self.full_name = full_name


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw))


class FakeChatRich:
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

    async def edit_message_reply_markup(self, markup):
        pass


class FakeBot:
    """Ловит вызовы bot-API, которые нам важны в UX-тестах."""
    def __init__(self):
        self.menus = []          # (chat_id, [(cmd, desc), ...])
        self.sent = []

    async def set_my_commands(self, commands, scope=None):
        chat_id = getattr(scope, "chat_id", None)
        self.menus.append((chat_id, [(c.command, c.description) for c in commands]))

    async def send_message(self, chat_id=None, text=None, reply_markup=None, **kw):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})

    async def edit_message_text(self, text=None, chat_id=None, message_id=None, **kw):
        self.sent.append({"chat_id": chat_id, "message_id": message_id, "text": text})


class FakeUpdate:
    def __init__(self, user, chat_id=1):
        self.effective_user = user
        self.effective_chat = FakeChatRich(chat_id)
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


# ── U1: меню команд по роли ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_u1_menu_applied_on_registration(tmp_path, monkeypatch):
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import handle_callback

    upd = FakeUpdate(FakeUser(501))
    upd.callback_query = FakeQuery("register_coordinator", upd.effective_user)
    ctx = FakeContext(bot=FakeBot())
    await handle_callback(upd, ctx)

    assert ctx.bot.menus, "меню должно быть выставлено при регистрации"
    chat_id, items = ctx.bot.menus[-1]
    assert chat_id == 501
    names = {c for c, _ in items}
    # Координаторское меню содержит операционные команды.
    assert {"today", "incidents", "morning", "ok"} <= names


@pytest.mark.asyncio
async def test_u1_menu_parent_is_minimal(tmp_path, monkeypatch):
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import handle_callback

    upd = FakeUpdate(FakeUser(502))
    upd.callback_query = FakeQuery("register_parent", upd.effective_user)
    ctx = FakeContext(bot=FakeBot())
    await handle_callback(upd, ctx)

    chat_id, items = ctx.bot.menus[-1]
    assert chat_id == 502
    names = {c for c, _ in items}
    # Родителю — минимум (Hick's Law), без координаторских команд.
    assert "today" not in names and "incidents" not in names
    assert {"start", "status", "cancel_lesson", "whoami"} == names


@pytest.mark.asyncio
async def test_u1_role_command_updates_target_menu(tmp_path, monkeypatch):
    db = await _init_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "900")
    from src.bot.roles import cmd_role

    upd = FakeUpdate(FakeUser(900))
    ctx = FakeContext(["601", "tutor"], bot=FakeBot())
    await cmd_role(upd, ctx)

    chat_id, items = ctx.bot.menus[-1]
    assert chat_id == 601  # меню выставлено ЦЕЛЕВОМУ пользователю
    names = {c for c, _ in items}
    assert "cancel_lesson" in names and "today" not in names


@pytest.mark.asyncio
async def test_u1_menu_survives_bot_api_failure(tmp_path, monkeypatch):
    """Ошибка Telegram API при set_my_commands не должна ломать регистрацию."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import handle_callback

    class BadBot(FakeBot):
        async def set_my_commands(self, commands, scope=None):
            raise RuntimeError("telegram down")

    upd = FakeUpdate(FakeUser(503))
    upd.callback_query = FakeQuery("register_parent", upd.effective_user)
    ctx = FakeContext(bot=BadBot())
    await handle_callback(upd, ctx)  # не должно упасть
    # Пользователь всё равно зарегистрирован.
    from src.db.repository import UserRepository
    assert (await UserRepository(db).get_by_telegram_id("503"))["role"] == "parent"


# ── U2: кнопки действий на эскалации ─────────────────────────────────

async def _setup_escalation(db, engine_module=True):
    """Создаёт инцидент + workflow + координатора и возвращает id инцидента."""
    from src.db.repository import (
        IncidentRepository, ScheduledActionRepository, UserRepository, WorkflowRepository,
    )
    from src.workflows.engine import engine

    engine.repo = WorkflowRepository(db)
    engine.scheduler = ScheduledActionRepository(db)
    await UserRepository(db).create("999", "coordinator", "Мария Координатор")
    await UserRepository(db).create("555", "parent", "Родитель Миши")
    inc_id = await IncidentRepository(db).create(
        lesson_ref="C9", type="absence", status="pending")
    wid = await engine.start_workflow("absence_notification", {
        "incident_id": inc_id, "student_name": "Миша",
        "parent_telegram_id": "555", "lesson_ref": "C9"})
    return inc_id, wid


@pytest.mark.asyncio
async def test_u2_escalation_carries_action_buttons(tmp_path, monkeypatch):
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.events.bus import bus
    from src.events.types import EventTypes
    from src.workflows.absence import AbsenceWorkflow

    inc_id, wid = await _setup_escalation(db)
    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        await AbsenceWorkflow(db)._escalate(wid, inc_id)
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)

    esc = [d for d in captured if d.get("telegram_id") == "999"][-1]
    buttons = esc.get("buttons")
    assert buttons, "у эскалации должны быть кнопки действий"
    kinds = {tuple(k for k in b if b.get(k)) for b in buttons}
    assert any(b.get("callback_data") == f"coord_resolve:{inc_id}:ok" for b in buttons)
    assert any(b.get("url") == "tg://user?id=555" for b in buttons)


@pytest.mark.asyncio
async def test_u2_coordinator_closes_from_escalation(tmp_path, monkeypatch):
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import handle_callback
    from src.db.repository import IncidentRepository

    inc_id, wid = await _setup_escalation(db)
    from src.workflows.absence import AbsenceWorkflow
    await AbsenceWorkflow(db)._escalate(wid, inc_id)

    upd = FakeUpdate(FakeUser(999, full_name="Мария Координатор"))
    upd.callback_query = FakeQuery(f"coord_resolve:{inc_id}:ok", upd.effective_user)
    upd.callback_query.message = type("M", (), {"text": "🚨 Эскалация базовая"})()
    ctx = FakeContext()
    await handle_callback(upd, ctx)

    inc = await IncidentRepository(db).get(inc_id)
    assert inc["status"] == "resolved"
    assert inc["resolution"] == "coordinator_closed"
    # Сообщение отредактировано с отметкой (а не новое в чат).
    assert upd.callback_query.edits, "эскалация должна редактироваться"
    assert "Закрыто" in upd.callback_query.edits[-1][0]
    assert "Мария Координатор" in upd.callback_query.edits[-1][0]


@pytest.mark.asyncio
async def test_u2_non_coordinator_cannot_close(tmp_path, monkeypatch):
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import handle_callback
    from src.db.repository import IncidentRepository

    inc_id, wid = await _setup_escalation(db)
    from src.workflows.absence import AbsenceWorkflow
    await AbsenceWorkflow(db)._escalate(wid, inc_id)

    # Родитель получил пересланную эскалацию и тыкает кнопку.
    upd = FakeUpdate(FakeUser(555))
    upd.callback_query = FakeQuery(f"coord_resolve:{inc_id}:ok", upd.effective_user)
    ctx = FakeContext()
    await handle_callback(upd, ctx)

    inc = await IncidentRepository(db).get(inc_id)
    assert inc["status"] == "escalated", "не-координатор не должен закрывать"
    assert any("координатор" in (t or "") for t, _ in upd.callback_query.answers)


@pytest.mark.asyncio
async def test_u2_double_close_is_safe(tmp_path, monkeypatch):
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import handle_callback
    from src.db.repository import IncidentRepository

    inc_id, wid = await _setup_escalation(db)
    from src.workflows.absence import AbsenceWorkflow
    wf = AbsenceWorkflow(db)
    await wf._escalate(wid, inc_id)
    await wf.resolve_absence(inc_id, "999", resolution="first_close")

    upd = FakeUpdate(FakeUser(999))
    upd.callback_query = FakeQuery(f"coord_resolve:{inc_id}:ok", upd.effective_user)
    ctx = FakeContext()
    await handle_callback(upd, ctx)

    inc = await IncidentRepository(db).get(inc_id)
    assert inc["resolution"] == "first_close", "второе закрытие не перезаписывает"
    assert any("закрыта" in (t or "") for t, _ in upd.callback_query.answers)


# ── U3: подтверждения опасных действий ───────────────────────────────

@pytest.mark.asyncio
async def test_u3_demo_reset_preview_keeps_data(tmp_path, monkeypatch):
    """Команда НЕ удаляет до подтверждения и показывает, что именно будет стёрто."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    from src.bot.pilot import cmd_demo_reset
    from src.db.repository import IncidentRepository

    await IncidentRepository(db).create(lesson_ref="l1", type="absence", status="pending")
    upd = FakeUpdate(FakeUser(100))
    await cmd_demo_reset(upd, FakeContext([]))

    text, kw = upd.message.replies[-1]
    assert "Сбросить демо-данные" in text
    assert "incidents: 1" in text                    # превью последствий
    assert kw.get("reply_markup") is not None        # кнопки подтверждения
    incs = await IncidentRepository(db)._fetchall("SELECT * FROM incidents")
    assert len(incs) == 1, "превью ничего не удаляет"


@pytest.mark.asyncio
async def test_u3_demo_reset_confirm_flow(tmp_path, monkeypatch):
    """Кнопка confirm реально сбрасывает; cancel оставляет всё как есть."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    from src.bot.handlers import handle_callback
    from src.db.repository import IncidentRepository

    await IncidentRepository(db).create(lesson_ref="l1", type="absence", status="pending")

    # cancel — данные целы
    upd = FakeUpdate(FakeUser(100))
    upd.callback_query = FakeQuery("demo_reset:cancel", upd.effective_user)
    await handle_callback(upd, FakeContext())
    assert len(await IncidentRepository(db)._fetchall("SELECT * FROM incidents")) == 1
    assert "отменён" in upd.callback_query.edits[-1][0]

    # confirm — сброс + сообщение с результатом
    upd = FakeUpdate(FakeUser(100))
    upd.callback_query = FakeQuery("demo_reset:confirm", upd.effective_user)
    await handle_callback(upd, FakeContext())
    assert len(await IncidentRepository(db)._fetchall("SELECT * FROM incidents")) == 0
    assert "Демо-сброс выполнен" in upd.callback_query.edits[-1][0]


@pytest.mark.asyncio
async def test_u3_demo_reset_confirm_admin_only(tmp_path, monkeypatch):
    db = await _init_tmp_db(tmp_path, monkeypatch)
    # Админов нет вообще → никто не может подтвердить сброс.
    from src.bot.handlers import handle_callback
    from src.db.repository import IncidentRepository

    await IncidentRepository(db).create(lesson_ref="l1", type="absence", status="pending")
    upd = FakeUpdate(FakeUser(777))
    upd.callback_query = FakeQuery("demo_reset:confirm", upd.effective_user)
    await handle_callback(upd, FakeContext())
    assert len(await IncidentRepository(db)._fetchall("SELECT * FROM incidents")) == 1
    assert any("админ" in (t or "") for t, _ in upd.callback_query.answers)


@pytest.mark.asyncio
async def test_u3_kill_switch_buttons(tmp_path, monkeypatch):
    db = await _init_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    from src.bot.handlers import cmd_kill_switch, handle_callback, get_kill_switch_level

    # Команда без аргументов → кнопки уровней, а не подсказка синтаксиса.
    upd = FakeUpdate(FakeUser(100))
    await cmd_kill_switch(upd, FakeContext([]))
    text, kw = upd.message.replies[-1]
    assert "Kill Switch" in text and kw.get("reply_markup") is not None
    assert "Сейчас" in text

    # Нажатие кнопки → уровень сменился, сообщение отредактировано.
    upd = FakeUpdate(FakeUser(100))
    upd.callback_query = FakeQuery("killswitch:1", upd.effective_user)
    await handle_callback(upd, FakeContext())
    try:
        assert get_kill_switch_level() == 1
        assert "координаторам" in upd.callback_query.edits[-1][0]
    finally:
        upd = FakeUpdate(FakeUser(100))
        upd.callback_query = FakeQuery("killswitch:2", upd.effective_user)
        await handle_callback(upd, FakeContext())  # возвращаем дефолт


@pytest.mark.asyncio
async def test_u3_kill_switch_args_still_work(tmp_path, monkeypatch):
    """Обратная совместимость: /kill_switch 1 работает как раньше."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    from src.bot.handlers import cmd_kill_switch, get_kill_switch_level

    upd = FakeUpdate(FakeUser(100))
    await cmd_kill_switch(upd, FakeContext(["1"]))
    try:
        assert get_kill_switch_level() == 1
    finally:
        upd = FakeUpdate(FakeUser(100))
        await cmd_kill_switch(upd, FakeContext(["2"]))


# ── U4: онбординг ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_u4_registration_shows_honest_expectations(tmp_path, monkeypatch):
    """После регистрации — что бот РЕАЛЬНО делает, а не «разберусь» AI-магией."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import handle_callback

    upd = FakeUpdate(FakeUser(700))
    upd.callback_query = FakeQuery("register_parent", upd.effective_user)
    await handle_callback(upd, FakeContext())
    text = upd.callback_query.edits[-1][0]
    assert "разберусь" not in text
    assert "напомню" in text            # реактивная механика — напоминания
    assert "/cancel_lesson" in text     # существующая команда, которую можно упомянуть


@pytest.mark.asyncio
async def test_u4_tutor_registration_expectations(tmp_path, monkeypatch):
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import handle_callback

    upd = FakeUpdate(FakeUser(701))
    upd.callback_query = FakeQuery("register_tutor", upd.effective_user)
    await handle_callback(upd, FakeContext())
    text = upd.callback_query.edits[-1][0]
    assert "подтвердить готовность" in text or "отметить старт" in text


@pytest.mark.asyncio
async def test_u4_start_coordinator_single_message_with_help_button(tmp_path, monkeypatch):
    """Для координатора /start — одно сообщение; помощь по кнопке, с возвратом."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import cmd_start, handle_callback
    from src.db.repository import UserRepository

    await UserRepository(db).create("702", "coordinator", "Coord")
    upd = FakeUpdate(FakeUser(702))
    await cmd_start(upd, FakeContext([]))
    assert len(upd.message.replies) == 1
    text, kw = upd.message.replies[0]
    markup = kw.get("reply_markup")
    assert markup is not None
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "📋 Команды" in labels

    # Тап «Команды» → помощь; тап «Назад» → снова приветствие.
    upd.callback_query = FakeQuery("help_commands", upd.effective_user)
    await handle_callback(upd, FakeContext([]))
    assert "/today" in upd.callback_query.edits[-1][0]
    upd.callback_query = FakeQuery("help_back", upd.effective_user)
    await handle_callback(upd, FakeContext([]))
    assert "С возвращением" in upd.callback_query.edits[-1][0]


@pytest.mark.asyncio
async def test_u4_help_commands_for_parent_points_to_menu(tmp_path, monkeypatch):
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import handle_callback
    from src.db.repository import UserRepository

    await UserRepository(db).create("703", "parent", "Parent")
    upd = FakeUpdate(FakeUser(703))
    upd.callback_query = FakeQuery("help_commands", upd.effective_user)
    await handle_callback(upd, FakeContext([]))
    text = upd.callback_query.edits[-1][0]
    assert "меню" in text.lower()
    assert "/cancel_lesson" in text


# ── U5: чистка текстов ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_u5_absence_message_has_no_duplicated_option_list(tmp_path, monkeypatch):
    """Варианты ответов называют кнопки — в тексте они не дублируются."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.db.repository import (
        IncidentRepository, ScheduledActionRepository, UserRepository, WorkflowRepository,
    )
    from src.events.bus import bus
    from src.events.types import EventTypes
    from src.workflows.absence import AbsenceWorkflow
    from src.workflows.engine import engine

    engine.repo = WorkflowRepository(db)
    engine.scheduler = ScheduledActionRepository(db)
    await UserRepository(db).create("555", "parent", "Родитель")
    inc_id = await IncidentRepository(db).create(type="absence", status="pending")
    wid = await engine.start_workflow("absence_notification", {
        "incident_id": inc_id, "student_name": "Миша",
        "parent_telegram_id": "555", "lesson_ref": "C9"})

    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        await AbsenceWorkflow(db)._notify_parent(wid, inc_id)
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)

    parent_msg = [d for d in captured if d.get("telegram_id") == "555"][0]
    text = parent_msg["message"]
    assert "• всё в порядке" not in text          # дубль-список убран
    assert "• ученик опоздает" not in text
    assert "отсутствовал(а)" in text
    assert "кнопкой ниже или просто текстом" in text
    # А кнопки на месте со всеми тремя вариантами
    btn_texts = [b["text"] for b in parent_msg["buttons"]]
    assert any("в порядке" in t.lower() for t in btn_texts)
    assert any("опозда" in t.lower() for t in btn_texts)

@pytest.mark.asyncio
async def test_u5_resolve_confirmation_names_student(tmp_path, monkeypatch):
    """Подтверждение закрытия содержит имя ученика, а не только «Ситуация #N»."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import handle_callback
    from src.db.repository import (
        IncidentRepository, ScheduledActionRepository, UserRepository, WorkflowRepository,
    )
    from src.workflows.engine import engine

    engine.repo = WorkflowRepository(db)
    engine.scheduler = ScheduledActionRepository(db)
    await UserRepository(db).create("555", "parent", "Родитель")
    inc_id = await IncidentRepository(db).create(type="absence", status="pending")
    wid = await engine.start_workflow("absence_notification", {
        "incident_id": inc_id, "student_name": "Миша Иванов",
        "parent_telegram_id": "555", "lesson_ref": "C9"})
    # Нонс как в реальном флоу.
    repo = WorkflowRepository(db)
    data_engine = await engine.repo.get(wid)
    import json as _json
    data = _json.loads(data_engine["data"])
    data["parent_callback_nonce"] = "nonce1"
    await repo.update_data(wid, data)

    upd = FakeUpdate(FakeUser(555))
    upd.callback_query = FakeQuery(f"resolve:{inc_id}:nonce1:ok", upd.effective_user)
    await handle_callback(upd, FakeContext())

    text = upd.callback_query.edits[-1][0]
    assert "Миша Иванов" in text
    assert "закрыта" in text
    assert "закрыта в " not in text, "серверное время убрано из подтверждения"


# ── U6: отмена занятия кнопками ──────────────────────────────────────

@pytest.mark.asyncio
async def test_u6_cancel_intent_offers_class_buttons(tmp_path, monkeypatch):
    """Интент «отмена» → кнопки с реальными занятиями (ввод ID не нужен)."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.db.repository import MeritHubClassRepository, UserRepository
    from src.events.bus import bus
    from src.events.types import Event, EventTypes
    from src.workflows.cancellation import CancellationWorkflow

    await UserRepository(db).create("coord_1", "coordinator", "Координатор")
    await MeritHubClassRepository(db).upsert("C9", title="Math", start_time="2099-07-31T15:00:00+00:00")
    await MeritHubClassRepository(db).upsert("C10", title="Eng", start_time="2099-08-01T10:00:00+00:00")

    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        await CancellationWorkflow(db).handle_classified(Event(EventTypes.MESSAGE_CLASSIFIED, {
            "intent": "cancellation", "telegram_id": "555", "text": "отмени урок",
        }))
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)

    msg = [d for d in captured if d.get("telegram_id") == "555"][-1]
    assert "Какое занятие отменяем" in msg["message"]
    btns = msg["buttons"]
    assert any(b["callback_data"] == "cancel_class:C9" for b in btns)
    assert any(b["callback_data"] == "cancel_class:C10" for b in btns)
    # Кнопка читаема: содержит ID и время
    assert any("C9" in b["text"] and "15:00" in b["text"] for b in btns)


@pytest.mark.asyncio
async def test_u6_cancel_button_fires_existing_flow(tmp_path, monkeypatch):
    """Тап по кнопке → тот же LESSON_CANCELLED, что и команда (обвязка, не новая механика)."""
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
        upd = FakeUpdate(FakeUser(555))
        upd.callback_query = FakeQuery("cancel_class:C9", upd.effective_user)
        await handle_callback(upd, FakeContext())
    finally:
        bus.unsubscribe(EventTypes.LESSON_CANCELLED, cap)

    event = captured[-1]
    assert event["lesson_id"] == "C9"
    assert event["reported_by"] == "555"
    edit = upd.callback_query.edits[-1][0]
    assert "передана репетитору и координаторам" in edit
    assert "C9" in edit and "15:00" in edit  # человекочитаемый label


@pytest.mark.asyncio
async def test_u6_cancel_intent_fallback_without_classes(tmp_path, monkeypatch):
    """Нет известных занятий → старая текстовая подсказка."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.events.bus import bus
    from src.events.types import Event, EventTypes
    from src.workflows.cancellation import CancellationWorkflow

    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        await CancellationWorkflow(db).handle_classified(Event(EventTypes.MESSAGE_CLASSIFIED, {
            "intent": "cancellation", "telegram_id": "555",
        }))
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)

    msg = [d for d in captured if d.get("telegram_id") == "555"][-1]
    assert "/cancel_lesson <ID>" in msg["message"]
    assert not msg.get("buttons")


# ── U7: первый текст от незнакомца → выбор роли ──────────────────────

@pytest.mark.asyncio
async def test_u7_first_text_from_stranger_gets_role_choice(tmp_path, monkeypatch):
    """Новый пользователь пишет текстом → не создаём его молча «родителем»,
    а показываем выбор роли. Сообщение НЕ классифицируется до выбора."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import handle_message
    from src.db.repository import UserRepository
    from src.events.bus import bus
    from src.events.types import EventTypes

    classified = []

    async def cap(ev):
        classified.append(ev.data)

    bus.subscribe(EventTypes.MESSAGE_INCOMING, cap)
    try:
        upd = FakeUpdate(FakeUser(888))
        upd.message.text = "мой сын не придёт сегодня"
        await handle_message(upd, FakeContext())
    finally:
        bus.unsubscribe(EventTypes.MESSAGE_INCOMING, cap)

    # Роли-выбор показан, пользователь НЕ создан, текст НЕ классифицирован.
    text, kw = upd.message.replies[-1]
    assert "впервые" in text
    assert kw.get("reply_markup") is not None
    assert await UserRepository(db).get_by_telegram_id("888") is None
    assert not classified


@pytest.mark.asyncio
async def test_u7_known_user_text_still_processed(tmp_path, monkeypatch):
    """Регрессия: зарегистрированный пользователь → обычная обработка текста."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import handle_message
    from src.db.repository import UserRepository
    from src.events.bus import bus
    from src.events.types import EventTypes

    await UserRepository(db).create("889", "parent", "Родитель")
    incoming = []

    async def cap(ev):
        incoming.append(ev.data)

    bus.subscribe(EventTypes.MESSAGE_INCOMING, cap)
    try:
        upd = FakeUpdate(FakeUser(889))
        upd.message.text = "обычное сообщение без контекста"
        await handle_message(upd, FakeContext())
    finally:
        bus.unsubscribe(EventTypes.MESSAGE_INCOMING, cap)

    # Как раньше: текст уходит в классификатор.
    assert incoming, "текст известного пользователя должен попадать в MESSAGE_INCOMING"


@pytest.mark.asyncio
async def test_u7_after_role_choice_user_can_retext(tmp_path, monkeypatch):
    """Полный путь: текст → выбор роли → выбрал parent → повторное сообщение обрабатывается."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import handle_callback, handle_message
    from src.db.repository import UserRepository

    upd = FakeUpdate(FakeUser(890))
    upd.message.text = "привет"
    await handle_message(upd, FakeContext())

    upd.callback_query = FakeQuery("register_parent", upd.effective_user)
    await handle_callback(upd, FakeContext())
    assert (await UserRepository(db).get_by_telegram_id("890"))["role"] == "parent"

    # Повторное сообщение теперь проходит как у обычного родителя —
    # без инцидента/чекина попадёт в классификатор (ответ «Обрабатываю...»).
    upd2 = FakeUpdate(FakeUser(890))
    upd2.message.text = "привет ещё раз"
    await handle_message(upd2, FakeContext())
    assert upd2.message.replies, "повторный текст обработан"
