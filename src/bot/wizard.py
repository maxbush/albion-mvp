"""Кнопочные сценарии координатора (визарды): /schedule, /add_student, /add_tutor.

Принципы (зафиксированы в дизайне R5):
- Ни один ID не вводится руками: сущности выбираются из списков, cuid — авто.
- Одномессадж-визард: каждый шаг редактирует одну и ту же карточку (edit_message),
  чат не засоряется; free-text ответы пользователя удаляются после принятия.
- Состояние — в SQLite (wizard_state): переживает рестарт бота; TTL неактивности
  10 минут, просроченные состояния сметает sweep_expired_wizards с уведомлением.
- Канон времени — зона организации (H4/P4.1); пояса участников — только в
  dual-time превью перед созданием.
- Необратимое создание — только с карточки превью (Error Prevention).

Маршрутизация: handle_wz_callback и try_handle_wz_text вызываются из
src/bot/handlers.py (callbacks с префиксом "wz:" и обычный текст соответственно).
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError

from src.config import settings
from src.db.repository import (
    MeritHubClassRepository,
    MeritHubContactRepository,
    MeritHubEnrollmentRepository,
    MeritHubStudentRepository,
    UserRepository,
    WizardStateRepository,
)
from src.bot.roles import is_admin, is_coordinator_or_admin
from src.events.bus import bus
from src.events.types import Event, EventTypes
from src.integrations.factory import get_merithub_service
from src.workflows.lesson_ops import LessonOpsWorkflow
from src.utils.recurrence import (
    WD_RU, MONTHS_RU, fmt_days, fmt_occurrence_label, next_occurrence,
    org_now, org_zone_label, participant_time_line,
)

logger = logging.getLogger(__name__)

WIZARD_TTL_MIN = 10
PAGE_SIZE = 8

FLOW_TITLES = {
    "schedule": "Новое занятие",
    "add_student": "Новый ученик",
    "add_tutor": "Новый репетитор",
}
FLOW_COMMANDS = {"schedule": "schedule", "add_student": "add_student", "add_tutor": "add_tutor"}

TZ_CHOICES = [
    ("Europe/London", "London"),
    ("Europe/Paris", "Paris"),
    ("Europe/Vienna", "Vienna"),
    ("Asia/Dubai", "Dubai"),
    ("Asia/Almaty", "Almaty"),
    ("Europe/Moscow", "Moscow"),
]
HOURS = list(range(7, 21))          # сетка часов 07..20 (org-зона)
MINUTES = [0, 15, 30, 45]
DURATIONS = [30, 45, 60, 90]
DAY_ORDER = [1, 2, 3, 4, 5, 6, 0]     # кнопки Пн..Вс в кодах MeritHub

_TEXT_STEPS = {"tutor_search", "time_custom", "date_custom", "duration_custom",
               "name", "tz_custom"}


# =====================================================================
# Мелкие хелперы клавиатур/состояния
# =====================================================================

def _btn(text: str, cb: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=cb)


def _kb(rows: list[list]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expires_iso() -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(minutes=WIZARD_TTL_MIN)).isoformat()


async def _load(chat_id: str):
    """(state, expired_row): живое состояние или просроченное (которое удалено)."""
    repo = WizardStateRepository()
    row = await repo.get(chat_id)
    if not row:
        return None, None
    if row.get("expires_at") and row["expires_at"] < _now_iso():
        await repo.delete(chat_id)
        return None, row
    try:
        row["data"] = json.loads(row.get("data") or "{}")
    except Exception:
        row["data"] = {}
    return row, None


async def _save(state: dict) -> None:
    await WizardStateRepository().save(
        state["chat_id"], state["flow"], state["step"], state["data"], _expires_iso())


async def _ack(query, ack: dict, text: str | None = None, alert: bool = False) -> None:
    """Ответ на callback ровно один раз (toast для мелких ошибок, без спама в чат)."""
    if ack.get("done"):
        return
    try:
        if text:
            await query.answer(text, show_alert=alert)
        else:
            await query.answer()
    except Exception:
        pass
    ack["done"] = True


async def _show(upd: Update, ctx, state: dict, text: str, markup=None) -> None:
    """Показывает карточку шага.

    Из callback — редактирует сообщение кнопки. Из текстового ввода — редактирует
    запомненную карточку (msg_id); если отредактировать нельзя — шлёт новую.
    BadRequest 'message is not modified' при повторном показе — допустимая тишина.
    """
    query = upd.callback_query
    if query is not None:
        # запоминаем id карточки для последующего редактирования из текстовых шагов
        mid = getattr(getattr(query, "message", None), "message_id", None)
        if mid:
            state["data"]["msg_id"] = mid
        try:
            await query.edit_message_text(text, reply_markup=markup)
            return
        except BadRequest:
            return
        except TelegramError as e:
            logger.warning("Wizard: edit via query failed: %s", e)
            return
    chat_id = int(state["chat_id"])
    msg_id = state["data"].get("msg_id")
    if msg_id:
        try:
            await ctx.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id, text=text, reply_markup=markup)
            return
        except Exception:
            pass  # карточку не нашли — ниже шлём новую
    m = await ctx.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
    new_mid = getattr(m, "message_id", None)
    if new_mid:
        state["data"]["msg_id"] = new_mid
        await _save(state)


async def _delete_user_text(upd: Update) -> None:
    """Удаляет free-text сообщение пользователя — диалог остаётся карточкой."""
    try:
        await upd.message.delete()
    except Exception:
        pass


def _expired_text(flow: str) -> str:
    return (f"⌛ Сценарий «{FLOW_TITLES.get(flow, '?')}» прерван после "
            f"{WIZARD_TTL_MIN} мин бездействия. Данные не сохранены.\n"
            f"Начать заново: /{FLOW_COMMANDS.get(flow, 'schedule')}")


def _cancel_row(flow: str) -> list:
    return [_btn("❌ Отмена", f"wz:{flow}:cancel")]


def _back_cancel_row(flow: str) -> list:
    return [_btn("◀️ Назад", f"wz:{flow}:back"), _btn("❌ Отмена", f"wz:{flow}:cancel")]


def _pager(flow: str, page: int, total: int, cb_name: str) -> list:
    if total <= 1:
        return []
    return [
        _btn("◀️", f"wz:{flow}:{cb_name}:{max(0, page - 1)}"),
        _btn(f"{page + 1}/{total}", "wz:noop"),
        _btn("▶️", f"wz:{flow}:{cb_name}:{min(total - 1, page + 1)}"),
    ]


def _paged(items: list, page: int) -> tuple[list, int, int]:
    total = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(0, page), total - 1)
    return items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE], page, total


async def _next_cuid(prefix: str) -> str:
    """Авто-cuid: продолжает существующую нумерацию ('s01..' → 's19'), человек ID не видит."""
    rows = await MeritHubStudentRepository().list_all()
    best = 0
    for r in rows:
        m = re.fullmatch(rf"{re.escape(prefix)}(\d+)", r.get("client_user_id") or "")
        if m:
            best = max(best, int(m.group(1)))
    return f"{prefix}{best + 1:02d}"


# =====================================================================
# Поток «Новое занятие» (/schedule)
# =====================================================================

async def _sched_view(step: str, d: dict) -> tuple[str, InlineKeyboardMarkup | None]:
    """Рендер шага: (текст карточки, клавиатура)."""
    srepo = MeritHubStudentRepository()
    org = org_zone_label()

    if step == "tutor":
        tutors = await srepo.list_by_role("tutor")
        q = (d.get("tsearch_q") or "").lower()
        note = ""
        if q:
            tutors = [t for t in tutors if q in (t.get("name") or "").lower()]
            if not tutors:
                rows = [[_btn("◀️ К полному списку", "wz:sched:tclear")], _cancel_row("sched")]
                return (f"📅 Новое занятие\n\nПо запросу «{d.get('tsearch_q')}» никого нет."), _kb(rows)
            note = f" (поиск: «{d.get('tsearch_q')}»)"
        page_items, page, total = _paged(tutors, int(d.get("tutor_page", 0)))
        rows = [[_btn(t.get("name") or t["client_user_id"], f"wz:sched:tutor:{t['client_user_id']}")]
                for t in page_items]
        nav = _pager("sched", page, total, "tpage")
        if nav:
            rows.append(nav)
        rows.append([_btn("🔍 Найти", "wz:sched:tsearch")])
        rows.append(_cancel_row("sched"))
        return f"📅 Новое занятие\n\nРепетитор{note}:", _kb(rows)

    if step == "tutor_search":
        rows = [[_btn("◀️ К списку", "wz:sched:tclear")], _cancel_row("sched")]
        return "📅 Новое занятие\n\nВведите часть имени репетитора:", _kb(rows)

    if step == "students":
        selected = {s["cuid"]: s["name"] for s in d.get("students", [])}
        students = await srepo.list_by_role("student")
        page_items, page, total = _paged(students, int(d.get("student_page", 0)))
        rows = []
        for s in page_items:
            cuid, name = s["client_user_id"], s.get("name") or s["client_user_id"]
            mark = "✅ " if cuid in selected else ""
            rows.append([_btn(f"{mark}{name}", f"wz:sched:student:{cuid}")])
        nav = _pager("sched", page, total, "spage")
        if nav:
            rows.append(nav)
        rows.append([_btn(f"Готово · {len(selected)}", "wz:sched:sdone")])
        rows.append(_back_cancel_row("sched"))
        sel_line = "\nВыбрано: " + ", ".join(selected.values()) if selected else ""
        return (f"📅 Новое занятие\n\nРепетитор: {d.get('tutor_name')}\n"
                f"Ученики (можно несколько):{sel_line}"), _kb(rows)

    if step == "type":
        text = ("Тип занятия\n\n"
                "🔁 Регулярное — каждую неделю в выбранные\nдни, пока вы не остановите.\n"
                "1️⃣ Разовое — один раз, в конкретную дату.\n\n"
                "⚠️ Тип нельзя изменить после создания.")
        rows = [[_btn("🔁 Регулярное", "wz:sched:type:perma"),
                 _btn("1️⃣ Разовое", "wz:sched:type:one")],
                _back_cancel_row("sched")]
        return text, _kb(rows)

    if step == "days":
        days = d.get("days", [])
        rows = [[_btn(("✅ " if code in days else "") + WD_RU[code].capitalize(),
                      f"wz:sched:day:{code}") for code in DAY_ORDER]]
        rows.append([_btn(f"Готово · {len(days)}", "wz:sched:ddone")])
        rows.append(_back_cancel_row("sched"))
        sel = f"\nВыбрано: {fmt_days(days)}" if days else ""
        return f"Дни недели{sel}", _kb(rows)

    if step == "date":
        today = org_now().date()
        from datetime import timedelta as _td
        rows, labels = [], []
        for i in range(5):
            dd = today + _td(days=i)
            if i == 0:
                label = f"Сегодня · {dd.day:02d} {MONTHS_RU[dd.month]}"
            elif i == 1:
                label = f"Завтра · {dd.day:02d} {MONTHS_RU[dd.month]}"
            else:
                label = f"{WD_RU[(dd.weekday() + 1) % 7].capitalize()} · {dd.day:02d} {MONTHS_RU[dd.month]}"
            rows.append([_btn(label, f"wz:sched:date:{dd.isoformat()}")])
        rows.append([_btn("Другая дата", "wz:sched:datec")])
        rows.append(_back_cancel_row("sched"))
        return f"Дата занятия ({org})", _kb(rows)

    if step == "date_custom":
        return (f"Дата занятия ({org})\n\nВведите дату как ДД.ММ "
                f"(год — ближайшая будущая), например 25.08:"), _kb([_back_cancel_row("sched")])

    if step == "hour":
        hhmm = f"{d.get('hour')}:" if d.get("hour") is not None else ""
        rows = [[_btn(f"{h:02d}", f"wz:sched:hour:{h}") for h in HOURS[:7]],
                [_btn(f"{h:02d}", f"wz:sched:hour:{h}") for h in HOURS[7:]]]
        rows.append(_back_cancel_row("sched"))
        return f"Время начала ({org})\n\nЧас: {hhmm}", _kb(rows)

    if step == "minute":
        rows = [[_btn(f":{m:02d}", f"wz:sched:min:{m}") for m in MINUTES]]
        rows.append([_btn("Своё время", "wz:sched:timec")])
        rows.append(_back_cancel_row("sched"))
        hh = int(d.get("hour", 0))
        return f"Время: {hh:02d}:__ ({org})\n\nМинуты:", _kb(rows)

    if step == "time_custom":
        return (f"Своё время ({org})\n\nВведите время как ЧЧ:ММ, например 16:45:"), \
            _kb([_back_cancel_row("sched")])

    if step == "duration":
        rows = [[_btn(f"{m} мин", f"wz:sched:dur:{m}") for m in DURATIONS]]
        rows.append([_btn("Своя", "wz:sched:durc")])
        rows.append(_back_cancel_row("sched"))
        return "Длительность занятия", _kb(rows)

    if step == "duration_custom":
        return "Длительность (минут)\n\nВведите число от 15 до 240:", \
            _kb([_back_cancel_row("sched")])

    if step == "preview":
        return await _sched_preview_text(d), _kb([
            [_btn("✅ Создать занятие", "wz:sched:confirm")],
            [_btn("✏️ Изменить", "wz:sched:editmenu")],
            _back_cancel_row("sched"),
        ])

    if step == "editmenu":
        rows = [[_btn("Репетитора", "wz:sched:edit:tutor"),
                 _btn("Учеников", "wz:sched:edit:students")]]
        if d.get("ctype") == "perma":
            rows.append([_btn("Тип", "wz:sched:edit:type"), _btn("Дни", "wz:sched:edit:days")])
        else:
            rows.append([_btn("Тип", "wz:sched:edit:type"), _btn("Дату", "wz:sched:edit:date")])
        rows.append([_btn("Время", "wz:sched:edit:time"), _btn("Длительность", "wz:sched:edit:duration")])
        rows.append([_btn("◀️ К проверке", "wz:sched:topreview")])
        return "Что изменить?", _kb(rows)

    return "Неизвестный шаг сценария.", _kb([_cancel_row("sched")])


async def _sched_occurrence_dt(d: dict):
    """Дата-время ближайшего занятия (aware, org-зона): для превью и создания."""
    hh, mm = int(d.get("hour") or 0), int(d.get("minute") or 0)
    if d.get("ctype") == "perma":
        return next_occurrence(d.get("days") or [], (hh, mm))
    from datetime import date as _date, datetime as _dt
    try:
        dd = _date.fromisoformat(d.get("date"))
    except Exception:
        return None
    return _dt(dd.year, dd.month, dd.day, hh, mm, tzinfo=settings.org_zone())


def _sched_main_line(d: dict, occ_dt) -> str:
    """'🔁 ср, сб · 15:30 London · 60 мин' / '1️⃣ ср 06 авг · 15:30 London · 60 мин'."""
    org = org_zone_label()
    hhmm = f"{int(d.get('hour') or 0):02d}:{int(d.get('minute') or 0):02d}"
    dur = d.get("duration") or 60
    if d.get("ctype") == "perma":
        return f"🔁 {fmt_days(d.get('days') or [])} · {hhmm} {org} · {dur} мин"
    day_label = fmt_occurrence_label(occ_dt).split(",")[0] if occ_dt else d.get("date", "")
    return f"1️⃣ {day_label} · {hhmm} {org} · {dur} мин"


async def _sched_preview_text(d: dict) -> str:
    occ_dt = await _sched_occurrence_dt(d)
    students = d.get("students", [])
    names = ", ".join(s["name"] for s in students) or "—"
    lines = ["📋 Проверьте перед созданием", ""]
    lines.append(f"🧑‍🏫 {d.get('tutor_name')}")
    lines.append(f"👥 {names}")
    lines.append(_sched_main_line(d, occ_dt))
    if occ_dt:
        lines.append("")
        lines.append("🌍 Участники увидят:")
        srepo = MeritHubStudentRepository()
        trow = await srepo.get_by_client_id(d.get("tutor_cuid") or "")
        lines.append(participant_time_line(
            occ_dt, (trow or {}).get("name") or d.get("tutor_name"), (trow or {}).get("timezone")))
        for s in students:
            srow = await srepo.get_by_client_id(s["cuid"])
            lines.append(participant_time_line(occ_dt, s["name"], (srow or {}).get("timezone")))
        # Мягкий анти-дубль (предупреждение без блокировки — дубли бывают легитимны)
        dup = await _sched_find_duplicate(d)
        if dup:
            lines.append("")
            lines.append(f"⚠️ Похожее занятие уже есть: {dup}")
    lines.append("")
    ctype_ru = "регулярное" if d.get("ctype") == "perma" else "разовое"
    lines.append(f"⚠️ Тип ({ctype_ru}) изменить после создания нельзя.")
    return "\n".join(lines)


async def _sched_find_duplicate(d: dict) -> str | None:
    """Серия/занятие того же репетитора с тем же временем — подсвечиваем в превью."""
    classes = await MeritHubClassRepository().list_all()
    hhmm = f"{int(d.get('hour') or 0):02d}:{int(d.get('minute') or 0):02d}"
    for c in classes:
        if c.get("tutor_client_user_id") != d.get("tutor_cuid"):
            continue
        ctype = c.get("class_type") or "oneTime"
        ctime = (c.get("start_time") or "")[11:16]
        if d.get("ctype") == "perma" and ctype == "perma":
            shared = set(d.get("days") or []) & set(json.loads(c.get("schedule_days") or "[]")
                                                      if c.get("schedule_days") else [])
            if shared and ctime == hhmm:
                title = c.get("title") or c["class_id"]
                return f"🔁 {fmt_days(sorted(shared))} {ctime} · {title}"
        if d.get("ctype") == "one" and ctype != "perma":
            if ctime == hhmm and (c.get("start_time") or "")[:10] == d.get("date"):
                return (c.get("title") or c["class_id"])
    return None


async def _sched_goto(upd: Update, ctx, state: dict, step: str, extra_text: str | None = None) -> None:
    state["step"] = step
    await _save(state)
    text, markup = await _sched_view(step, state["data"])
    if extra_text:
        text = f"{extra_text}\n\n{text}"
    await _show(upd, ctx, state, text, markup)


async def _sched_cancel(upd: Update, state: dict) -> None:
    await WizardStateRepository().delete(state["chat_id"])
    await _show(upd, None, state, "Создание занятия отменено.", None)


# Шаги, после которых в обычном режиме идём сюда:
_NEXT = {
    "tutor": "students", "students": "type", "days": "hour", "date": "hour",
    "date_custom": "hour", "hour": "minute", "minute": "duration", "time_custom": "duration",
    "duration": "preview", "duration_custom": "preview",
}
_BACK = {
    "students": "tutor", "tutor_search": "tutor", "type": "students",
    "days": "type", "date": "type", "date_custom": "date", "hour": None,  # зависит от типа
    "minute": "hour", "time_custom": "minute", "duration": "minute",
    "duration_custom": "duration", "preview": "duration",
}
# Какой edit-флаг «закрывает» шаг (после правки одного поля — обратно в превью):
_EDIT_DONE = {
    "tutor": "tutor", "students": "students", "days": "days",
    "date": "date", "date_custom": "date", "duration": "duration", "duration_custom": "duration",
    "minute": "time", "time_custom": "time",
}


async def _sched_advance(upd: Update, ctx, state: dict, just_done: str) -> None:
    """Переход вперёд: в режиме правки — к превью, иначе — к следующему шагу."""
    d = state["data"]
    edit = d.get("edit")
    if edit and _EDIT_DONE.get(just_done) == edit:
        d.pop("edit", None)
        await _sched_goto(upd, ctx, state, "preview")
        return
    nxt = _NEXT.get(just_done)
    # Внимание: редактирование времени идёт парой час+минуты — флаг edit='time'
    # живёт до minute (см. ветку 'hour' в _sched_cb), досрочно не снимаем.
    await _sched_goto(upd, ctx, state, nxt or "preview")


async def _sched_back(upd: Update, ctx, state: dict) -> None:
    d = state["data"]
    if d.get("edit"):
        d.pop("edit", None)
        await _sched_goto(upd, ctx, state, "preview")
        return
    cur = state["step"]
    if cur == "hour":
        prev = "days" if d.get("ctype") == "perma" else "date"
    else:
        prev = _BACK.get(cur)
    await _sched_goto(upd, ctx, state, prev or "tutor")


async def _sched_confirm(upd: Update, ctx, state: dict, ack: dict | None = None) -> None:
    """Создание класса в MeritHub + локальные записи + напоминания первого занятия."""
    d = state["data"]
    if d.get("submitting"):
        if ack is not None:
            await _ack(upd.callback_query, ack, "Уже создаю…")
        return
    d["submitting"] = True
    state["step"] = "submitting"
    await _save(state)
    await _show(upd, ctx, state, "⏳ Создаю занятие в MeritHub…", None)

    res = await _sched_create(d)
    d["submitting"] = False

    if not res["ok"]:
        state["step"] = "preview"
        await _save(state)
        text = (f"❌ MeritHub не создал занятие\n\nПричина: {res['reason']}\n\n"
                "Ничего не создано. Введённые данные сохранены.")
        await _show(upd, ctx, state, text, _kb([
            [_btn("🔁 Повторить", "wz:sched:retry")],
            [_btn("✏️ Изменить", "wz:sched:editmenu")],
            [_btn("❌ Отмена", "wz:sched:cancel")],
        ]))
        return

    # Success — финальная карточка, состояние удаляем
    await WizardStateRepository().delete(state["chat_id"])
    names = ", ".join(res["names"]) or "—"
    lines = ["✅ Занятие создано", ""]
    lines.append(_sched_main_line(d, res["start_dt"]))
    lines.append(f"🧑‍🏫 {d.get('tutor_name')} · 👥 {names}")
    lines.append(f"🆔 {res['class_id']}")
    if res.get("start_dt"):
        lines.append(f"Ближайшее: {fmt_occurrence_label(res['start_dt'])} {org_zone_label()}")
    notes = res.get("notes") or []
    if notes:
        lines.append("")
        lines.extend(notes)
    await _show(upd, ctx, state, "\n".join(lines),
                _kb([[_btn("➕ Ещё занятие", "wz:sched:again")]]))


async def _sched_create(d: dict) -> dict:
    """Вызовы MeritHub API + локальные записи. Зеркалит /mh_schedule, но с сериями."""
    srepo = MeritHubStudentRepository()
    tutor = await srepo.get_by_client_id(d.get("tutor_cuid") or "")
    if not tutor or not tutor.get("merithub_user_id"):
        return {"ok": False, "reason": f"Репетитор {d.get('tutor_cuid')} не найден. Сначала /add_tutor."}

    student_rows, missing = [], []
    for s in d.get("students", []):
        row = await srepo.get_by_client_id(s["cuid"])
        if not row or not row.get("merithub_user_id"):
            missing.append(s["name"])
            continue
        student_rows.append(row)
    if not student_rows:
        return {"ok": False, "reason": "Ни один из выбранных учеников не найден в базе."}

    ctype = "perma" if d.get("ctype") == "perma" else "oneTime"
    occ_dt = await _sched_occurrence_dt(d)
    if occ_dt is None:
        return {"ok": False, "reason": "Не удалось вычислить дату занятия (проверьте дни/дату)."}
    start_iso = occ_dt.isoformat()
    names = [s.get("name") or s["client_user_id"] for s in student_rows]
    title = ("; ".join(names) + f" — {tutor.get('name') or d.get('tutor_cuid')}")[:150]

    client = get_merithub_service()
    try:
        sched = await client.schedule_class(
            tutor["merithub_user_id"],
            title=title,
            start_time=start_iso,
            duration=int(d.get("duration") or 60),
            timezone=settings.albion_org_timezone,
            type=ctype,
            schedule=d.get("days") if ctype == "perma" else None,
        )
    except Exception as e:
        return {"ok": False, "reason": f"ошибка API: {str(e)[:180]}"}

    info = client.parse_schedule(sched)
    class_id = info.get("class_id")
    if not class_id:
        return {"ok": False, "reason": f"не получен classId: {str(sched)[:180]}"}

    await MeritHubClassRepository().upsert(
        class_id,
        host_link=info.get("host_link"),
        participant_link=info.get("participant_link"),
        title=title,
        start_time=start_iso,
        tutor_client_user_id=d.get("tutor_cuid"),
        tutor_merithub_user_id=tutor["merithub_user_id"],
        class_type=ctype,
        schedule_days=json.dumps(d.get("days") or []) if ctype == "perma" else None,
        duration=int(d.get("duration") or 60),
        timezone=settings.albion_org_timezone,
    )

    # Персональные ссылки участникам (как /mh_schedule): host → tutor, participant → ученикам
    notes, warnings = [], []
    users = []
    if info.get("host_link"):
        users.append({"userId": tutor["merithub_user_id"],
                      "userLink": info["host_link"], "userType": "su"})
    if info.get("participant_link"):
        for s in student_rows:
            users.append({"userId": s["merithub_user_id"],
                          "userLink": info["participant_link"], "userType": "su"})
    user_links = {}
    if users:
        try:
            resp = await client.add_users_to_class(class_id, users)
            user_links = client.parse_user_links(resp)
        except Exception as e:
            logger.warning("Wizard: add_users_to_class failed: %s", e)
            warnings.append("⚠️ Персональные ссылки не получены — отправьте из MeritHub вручную.")

    start_display = fmt_occurrence_label(occ_dt)
    contact = await MeritHubContactRepository().get(d.get("tutor_cuid") or "")
    tutor_tg = (contact or {}).get("telegram_id")
    tutor_link = user_links.get(tutor.get("merithub_user_id", ""))
    if tutor_tg and tutor_link:
        # Ссылка репетитору — на его языке (i18n, П3).
        from src.utils.i18n import lang_of, tr
        await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
            "telegram_id": tutor_tg,
            "message": tr("tutor_link", await lang_of(tutor_tg),
                          time=start_display, students=", ".join(names),
                          url=client.room_url(tutor_link)),
        }))
        notes.append("📎 Ссылки отправлены: репетитору")
    else:
        warnings.append("⚠️ Ссылка репетитору не отправлена — привяжите TG (/add_tutor).")

    for s in student_rows:
        parent_tg, s_link = s.get("parent_telegram_id"), user_links.get(s.get("merithub_user_id", ""))
        if parent_tg and s_link:
            await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
                "telegram_id": parent_tg,
                "message": (f"📎 Ссылка для подключения:\nУченик: {s.get('name')}\n"
                            f"🕐 {start_display}\n🔗 {client.room_url(s_link)}"),
            }))
            notes.append(f"📎 Ссылка отправлена: {s.get('name')} (родителю)")
        else:
            warnings.append(
                f"⚠️ Ссылка для {s.get('name')} не отправлена — родитель не привязан (/add_student).")
    if missing:
        warnings.append(f"⚠️ Пропущено (нет в базе): {', '.join(missing)}")

    # Зачисление — по нему webhook attendance посчитает неявки
    erepo = MeritHubEnrollmentRepository()
    await erepo.add(class_id, tutor["merithub_user_id"], client_user_id=d.get("tutor_cuid"),
                    parent_telegram_id=None, student_name=tutor.get("name"), role="tutor")
    for s in student_rows:
        await erepo.add(class_id, s["merithub_user_id"], client_user_id=s["client_user_id"],
                        parent_telegram_id=s.get("parent_telegram_id"),
                        student_name=s.get("name"), role="student")

    # Напоминания/чеки на БЛИЖАЙШЕЕ занятие серии. Полная рекуррентность
    # напоминаний (каждый occurrence) — после демо (D6); детект неявок и так
    # webhook-driven (classStatus lv приходит на каждое занятие серии).
    await LessonOpsWorkflow().schedule_class_coordination(
        class_id=class_id,
        start_time=start_iso,
        tutor_name=tutor.get("name") or d.get("tutor_cuid"),
        tutor_telegram_id=tutor_tg,
        tutor_timezone=tutor.get("timezone"),
        student_rows=student_rows,
    )
    return {"ok": True, "class_id": class_id, "names": names,
            "notes": notes + warnings, "start_dt": occ_dt}


async def _sched_cb(state: dict, args: list, upd: Update, ctx, ack: dict) -> None:
    q = upd.callback_query
    d = state["data"]
    a = args[0] if args else ""

    if a == "cancel":
        await _ack(q, ack)
        await _sched_cancel(upd, state)
        return
    if a == "back":
        await _ack(q, ack)
        await _sched_back(upd, ctx, state)
        return
    if a == "again":
        await _ack(q, ack)
        await WizardStateRepository().delete(state["chat_id"])
        await _begin_schedule(upd, ctx, state["chat_id"], via_callback=True)
        return
    if a == "topreview":
        await _ack(q, ack)
        d.pop("edit", None)
        await _sched_goto(upd, ctx, state, "preview")
        return
    if a == "tutor":
        await _ack(q, ack)
        row = await MeritHubStudentRepository().get_by_client_id(args[1])
        d["tutor_cuid"], d["tutor_name"] = args[1], (row or {}).get("name") or args[1]
        await _sched_advance(upd, ctx, state, "tutor")
        return
    if a == "tpage":
        await _ack(q, ack)
        d["tutor_page"] = int(args[1])
        await _sched_goto(upd, ctx, state, "tutor")
        return
    if a == "tsearch":
        await _ack(q, ack)
        await _sched_goto(upd, ctx, state, "tutor_search")
        return
    if a == "tclear":
        await _ack(q, ack)
        d.pop("tsearch_q", None)
        d["tutor_page"] = 0
        await _sched_goto(upd, ctx, state, "tutor")
        return
    if a == "student":
        await _ack(q, ack)
        selected = {s["cuid"]: s["name"] for s in d.get("students", [])}
        cuid = args[1]
        if cuid in selected:
            selected.pop(cuid)
        else:
            row = await MeritHubStudentRepository().get_by_client_id(cuid)
            selected[cuid] = (row or {}).get("name") or cuid
        d["students"] = [{"cuid": c, "name": n} for c, n in selected.items()]
        await _sched_goto(upd, ctx, state, "students")
        return
    if a == "spage":
        await _ack(q, ack)
        d["student_page"] = int(args[1])
        await _sched_goto(upd, ctx, state, "students")
        return
    if a == "sdone":
        if not d.get("students"):
            await _ack(q, ack, "Выберите хотя бы одного ученика")
            return
        await _ack(q, ack)
        await _sched_advance(upd, ctx, state, "students")
        return
    if a == "type":
        await _ack(q, ack)
        new = "perma" if args[1] == "perma" else "one"
        if d.get("ctype") != new:
            # тип — one-way door и определяет ветку: сбрасываем зависимые поля
            d.pop("days", None); d.pop("date", None)
            d.pop("hour", None); d.pop("minute", None)
            d.pop("edit", None)
        d["ctype"] = new
        await _sched_goto(upd, ctx, state, "days" if new == "perma" else "date")
        return
    if a == "day":
        await _ack(q, ack)
        code = int(args[1])
        days = set(d.get("days", []))
        days.symmetric_difference_update({code})
        d["days"] = sorted(days)
        await _sched_goto(upd, ctx, state, "days")
        return
    if a == "ddone":
        if not d.get("days"):
            await _ack(q, ack, "Выберите хотя бы один день")
            return
        await _ack(q, ack)
        await _sched_advance(upd, ctx, state, "days")
        return
    if a == "date":
        await _ack(q, ack)
        d["date"] = args[1]
        await _sched_advance(upd, ctx, state, "date")
        return
    if a == "datec":
        await _ack(q, ack)
        await _sched_goto(upd, ctx, state, "date_custom")
        return
    if a == "hour":
        await _ack(q, ack)
        d["hour"] = int(args[1])
        if d.get("edit") == "time":
            await _sched_goto(upd, ctx, state, "minute")
        else:
            await _sched_advance(upd, ctx, state, "hour")
        return
    if a == "min":
        await _ack(q, ack)
        d["minute"] = int(args[1])
        await _sched_advance(upd, ctx, state, "minute")
        return
    if a == "timec":
        await _ack(q, ack)
        await _sched_goto(upd, ctx, state, "time_custom")
        return
    if a == "dur":
        await _ack(q, ack)
        d["duration"] = int(args[1])
        await _sched_advance(upd, ctx, state, "duration")
        return
    if a == "durc":
        await _ack(q, ack)
        await _sched_goto(upd, ctx, state, "duration_custom")
        return
    if a == "editmenu":
        await _ack(q, ack)
        await _sched_goto(upd, ctx, state, "editmenu")
        return
    if a == "edit":
        await _ack(q, ack)
        field = args[1]
        d["edit"] = field
        step = {"tutor": "tutor", "students": "students", "type": "type",
                "days": "days", "date": "date", "time": "hour", "duration": "duration"}[field]
        await _sched_goto(upd, ctx, state, step)
        return
    if a == "retry":
        await _ack(q, ack)
        await _sched_confirm(upd, ctx, state, ack)
        return
    if a == "confirm":
        await _ack(q, ack)
        await _sched_confirm(upd, ctx, state, ack)
        return
    await _ack(q, ack, "Неизвестное действие")


async def _sched_text(state: dict, upd: Update, ctx) -> bool:
    d = state["data"]
    text = (upd.message.text or "").strip()
    step = state["step"]

    if step == "tutor_search":
        d["tsearch_q"] = text
        d["tutor_page"] = 0
        await _delete_user_text(upd)
        await _sched_goto(upd, ctx, state, "tutor")
        return True

    if step == "time_custom":
        m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", text)
        if not m:
            await _delete_user_text(upd)
            await _sched_goto(upd, ctx, state, "time_custom",
                              "⚠️ Формат: ЧЧ:ММ (00:00–23:59), например 16:45.")
            return True
        d["hour"], d["minute"] = int(m.group(1)), int(m.group(2))
        await _delete_user_text(upd)
        await _sched_advance(upd, ctx, state, "time_custom")
        return True

    if step == "date_custom":
        m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?", text)
        err = None
        if not m:
            err = "⚠️ Формат: ДД.ММ, например 25.08."
        else:
            from datetime import date as _date
            today = org_now().date()
            year = int(m.group(3)) if m.group(3) else today.year
            try:
                cand = _date(year, int(m.group(2)), int(m.group(1)))
                # ДД.ММ без года — ближайшая будущая (этот или следующий год)
                if not m.group(3) and cand < today:
                    cand = _date(year + 1, int(m.group(2)), int(m.group(1)))
                if cand < today:
                    err = "⚠️ Дата в прошлом — укажите будущую."
                else:
                    d["date"] = cand.isoformat()
            except ValueError:
                err = "⚠️ Такой даты нет. Формат: ДД.ММ, например 25.08."
        await _delete_user_text(upd)
        if err:
            await _sched_goto(upd, ctx, state, "date_custom", err)
        else:
            await _sched_advance(upd, ctx, state, "date_custom")
        return True

    if step == "duration_custom":
        if not text.isdigit() or not (15 <= int(text) <= 240):
            await _delete_user_text(upd)
            await _sched_goto(upd, ctx, state, "duration_custom",
                              "⚠️ Число минут от 15 до 240, например 45.")
            return True
        d["duration"] = int(text)
        await _delete_user_text(upd)
        await _sched_advance(upd, ctx, state, "duration_custom")
        return True

    return False


# =====================================================================
# Потоки «Новый ученик» и «Новый репетитор» (/add_student, /add_tutor)
# Общий каркас: имя → пояс → привязка (родитель/TG репетитора) → превью → создание.
# =====================================================================

def _person_flow_of(flow: str) -> str:
    return "student" if flow == "add_student" else "tutor"


async def _person_view(flow: str, step: str, d: dict) -> tuple[str, InlineKeyboardMarkup | None]:
    kind = _person_flow_of(flow)
    title = FLOW_TITLES[flow]

    if step == "name":
        label = "ученика" if kind == "student" else "репетитора"
        return (f"{('👨‍🎓' if kind == 'student' else '🧑‍🏫')} {title}\n\n"
                f"Введите имя и фамилию {label}:"), _kb([_cancel_row(flow)])

    if step == "tz":
        rows = [[_btn(label, f"wz:{flow}:tz:{tz}")] for tz, label in TZ_CHOICES]
        rows.append([_btn("Другая зона", f"wz:{flow}:tzc")])
        rows.append(_back_cancel_row(flow))
        return (f"{title}\n\nИмя: {d.get('name')}\n"
                f"Часовой пояс (по умолчанию London):"), _kb(rows)

    if step == "tz_custom":
        return (f"{title}\n\nВведите зону как Europe/Paris:"), _kb([_back_cancel_row(flow)])

    if step == "link":
        role = "parent" if kind == "student" else "tutor"
        label = "родителя" if kind == "student" else "TG-аккаунт репетитора"
        users = await UserRepository().list_by_role(role)
        page_items, page, total = _paged(users, int(d.get("link_page", 0)))
        rows = []
        for u in page_items:
            nm = u.get("name") or u.get("username") or u["telegram_id"]
            rows.append([_btn(nm, f"wz:{flow}:link:{u['telegram_id']}")])
        nav = _pager(flow, page, total, "lpage")
        if nav:
            rows.append(nav)
        rows.append([_btn("⏭ Привязать позже", f"wz:{flow}:linkskip")])
        rows.append(_back_cancel_row(flow))
        if not users:
            rows = [[_btn("⏭ Привязать позже", f"wz:{flow}:linkskip")], _back_cancel_row(flow)]
        return (f"{title}\n\nИмя: {d.get('name')} · {d.get('tz_label')}\n"
                f"Выберите {label} из зарегистрированных:"), _kb(rows)

    if step == "preview":
        who = d.get("link_name") or "не привязан"
        who_label = "Родитель" if kind == "student" else "TG репетитора"
        # Зона — полная IANA (Asia/Dubai), чтобы не перепутать похожие города.
        text = (f"📋 Проверьте перед созданием\n\n"
                f"🧾 {d.get('name')}\n"
                f"🌍 {d.get('tz') or d.get('tz_label')}\n"
                f"{who_label}: {who}")
        return text, _kb([
            [_btn("✅ Создать", f"wz:{flow}:confirm")],
            _back_cancel_row(flow),
        ])

    return "Неизвестный шаг сценария.", _kb([_cancel_row(flow)])


async def _person_goto(upd: Update, ctx, state: dict, step: str, extra_text: str | None = None) -> None:
    state["step"] = step
    await _save(state)
    text, markup = await _person_view(state["flow"], step, state["data"])
    if extra_text:
        text = f"{extra_text}\n\n{text}"
    await _show(upd, ctx, state, text, markup)


_PERSON_NEXT = {"name": "tz", "tz": "link", "tz_custom": "link", "link": "preview"}
_PERSON_BACK = {"tz": "name", "tz_custom": "tz", "link": "tz", "preview": "link"}


async def _person_create(flow: str, d: dict) -> dict:
    kind = _person_flow_of(flow)
    cuid = await _next_cuid("s" if kind == "student" else "t")
    mh_id = f"mh_{cuid}"
    if settings.merithub_use_real:
        try:
            client = get_merithub_service()
            resp = await client.add_user(
                client_user_id=cuid, name=d["name"],
                role="M" if kind == "student" else "C",
                email=f"{cuid}@albion.local")
            mid = client._extract_id(resp, "userId", "id", "UserId", "userID")
            if mid:
                mh_id = mid
        except Exception as e:
            return {"ok": False, "reason": str(e)[:180]}
    srepo = MeritHubStudentRepository()
    if kind == "student":
        await srepo.upsert(cuid, merithub_user_id=mh_id, name=d["name"],
                           parent_telegram_id=d.get("link_tg"), timezone=d.get("tz"),
                           role="student")
        if d.get("link_tg"):
            await UserRepository().set_role_by_telegram(
                d["link_tg"], "parent", name=f"Родитель: {d['name']}")
    else:
        await srepo.upsert(cuid, merithub_user_id=mh_id, name=d["name"],
                           timezone=d.get("tz"), role="tutor")
        if d.get("link_tg"):
            await MeritHubContactRepository().upsert(
                cuid, d["link_tg"], "tutor", name=d["name"])
    return {"ok": True, "cuid": cuid}


async def _person_cb(state: dict, args: list, upd: Update, ctx, ack: dict) -> None:
    q = upd.callback_query
    flow, d = state["flow"], state["data"]
    a = args[0] if args else ""

    if a == "cancel":
        await _ack(q, ack)
        await WizardStateRepository().delete(state["chat_id"])
        await _show(upd, ctx, state, f"{FLOW_TITLES[flow]} — отменено.", None)
        return
    if a == "back":
        await _ack(q, ack)
        await _person_goto(upd, ctx, state, _PERSON_BACK.get(state["step"], "name"))
        return
    if a == "lpage":
        await _ack(q, ack)
        d["link_page"] = int(args[1])
        await _person_goto(upd, ctx, state, "link")
        return
    if a == "tz":
        await _ack(q, ack)
        tz = args[1]
        label = dict(TZ_CHOICES).get(tz, tz)
        d["tz"], d["tz_label"] = tz, label
        await _person_goto(upd, ctx, state, "link")
        return
    if a == "tzc":
        await _ack(q, ack)
        await _person_goto(upd, ctx, state, "tz_custom")
        return
    if a == "link":
        await _ack(q, ack)
        u = await UserRepository().get_by_telegram_id(args[1])
        d["link_tg"] = args[1]
        d["link_name"] = (u or {}).get("name") or (u or {}).get("username") or args[1]
        await _person_goto(upd, ctx, state, "preview")
        return
    if a == "linkskip":
        await _ack(q, ack)
        d.pop("link_tg", None); d.pop("link_name", None)
        await _person_goto(upd, ctx, state, "preview")
        return
    if a == "confirm":
        await _ack(q, ack)
        if d.get("submitting"):
            return
        d["submitting"] = True
        await _save(state)
        await _show(upd, ctx, state, "⏳ Создаю…", None)
        res = await _person_create(flow, d)
        d["submitting"] = False
        title = FLOW_TITLES[flow]
        if not res["ok"]:
            await _save(state)
            await _show(upd, ctx, state,
                        f"❌ Не удалось создать ({res['reason']}). Данные сохранены.",
                        _kb([[_btn("🔁 Повторить", f"wz:{flow}:confirm")],
                             _cancel_row(flow)]))
            return
        await WizardStateRepository().delete(state["chat_id"])
        who = d.get("link_name") or "не привязан"
        who_label = "Родитель" if flow == "add_student" else "TG репетитора"
        text = (f"✅ {title.replace('Новый', 'Создан').replace('Новая', 'Создана')}: "
                f"{d.get('name')} ({res['cuid']}) · {d.get('tz_label')}\n"
                f"{who_label}: {who}")
        await _show(upd, ctx, state, text, _kb([[_btn("📅 К занятию", "wz:person:toschedule")]]))
        return
    await _ack(q, ack, "Неизвестное действие")


async def _person_text(state: dict, upd: Update, ctx) -> bool:
    flow, d = state["flow"], state["data"]
    text = (upd.message.text or "").strip()
    step = state["step"]

    if step == "name":
        if len(text) < 2:
            await _delete_user_text(upd)
            await _person_goto(upd, ctx, state, "name", "⚠️ Введите имя (минимум 2 символа).")
            return True
        d["name"] = text
        await _delete_user_text(upd)
        await _person_goto(upd, ctx, state, "tz")
        return True

    if step == "tz_custom":
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(text)
            ok = True
        except Exception:
            ok = False
        await _delete_user_text(upd)
        if not ok:
            await _person_goto(upd, ctx, state, "tz_custom",
                               "⚠️ Неизвестная зона. Пример: Europe/Paris, Asia/Dubai.")
            return True
        d["tz"], d["tz_label"] = text, text
        await _person_goto(upd, ctx, state, "link")
        return True

    return False


# =====================================================================
# Точки входа: команды, роутинг callback'ов и текста, sweeper
# =====================================================================

async def _begin_schedule(upd: Update, ctx, chat_id: str, via_callback: bool = False) -> None:
    srepo = MeritHubStudentRepository()
    tutors = await srepo.list_by_role("tutor")
    students = await srepo.list_by_role("student")
    miss = []
    if not tutors:
        miss.append("• нет ни одного репетитора — /add_tutor")
    if not students:
        miss.append("• нет ни одного ученика — /add_student")
    if miss:
        text = "📅 Новое занятие\n\nПока не с кем проводить:\n" + "\n".join(miss)
        if upd.callback_query is not None:
            await upd.callback_query.edit_message_text(text)
        else:
            await upd.message.reply_text(text)
        return
    state = {"chat_id": str(chat_id), "flow": "schedule", "step": "tutor", "data": {}}
    text, markup = await _sched_view("tutor", state["data"])
    if via_callback and upd.callback_query is not None:
        await _show(upd, ctx, state, text, markup)
    else:
        m = await upd.message.reply_text(text, reply_markup=markup)
        mid = getattr(m, "message_id", None)
        if mid:
            state["data"]["msg_id"] = mid
    await _save(state)


async def _begin_person(upd: Update, ctx, chat_id: str, flow: str) -> None:
    state = {"chat_id": str(chat_id), "flow": flow, "step": "name", "data": {}}
    text, markup = await _person_view(flow, "name", state["data"])
    m = await upd.message.reply_text(text, reply_markup=markup)
    mid = getattr(m, "message_id", None)
    if mid:
        state["data"]["msg_id"] = mid
    await _save(state)


async def _start_flow(upd: Update, flow: str) -> None:
    if not await is_coordinator_or_admin(upd.effective_user.id):
        await upd.message.reply_text("⛔ Только координатор/админ.")
        return
    if getattr(upd.effective_chat, "type", "private") != "private":
        await upd.message.reply_text("Этот сценарий работает в личных сообщениях с ботом.")
        return
    chat_id = str(upd.effective_chat.id)
    # Повторный запуск при живом сценарии — детерминированно начинает заново.
    await WizardStateRepository().delete(chat_id)
    if flow == "schedule":
        await _begin_schedule(upd, None, chat_id)
    else:
        await _begin_person(upd, None, chat_id, flow)


async def cmd_schedule(upd: Update, _ctx) -> None:
    """/schedule — кнопочное создание занятия или регулярной серии (без ID)."""
    await _start_flow(upd, "schedule")


async def cmd_add_student(upd: Update, _ctx) -> None:
    """/add_student — кнопочное создание ученика: имя → пояс → родитель (без ID)."""
    await _start_flow(upd, "add_student")


async def cmd_add_tutor(upd: Update, _ctx) -> None:
    """/add_tutor — кнопочное создание репетитора: имя → пояс → TG (без ID)."""
    await _start_flow(upd, "add_tutor")


async def handle_wz_callback(upd: Update, ctx) -> None:
    """Роутер всех callback'ов с префиксом wz: (вызывается из handle_callback)."""
    q = upd.callback_query
    ack = {"done": False}
    chat_id = str(upd.effective_chat.id)
    parts = (q.data or "").split(":")
    # wz:noop — статичные кнопки-счётчики (например '2/3' в пагинации)
    if parts[1:2] == ["noop"]:
        await _ack(q, ack)
        return
    # wz:person:toschedule — кросс-потоковая кнопка «📅 К занятию» с финальной
    # карточки: состояние там уже удалено, поэтому обрабатываем БЕЗ состояния.
    if parts[1:3] == ["person", "toschedule"]:
        if not await is_coordinator_or_admin(upd.effective_user.id):
            await _ack(q, ack, "⛔ Только координатор/админ.", alert=True)
            return
        await _ack(q, ack)
        await WizardStateRepository().delete(chat_id)
        await _begin_schedule(upd, ctx, chat_id, via_callback=True)
        return
    # wz:sched:again — «➕ Ещё занятие» с финальной карточки создания (R9-2):
    # состояние удалено при успешном создании, поэтому ветка в _sched_cb была
    # недостижима — кнопка молча умирала. Обрабатываем без состояния.
    if parts[1:3] == ["sched", "again"]:
        if not await is_coordinator_or_admin(upd.effective_user.id):
            await _ack(q, ack, "⛔ Только координатор/админ.", alert=True)
            return
        await _ack(q, ack)
        await WizardStateRepository().delete(chat_id)
        await _begin_schedule(upd, ctx, chat_id, via_callback=True)
        return
    state, expired = await _load(chat_id)
    if state is None:
        if expired:
            await _ack(q, ack)
            try:
                await q.edit_message_text(_expired_text(expired.get("flow", "")))
            except Exception:
                pass
        else:
            await _ack(q, ack, "Этот сценарий уже закрыт. Начните заново.")
        return
    if not await is_coordinator_or_admin(upd.effective_user.id):
        await _ack(q, ack, "⛔ Только координатор/админ.", alert=True)
        return
    # Значение "submitting" блокирует повторные действия на время вызова API
    if state["data"].get("submitting") and parts[2:3] != [] and parts[2] != "retry":
        await _ack(q, ack, "Уже создаю…")
        return
    flow = state["flow"]
    args = parts[2:]
    if flow == "schedule":
        await _sched_cb(state, args, upd, ctx, ack)
    elif flow in ("add_student", "add_tutor"):
        await _person_cb(state, args, upd, ctx, ack)
    else:
        await _ack(q, ack, "Неизвестный сценарий")
    await _ack(q, ack)


