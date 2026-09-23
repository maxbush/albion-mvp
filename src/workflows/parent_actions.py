"""Родительские callback-действия по инциденту неявки — канал-нейтрально.

Ядро кнопок «✅ Всё хорошо / ❌ Не придём / ⏰ Опоздаем» (+ уточнение
интервала) извлечено из bot/handlers.py: та же логика обслуживает входящие
кнопки WhatsApp (quick-reply с тем же callback_id). Возвращает dict со
статусом; отображение (toast/editMessageText vs новое сообщение) — забота
канала.

Статусы:
  resolved           — инцидент закрыт, text — финальный ответ родителю
  late_ask           — нужен выбор интервала: text + buttons (3 quick-reply)
  already_processed  — повторное нажатие (idempotency)
  already_closed     — инцидент resolved; clear_markup=True (TG снимает кнопки)
  not_found          — инцидента нет
  no_workflow        — workflow не найден / уже закрыт
  bad_nonce          — кнопка устарела (несовпадение nonce)
  not_parent         — кнопка чужая (actor != parent_telegram_id)
  bad_data           — не удалось распарсить callback
  toast              — короткое всплывающее подтверждение (TG answer; WA — текст)
"""

import json
import logging

from src.channels.base import ChannelButton
from src.db.repository import (
    IdempotencyRepository,
    IncidentRepository,
    MeritHubContactRepository,
    WorkflowRepository,
)

logger = logging.getLogger(__name__)

_LATE_MINUTES = ("5", "15", "30+")


def _digits(v: str) -> str:
    return "".join(c for c in str(v) if c.isdigit())


async def _same_actor(expected: str, actor: str, db_path: str | None) -> bool:
    """expected — parent_telegram_id из workflow (TG id, 'wa:+…' или телефон);
    actor — canonical ref ('wa:+…' либо TG id). Кросс-канальное сравнение
    идёт через merithub_contacts (phone ↔ telegram_id)."""
    exp = expected[3:] if expected.startswith("wa:") else expected
    act = actor[3:] if actor.startswith("wa:") else actor
    if str(expected) == str(actor) or str(exp) == str(act):
        return True
    contacts = MeritHubContactRepository(db_path)
    # одна сторона — телефон, другая — TG: ищем контакт по телефону
    for phone, tg in ((act, exp), (exp, act)):
        row = await contacts.get_by_phone(phone)
        if row and str(row.get("telegram_id") or "") == str(tg):
            return True
    return False


