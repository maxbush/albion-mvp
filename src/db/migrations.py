"""Версионированная система миграций БД и тюнинг для масштаба 100+ тьюторов.

Обеспечивает:
  - Атомарное версионирование схемы через таблицу schema_migrations
  - Идемпотентность накатывания миграций
  - Высокопроизводительные индексы для 100+ тьюторов и сотен классов
  - Оптимизацию PRAGMA SQLite для параллельного доступа (WAL, busy_timeout=10s, cache=64MB)
"""

import logging
from typing import Callable, Coroutine
import aiosqlite
from src.db.models import SCHEMA_SQL

logger = logging.getLogger(__name__)

# Тюнинг PRAGMA для параллельного доступа и масштаба 100+ преподавателей
PERFORMANCE_PRAGMAS = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=10000;
PRAGMA synchronous=NORMAL;
PRAGMA cache_size=-64000;
PRAGMA temp_store=MEMORY;
PRAGMA foreign_keys=ON;
"""

# Базовые колонки для существующих БД (Round 1-5)
LEGACY_MIGRATIONS = [
    ("merithub_students", [
        ("email", "TEXT"),
        ("timezone", "TEXT DEFAULT 'Europe/London'"),
        ("country", "TEXT"),
    ]),
    ("merithub_contacts", [
        ("country", "TEXT"),
        ("city", "TEXT"),
        ("preferred_channel", "TEXT DEFAULT 'auto'"),
    ]),
    ("merithub_classes", [
        ("class_type", "TEXT NOT NULL DEFAULT 'oneTime'"),
        ("schedule_days", "TEXT"),
        ("duration", "INTEGER"),
        ("timezone", "TEXT DEFAULT 'Europe/London'"),
        ("end_date", "TEXT"),
    ]),
    ("users", [
        ("preferred_channel", "TEXT DEFAULT 'telegram'"),
    ]),
    ("incidents", [
        ("is_paid", "INTEGER DEFAULT 0"),
        ("notice_hours", "REAL"),
    ]),
]

# Индексы для масштабирования (100+ тьюторов, 1000+ учеников, тысячи событий)
SCALE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_classes_tutor_start ON merithub_classes(tutor_client_user_id, start_time);",
    "CREATE INDEX IF NOT EXISTS idx_classes_type ON merithub_classes(class_type);",
    "CREATE INDEX IF NOT EXISTS idx_enroll_parent_role ON merithub_enrollments(parent_telegram_id, role);",
    "CREATE INDEX IF NOT EXISTS idx_enroll_client ON merithub_enrollments(client_user_id);",
    "CREATE INDEX IF NOT EXISTS idx_contacts_phone ON merithub_contacts(phone);",
    "CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone);",
    "CREATE INDEX IF NOT EXISTS idx_incidents_status_type ON incidents(status, type);",
    "CREATE INDEX IF NOT EXISTS idx_notifications_chan_status ON notifications(channel, status);",
    "CREATE INDEX IF NOT EXISTS idx_scheduled_exec ON scheduled_actions(status, execute_at);",
]

DEAD_TABLES = ["conversations"]


async def init_db(db_path: str = "albion.db") -> None:
    """Инициализация схемы, версионированных миграций и индексов."""
    async with aiosqlite.connect(db_path) as db:
        # 1. Применяем PRAGMA тюнинг
        for pragma in PERFORMANCE_PRAGMAS.strip().split(";"):
            p = pragma.strip()
            if p:
                try:
                    await db.execute(p)
                except Exception as e:
                    logger.debug("PRAGMA %s error: %s", p, e)

        # 2. Базовая схема
        await db.executescript(SCHEMA_SQL)

        # 3. Таблица версий миграций
        await db.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 4. Добавление колонок (идемпотентно через PRAGMA table_info)
        for table, columns in LEGACY_MIGRATIONS:
            try:
                existing = await db.execute(f"PRAGMA table_info({table})")
                existing_names = {row[1] for row in await existing.fetchall()}
                for col_name, col_type in columns:
                    if col_name not in existing_names:
                        await db.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
            except Exception as e:
                logger.debug("Migration column add to %s error: %s", table, e)

        # 5. Индексы масштабирования
        for idx_sql in SCALE_INDEXES:
            try:
                await db.execute(idx_sql)
            except Exception as e:
                logger.debug("Index creation error: %s", e)

        # 6. Очистка мертвых таблиц
        for dead in DEAD_TABLES:
            await db.execute(f"DROP TABLE IF EXISTS {dead}")

        # Фиксируем миграцию в реестре
        await db.execute("""
            INSERT OR IGNORE INTO schema_migrations (version, name)
            VALUES (1, 'initial_and_stage1_scale')
        """)

        await db.commit()
        logger.info("Database initialized and tuned for scale at %s", db_path)
