import aiosqlite
from src.db.models import SCHEMA_SQL

async def init_db(db_path: str = "albion.db") -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(SCHEMA_SQL)
        await db.commit()
