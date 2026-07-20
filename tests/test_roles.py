"""Тесты управления ролями (/whoami /role /roles) и хелперов."""

import pytest

from src.config import settings
from src.db.repository import UserRepository
from src.bot import roles


# ── Лёгкие фейки telegram-объектов для handler-тестов ──────────────────
class FakeUser:
    def __init__(self, id, username=None, full_name="Test User"):
        self.id = id
        self.username = username
        self.full_name = full_name


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, user):
        self.effective_user = user
        self.message = FakeMessage()


class FakeContext:
    def __init__(self, args=None):
        self.args = args or []


# =====================================================================
# Хелперы
# =====================================================================

def test_parse_admin_ids():
    assert roles.parse_admin_ids("111, 222 ,333") == {"111", "222", "333"}
    assert roles.parse_admin_ids("") == set()
    assert roles.parse_admin_ids(None) == set()


def test_is_admin(monkeypatch):
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100,200")
    assert roles.is_admin("100") is True
    assert roles.is_admin(200) is True
    assert roles.is_admin("999") is False


# =====================================================================
# Repository
# =====================================================================

@pytest.mark.asyncio
async def test_set_role_by_telegram_creates_then_updates(db_path):
    repo = UserRepository(db_path)
    uid, created = await repo.set_role_by_telegram("555", "tutor", name="Тест")
    assert created is True
    rec = await repo.get_by_telegram_id("555")
    assert rec["role"] == "tutor" and rec["name"] == "Тест"

    uid2, created2 = await repo.set_role_by_telegram("555", "coordinator")
    assert created2 is False and uid2 == uid
    assert (await repo.get_by_telegram_id("555"))["role"] == "coordinator"


@pytest.mark.asyncio
async def test_get_by_username_and_list_by_role(db_path):
    repo = UserRepository(db_path)
    await repo.create("1", "parent", "P", username="alice")
    await repo.create("2", "tutor", "T", username="bob")
    assert (await repo.get_by_username("@Alice"))["telegram_id"] == "1"
    assert (await repo.get_by_username("bob"))["telegram_id"] == "2"
    tutors = await repo.list_by_role("tutor")
    assert len(tutors) == 1 and tutors[0]["telegram_id"] == "2"


@pytest.mark.asyncio
async def test_list_all_returns_everyone(db_path):
    repo = UserRepository(db_path)
    await repo.create("1", "parent", "P")
    await repo.create("2", "tutor", "T")
    assert len(await repo.list_all()) == 2


@pytest.mark.asyncio
async def test_get_coordinator_ids_with_fallback(db_path):
    # Пусто и нет coordinator_1 → пустой список
    assert await roles.get_coordinator_ids(db_path) == []
    # coordinator_1 (демо-сид) подхватывается как фолбэк
    repo = UserRepository(db_path)
    await repo.create("coordinator_1", "coordinator", "Coord")
    assert await roles.get_coordinator_ids(db_path) == ["coordinator_1"]
    # Реальный координатор по роли тоже попадает в список
    await repo.create("999", "coordinator", "Real Coord")
    ids = await roles.get_coordinator_ids(db_path)
    assert "999" in ids and "coordinator_1" in ids


# =====================================================================
# Handlers (end-to-end на temp БД через chdir)
# =====================================================================

@pytest.mark.asyncio
async def test_cmd_role_gating_and_assignment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")  # albion.db создаётся в tmp_path
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")

    from src.bot.roles import cmd_role, cmd_whoami, cmd_roles

    # Не-админ получает отказ
    nonadmin = FakeUpdate(FakeUser(200, "bob"))
    await cmd_role(nonadmin, FakeContext(["200", "tutor"]))
    assert any("⛔" in r for r in nonadmin.message.replies)

    # Админ назначает роль по числовому TG ID (создаёт пользователя)
    admin = FakeUpdate(FakeUser(100, "admin"))
    await cmd_role(admin, FakeContext(["200", "tutor"]))
    rec = await UserRepository("albion.db").get_by_telegram_id("200")
    assert rec and rec["role"] == "tutor"
    assert any("✅" in r for r in admin.message.replies)

    # whoami показывает назначенную роль
    wa = FakeUpdate(FakeUser(200, "bob"))
    await cmd_whoami(wa, FakeContext([]))
    assert any("tutor" in r for r in wa.message.replies)

    # roles (админ) видит участника
    rl = FakeUpdate(FakeUser(100, "admin"))
    await cmd_roles(rl, FakeContext([]))
    assert any("200" in r for r in rl.message.replies)


@pytest.mark.asyncio
async def test_cmd_role_rejects_bad_role_and_unknown_username(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    from src.bot.roles import cmd_role

    admin = FakeUpdate(FakeUser(100, "admin"))

    # Неверная роль
    await cmd_role(admin, FakeContext(["200", "wizard"]))
    assert any("Неизвестная роль" in r for r in admin.message.replies)

    # @username ещё не зарегистрированного пользователя → понятная ошибка
    admin2 = FakeUpdate(FakeUser(100, "admin"))
    await cmd_role(admin2, FakeContext(["@ghost", "parent"]))
    assert any("ещё не заходил" in r for r in admin2.message.replies)

    # Неверный формат аргументов
    admin3 = FakeUpdate(FakeUser(100, "admin"))
    await cmd_role(admin3, FakeContext(["onlyone"]))
    assert any("Использование" in r for r in admin3.message.replies)