async def try_handle_wz_text(upd: Update, ctx) -> bool:
    """Перехват free-text ввода шагов визарда. True = сообщение обработано."""
    if not upd.message or not upd.message.text or upd.message.text.startswith("/"):
        return False
    chat_id = str(upd.effective_chat.id)
    state, expired = await _load(chat_id)
    if expired and not state:
        await upd.message.reply_text(_expired_text(expired.get("flow", "")))
        return True
    if not state or state.get("step") not in _TEXT_STEPS:
        return False
    if not await is_coordinator_or_admin(upd.effective_user.id):
        return False
    if state["flow"] == "schedule":
        return await _sched_text(state, upd, ctx)
    return await _person_text(state, upd, ctx)


async def sweep_expired_wizards(bot) -> int:
    """Один проход sweeper'а: удаляет просроченные состояния и уведомляет чаты.

    Вызывается из wizard_expiry_loop (демон в main) и напрямую из тестов.
    """
    repo = WizardStateRepository()
    rows = await repo.list_expired(_now_iso())
    swept = 0
    for r in rows:
        await repo.delete(r["chat_id"])
        swept += 1
        try:
            await bot.send_message(chat_id=int(r["chat_id"]), text=_expired_text(r.get("flow", "")))
        except Exception as e:
            logger.warning("Wizard sweep notify failed chat=%s: %s", r["chat_id"], e)
    return swept


async def wizard_expiry_loop(bot, interval_sec: int = 60) -> None:
    """Демон: раз в минуту сметает просроченные визарды (и после рестарта бота)."""
    while True:
        await asyncio.sleep(interval_sec)
        try:
            swept = await sweep_expired_wizards(bot)
            if swept:
                logger.info("Wizard sweeper: expired=%d", swept)
        except Exception as e:
            logger.error("Wizard sweeper crashed: %s", e)
