import os, tempfile, asyncio, pytest, pytest_asyncio
from src.db.migrations import init_db
from src.integrations.airtable_mock import MockAirtableService
from src.integrations.merithub_mock import MockMeritHubService
from src.workflows.engine import engine

@pytest.fixture(scope="function")
def db_path():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    yield f.name
    os.unlink(f.name)

@pytest_asyncio.fixture(autouse=True)
async def db(db_path):
    await init_db(db_path)
    from src.db.repository import WorkflowRepository, ScheduledActionRepository
    engine.repo = WorkflowRepository(db_path)
    engine.scheduler = ScheduledActionRepository(db_path)
    return db_path

@pytest_asyncio.fixture
async def airtable(): return MockAirtableService()

@pytest_asyncio.fixture
async def merithub(): return MockMeritHubService()
