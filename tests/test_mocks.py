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

@pytest.mark.asyncio
async def test_merithub_absent():
    s = MockMeritHubService(); assert await s.mark_absent("mh_lesson_1") is True

@pytest.mark.asyncio
async def test_merithub_balance():
    s = MockMeritHubService()
    assert await s.get_balance("student_1") == 150.0
    assert await s.check_low_balance("student_2") is True