async def process_parent_callback(data: str, actor_id: str, db_path: str | None = None) -> dict:
    """Обработать resolve:* / resolve_late_time:* независимо от канала."""
    is_late_pick = data.startswith("resolve_late_time:")
    parts = data.split(":")
    try:
        inc_id = int(parts[1])
        nonce = parts[2]
        detail = parts[3] if len(parts) > 3 else ("ok" if not is_late_pick else None)
        if detail is None:
            raise ValueError
    except (IndexError, ValueError):
        text = ("Не смог прочитать нажатие." if is_late_pick
                else "Не смог прочитать нажатие — попробуйте ещё раз или напишите текстом.")
        return {"status": "bad_data", "text": text}

    idem = IdempotencyRepository(db_path)
    idem_key = f"tg_callback:{data}"  # ключ opaque: общий для всех каналов
    if await idem.exists(idem_key):
        return {"status": "already_processed", "toast": "✅ Уже обработано"}

    inc = await IncidentRepository(db_path).get(inc_id)
    if not inc:
        return {"status": "not_found", "text": "Ситуация не найдена."}
    if inc["status"] == "resolved":
        return {"status": "already_closed", "toast": "ℹ️ Эта ситуация уже закрыта",
                "clear_markup": True}
    was_escalated = inc["status"] == "escalated"

    wf_repo = WorkflowRepository(db_path)
    wf_rows = await wf_repo.find_by_json("incident_id", inc_id, limit=1)
    wf_row = wf_rows[0] if wf_rows else None
    if not wf_row:
        return {"status": "no_workflow", "text": "Ситуация уже закрыта или workflow не найден."}
    try:
        wf_data = json.loads(wf_row.get("data") or "{}")
    except Exception:
        wf_data = {}
    expected_nonce = wf_data.get("parent_callback_nonce")
    if expected_nonce and expected_nonce != nonce:
        return {"status": "bad_nonce", "toast": "⛔ Кнопка устарела", "alert": True}
    expected_parent = wf_data.get("parent_telegram_id")
    if expected_parent and not await _same_actor(
            str(expected_parent), str(actor_id), db_path):
        return {"status": "not_parent", "toast": "⛔ Это сообщение не для вас", "alert": True}

    # detail — из callback'а, whitelist: иначе любая строка после nonce
    # резолвила бы инцидент с фиктивным resolution
    allowed = _LATE_MINUTES if is_late_pick else ("ok", "no", "late")
    if detail not in allowed:
        return {"status": "bad_data", "text": "Не смог прочитать нажатие."}

    from src.utils.i18n import lang_of, tr
    lang = await lang_of(str(actor_id))
    from src.workflows.absence import AbsenceWorkflow
    wf = AbsenceWorkflow(db_path)

    if is_late_pick:
        await wf.resolve_absence(inc_id, str(actor_id), resolution="parent_late")
        await wf.notify_coordinators_parent_reply(
            inc_id, "late", late_minutes=detail,
            parent_telegram_id=str(actor_id),
        )
        await idem.save(idem_key, "callback", response="resolved_late")
        for other in ("ok", "no", "late"):
            await idem.save(f"tg_callback:resolve:{inc_id}:{nonce}:{other}",
                            "callback_blocked", response="blocked_by_resolve_late")
        for other_mins in _LATE_MINUTES:
            if other_mins != detail:
                await idem.save(f"tg_callback:resolve_late_time:{inc_id}:{nonce}:{other_mins}",
                                "callback_blocked", response="blocked_by_resolve_late")
        ack = tr("ack_late_detail_parent", lang, mins=f"{detail} мин")
    elif detail == "late":
        # Сначала уточняем интервал — инцидент НЕ резолвим (эскалация по
        # таймеру остаётся страховкой). Кнопки ≤3 → рендерятся во всех каналах.
        return {
            "status": "late_ask",
            "text": tr("ack_late_ask_mins_parent", lang),
            "buttons": [
                ChannelButton(label="на 5 мин", callback_id=f"resolve_late_time:{inc_id}:{nonce}:5"),
                ChannelButton(label="на 15 мин", callback_id=f"resolve_late_time:{inc_id}:{nonce}:15"),
                ChannelButton(label="на 30+ мин", callback_id=f"resolve_late_time:{inc_id}:{nonce}:30+"),
            ],
        }
    else:
        action_map = {
            "ok": ("parent_ok", "✅ Всё в порядке! Спасибо."),
            "no": ("parent_not_coming", "❌ Спасибо! Отметили, что сегодня занятия не будет."),
        }
        resolution, ack = action_map.get(detail, ("parent_confirmed", "✅ Ответ получен."))
        outcome = {"ok": "ok", "no": "no_show"}.get(detail, "free_text")
        await wf.resolve_absence(inc_id, str(actor_id), resolution=resolution)
        await wf.notify_coordinators_parent_reply(
            inc_id, outcome, parent_telegram_id=str(actor_id),
        )
        await idem.save(idem_key, "callback", response="resolved")
        for other in ("ok", "no", "late"):
            if other != detail:
                await idem.save(f"tg_callback:resolve:{inc_id}:{nonce}:{other}",
                                "callback_blocked", response="blocked_by_resolve")

    if was_escalated:
        ack += ("\n\nℹ️ Координатор уже был уведомлён об отсутствии ответа. "
                "Ваш ответ передан — инцидент закрыт.")
    student_label = wf_data.get("student_name") or "Ученик"
    ack += "\n\nОшиблись? Напишите текстом — координатор поможет."
    logger.info("Incident %d resolved via callback data=%s actor=%s", inc_id, data, actor_id)
    return {"status": "resolved", "text": f"{ack}\nОтметили: {student_label} · вопрос закрыт."}


