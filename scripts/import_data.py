#!/usr/bin/env python3
"""Миграция реальных данных клиента в БД ALBION (этап 1, PR7).

CSV → upsert в merithub_students / merithub_contacts / merithub_classes /
merithub_enrollments. Идемпотентно: повторный прогон обновляет, а не
дублирует (репозитории COALESCE-апсёртят — импорт не затирает поля,
которых нет в CSV).

Виды (--kind):
  students    — ученики/родители → students + contacts
  tutors      — репетиторы → students (role=tutor) + contacts
  classes     — расписание → merithub_classes
  enrollments — связка класс↔ученик → merithub_enrollments

Колонки CSV (шаблон — --template KIND):
  students/tutors: client_user_id,name,email,parent_telegram_id,phone,timezone,country
  classes:         class_id,title,class_type,schedule_days,start_time,
                   duration,tutor_client_user_id,end_date
  enrollments:     class_id,client_user_id,merithub_user_id,
                   parent_telegram_id,student_name,role

  class_type: perma | oneTime (one). schedule_days для perma — коды
  MeritHub 0=вс..6=сб через запятую ("1,3") или русские дни ("пн,ср").
  start_time: "YYYY-MM-DDTHH:MM". duration — минуты.

Отчёт: строк прочитано / импортировано / с ошибками + предупреждения
(нет телефона/TG, неизвестные ссылки). --dry-run: валидация и отчёт
без записи.

Использование:
  python scripts/import_data.py students.csv --kind students --dry-run
  python scripts/import_data.py students.csv --kind students
  python scripts/import_data.py --template classes > classes_template.csv
"""

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import settings  # noqa: E402

DAY_CODES = {"вс": 0, "пн": 1, "вт": 2, "ср": 3, "чт": 4, "пт": 5, "сб": 6}

TEMPLATES = {
    "students": ["client_user_id", "name", "email", "parent_telegram_id",
                 "phone", "timezone", "country"],
    "tutors": ["client_user_id", "name", "email", "parent_telegram_id",
               "phone", "timezone", "country"],
    "classes": ["class_id", "title", "class_type", "schedule_days",
                "start_time", "duration", "tutor_client_user_id", "end_date"],
    "enrollments": ["class_id", "client_user_id", "merithub_user_id",
                    "parent_telegram_id", "student_name", "role"],
}


def _clean(v):
    v = (v or "").strip()
    return v or None


def _days(raw: str) -> str | None:
    """'1,3' или 'пн,ср' → JSON '[1,3]' (коды MeritHub)."""
    if not (raw or "").strip():
        return None
    out = []
    for part in raw.replace(";", ",").split(","):
        p = part.strip().lower()
        if not p:
            continue
        if p.isdigit():
            out.append(int(p))
        elif p in DAY_CODES:
            out.append(DAY_CODES[p])
        else:
            raise ValueError(f"неизвестный день '{p}'")
    return json.dumps(sorted(set(out)))


def _ctype(raw: str) -> str:
    raw = (raw or "").strip().lower()
    if raw in ("perma", "перм", "регулярное", "регулярный"):
        return "perma"
    return "oneTime"


def read_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [dict(r) for r in csv.DictReader(f)]


class Report:
    def __init__(self):
        self.read = 0
        self.imported = 0
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def err(self, i: int, msg: str):
        self.errors.append(f"строка {i}: {msg}")

    def warn(self, i: int, msg: str):
        self.warnings.append(f"строка {i}: {msg}")

    def print(self, kind: str, dry_run: bool):
        mode = "DRY-RUN" if dry_run else "IMPORT"
        print(f"\n== {mode} {kind}: прочитано {self.read}, "
              f"{'будет записано' if dry_run else 'записано'} {self.imported}, "
              f"ошибок {len(self.errors)}, предупреждений {len(self.warnings)}")
        for m in self.errors[:20]:
            print(f"  ERR  {m}")
        for m in self.warnings[:20]:
            print(f"  WARN {m}")
        if len(self.errors) > 20:
            print(f"  … ещё ошибок: {len(self.errors) - 20}")
        if len(self.warnings) > 20:
            print(f"  … ещё предупреждений: {len(self.warnings) - 20}")


