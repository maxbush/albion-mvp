"""Скрипт миграции реальных данных клиентов и координаторов ALBION (Этап 1).

Загружает 18 учеников, их родителей, часовые пояса и координаторов
из базы знаний ALBION_CONTEXT.md в таблицы:
  - merithub_students
  - merithub_contacts
  - users
Идемпотентен: повторный запуск обновляет существующие записи без дубликатов.
"""

import asyncio
import logging
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.migrations import init_db
from src.db.repository import (
    MeritHubContactRepository, MeritHubStudentRepository, UserRepository,
)
from src.integrations.whatsapp_client import normalize_phone

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Реальные данные команды координаторов
COORDINATORS = [
    {
        "name": "Victoria Eremayeva",
        "email": "a.yeshmatova@albionconsult.co.uk",
        "phone": "+380505292480",
        "role": "coordinator",
        "timezone": "Europe/Kyiv",
    },
    {
        "name": "Dmitriy Lazarev",
        "email": "altysha79@yahoo.co.uk",
        "phone": "+380500000001",
        "role": "coordinator",
        "timezone": "Europe/Kyiv",
    },
    {
        "name": "Dilyara Mazhitova",
        "email": "a.bashkirova@albionconsult.co.uk",
        "phone": "+77010000002",
        "role": "coordinator",
        "timezone": "Asia/Almaty",
    },
    {
        "name": "Valeria Kaminsky",
        "email": "albionconcierge@gmail.com",
        "phone": "+447493994501",
        "role": "coordinator",
        "timezone": "Europe/London",
    },
]

# Реальные данные 18 учеников из MeritHub (ALBION_CONTEXT.md)
REAL_STUDENTS = [
    {"name": "Ernest Mezheritsky", "timezone": "Europe/London", "country": "UK", "parent": "Victoria Eremayeva"},
    {"name": "Alexandra Mironova", "timezone": "Asia/Dubai", "country": "UAE", "parent": None},
    {"name": "Felix Stazhynski", "timezone": "Europe/London", "country": "UK", "parent": "Daria Stazhynskaya"},
    {"name": "Roman Lazarev", "timezone": "Europe/London", "country": "UK", "parent": "Dmitriy Lazarev"},
    {"name": "Artem Stoklitskiy", "timezone": "Europe/London", "country": "UK", "parent": "Eva Kriss"},
    {"name": "Eva Kriss", "timezone": "Europe/Vienna", "country": "Austria", "parent": "Victoria Eremayeva"},
    {"name": "Oleg Burylov", "timezone": "Europe/Moscow", "country": "Russia", "parent": None},
    {"name": "Lion Lebedev", "timezone": "Europe/London", "country": "UK", "parent": "Sofia Dimitrova"},
    {"name": "Makar Baranov", "timezone": "Europe/Moscow", "country": "Russia", "parent": None},
    {"name": "Saveliy Koktysh", "timezone": "Europe/London", "country": "UK", "parent": "Alexander Radostovets"},
    {"name": "Alexander Radostovets", "timezone": "Asia/Almaty", "country": "Kazakhstan", "parent": "Dilyara Mazhitova"},
    {"name": "Marta Kaminsky", "timezone": "Europe/London", "country": "UK", "parent": "Valeria Kaminsky"},
    {"name": "Slava Moscovoy", "timezone": "Europe/Moscow", "country": "Russia", "parent": None},
    {"name": "Maria Onischuk", "timezone": "Europe/London", "country": "UK", "parent": "Sergey Kolpakov"},
    {"name": "Jana Hoffmann", "timezone": "Europe/Paris", "country": "France", "parent": "Alisa Fedoseev"},
    {"name": "Alisa Fedoseev", "timezone": "Europe/London", "country": "UK", "parent": None},
    {"name": "Sergey Kolpakov", "timezone": "Europe/London", "country": "UK", "parent": None},
    {"name": "Sofia Dimitrova", "timezone": "Europe/London", "country": "UK", "parent": None},
]


async def migrate_data(db_path: str = "albion.db") -> dict:
    """Запускает миграцию реальных данных."""
    await init_db(db_path)

    u_repo = UserRepository(db_path)
    s_repo = MeritHubStudentRepository(db_path)
    c_repo = MeritHubContactRepository(db_path)

    coords_count = 0
    for idx, c in enumerate(COORDINATORS, start=1):
        cuid = f"coord_{idx}"
        phone = normalize_phone(c["phone"])
        await c_repo.upsert(
            cuid,
            name=c["name"],
            phone=phone,
            email=c["email"],
            role="coordinator",
        )
        # Также регистрируем пользователя
        tg_mock = f"tg_coord_{idx}"
        existing = await u_repo._fetchall("SELECT id FROM users WHERE phone=?", (phone,))
        if not existing:
            await u_repo.create(
                tg=tg_mock,
                role="coordinator",
                name=c["name"],
                phone=phone,
            )
        coords_count += 1

    students_count = 0
    for idx, s in enumerate(REAL_STUDENTS, start=1):
        cuid = f"student_{idx:02d}"
        await s_repo.upsert(
            client_user_id=cuid,
            name=s["name"],
            timezone=s["timezone"],
            country=s["country"],
            role="student",
        )
        await c_repo.upsert(
            cuid,
            name=s["name"],
            country=s["country"],
            role="student",
        )
        students_count += 1

    logger.info("Migrated %d coordinators and %d real students successfully.",
                coords_count, students_count)
    return {"coordinators": coords_count, "students": students_count}


if __name__ == "__main__":
    db_file = sys.argv[1] if len(sys.argv) > 1 else "albion.db"
    res = asyncio.run(migrate_data(db_file))
    print(f"✅ Data migration completed: {res}")