async def process_checkin_callback(cb_data: str, actor_id: str,
                                   db_path: str | None = None) -> tuple[str, list[dict] | None]:
    """Канало-независимая обработка checkin-колбэков (напоминание перед уроком).

    Зеркалит TG-обработчик в handlers.py: parse → идемпотентность → состояние
    → nonce → адресат → запись ответа. Возвращает (reply, buttons)."""
    import json
    from datetime import datetime, timedelta, timezone
    from src.db.repository import (
        IdempotencyRepository, ScheduledActionRepository, WorkflowRepository,
    )
    from src.utils.i18n import lang_of, tr

    def _parse(data: str, need: int) -> tuple[int, str, str] | None:
        parts = data.split(":")
        if len(parts) < need:
            return None
        try:
            return int(parts[1]), parts[2], parts[3]
        except (IndexError, ValueError):
            return None

    lang = await lang_of(actor_id)

    if cb_data.startswith("checkin_late_time:"):
        parsed = _parse(cb_data, 4)
        if not parsed:
            return "Не смог прочитать нажатие.", None
        wid, _nonce, mins_str = parsed
        idem_key = f"{cb_data}"
        if await IdempotencyRepository(db_path).exists(idem_key):
            return "✅ Уже обработано", None
        from src.workflows.lesson_ops import LessonOpsWorkflow
        wf_row = await WorkflowRepository(db_path).get(wid)
        try:
            wf_data = json.loads(wf_row.get("data") or "{}") if wf_row else {}
        except Exception:
            wf_data = {}
        actor_type = wf_data.get("actor_type")
        expected = wf_data.get("actor_telegram_id")
        if expected and not await _same_actor(str(expected), str(actor_id), db_path):
            return "⛔ Это сообщение не для вас", None
        if not wf_row or wf_row.get("state") != "running":
            return "Этот сценарий уже завершён.", None
        ops = LessonOpsWorkflow(db_path)
        await ScheduledActionRepository(db_path).cancel_by_workflow(wid)
        await ops.notify_late_detail(wid, mins_str)
        await WorkflowRepository(db_path).update_state(
            wid, "completed",
            {**wf_data, "response_status": "late", "late_minutes": mins_str})
        await IdempotencyRepository(db_path).save(
            idem_key, "checkin_late_time", response=mins_str)
        ack_key = "ack_late_detail_parent" if actor_type == "parent" else "ack_late_detail"
        return tr(ack_key, lang, mins=f"{mins_str} мин"), None

    parsed = _parse(cb_data, 4)
    if not parsed:
        return "Не смог прочитать нажатие — попробуйте ещё раз или напишите текстом.", None
    wid, nonce, action = parsed
    idem = IdempotencyRepository(db_path)
    idem_key = f"{cb_data}"
    if await idem.exists(idem_key):
        return "✅ Уже обработано", None
    repo = WorkflowRepository(db_path)
    wf_row = await repo.get(wid)
    if not wf_row or wf_row.get("state") != "running":
        return "Этот сценарий уже завершён.", None
    try:
        wf_data = json.loads(wf_row.get("data") or "{}")
    except Exception:
        wf_data = {}
    expected_nonce = wf_data.get("nonce")
    if expected_nonce and expected_nonce != nonce:
        return "⛔ Кнопка устарела", None
    expected = wf_data.get("actor_telegram_id")
    if expected and not await _same_actor(str(expected), str(actor_id), db_path):
        return "⛔ Это сообщение не для вас", None

    from src.workflows.lesson_ops import LessonOpsWorkflow
    ops = LessonOpsWorkflow(db_path)
    actor_type = wf_data.get("actor_type")
    if action == "late":
        # Как в TG: ждём выбор минут; fallback-алерт — страховка, что факт
        # опоздания не потеряется, если пользователь дальше не нажмёт.
        wf_data["response_status"] = "late"
        wf_data["responded_at"] = datetime.now(timezone.utc).isoformat()
        await repo.update_data(wid, wf_data)
        sched = ScheduledActionRepository(db_path)
        await sched.cancel_by_workflow(wid)
        await sched.create(
            wid,
            (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
            "checkin_late_fallback", {"workflow_id": wid})
        await idem.save(idem_key, "checkin", response=action)
        msg_key = "ack_late_ask_mins_parent" if actor_type == "parent" else "ack_late_ask_mins"
        buttons = [
            {"text": tr("tutor_btn_late_5", lang), "callback_data": f"checkin_late_time:{wid}:{nonce}:5"},
            {"text": tr("tutor_btn_late_15", lang), "callback_data": f"checkin_late_time:{wid}:{nonce}:15"},
            {"text": tr("tutor_btn_late_30", lang), "callback_data": f"checkin_late_time:{wid}:{nonce}:30+"},
        ]
        return tr(msg_key, lang), buttons

    await ops.record_checkin_response(wid, actor_tg=str(actor_id), action=action)
    await idem.save(idem_key, "checkin", response=action)
    ack = tr(f"ack_{action}", lang)
    return (ack if ack != f"ack_{action}" else "✅ Ответ принят."), None


def is_parent_callback(data: str) -> bool:
    return (data.startswith("resolve:")
            or data.startswith("resolve_late_time:")
            or data.startswith("checkin:")
            or data.startswith("checkin_late_time:"))
