import pytest
from src.integrations.airtable_mock import MockAirtableService, Lead
from src.integrations.merithub_mock import MockMeritHubService

@pytest.mark.asyncio
async def test_airtable_tutor(): assert (await MockAirtableService().get_tutor("tutor_1")).name == "Анна Петрова"

@pytest.mark.asyncio
async def test_airtable_mark_absent():
    s = MockAirtableService(); assert await s.mark_absent("lesson_1") is True
    assert (await s.get_lesson("lesson_1")).status == "absent"

@pytest.mark.asyncio
async def test_airtable_lead():
    lid = await MockAirtableService().create_lead(Lead("","test",{}))
    assert lid.startswith("lead_")

# УДАЛЕНО (R7-10): mark_absent/get_lesson/cancel_lesson убраны из MockMeritHubService —
# у реального MeritHubClient этих методов нет (create-only вендор), mock не должен
# создавать ложный интерфейс. Отсутствие проверено контракт-тестом ниже.


@pytest.mark.asyncio
async def test_merithub_mock_matches_real_interface():
    """Контракт: mock не предлагает методов, которых нет у реального клиента."""
    from src.integrations.merithub_client import MeritHubClient
    mock = {m for m in dir(MockMeritHubService) if not m.startswith("_")}
    real = {m for m in dir(MeritHubClient) if not m.startswith("_")}
    extra = mock - real
    assert not extra, f"mock торчит за пределы реального интерфейса: {extra}"

# ОТКЛЮЧЕНО (Round 3): методы баланса закомментированы в mock — ждём интеграцию с Xero.
# @pytest.mark.asyncio
# async def test_merithub_balance():
#     s = MockMeritHubService()
#     assert await s.get_balance("student_1") == 150.0
#     assert await s.check_low_balance("student_2") is True
