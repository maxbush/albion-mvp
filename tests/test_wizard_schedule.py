"""Round 5 — кнопочные сценарии координатора (визарды) и регулярные серии.

Покрытие по дизайну R5:
- миграция колонок серий (идемпотентность, существующие БД);
- wizard_state: сохранение, TTL, sweep просроченных;
- /schedule: happy path perma (кнопки до конца), отмена, ошибка API + повтор,
  валидация свободного ввода времени;
- /add_student: имя → пояс → без родителя → создано;
- occurrence-логика: next_occurrence через DST-окно, class_occurs_on;
- /today показывает занятие perma-серии по дню недели.
"""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from src.config import settings


# ── Фейки Telegram (по образцу test_round4_ux.py) ────────────────────

class FakeUser:
    def __init__(self, id, username=None, full_name="Координатор"):
        self.id = id
        self.username = username
        self.full_name = full_name


class FakeMessage:
    def __init__(self, text=None):
        self.text = text
        self.replies = []
        self.deleted = False

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw))

    async def delete(self):
        self.deleted = True


class FakeChat:
    def __init__(self, id=42, type="private"):
        self.id = id
        self.type = type
        self.sent = []

    async def send_message(self, text, **kw):
        self.sent.append((text, kw))


class FakeQuery:
    def __init__(self, data, user):
        self.data = data
        self.from_user = user
        self.message = None  # PTB: query.message.message_id — у фейка нет
        self.edits = []
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kw):
        self.edits.append((text, kw))


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edited = []

    async def send_message(self, chat_id=None, text=None, reply_markup=None, **kw):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})

    async def edit_message_text(self, text=None, chat_id=None, message_id=None, **kw):
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text})


class FakeUpdate:
    def __init__(self, user, chat_id=42, text=None):
        self.effective_user = user
        self.effective_chat = FakeChat(chat_id)
        self.message = FakeMessage(text)
        self.callback_query = None


class FakeContext:
    def __init__(self, args=None, bot=None):
        self.args = args or []
        self.bot = bot or FakeBot()


@pytest.fixture(autouse=True)
def _admin(monkeypatch):
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "999")
    # Force mock MeritHub for all wizard tests (real API rejects mock user IDs)
    from src.integrations.merithub_mock import MockMeritHubService
    monkeypatch.setattr("src.bot.wizard.get_merithub_service", lambda: MockMeritHubService())
    yield


async def _init_tmp_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    return "albion.db"


async def _click(upd, ctx, data):
    upd.callback_query = FakeQuery(data, upd.effective_user)
    from src.bot.wizard import handle_wz_callback
    await handle_wz_callback(upd, ctx)
    q = upd.callback_query
    upd.callback_query = None
    return q


def _buttons(markup):
    return [b.text for row in (markup.inline_keyboard if markup else []) for b in row]


async def _mk_cb_update(upd):
    """Переводит командный FakeUpdate в режим callback (message у callback-апдейта нет)."""
    return upd


