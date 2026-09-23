"""Детектор конфликтов расписания (пересечений занятий у репетитора).

Проверяет пересечения временных интервалов с учётом:
  - Типов занятий: perma vs perma, perma vs oneTime, oneTime vs oneTime
  - Дней недели (schedule_days) для регулярных занятий
  - Временных интервалов [start_time, start_time + duration)
  - Границ действия серий [start_date, end_date]

Формула пересечения полуинтервалов [S1, E1) и [S2, E2):
  max(S1, S2) < min(E1, E2)
"""

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from src.config import settings
from src.db.repository import MeritHubClassRepository
from src.utils.recurrence import (
    WD_RU, fmt_days, mh_weekday, parse_days,
)

logger = logging.getLogger(__name__)


@dataclass
class ScheduleConflict:
    """Информация о конфликте в расписании."""
    conflicting_class_id: str
    conflicting_title: str
    class_type: str  # 'perma' или 'oneTime'
    days_or_date: str
    existing_time_range: str
    proposed_time_range: str
    overlap_minutes: int
    message: str


def _time_str_to_minutes(hhmm: str) -> int:
    """'15:30' -> 930 минут от полуночи."""
    parts = hhmm.strip().split(":")
    h = int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else 0
    m = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    return h * 60 + m


def _minutes_to_time_str(mins: int) -> str:
    """930 -> '15:30'."""
    h = (mins // 60) % 24
    m = mins % 60
    return f"{h:02d}:{m:02d}"


def intervals_overlap(s1: int, dur1: int, s2: int, dur2: int) -> tuple[bool, int]:
    """Проверяет пересечение двух интервалов [s1, s1+dur1) и [s2, s2+dur2).

    Возвращает (is_overlap, overlap_minutes).
    Граничное совпадение (s1+dur1 == s2) НЕ считается пересечением (стык в стык).
    """
    e1 = s1 + max(dur1, 1)
    e2 = s2 + max(dur2, 1)
    overlap = min(e1, e2) - max(s1, s2)
    if overlap > 0:
        return True, overlap
    return False, 0


async def check_tutor_conflicts(
    tutor_client_user_id: str,
    class_type: str,  # 'perma' или 'oneTime' (или 'one')
    start_time_hhmm: str,  # '15:30'
    duration_min: int = 60,
    schedule_days: list[int] | None = None,  # [1, 3] для perma
    start_date: str | None = None,  # 'YYYY-MM-DD' для oneTime или старт perma
    end_date: str | None = None,  # 'YYYY-MM-DD' для perma
    exclude_class_id: str | None = None,
    db_path: str | None = None,
) -> list[ScheduleConflict]:
    """Проверяет пересечения для репетитора со всеми существующими классами в БД.

    Возвращает список обнаруженных конфликтов (пустой список = всё чисто).
    """
    if not tutor_client_user_id:
        return []

    ctype = "perma" if class_type in ("perma", "perma_series") else "oneTime"
    proposed_days = list(schedule_days or [])
    proposed_s = _time_str_to_minutes(start_time_hhmm)
    proposed_dur = max(duration_min, 1)
    proposed_range_str = f"{_minutes_to_time_str(proposed_s)}–{_minutes_to_time_str(proposed_s + proposed_dur)}"

    crepo = MeritHubClassRepository(db_path)
    all_classes = await crepo.list_all()

    conflicts: list[ScheduleConflict] = []

    for c in all_classes:
        cid = c.get("class_id")
        if not cid or cid == exclude_class_id:
            continue
        if c.get("tutor_client_user_id") != tutor_client_user_id:
            continue

        c_type = c.get("class_type") or "oneTime"
        c_title = c.get("title") or cid
        c_raw_start = c.get("start_time") or ""
        c_time_hhmm = c_raw_start[11:16] if len(c_raw_start) >= 16 else "00:00"
        c_date = c_raw_start[:10] if len(c_raw_start) >= 10 else None
        c_dur = int(c.get("duration") or 60)
        c_days = parse_days(c.get("schedule_days"))
        c_end_date = (c.get("end_date") or "")[:10] or None

        c_s = _time_str_to_minutes(c_time_hhmm)
        is_time_overlap, overlap_mins = intervals_overlap(proposed_s, proposed_dur, c_s, c_dur)
        if not is_time_overlap:
            continue  # По времени нет пересечения

        existing_range_str = f"{_minutes_to_time_str(c_s)}–{_minutes_to_time_str(c_s + c_dur)}"

        # Время пересекается! Теперь проверяем пересечение по дням / датам:

        # 1. PERMA vs PERMA
        if ctype == "perma" and c_type == "perma":
            shared_days = set(proposed_days) & set(c_days)
            if shared_days:
                # Проверяем пересечение диапазонов дат серий
                p_start = start_date or "2000-01-01"
                p_end = end_date or "2099-12-31"
                e_start = c_date or "2000-01-01"
                e_end = c_end_date or "2099-12-31"
                if max(p_start, e_start) <= min(p_end, e_end):
                    day_names = fmt_days(sorted(shared_days))
                    msg = (
                        f"Конфликт с регулярной серией «{c_title}»: {day_names} {existing_range_str} "
                        f"(пересечение {overlap_mins} мин)."
                    )
                    conflicts.append(ScheduleConflict(
                        conflicting_class_id=cid,
                        conflicting_title=c_title,
                        class_type="perma",
                        days_or_date=day_names,
                        existing_time_range=existing_range_str,
                        proposed_time_range=proposed_range_str,
                        overlap_minutes=overlap_mins,
                        message=msg,
                    ))

        # 2. PERMA (новое) vs ONETIME (существующее)
        elif ctype == "perma" and c_type != "perma":
            if c_date:
                try:
                    d_obj = date.fromisoformat(c_date)
                    d_day = mh_weekday(d_obj)
                    if d_day in proposed_days:
                        p_start = start_date or "2000-01-01"
                        p_end = end_date or "2099-12-31"
                        if p_start <= c_date <= p_end:
                            day_name = f"{c_date} ({WD_RU.get(d_day, '')})"
                            msg = (
                                f"Конфликт с разовым занятием «{c_title}»: {day_name} {existing_range_str} "
                                f"(пересечение {overlap_mins} мин)."
                            )
                            conflicts.append(ScheduleConflict(
                                conflicting_class_id=cid,
                                conflicting_title=c_title,
                                class_type="oneTime",
                                days_or_date=day_name,
                                existing_time_range=existing_range_str,
                                proposed_time_range=proposed_range_str,
                                overlap_minutes=overlap_mins,
                                message=msg,
                            ))
                except Exception as e:
                    logger.warning("Error checking perma vs onetime conflict: %s", e)

        # 3. ONETIME (новое) vs PERMA (существующее)
        elif ctype != "perma" and c_type == "perma":
            if start_date:
                try:
                    d_obj = date.fromisoformat(start_date)
                    d_day = mh_weekday(d_obj)
                    if d_day in c_days:
                        e_start = c_date or "2000-01-01"
                        e_end = c_end_date or "2099-12-31"
                        if e_start <= start_date <= e_end:
                            day_name = f"{start_date} ({WD_RU.get(d_day, '')})"
                            msg = (
                                f"Конфликт с регулярной серией «{c_title}»: {day_name} {existing_range_str} "
                                f"(пересечение {overlap_mins} мин)."
                            )
                            conflicts.append(ScheduleConflict(
                                conflicting_class_id=cid,
                                conflicting_title=c_title,
                                class_type="perma",
                                days_or_date=day_name,
                                existing_time_range=existing_range_str,
                                proposed_time_range=proposed_range_str,
                                overlap_minutes=overlap_mins,
                                message=msg,
                            ))
                except Exception as e:
                    logger.warning("Error checking onetime vs perma conflict: %s", e)

        # 4. ONETIME (новое) vs ONETIME (существующее)
        elif ctype != "perma" and c_type != "perma":
            if start_date and c_date and start_date == c_date:
                msg = (
                    f"Конфликт с разовым занятием «{c_title}»: {start_date} {existing_range_str} "
                    f"(пересечение {overlap_mins} мин)."
                )
                conflicts.append(ScheduleConflict(
                    conflicting_class_id=cid,
                    conflicting_title=c_title,
                    class_type="oneTime",
                    days_or_date=start_date,
                    existing_time_range=existing_range_str,
                    proposed_time_range=proposed_range_str,
                    overlap_minutes=overlap_mins,
                    message=msg,
                ))

    return conflicts
