"""Материализация расписания с учётом schedule_overrides (этап 1).

class_occurs_on знает только паттерн серии; overrides — маска поверх:
'cancelled' прячет дату, 'moved' снимает исходную и вводит занятие в
new_date/new_time. Эти функции — единственная точка, где БД-правки
применяются к расписанию; все списки занятий (/today, digest, списки
отмены/переноса) обязаны идти через них, иначе перенесённое занятие
«двоится» или отменённое продолжает висеть.
"""

from datetime import date

from src.db.repository import (
    MeritHubClassRepository,
    ScheduleOverrideRepository,
)
from src.utils.recurrence import class_occurs_on


async def effective_dates(
    class_row: dict,
    days: list[date],
    db_path: str | None = None,
) -> dict[str, str | None]:
    """Эффективные занятия класса на заданные даты.

    Возвращает {iso_date: time_override_or_None}: ключ — дата занятия,
    значение — 'HH:MM' если время переопределено переносом (иначе None —
    брать hhmm из class_row.start_time).
    """
    repo = ScheduleOverrideRepository(db_path)
    class_id = class_row.get("class_id")
    ovr = {o["occurrence_date"]: o for o in await repo.list_for_class(class_id)}
    span = {d.isoformat(): d for d in days}
    out: dict[str, str | None] = {}
    for d in days:
        iso = d.isoformat()
        if iso in ovr:  # cancelled или перенесено с этой даты — маскируем
            continue
        if class_occurs_on(class_row, d):
            out[iso] = None
    for o in ovr.values():
        if o["action"] == "moved" and o.get("new_date") in span:
            out[o["new_date"]] = o.get("new_time")
    return out


async def occurrences_on_date(
    d: date,
    db_path: str | None = None,
) -> list[tuple[dict, str | None]]:
    """(class_row, time_override) для всех занятий на дату d (org-канон).

    Для coordinator-обзоров: /today, утренний дайджест, команда /lessons.
    Перенесённые В дату классы возвращаются со временем из override.
    """
    iso = d.isoformat()
    crepo = MeritHubClassRepository(db_path)
    orepo = ScheduleOverrideRepository(db_path)
    masked = {o["class_id"] for o in await orepo.list_on(iso)}
    moved_in = await orepo.moved_to(iso)
    out: list[tuple[dict, str | None]] = []
    moved_ids = {o["class_id"] for o in moved_in}
    for c in await crepo.list_all():
        if c["class_id"] in masked:
            continue
        if class_occurs_on(c, d):
            out.append((c, None))
    if moved_ids:
        cmap = await crepo.get_many(list(moved_ids))
        for o in moved_in:
            c = cmap.get(o["class_id"])
            if c:
                out.append((c, o.get("new_time")))
    return out


def occurrence_start_iso(class_row: dict, date_iso: str, time_override: str | None = None) -> str:
    """'YYYY-MM-DDTHH:MM' начала occurrence в org-каноне (без tz-суффикса —
    тот же текстовый формат, что merithub_classes.start_time)."""
    hhmm = time_override or (class_row.get("start_time") or "")[11:16] or "00:00"
    return f"{date_iso}T{hhmm}"