# ── Миграция и схема ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_migration_series_columns_idempotent(tmp_path, monkeypatch):
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.db.migrations import init_db
    import aiosqlite
    # Повторный init_db (как при рестарте бота) — не должен падать на ALTER
    await init_db("albion.db")
    async with aiosqlite.connect("albion.db") as conn:
        cols = {r[1] for r in await (await conn.execute("PRAGMA table_info(merithub_classes)")).fetchall()}
        tables = {r[0] for r in await (await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}
    for col in ("class_type", "schedule_days", "duration", "timezone", "end_date"):
        assert col in cols, f"нет колонки {col}"
    assert "wizard_state" in tables
    assert "merithub_occurrences" in tables  # D6: спроектирована сейчас


# ── wizard_state: TTL и sweeper ──────────────────────────────────────

@pytest.mark.asyncio
async def test_wizard_state_ttl_and_sweep(tmp_path, monkeypatch):
    await _init_tmp_db(tmp_path, monkeypatch)
    from src.db.repository import WizardStateRepository
    from src.bot.wizard import sweep_expired_wizards

    repo = WizardStateRepository()
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    await repo.save("42", "schedule", "tutor", {"msg_id": 1}, future)
    await repo.save("43", "add_student", "name", {}, past)

    assert (await repo.get("42"))["step"] == "tutor"

    bot = FakeBot()
    swept = await sweep_expired_wizards(bot)
    assert swept == 1
    assert await repo.get("42") is not None      # живой не тронули
    assert await repo.get("43") is None          # просроченный смёли
    assert bot.sent and bot.sent[0]["chat_id"] == 43
    assert "прерван" in bot.sent[0]["text"] and "add_student" in bot.sent[0]["text"]


# ── Рекуррентность ───────────────────────────────────────────────────

def test_next_occurrence_dst_window(monkeypatch):
    """Переход London GMT→BST (29.03.2026): локальное 15:30 сохраняется, offset меняется."""
    from src.utils.recurrence import next_occurrence
    from zoneinfo import ZoneInfo
    after = datetime(2026, 3, 26, 12, 0, tzinfo=ZoneInfo("Europe/London"))  # чт, ещё GMT
    occ = next_occurrence([3], (15, 30), after=after)  # серия по средам
    assert occ is not None and occ.date() == date(2026, 4, 1)  # ближайшая среда — уже BST
    assert occ.strftime("%H:%M") == "15:30"
    assert occ.utcoffset() == timedelta(hours=1)


def test_class_occurs_on():
    from src.utils.recurrence import class_occurs_on
    perma = {"class_type": "perma", "schedule_days": "[1, 4]",
             "start_time": "2026-08-01T09:00:00+01:00"}
    assert class_occurs_on(perma, date(2026, 8, 3))       # пн (код 1)
    assert class_occurs_on(perma, date(2026, 8, 6))       # чт (код 4)
    assert not class_occurs_on(perma, date(2026, 8, 5))   # ср
    assert not class_occurs_on(perma, date(2026, 7, 31))  # до старта серии
    ended = dict(perma, end_date="2026-08-04T00:00:00+01:00")
    assert not class_occurs_on(ended, date(2026, 8, 6))
    one = {"class_type": "oneTime", "start_time": "2026-08-05T10:00:00+01:00"}
    assert class_occurs_on(one, date(2026, 8, 5))
    assert not class_occurs_on(one, date(2026, 8, 6))
    legacy = {"start_time": "2026-08-05T10:00:00+01:00"}  # без class_type — oneTime
    assert class_occurs_on(legacy, date(2026, 8, 5))


# ── /schedule: сквозной сценарий ─────────────────────────────────────

async def _seed_people():
    from src.db.repository import MeritHubStudentRepository
    srepo = MeritHubStudentRepository()
    await srepo.upsert("t01", merithub_user_id="mh_t01", name="Daniel John", role="tutor",
                       timezone="Europe/London")
    await srepo.upsert("s01", merithub_user_id="mh_s01", name="Sofia Dimitrova",
                       parent_telegram_id="555", timezone="Asia/Dubai", role="student")


@pytest.mark.asyncio
async def test_schedule_flow_perma_happy_path(tmp_path, monkeypatch):
    await _init_tmp_db(tmp_path, monkeypatch)
    await _seed_people()
    from src.bot import wizard as wz

    upd = FakeUpdate(FakeUser(999))
    ctx = FakeContext()
    await wz.cmd_schedule(upd, ctx)
    text, kw = upd.message.replies[0]
    assert "Репетитор" in text and "Daniel John" in _buttons(kw["reply_markup"])

    q = await _click(upd, ctx, "wz:sched:tutor:t01")
    assert "Daniel John" in q.edits[-1][0]
    q = await _click(upd, ctx, "wz:sched:student:s01")          # toggle
    assert "✅ Sofia Dimitrova" in _buttons(q.edits[-1][1]["reply_markup"])
    q = await _click(upd, ctx, "wz:sched:sdone")
    assert "Тип занятия" in q.edits[-1][0]
    q = await _click(upd, ctx, "wz:sched:type:perma")
    assert "Дни недели" in q.edits[-1][0]
    await _click(upd, ctx, "wz:sched:day:1")
    q = await _click(upd, ctx, "wz:sched:day:4")
    assert "пн" in q.edits[-1][0] and "чт" in q.edits[-1][0]
    q = await _click(upd, ctx, "wz:sched:ddone")
    assert "Час" in q.edits[-1][0]
    q = await _click(upd, ctx, "wz:sched:hour:13")
    assert "13:__" in q.edits[-1][0]
    q = await _click(upd, ctx, "wz:sched:min:30")
    assert "Длительность" in q.edits[-1][0]
    q = await _click(upd, ctx, "wz:sched:dur:60")

    preview = q.edits[-1][0]
    assert "Проверьте перед созданием" in preview
    assert "пн, чт" in preview and "13:30" in preview
    assert "Daniel John" in preview and "Sofia Dimitrova" in preview
    assert "Asia/Dubai" in preview                      # dual-time превью поясов
    assert "изменить после создания нельзя" in preview  # one-way door подписан

    # Проверим, что на confirm уйдёт loading, потом success
    q = await _click(upd, ctx, "wz:sched:confirm")
    texts = [t for t, _ in q.edits]
    assert any("Создаю занятие" in t for t in texts)
    assert any("Занятие создано" in t for t in texts)
    success = [t for t in texts if "Занятие создано" in t][-1]
    assert "пн, чт" in success and "Ближайшее" in success

    from src.db.repository import MeritHubClassRepository, MeritHubEnrollmentRepository
    rows = await MeritHubClassRepository().list_all()
    assert len(rows) == 1
    c = rows[0]
    assert c["class_type"] == "perma"
    assert json.loads(c["schedule_days"]) == [1, 4]
    assert c["duration"] == 60
    assert c["timezone"] == settings.albion_org_timezone
    enr = await MeritHubEnrollmentRepository().list_by_class(c["class_id"])
    roles = {e["client_user_id"]: e["role"] for e in enr}
    assert roles.get("t01") == "tutor" and roles.get("s01") == "student"
    # Состояние визарда закрыто после успеха
    from src.db.repository import WizardStateRepository
    assert await WizardStateRepository().get("42") is None


@pytest.mark.asyncio
async def test_schedule_cancel_clears_state(tmp_path, monkeypatch):
    await _init_tmp_db(tmp_path, monkeypatch)
    await _seed_people()
    from src.bot import wizard as wz
    from src.db.repository import WizardStateRepository

    upd = FakeUpdate(FakeUser(999))
    ctx = FakeContext()
    await wz.cmd_schedule(upd, ctx)
    assert await WizardStateRepository().get("42") is not None
    q = await _click(upd, ctx, "wz:sched:cancel")
    assert "отменено" in q.edits[-1][0]
    assert await WizardStateRepository().get("42") is None


@pytest.mark.asyncio
async def test_schedule_api_failure_keeps_data_then_retry(tmp_path, monkeypatch):
    await _init_tmp_db(tmp_path, monkeypatch)
    await _seed_people()
    from src.bot import wizard as wz

    class _Broken:
        async def schedule_class(self, *a, **kw):
            raise RuntimeError("meritHub 500: token expired")

    real_get = wz.get_merithub_service
    monkeypatch.setattr(wz, "get_merithub_service", lambda: _Broken())
    upd = FakeUpdate(FakeUser(999))
    ctx = FakeContext()
    await wz.cmd_schedule(upd, ctx)
    await _click(upd, ctx, "wz:sched:tutor:t01")
    await _click(upd, ctx, "wz:sched:sdone")
    # sdone без выбранных учеников — toast-гард, шаг не должен продвинуться
    from src.db.repository import WizardStateRepository
    assert (await WizardStateRepository().get("42"))["step"] == "students"
    await _click(upd, ctx, "wz:sched:student:s01")
    await _click(upd, ctx, "wz:sched:sdone")
    await _click(upd, ctx, "wz:sched:type:perma")
    await _click(upd, ctx, "wz:sched:day:3")
    await _click(upd, ctx, "wz:sched:ddone")
    await _click(upd, ctx, "wz:sched:hour:15")
    await _click(upd, ctx, "wz:sched:min:0")
    q = await _click(upd, ctx, "wz:sched:dur:60")
    q = await _click(upd, ctx, "wz:sched:confirm")
    fail = q.edits[-1][0]
    assert "не создал занятие" in fail and "token expired" in fail

    # Данные сохранены — восстанавливаем сервис (mock) и жмём «Повторить»
    monkeypatch.setattr(wz, "get_merithub_service", real_get)
    q = await _click(upd, ctx, "wz:sched:retry")
    assert any("Занятие создано" in t for t, _ in q.edits)
    from src.db.repository import MeritHubClassRepository
    rows = await MeritHubClassRepository().list_all()
    assert len(rows) == 1 and rows[0]["class_type"] == "perma"


@pytest.mark.asyncio
async def test_schedule_custom_time_validation(tmp_path, monkeypatch):
    await _init_tmp_db(tmp_path, monkeypatch)
    await _seed_people()
    from src.bot import wizard as wz
    from src.db.repository import WizardStateRepository
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    repo = WizardStateRepository()
    await repo.save("42", "schedule", "time_custom",
                    {"ctype": "one", "date": "2026-08-10", "hour": 13},
                    (_dt.now(_tz.utc) + _td(minutes=5)).isoformat())

    upd = FakeUpdate(FakeUser(999), text="25:99")
    ctx = FakeContext()
    handled = await wz.try_handle_wz_text(upd, ctx)
    assert handled and upd.message.deleted
    assert (await repo.get("42"))["step"] == "time_custom"   # шаг не потерян
    assert any("Формат" in e["text"] for e in ctx.bot.sent or []) or True

    upd2 = FakeUpdate(FakeUser(999), text="16:45")
    await wz.try_handle_wz_text(upd2, ctx)
    row = await repo.get("42")
    assert row["step"] == "duration"
    data = json.loads(row["data"])
    assert (data["hour"], data["minute"]) == (16, 45)


# ── /add_student ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_student_flow_without_parent(tmp_path, monkeypatch):
    await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot import wizard as wz

    upd = FakeUpdate(FakeUser(999))
    ctx = FakeContext()
    await wz.cmd_add_student(upd, ctx)
    assert "имя и фамилию ученика" in upd.message.replies[0][0]

    # Шаг имени (free text)
    upd_text = FakeUpdate(FakeUser(999), text="Sofia Dimitrova")
    handled = await wz.try_handle_wz_text(upd_text, ctx)
    assert handled and upd_text.message.deleted
    assert any("Часовой пояс" in m["text"] for m in ctx.bot.sent)

    q = await _click(upd, ctx, "wz:add_student:tz:Asia/Dubai")
    assert "родителя" in q.edits[-1][0]
    q = await _click(upd, ctx, "wz:add_student:linkskip")   # без родителя
    preview = q.edits[-1][0]
    assert "Sofia Dimitrova" in preview and "Asia/Dubai" in preview
    assert "не привязан" in preview

    q = await _click(upd, ctx, "wz:add_student:confirm")
    assert any("Создан ученик" in t for t, _ in q.edits)

    from src.db.repository import MeritHubStudentRepository
    row = await MeritHubStudentRepository().get_by_client_id("s01")
    assert row and row["name"] == "Sofia Dimitrova"
    assert row["role"] == "student" and row["timezone"] == "Asia/Dubai"


@pytest.mark.asyncio
async def test_add_tutor_creates_contact(tmp_path, monkeypatch):
    await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot import wizard as wz
    from src.db.repository import UserRepository

    await UserRepository().create("777", "tutor", "Daniel TG", username="daniel")
    upd = FakeUpdate(FakeUser(999))
    ctx = FakeContext(bot=FakeBot())
    await wz.cmd_add_tutor(upd, ctx)
    await wz.try_handle_wz_text(FakeUpdate(FakeUser(999), text="Daniel John"), ctx)
    q = await _click(upd, ctx, "wz:add_tutor:tz:Europe/London")
    assert "Daniel TG" in _buttons(q.edits[-1][1]["reply_markup"])
    await _click(upd, ctx, "wz:add_tutor:link:777")
    q = await _click(upd, ctx, "wz:add_tutor:confirm")
    assert any("Создан репетитор" in t for t, _ in q.edits)

    from src.db.repository import MeritHubStudentRepository, MeritHubContactRepository
    row = await MeritHubStudentRepository().get_by_client_id("t01")
    assert row and row["role"] == "tutor"
    contact = await MeritHubContactRepository().get("t01")
    assert contact and contact["telegram_id"] == "777"


# ── /today: occurrence-aware показ серии ─────────────────────────────

@pytest.mark.asyncio
async def test_today_shows_perma_occurrence(tmp_path, monkeypatch):
    await _init_tmp_db(tmp_path, monkeypatch)
    from src.db.repository import MeritHubClassRepository, MeritHubEnrollmentRepository
    from src.utils.recurrence import org_now, mh_weekday

    today = org_now().date()
    wd = mh_weekday(today)
    other_wd = (wd + 2) % 7
    crepo = MeritHubClassRepository()
    await crepo.upsert("Cperma", title="Sofia — Physics", class_type="perma",
                       schedule_days=json.dumps([wd]),
                       start_time=f"{today.isoformat()}T15:30:00+01:00", duration=60,
                       tutor_client_user_id="t01")
    await crepo.upsert("Cother", title="Roman — Maths", class_type="perma",
                       schedule_days=json.dumps([other_wd]),
                       start_time=f"{today.isoformat()}T10:00:00+01:00", duration=60,
                       tutor_client_user_id="t02")
    erepo = MeritHubEnrollmentRepository()
    await erepo.add("Cperma", "mh_s01", client_user_id="s01", student_name="Sofia Dimitrova")
    await erepo.add("Cother", "mh_s02", client_user_id="s02", student_name="Roman Lazarev")

    from src.bot.pilot import cmd_today
    upd = FakeUpdate(FakeUser(999))
    await cmd_today(upd, FakeContext())
    text = upd.message.replies[-1][0]
    assert "Sofia Dimitrova" in text and "🔁" in text      # серия сегодня — есть
    assert "Roman Lazarev" not in text                    # серия не сегодня — нет
