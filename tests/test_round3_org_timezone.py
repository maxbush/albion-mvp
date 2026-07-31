"""Round 3 / P4.1 (H4, решение владельца): канон расписания = зона ОРГАНИЗАЦИИ.

Единая точка правды — settings.albion_org_timezone (default Europe/London):
- naive-время трактуется в org-зоне (не UTC, не зоне репетитора);
- /mh_schedule создаёт класс в org-зоне;
- дефолт зоны ученика (display) — org-зона;
- невалидная зона → предсказуемый fallback на Europe/London.
"""

import pytest
from datetime import datetime, timezone

from src.config import settings
from src.workflows.lesson_ops import _format_dual_time, _parse_dt, _schedule_at


def test_p41_naive_interpreted_in_org_zone(monkeypatch):
    """Сменили org-зону → naive-время трактуется в НОВОЙ зоне (не в London)."""
    monkeypatch.setattr(settings, "albion_org_timezone", "Asia/Almaty")
    dt = _parse_dt("2026-07-28T15:00:00")
    # Almaty UTC+5 (без DST) → 15:00 Almaty = 10:00 UTC
    assert dt.astimezone(timezone.utc).hour == 10


def test_p41_default_org_zone_is_london():
    """Default: Europe/London — летом 15:00 London = 14:00 UTC (BST)."""
    dt = _parse_dt("2026-07-28T15:00:00")
    assert dt.astimezone(timezone.utc).hour == 14


def test_p41_schedule_at_follows_org_zone(monkeypatch):
    monkeypatch.setattr(settings, "albion_org_timezone", "Asia/Almaty")
    out = _schedule_at(datetime(2099, 7, 28, 15, 0, 0))
    out_dt = datetime.fromisoformat(out)
    assert out_dt.astimezone(timezone.utc).hour == 10  # 15:00 Almaty = 10:00 UTC


def test_p41_dual_time_follows_org_zone(monkeypatch):
    """Опорная часть dual-time — org-зона; суффикс-разница — от неё же."""
    monkeypatch.setattr(settings, "albion_org_timezone", "Asia/Almaty")
    result = _format_dual_time("2026-07-28T10:00:00+00:00", "Europe/London")
    assert "15:00 (Almaty)" in result
    assert "11:00 (ваше время, Europe/London)" in result
    assert "[-4ч к Almaty]" in result


def test_p41_same_zone_no_dual(monkeypatch):
    """Пользователь в org-зоне → только один показ, без 'ваше время'."""
    monkeypatch.setattr(settings, "albion_org_timezone", "Asia/Almaty")
    result = _format_dual_time("2026-07-28T10:00:00+00:00", "Asia/Almaty")
    assert "ваше время" not in result


def test_p41_invalid_org_zone_falls_back_to_london(monkeypatch, caplog):
    """Мусор в ALBION_ORG_TIMEZONE → предсказуемый Europe/London + warning."""
    monkeypatch.setattr(settings, "albion_org_timezone", "Bad/Zone")
    dt = _parse_dt("2026-07-28T15:00:00")
    assert dt.astimezone(timezone.utc).hour == 14  # London BST


@pytest.mark.asyncio
async def test_p41_mh_schedule_uses_org_timezone(tmp_path, monkeypatch):
    """E2E команды: /mh_schedule передаёт org-зону в schedule_class."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    monkeypatch.setattr(settings, "albion_org_timezone", "Europe/Berlin")

    from src.bot.pilot import cmd_mh_schedule
    from src.db.repository import MeritHubStudentRepository
    from src.integrations.merithub_mock import MockMeritHubService

    await MeritHubStudentRepository("albion.db").upsert(
        "t1", merithub_user_id="mh_t1", name="Репетитор", role="tutor")

    fake = MockMeritHubService()
    captured = {}

    async def spy(instructor_merithub_id, **kw):
        captured.update(kw)
        return captured

    fake.schedule_class = spy
    # schedule_class должен вернуть parseable ответ — добавляем classId.
    async def spy2(instructor_merithub_id, **kw):
        captured.update(kw)
        return {"classId": "C_TZ", "commonLinks": {}}
    fake.schedule_class = spy2
    monkeypatch.setattr(
        "src.integrations.factory.get_merithub_service", lambda: fake)

    class FakeUser:
        id = 100
        username = None
        full_name = "Admin"

    class FakeMsg:
        def __init__(self):
            self.replies = []

        async def reply_text(self, text, **kw):
            self.replies.append(text)

    upd = type("U", (), {})()
    upd.effective_user = FakeUser()
    upd.effective_chat = type("C", (), {"id": 1})()
    upd.message = FakeMsg()

    ctx = type("X", (), {"args": ["t1", "2026-07-30T15:00:00+01:00", "60", "s1"]})()
    await cmd_mh_schedule(upd, ctx)

    assert captured.get("timezone") == "Europe/Berlin", (
        f"schedule_class должен получать org-зону, получено: {captured}")


@pytest.mark.asyncio
async def test_p41_student_default_tz_is_org_zone(tmp_path, monkeypatch):
    """Ученик без явной зоны получает org-зону как display-дефолт."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    monkeypatch.setattr(settings, "albion_org_timezone", "Europe/Berlin")

    from src.db.repository import MeritHubStudentRepository
    repo = MeritHubStudentRepository("albion.db")
    await repo.upsert("s9", name="Без зоны", role="student")
    row = await repo.get_by_client_id("s9")
    assert row["timezone"] == "Europe/Berlin"
