import logging

from src.config import settings
from src.db.engine import asyncpg, ddl_pg, is_postgres, pg_dsn, PG_COMPAT_SQL
from src.db.models import SCHEMA_SQL

logger = logging.getLogger(__name__)

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


# Мёртвые таблицы: удалить из существующих БД (идемпотентно, R7-11).
DEAD_TABLES = ["conversations"]


async def init_db(db_path: str | None = None) -> None:
    """Применить схему + миграции. Аргумент — sqlite-путь или postgres:// DSN."""
    dsn = db_path or settings.db_dsn
    if is_postgres(dsn):
        await _init_pg(dsn)
        return
    await _init_sqlite(dsn)


async def _init_sqlite(db_path: str) -> None:
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(SCHEMA_SQL)
        for table, columns in MIGRATIONS:
            try:
                existing = await db.execute(f"PRAGMA table_info({table})")
                existing_names = {row[1] for row in await existing.fetchall()}
                for col_name, col_type in columns:
                    if col_name not in existing_names:
                        await db.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
            except Exception:
                pass  # таблица может ещё не существовать при первом запуске
        for dead in DEAD_TABLES:
            await db.execute(f"DROP TABLE IF EXISTS {dead}")
        await db.commit()


async def _init_pg(dsn: str) -> None:
    """Схема на Postgres: compat-функции → DDL → миграции колонок."""
    if asyncpg is None:
        raise RuntimeError("asyncpg не установлен — DATABASE_URL указывает на Postgres")
    conn = await asyncpg.connect(pg_dsn(dsn), server_settings={"TimeZone": "UTC"})
    try:
        # параллельный init_db (бот + webhook-процесс) не должен гонять DDL
        # одновременно — advisory lock сериализует прогон на уровне PG
        await conn.execute("SELECT pg_advisory_lock(72831415)")
        try:
            await conn.execute(PG_COMPAT_SQL)
            await conn.execute(ddl_pg(SCHEMA_SQL))
            for table, columns in MIGRATIONS:
                rows = await conn.fetch(
                    "SELECT column_name FROM information_schema.columns WHERE table_name=$1",
                    table)
                existing_names = {r["column_name"] for r in rows}
                for col_name, col_type in columns:
                    if col_name not in existing_names:
                        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
            for dead in DEAD_TABLES:
                await conn.execute(f"DROP TABLE IF EXISTS {dead}")
        finally:
            await conn.execute("SELECT pg_advisory_unlock(72831415)")
    finally:
        await conn.close()
    logger.info("Postgres schema ready")
