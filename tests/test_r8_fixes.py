"""Round 8 — total audit fixes (MASTER_PLAN v5).

Проверяем:
1) R8-8: SafeStreamHandler не падает с UnicodeEncodeError и не спамит трейсбек
   при записи emoji / кириллицы в ASCII-поток.
"""

import io
import logging
import pytest
from src.utils.logging import SafeStreamHandler, setup_logging


class FakeAsciiStream(io.StringIO):
    """Эмуляция потока с ASCII-кодировкой, который выбрасывает UnicodeEncodeError
    при записи символов вне ASCII, как sys.stdout в урезанных контейнерах."""
    encoding = "ascii"

    def write(self, s: str) -> int:
        # Пытаемся закодировать в ascii: если есть не-ASCII символы, упадёт.
        s.encode("ascii", errors="strict")
        return super().write(s)


def test_safe_stream_handler_ascii_fallback(caplog):
    """R8-8: SafeStreamHandler превентивно кодирует сообщение и заменяет символы
    при записи в поток с ограниченной кодировкой (ascii), без вызова handleError."""
    stream = FakeAsciiStream()
    handler = SafeStreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))

    logger = logging.getLogger("test_safe_stream")
    logger.setLevel(logging.INFO)
    logger.handlers = [handler]

    # Запись с emoji и кириллицей в ASCII stream не должна упасть или вызвать handleError
    logger.info("Привет 👨‍👩‍👦 Hello")

    output = stream.getvalue()
    assert "Hello" in output
    # Кириллица и emoji должны быть безопасно заменены на '?'
    assert "?" in output
    # Не должно быть системного спама об ошибке логирования
    assert "--- Logging error ---" not in output


class FakeUtf8Stream(io.StringIO):
    encoding = "utf-8"


def test_safe_stream_handler_utf8_clean():
    """R8-8: Для utf-8 потока SafeStreamHandler сохраняет все символы без изменений."""
    stream = FakeUtf8Stream()
    handler = SafeStreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))

    logger = logging.getLogger("test_safe_stream_utf8")
    logger.setLevel(logging.INFO)
    logger.handlers = [handler]

    logger.info("Привет 👨‍👩‍👦 Hello")
    output = stream.getvalue()
    assert "Привет 👨‍👩‍👦 Hello" in output


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


class FakeChat:
    def __init__(self, id=1, type="private"):
        self.id = id
        self.type = type


class FakeUpdate:
    def __init__(self, user, chat_id=1, chat_type="private"):
        self.effective_user = user
        self.effective_chat = FakeChat(chat_id, type=chat_type)
        self.message = FakeMessage()
        self.callback_query = None


class FakeContext:
    def __init__(self, args=None, bot=None):
        self.args = args or []
        self.bot = bot


async def _init_tmp_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "albion.db")
    from src.db.migrations import init_db
    await init_db(db)
    return db


@pytest.mark.asyncio
async def test_r8_2_coordinator_tools_access_for_non_admin_coordinator(tmp_path, monkeypatch):
    """R8-2: не-админ координатор имеет доступ к /incidents, /today, /morning, /leads и /schedule,
    а обычные пользователи (parent, stranger) получают отказ."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.config import settings
    from src.db.repository import UserRepository
    from src.bot.pilot import cmd_incidents, cmd_leads, cmd_today, cmd_morning_digest
    from src.bot.wizard import cmd_schedule

    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    users = UserRepository(db)
    await users.set_role_by_telegram("888", "coordinator")
    await users.set_role_by_telegram("999", "parent")

    # 1. Незнакомец (777) или родитель (999) не могут открыть команды координатора
    for tg_id in (777, 999):
        upd = FakeUpdate(FakeUser(tg_id, "user"))
        await cmd_incidents(upd, FakeContext())
        assert any("⛔" in t for t, _ in upd.message.replies)

    # 2. Координатор (888), не являющийся админом в settings, имеет доступ ко всем 5 командам
    coord_upd = FakeUpdate(FakeUser(888, "coord"), chat_id=888)
    await cmd_incidents(coord_upd, FakeContext())
    assert not any("⛔" in t for t, _ in coord_upd.message.replies)

    coord_upd = FakeUpdate(FakeUser(888, "coord"), chat_id=888)
    await cmd_leads(coord_upd, FakeContext())
    assert not any("⛔" in t for t, _ in coord_upd.message.replies)

    coord_upd = FakeUpdate(FakeUser(888, "coord"), chat_id=888)
    await cmd_today(coord_upd, FakeContext())
    assert not any("⛔" in t for t, _ in coord_upd.message.replies)

    coord_upd = FakeUpdate(FakeUser(888, "coord"), chat_id=888)
    await cmd_morning_digest(coord_upd, FakeContext())
    assert not any("⛔" in t for t, _ in coord_upd.message.replies)

    coord_upd = FakeUpdate(FakeUser(888, "coord"), chat_id=888)
    await cmd_schedule(coord_upd, FakeContext())
    assert not any("⛔" in t for t, _ in coord_upd.message.replies)


@pytest.mark.asyncio
async def test_r8_3_cmd_ok_access_control(tmp_path, monkeypatch):
    """R8-3: /ok запрещён не-координаторам/не-админам и разрешён координатору."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.db.repository import UserRepository
    from src.bot.handlers import cmd_ok

    users = UserRepository(db)
    await users.set_role_by_telegram("888", "coordinator")
    await users.set_role_by_telegram("999", "parent")

    # 1. Незнакомец и родитель получают отказ
    for tg_id in (777, 999):
        upd = FakeUpdate(FakeUser(tg_id, "stranger"))
        await cmd_ok(upd, FakeContext(["1"]))
        assert any("⛔" in t for t, _ in upd.message.replies)

    # 2. Координатор проходит проверку прав
    upd = FakeUpdate(FakeUser(888, "coord"))
    await cmd_ok(upd, FakeContext())  # без аргументов
    assert any("Используйте: /ok <ID ситуации>" in t for t, _ in upd.message.replies)
    assert not any("⛔" in t for t, _ in upd.message.replies)


