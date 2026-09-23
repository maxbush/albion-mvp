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
    WorkflowRepository,
)

logger = logging.getLogger(__name__)

_LATE_MINUTES = ("5", "15", "30+")


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
    if expected_parent and str(expected_parent) != str(actor_id):
        return {"status": "not_parent", "toast": "⛔ Это сообщение не для вас", "alert": True}

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


def is_parent_callback(data: str) -> bool:
    return data.startswith("resolve:") or data.startswith("resolve_late_time:")
