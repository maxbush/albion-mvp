"""Рекуррентность занятий (perma-серии MeritHub).

MeritHub кодирует дни недели как: 0=вс, 1=пн, 2=вт, 3=ср, 4=чт, 5=пт, 6=сб.
У нас занятия серий НЕ хранятся по одному occurrence — вычисляются на лету
из паттерна (дни + время + start_time серии). Канон — зона организации
(settings.albion_org_timezone, решение H4/P4.1).
"""

import json
from datetime import date, datetime, time, timedelta, timezone

from src.config import settings

# Ключ = код дня MeritHub (0=вс).
WD_RU = {0: "вс", 1: "пн", 2: "вт", 3: "ср", 4: "чт", 5: "пт", 6: "сб"}
WD_EN = {0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat"}
MONTHS_RU = ["", "янв", "фев", "мар", "апр", "мая", "июн",
             "июл", "авг", "сент", "окт", "нояб", "дек"]


def org_now() -> datetime:
    """Текущее время в канонической зоне организации."""
    return datetime.now(settings.org_zone())


def org_zone_label() -> str:
    """'Europe/London' → 'London' — короткая подпись канонической зоны."""
    return settings.albion_org_timezone.split("/")[-1]


def fmt_dt_org(raw) -> str:
    """ISO-строку времени → '01.08, 10:12 (London)' в org-зоне.

    Naive-вход считаем UTC (формат SQLite CURRENT_TIMESTAMP).
    Единая точка форматирования времени для всех карточек координатора
    (никаких голых 'UTC'-обрубков — решение R7-4)."""
    if not raw:
        return "—"
    try:
        dt = datetime.fromisoformat(str(raw).replace(" ", "T"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt.astimezone(settings.org_zone()).strftime("%d.%m, %H:%M")
                + f" ({org_zone_label()})")
    except Exception:
        return str(raw)[:16]


def mh_weekday(d: date) -> int:
    """python-weekday (пн=0..вс=6) → код MeritHub (вс=0..сб=6)."""
    return (d.weekday() + 1) % 7


def parse_days(raw) -> list[int]:
    """JSON-строка/список дней → list[int]. Терпимо к мусору в данных."""
    if isinstance(raw, list):
        return [int(x) for x in raw if str(x).isdigit() or isinstance(x, int)]
    try:
        val = json.loads(raw or "[]")
    except Exception:
        return []
    if not isinstance(val, list):
        return []
    return [int(x) for x in val if isinstance(x, (int, float)) or str(x).isdigit()]


def fmt_days(days: list[int]) -> str:
    """[1, 4] → 'пн, чт' (в порядке пн..вс)."""
    ordered = sorted(days, key=lambda x: (x + 6) % 7)  # пн(1)→0 ... вс(0)→6
    return ", ".join(WD_RU.get(d, str(d)) for d in ordered)


def next_occurrence(days: list[int], hhmm: tuple[int, int],
                    after: datetime | None = None) -> datetime | None:
    """Ближайшее занятие серии после `after` (по умолчанию — сейчас, org-зона).

    Ищем вперёд до 370 дней. Возвращает aware datetime в org-зоне.
    """
    if not days:
        return None
    zone = settings.org_zone()
    after = after or org_now()
    if after.tzinfo is None:
        after = after.replace(tzinfo=zone)
    d = after.date()
    for i in range(370):
        cand_d = d + timedelta(days=i)
        if mh_weekday(cand_d) not in days:
            continue
        cand = datetime.combine(cand_d, time(hhmm[0], hhmm[1]), tzinfo=zone)
        if cand > after:
            return cand
    return None


def occurs_on(days: list[int], d: date) -> bool:
    """Есть ли занятие серии в дату d (без учёта start/end серии)."""
    return bool(days) and mh_weekday(d) in days


def class_occurs_on(class_row: dict, d: date) -> bool:
    """Приходится ли занятие класса (oneTime или perma) на дату d (org-канон).

    oneTime: дата start_time == d.
    perma:   день недели ∈ schedule_days И d ∈ [start серии, end_date].
    """
    ctype = class_row.get("class_type") or "oneTime"
    start = (class_row.get("start_time") or "")[:10]
    if ctype != "perma":
        return bool(start) and start == d.isoformat()
    days = parse_days(class_row.get("schedule_days"))
    if not occurs_on(days, d):
        return False
    if start and d.isoformat() < start:
        return False
    end = (class_row.get("end_date") or "")[:10]
    if end and d.isoformat() > end:
        return False
    return True


def fmt_occurrence_label(dt: datetime) -> str:
    """'сб 02 авг, 15:30' — подпись занятия для карточек."""
    return (f"{WD_RU[mh_weekday(dt.date())]} {dt.day:02d} {MONTHS_RU[dt.month]}, "
            f"{dt.strftime('%H:%M')}")


def participant_time_line(dt: datetime, name: str, user_tz: str | None,
                          default_note: str = " (по умолчанию)") -> str:
    """Строка превью '• Sofia — 18:30 (Asia/Dubai, +3ч)'.

    dt — aware datetime в org-зоне. Разница поясов считается на дату занятия
    (DST-aware, не константа): сравниваем utcoffset() обеих зон в этот момент.
    """
    from zoneinfo import ZoneInfo
    org_tz = settings.albion_org_timezone
    tz = user_tz or org_tz
    note = default_note if not user_tz else ""
    if tz == org_tz:
        return f"• {name} — {dt.strftime('%H:%M')} ({org_tz}{note})"
    try:
        loc = dt.astimezone(ZoneInfo(tz))
    except Exception:
        return f"• {name} — {dt.strftime('%H:%M')} ({org_tz}{note})"
    org_off = dt.utcoffset() or timedelta(0)
    user_off = loc.utcoffset() or timedelta(0)
    diff = (user_off - org_off).total_seconds() / 3600
    if diff == 0:
        return f"• {name} — {loc.strftime('%H:%M')} ({tz}{note})"
    sign = "+" if diff > 0 else ""
    shown = int(diff) if diff == int(diff) else round(diff, 1)
    return f"• {name} — {loc.strftime('%H:%M')} ({tz}, {sign}{shown}ч{note})"


def org_day_utc_bounds(d: date) -> tuple[str, str]:
    """UTC-границы org-дня d для created_at (R9-6).

    Возвращает (start, end) в формате 'YYYY-MM-DD HH:MM:SS' (UTC, naive) —
    том же, в котором SQLite хранит CURRENT_TIMESTAMP, поэтому сравнение
    строк корректно. Раньше 'сегодня' в /today считалось по серверной naive-
    зоне через LIKE 'YYYY-MM-DD%' — при сервере не в org-зоне списки занятий
    и инцидентов расходились."""
    zone = settings.org_zone()
    start = datetime(d.year, d.month, d.day, tzinfo=zone)
    end = start + timedelta(days=1)
    fmt = "%Y-%m-%d %H:%M:%S"
    return (start.astimezone(timezone.utc).strftime(fmt),
            end.astimezone(timezone.utc).strftime(fmt))