async def _import_people(rows, role: str, rep: Report, db_path, dry_run):
    """students/tutors: merithub_students + merithub_contacts."""
    from src.db.repository import (
        MeritHubContactRepository, MeritHubStudentRepository)
    srepo = MeritHubStudentRepository(db_path)
    crepo = MeritHubContactRepository(db_path)
    for i, r in enumerate(rows, 2):
        cuid = _clean(r.get("client_user_id"))
        if not cuid:
            rep.err(i, "нет client_user_id")
            continue
        tg = _clean(r.get("parent_telegram_id"))
        phone = _clean(r.get("phone"))
        if not tg and not phone:
            rep.warn(i, f"{cuid} ({r.get('name') or '?'}) — нет TG и телефона: "
                        "уведомления доставлять некуда")
        if not dry_run:
            await srepo.upsert(
                cuid, name=_clean(r.get("name")), email=_clean(r.get("email")),
                parent_telegram_id=tg, timezone=_clean(r.get("timezone")),
                country=_clean(r.get("country")), role=role)
            await crepo.upsert(
                cuid, telegram_id=tg, role=role, name=_clean(r.get("name")),
                phone=phone, email=_clean(r.get("email")),
                country=_clean(r.get("country")))
        rep.imported += 1


async def _import_classes(rows, rep: Report, db_path, dry_run):
    from src.db.repository import MeritHubClassRepository
    repo = MeritHubClassRepository(db_path)
    for i, r in enumerate(rows, 2):
        cid = _clean(r.get("class_id"))
        if not cid:
            rep.err(i, "нет class_id")
            continue
        try:
            days = _days(r.get("schedule_days"))
        except ValueError as e:
            rep.err(i, str(e))
            continue
        if not _clean(r.get("start_time")):
            rep.err(i, "нет start_time (нужен 'YYYY-MM-DDTHH:MM')")
            continue
        try:
            dur = int(r.get("duration") or 60)
        except ValueError:
            rep.err(i, "duration не число")
            continue
        ctype = _ctype(r.get("class_type"))
        if ctype == "perma" and not days:
            rep.warn(i, f"{cid}: perma без schedule_days — занятия не материализуются")
        if not dry_run:
            await repo.upsert(
                cid, title=_clean(r.get("title")), class_type=ctype,
                schedule_days=days, start_time=_clean(r.get("start_time")),
                duration=dur, tutor_client_user_id=_clean(r.get("tutor_client_user_id")),
                end_date=_clean(r.get("end_date")))
        rep.imported += 1


async def _import_enrollments(rows, rep: Report, db_path, dry_run):
    from src.db.repository import MeritHubEnrollmentRepository
    repo = MeritHubEnrollmentRepository(db_path)
    for i, r in enumerate(rows, 2):
        cid = _clean(r.get("class_id"))
        cuid = _clean(r.get("client_user_id"))
        muid = _clean(r.get("merithub_user_id")) or cuid
        if not cid or not muid:
            rep.err(i, "нужны class_id и client_user_id (или merithub_user_id)")
            continue
        if not dry_run:
            await repo.add(
                cid, muid, client_user_id=cuid,
                parent_telegram_id=_clean(r.get("parent_telegram_id")),
                student_name=_clean(r.get("student_name")),
                role=_clean(r.get("role")) or "student")
        rep.imported += 1


async def run(path: str, kind: str, db_path: str, dry_run: bool) -> int:
    rows = read_csv(path)
    rep = Report()
    rep.read = len(rows)
    if kind in ("students", "tutors"):
        await _import_people(rows, "student" if kind == "students" else "tutor",
                             rep, db_path, dry_run)
    elif kind == "classes":
        await _import_classes(rows, rep, db_path, dry_run)
    else:
        await _import_enrollments(rows, rep, db_path, dry_run)
    rep.print(kind, dry_run)
    return 1 if rep.errors else 0


def main() -> int:
    p = argparse.ArgumentParser(description="Импорт данных клиента из CSV в ALBION")
    p.add_argument("csv", nargs="?", help="путь к CSV-файлу")
    p.add_argument("--kind", choices=sorted(TEMPLATES), help="вид данных")
    p.add_argument("--dsn", default=None,
                   help="БД (по умолчанию — DATABASE_URL / albion.db как у бота)")
    p.add_argument("--dry-run", action="store_true",
                   help="валидация + отчёт, ничего не писать")
    p.add_argument("--template", choices=sorted(TEMPLATES), metavar="KIND",
                   help="напечатать CSV-шаблон для вида и выйти")
    args = p.parse_args()

    if args.template:
        print(",".join(TEMPLATES[args.template]))
        return 0
    if not args.csv or not args.kind:
        p.error("нужны CSV-файл и --kind (или --template)")

    db_path = args.dsn or settings.db_dsn

    async def _go():
        if not args.dry_run:
            from src.db.migrations import init_db
            await init_db(db_path)
        return await run(args.csv, args.kind, db_path, args.dry_run)

    return asyncio.run(_go())


if __name__ == "__main__":
    raise SystemExit(main())
