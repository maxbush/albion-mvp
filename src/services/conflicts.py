"""Контроль пересечений занятий у репетитора (PR5, PHASE1_ARCHITECTURE.md §5).

find_conflicts() — единственная точка проверки: материализует расписание
репетитора через services.overrides (perma-паттерн + точечные маски) и
сравнивает интервалы кандидата со слотами остальных его классов.

Политика применения — у вызывающего: визард /schedule для perma-кандидата
делает жёсткий блок (решение владельца), для разового — предупреждение;
/reschedule блокирует слот назначения.
"""

from datetime import date, timedelta

from src.db.repository import MeritHubClassRepository
from src.services.overrides import effective_dates
from src.utils.recurrence import org_now

HORIZON_DAYS = 60


def _hhmm_to_min(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


async def _occurrences(c: dict, start: date, end: date, db_path=None) -> list[tuple[date, int, int]]:
    """Эффективные слоты класса в [start, end]: (date, start_min, dur_min)."""
    eff = await effective_dates(
        c, [start + timedelta(days=i) for i in range((end - start).days + 1)],
        db_path=db_path)
    dur = int(c.get("duration") or 60)
    out = []
    for d_iso, t_ovr in sorted(eff.items()):
        hhmm = t_ovr or (c.get("start_time") or "")[11:16]
        if len(hhmm) >= 5:
            out.append((date.fromisoformat(d_iso), _hhmm_to_min(hhmm), dur))
    return out


async def find_conflicts(tutor_cuid: str, candidate: dict,
                         db_path=None) -> list[dict]:
    """Пересечения кандидата с занятиями репетитора.

    candidate: {
        ctype: 'perma'|'one',
        days: [mh codes 0=вс..6=сб]  (perma),
        date: 'YYYY-MM-DD'           (one),
        hhmm: 'HH:MM',
        duration: int (минуты),
        exclude_occurrence: (class_id, 'YYYY-MM-DD') | None
            # при переносе исключаем ИСХОДНЫЙ occurrence — не весь класс:
            # другие его слоты по-прежнему заняты и конфликтуют.
    }
    → [{class_id, title, count, examples: ['YYYY-MM-DD HH:MM', ...]}]
    """
    now = org_now()
    today = now.date()
    horizon = today + timedelta(days=HORIZON_DAYS)
    dur = int(candidate.get("duration") or 60)
    start_min = _hhmm_to_min(candidate["hhmm"])
    excl_cid, excl_date = candidate.get("exclude_occurrence") or (None, None)

    # слоты кандидата
    cand: list[tuple[date, int, int]] = []
    if candidate.get("ctype") == "perma":
        for i in range((horizon - today).days + 1):
            d = today + timedelta(days=i)
            if (d.weekday() + 1) % 7 in (candidate.get("days") or []):
                # сегодняшний слот уже прошёл — серия реально начнётся
                # со следующего occurrence
                if d == today and start_min <= now.hour * 60 + now.minute:
                    continue
                cand.append((d, start_min, dur))
    else:
        cand.append((date.fromisoformat(candidate["date"]), start_min, dur))

    # окно материализации существующих занятий: для разового кандидата —
    # сама его дата (может быть дальше HORIZON_DAYS), для серии — горизонт.
    if candidate.get("ctype") == "perma":
        w_start, w_end = today, horizon
    else:
        w_start = w_end = cand[0][0]

    classes = await MeritHubClassRepository(db_path).list_all()
    seen: dict[str, dict] = {}
    for c in classes:
        if c.get("tutor_client_user_id") != tutor_cuid:
            continue
        hits = []
        for d, s, dd in await _occurrences(c, w_start, w_end, db_path):
            if c["class_id"] == excl_cid and d.isoformat() == excl_date:
                continue  # исходный слот переноса — не конфликт
            for cd, cs, cdd in cand:
                if d == cd and s < cs + cdd and cs < s + dd:
                    hits.append((d, s))
                    break
        if hits:
            hhmm0 = (c.get("start_time") or "")[11:16]
            seen[c["class_id"]] = {
                "class_id": c["class_id"],
                "title": c.get("title") or c["class_id"],
                "count": len(hits),
                "examples": [f"{d.isoformat()} {s // 60:02d}:{s % 60:02d}"
                             for d, s in sorted(hits)[:3]],
                "slot_hhmm": hhmm0,
            }
    return list(seen.values())


def conflict_lines(conflicts: list[dict]) -> list[str]:
    """Текст для карточек/ответов: по классу — примеры дат."""
    lines = []
    for c in conflicts:
        ex = ", ".join(c["examples"])
        more = f" (+{c['count'] - len(c['examples'])} слотов)" if c["count"] > len(c["examples"]) else ""
        lines.append(f"• {c['title']}: {ex}{more}")
    return lines
