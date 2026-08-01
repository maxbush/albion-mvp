import aiosqlite
from src.db.models import SCHEMA_SQL

# Миграции: новые колонки для существующих БД.
# CREATE TABLE IF NOT EXISTS не трогает существующие таблицы,
# поэтому ALTER TABLE добавляет недостающие колонки.
MIGRATIONS = [
    ("merithub_students", [
        ("email", "TEXT"),
        ("timezone", "TEXT DEFAULT 'Europe/London'"),
        ("country", "TEXT"),
    ]),
    ("merithub_contacts", [
        ("country", "TEXT"),
        ("city", "TEXT"),
    ]),
    # Round 5: регулярные серии занятий (perma) — существующие БД получают колонки.
    ("merithub_classes", [
        ("class_type", "TEXT NOT NULL DEFAULT 'oneTime'"),
        ("schedule_days", "TEXT"),
        ("duration", "INTEGER"),
        ("timezone", "TEXT DEFAULT 'Europe/London'"),
        ("end_date", "TEXT"),
    ]),
]


async def init_db(db_path: str = "albion.db") -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(SCHEMA_SQL)
        # Миграции для существующих БД
        for table, columns in MIGRATIONS:
            try:
                existing = await db.execute(f"PRAGMA table_info({table})")
                existing_names = {row[1] for row in await existing.fetchall()}
                for col_name, col_type in columns:
                    if col_name not in existing_names:
                        await db.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
            except Exception:
                pass  # таблица может ещё не существовать при первом запуске
        await db.commit()