@pytest.mark.asyncio
async def test_r8_5_webhook_lesson_started_and_completed_subscribers(tmp_path, monkeypatch):
    """R8-5: LESSON_STARTED (lv) мгновенно закрывает class_live_check,
    а LESSON_COMPLETED (cp) закрывает оставшиеся prelesson-проверки урока."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.db.repository import WorkflowRepository, ScheduledActionRepository
    from src.workflows.lesson_ops import register_handlers
    from src.events.bus import bus
    from src.events.types import Event, EventTypes

    await register_handlers()
    wf_repo = WorkflowRepository(db)
    sched_repo = ScheduledActionRepository(db)

    # 1. Проверяем LESSON_STARTED -> реактивное закрытие class_live_check
    wid_live = await wf_repo.create("class_live_check", "running", {"class_id": "cls_lv"})
    await sched_repo.create(wid_live, "2026-08-02T15:00:00Z", "class_live_check", {"workflow_id": wid_live})

    await bus.publish(Event(EventTypes.LESSON_STARTED, {"class_id": "cls_lv", "status": "lv"}))

    wf = await wf_repo.get(wid_live)
    assert wf["state"] == "completed"
    assert '"resolved_by": "webhook_lv"' in wf["data"]
    assert '"response_status": "class_live"' in wf["data"]
    # Запланированная проверка через 90 мин должна быть отменена
    actions = await sched_repo._fetchall("SELECT status FROM scheduled_actions WHERE workflow_id=?", (wid_live,))
    assert actions[0]["status"] == "cancelled"

    # 2. Проверяем LESSON_COMPLETED -> завершение активного tutor_start_check
    wid_start = await wf_repo.create("tutor_start_check", "running", {"class_id": "cls_cp"})
    await bus.publish(Event(EventTypes.LESSON_COMPLETED, {"class_id": "cls_cp", "status": "cp"}))

    wf_cp = await wf_repo.get(wid_start)
    assert wf_cp["state"] == "completed"
    assert '"resolved_by": "webhook_cp"' in wf_cp["data"]


class FakeCallbackQuery:
    def __init__(self, data, user_id=100):
        self.data = data
        self.from_user = FakeUser(user_id, "test_user")
        self.message = FakeMessage()
        self.edits = []
        self.answers = []

    async def edit_message_text(self, text, **kw):
        self.edits.append((text, kw))

    async def answer(self, text=None, **kw):
        self.answers.append((text, kw))


@pytest.mark.asyncio
async def test_r8_6_no_dead_callbacks_in_handle_callback(tmp_path, monkeypatch):
    """R8-6: мертвые callback_data (role_coordinator, role_parent, demo_absent, demo_report)
    удалены из handle_callback и корректно попадают в дефолтный обработчик нераспознанных действий."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import handle_callback

    for cb in ("role_coordinator", "role_parent", "demo_absent", "demo_report"):
        upd = FakeUpdate(FakeUser(100))
        upd.callback_query = FakeCallbackQuery(cb)
        await handle_callback(upd, FakeContext())
        assert any("Не понял действие" in t for t, _ in upd.callback_query.edits)
        assert not any("Вы в роли" in t for t, _ in upd.callback_query.edits)


class CallableWithoutName:
    async def __call__(self, event):
        raise ValueError("callable without name failed")


@pytest.mark.asyncio
async def test_r8_7_eventbus_safe_handler_names_and_recursion_protection():
    """R8-7: EventBus безопасно обрабатывает подписчиков без __name__
    и защищён от рекурсивных падений при ошибках в SYSTEM_DLQ_ALERT."""
    from src.events.bus import EventBus
    from src.events.types import Event, EventTypes

    bus = EventBus()
    alerts = []

    async def dlq_handler(event):
        alerts.append(event)
        raise RuntimeError("dlq handler also failed")

    bus.subscribe(EventTypes.SYSTEM_DLQ_ALERT, dlq_handler)
    bus.subscribe("test.event", CallableWithoutName())

    report = await bus.publish(Event("test.event", {"foo": "bar"}))
    assert report.failed == 1
    assert "callable without name failed" in report.errors[0]["error"]
    assert len(alerts) == 1
    assert alerts[0].data["event_type"] == "test.event"


@pytest.mark.asyncio
async def test_r8_9_incidents_have_close_buttons_and_no_ok_in_menu(tmp_path, monkeypatch):
    """R8-9 (UX Вариант 3): команда /ok удалена из меню координатора,
    а в выдаче /incidents есть кнопки [✅ Закрыть #ID] для закрытия в 1 клик."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.roles import ROLE_COMMAND_MENUS
    from src.db.repository import IncidentRepository, UserRepository
    from src.bot.pilot import cmd_incidents
    from src.bot.handlers import handle_callback

    coord_cmds = {c for c, _ in ROLE_COMMAND_MENUS["coordinator"]}
    assert "ok" not in coord_cmds  # команда удалена из меню
    assert "incidents" in coord_cmds

    inc_id = await IncidentRepository(db).create(lesson_ref="cls_1", type="absence", status="pending")
    await UserRepository(db).set_role_by_telegram("888", "coordinator")

    upd = FakeUpdate(FakeUser(888, "coord"), chat_id=888)
    await cmd_incidents(upd, FakeContext())

    replies = upd.message.replies
    assert len(replies) == 1
    text, kw = replies[0]
    assert f"#{inc_id}" in text
    markup = kw.get("reply_markup")
    assert markup is not None
    assert any("coord_resolve:" in btn.callback_data for row in markup.inline_keyboard for btn in row)

    # Нажимаем кнопку закрытия в 1 клик
    cb_upd = FakeUpdate(FakeUser(888, "coord"), chat_id=888)
    cb_upd.callback_query = FakeCallbackQuery(f"coord_resolve:{inc_id}", user_id=888)
    await handle_callback(cb_upd, FakeContext())

    inc = await IncidentRepository(db).get(inc_id)
    assert inc["status"] == "resolved"
    assert "coordinator_closed" in inc["resolution"]


@pytest.mark.asyncio
async def test_r8_10_tutor_late_delay_detail_buttons_and_notification(tmp_path, monkeypatch):
    """R8-10 (UX Вариант 4b): после нажатия «⏰ Опоздаю» репетитору предлагается
    выбрать интервал (5, 15, 30+ мин), после чего координатору отправляется уточнение."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.db.repository import WorkflowRepository, UserRepository, NotificationRepository
    from src.bot.handlers import handle_callback

    wf_repo = WorkflowRepository(db)
    user_repo = UserRepository(db)
    notif_repo = NotificationRepository(db)
    await user_repo.set_role_by_telegram("555", "tutor")
    await user_repo.set_role_by_telegram("888", "coordinator")

    data = {
        "class_id": "cls_late",
        "actor_type": "tutor",
        "actor_telegram_id": "555",
        "tutor_name": "Анна Репетитор",
        "student_names": ["Иван Ученик"],
        "start_time": "2026-08-02T15:00:00Z",
        "nonce": "testnonce123",
    }
    wid = await wf_repo.create("prelesson_tutor", "running", data)

    # 1. Тьютор нажимает кнопку «⏰ Опоздаю»
    upd = FakeUpdate(FakeUser(555, "tutor"), chat_id=555)
    upd.callback_query = FakeCallbackQuery(f"checkin:{wid}:testnonce123:late", user_id=555)
    await handle_callback(upd, FakeContext())

    assert any("На сколько минут задержитесь?" in t for t, _ in upd.callback_query.edits)
    kb = upd.callback_query.edits[-1][1].get("reply_markup")
    assert kb is not None
    assert any("checkin_late_time:" in btn.callback_data for row in kb.inline_keyboard for btn in row)

    # 2. Тьютор выбирает «на 15 мин»
    upd_detail = FakeUpdate(FakeUser(555, "tutor"), chat_id=555)
    upd_detail.callback_query = FakeCallbackQuery(f"checkin_late_time:{wid}:testnonce123:15", user_id=555)
    await handle_callback(upd_detail, FakeContext())

    assert any("что вы задержитесь на 15 мин" in t for t, _ in upd_detail.callback_query.edits)

    # 3. Проверяем, что координатор получил уведомление с деталями
    notifs = await notif_repo._fetchall("SELECT content FROM notifications WHERE type='ops_alert' ORDER BY id DESC LIMIT 5")
    assert any("Уточнение по опозданию репетитора" in n["content"] and "на 15 мин" in n["content"] for n in notifs)
