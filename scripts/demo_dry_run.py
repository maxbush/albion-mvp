#!/usr/bin/env python3
"""Сухой прогон (dry run) демо-сценария DEMO_RUNBOOK.md — без Telegram и сети.

Гонит РЕАЛЬНЫЕ workflow бота (неявка → уведомление → кнопки/free text →
эскалация, отмена урока, статистика инцидентов) на временной SQLite-базе
и фиктивных telegram-объектах, фиксируя каждое сообщение: кто получил,
какой текст и какие кнопки увидел. Результат — пошаговая расшифровка,
которую удобно сверять перед живым демо клиенту и использовать как
основу для скриншотов/презентации.

Запуск:
    python scripts/demo_dry_run.py                 # печать в stdout
    python scripts/demo_dry_run.py --out DEMO_TRANSCRIPT.md

Сцены (по DEMO_RUNBOOK.md):
  2a. Родитель жмёт «✅ Всё в порядке»  — ситуация закрыта мгновенно
  2b. Родитель жмёт «⏰ Опоздаем»        — координатор в курсе
  2c. Родитель пишет свободный текст     — распознавание смысла
  2d. Родитель молчит → эскалация        — management by exception
  2e. /cancel_lesson — уведомления репетитору и координатору
  +  /incidents — статистика для клиента
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)


# ── Актёры демо ──────────────────────────────────────────────────────
ADMIN_TG, ADMIN_NAME = "1001", "Макс (владелец)"
COORD_TG, COORD_NAME = "2001", "Ольга (координатор)"
PARENT_TG, PARENT_NAME = "3001", "Елена (родитель)"
TUTOR_TG, TUTOR_NAME = "tutor_1", "Иван (репетитор)"  # tg как у tutor_1 в mock-данных

ROLE_BADGE = {
    ADMIN_TG: "👑 Владелец",
    COORD_TG: "👨‍💼 Координатор",
    PARENT_TG: "👨‍👩‍👦 Родитель",
    TUTOR_TG: "🧑‍🏫 Репетитор",
}


class Transcript:
    """Накапливает и рендерит расшифровку прогона."""

    def __init__(self) -> None:
        self.blocks: list[str] = []

    @staticmethod
    def who(tg: str) -> str:
        return ROLE_BADGE.get(str(tg), f"TG {tg}")

    def scene(self, title: str, comment: str = "") -> None:
        block = f"## {title}"
        if comment:
            block += f"\n\n> {comment}"
        self.blocks.append(block)

    def note(self, text: str) -> None:
        self.blocks.append(f"*({text})*")

    def command(self, tg: str, text: str) -> None:
        self.blocks.append(f"**{self.who(tg)} → бот:** `{text}`")

    def press(self, tg: str, button: str) -> None:
        self.blocks.append(f"**{self.who(tg)}** нажимает кнопку «{button}»")

    def free_text(self, tg: str, text: str) -> None:
        self.blocks.append(f"**{self.who(tg)}** пишет текстом: «{text}»")

    def message(self, to_tg: str, text: str, buttons: list[dict] | None = None,
                *, edited: bool = False) -> None:
        header = f"**🤖 Бот → {self.who(to_tg)}"
        header += " (сообщение изменено):**" if edited else ":**"
        body = "\n".join(f"> {line}" for line in text.splitlines())
        block = f"{header}\n{body}"
        if buttons:
            parts = []
            for b in buttons:
                parts.append(f"[{b['text']}]" if b.get("callback_data") else f"[{b['text']} ↗]")
            block += "\n> кнопки: " + " ".join(parts)
        self.blocks.append(block)

    def render(self) -> str:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        head = (
            "# 📖 ALBION — расшифровка демо-прогона\n\n"
            f"> Сгенерировано автоматически: `python scripts/demo_dry_run.py` ({now}).\n"
            "> Прогон идёт по **реальным workflow бота** на временной SQLite-базе, "
            "без Telegram и сети.\n"
            "> Каждое сообщение ниже — то, что увидит участник в живом демо.\n\n"
            "## 🎭 Участники\n\n"
            "| Роль | TG ID | Кто играет |\n|---|---|---|\n"
            f"| 👑 Владелец (админ) | {ADMIN_TG} | {ADMIN_NAME} |\n"
            f"| 👨‍💼 Координатор | {COORD_TG} | {COORD_NAME} |\n"
            f"| 👨‍👩‍👦 Родитель | {PARENT_TG} | {PARENT_NAME} |\n"
            f"| 🧑‍🏫 Репетитор | {TUTOR_TG} | {TUTOR_NAME} |\n"
        )
        out = head
        for block in self.blocks:
            # Горизонтальный разделитель — только между сценами.
            out += "\n\n---\n\n" + block if block.startswith("## ") else "\n\n" + block
        return out + "\n"


# ── Фейки telegram (минимально достаточные для handlers) ─────────────

class FakeUser:
    def __init__(self, tg: str, full_name: str):
        self.id = int(tg) if tg.isdigit() else tg
        self.username = None
        self.full_name = full_name


class FakeChat:
    def __init__(self, chat_id: int):
        self.id = chat_id


class FakeMessage:
    def __init__(self, text: str = ""):
        self.text = text
        self.replies: list[tuple[str, dict]] = []

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw))


class FakeQuery:
    def __init__(self, data: str, user: FakeUser, message_text: str = ""):
        self.data = data
        self.from_user = user
        self.message = FakeMessage(message_text)
        self.answers: list[tuple] = []
        self.edits: list[str] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kw):
        self.edits.append(text)

    async def edit_message_reply_markup(self, markup):
        pass


class FakeUpdate:
    def __init__(self, tg: str, name: str, *, text: str = "", query: FakeQuery | None = None):
        self.effective_user = FakeUser(tg, name)
        self.effective_chat = FakeChat(999)
        self.message = FakeMessage(text)
        self.callback_query = query

    def reply_of(self, tg: str) -> "FakeUpdate":
        return self


class FakeCtx:
    def __init__(self, args: list[str] | None = None):
        self.args = args or []


# ── Планировщик: детерминированный дубликат scheduler_loop ───────────

async def force_due() -> None:
    """Перематывает время: все pending-задачи становятся просроченными."""
    from src.db.repository import ScheduledActionRepository
    await ScheduledActionRepository()._execute(
        "UPDATE scheduled_actions SET execute_at=? WHERE status='pending'",
        ("2000-01-01T00:00:00+00:00",))


async def run_due() -> int:
    """Один тик планировщика: просроченные задачи → SCHEDULER_TICK на шину."""
    from src.db.repository import ScheduledActionRepository
    from src.events.bus import bus
    from src.events.types import Event, EventTypes

    repo = ScheduledActionRepository()
    tasks = await repo.claim_pending(limit=20)
    fired = 0
    for task in tasks:
        aid = task["id"]
        payload = json.loads(task["payload"])
        report = await bus.publish(Event(EventTypes.SCHEDULER_TICK, {
            "action_id": aid, "action": task["action"],
            "workflow_id": task["workflow_id"], "data": payload,
            "execute_at": task["execute_at"],
        }))
        if report.total_handlers == 0:
            await repo.requeue(aid, "no handlers")
            raise RuntimeError(f"Нет обработчика для действия {task['action']}")
        if report.failed:
            await repo.requeue(aid, "handler failed")
            raise RuntimeError(f"Действие {task['action']} упало: {report.errors[0]['error']}")
        await repo.mark_done(aid)
        fired += 1
    return fired


async def fast_forward() -> None:
    """Эмулирует течение времени: перемотка + тик планировщика."""
    await force_due()
    await run_due()


# ── Основной сценарий ────────────────────────────────────────────────

async def run_demo(workdir: str | None = None) -> str:
    """Гонит все сцены демо и возвращает markdown-расшифровку."""
    tmp = None
    if workdir is None:
        tmp = tempfile.mkdtemp(prefix="albion_dry_run_")
        workdir = tmp
    os.chdir(workdir)

    from src.config import settings
    old_admins = settings.albion_admin_telegram_ids
    settings.albion_admin_telegram_ids = ADMIN_TG

    from src.db.migrations import init_db
    db_abs = os.path.join(workdir, "albion.db")
    await init_db(db_abs)

    # engine — глобальный синглтон: тестовые фикстуры (conftest) могли уже
    # перенаправить его в свою БД. Явно нацеливаем на базу прогона,
    # иначе workflow уйдёт в чужую базу, а остальное — в локальную.
    from src.db.repository import ScheduledActionRepository as _SchedRepo
    from src.db.repository import WorkflowRepository as _WfRepo
    from src.workflows.engine import engine
    old_engine_repos = (engine.repo, engine.scheduler)
    engine.repo = _WfRepo(db_abs)
    engine.scheduler = _SchedRepo(db_abs)

    from src.bot import handlers as H
    from src.bot import pilot as P
    from src.db.repository import IncidentRepository, UserRepository, WorkflowRepository
    from src.events.bus import bus
    from src.events.types import EventTypes
    from src.workflows.absence import AbsenceWorkflow
    from src.workflows.cancellation import CancellationWorkflow

    users = UserRepository()
    # Админ НЕ получает user-запись: is_admin() смотрит в ALBION_ADMIN_TELEGRAM_IDS,
    # а не в БД — так он не попадает и в рассылку координаторов (как в живом демо,
    # где роли играют разные люди).
    await users.create(COORD_TG, "coordinator", COORD_NAME)
    await users.create(PARENT_TG, "parent", PARENT_NAME)
    await users.create(TUTOR_TG, "tutor", TUTOR_NAME)

    rec = Transcript()

    # Подписки бота (то же, что делает main.py при старте).
    absence = AbsenceWorkflow()
    cancel = CancellationWorkflow()
    subscribed: list[tuple[str, object]] = []

    async def capture(ev):
        rec.message(ev.data.get("telegram_id"), ev.data.get("message", ""),
                    ev.data.get("buttons") or None)

    for etype, fn in [
        (EventTypes.SCHEDULER_TICK, absence.handle_scheduler_tick),
        (EventTypes.MESSAGE_CLASSIFIED, absence.handle_classified),
        (EventTypes.LESSON_ABSENT, absence.handle_lesson_absent),
        (EventTypes.LESSON_CANCELLED, cancel.handle_cancelled),
        (EventTypes.NOTIFICATION_REQUESTED, capture),
    ]:
        bus.subscribe(etype, fn)
        subscribed.append((etype, fn))

    async def latest_state() -> tuple[int, int, str]:
        """(incident_id, workflow_id, nonce) последнего запущенного сценария."""
        inc = await IncidentRepository()._fetchone(
            "SELECT id FROM incidents ORDER BY id DESC LIMIT 1")
        wf = await WorkflowRepository()._fetchone(
            "SELECT * FROM workflow_instances ORDER BY id DESC LIMIT 1")
        nonce = json.loads(wf["data"]).get("parent_callback_nonce", "") if wf else ""
        return int(inc["id"]), int(wf["id"]), nonce

    async def pilot_round() -> list[tuple[str, dict]]:
        """/pilot_absent от имени владельца → ответ бота → перемотка времени."""
        rec.command(ADMIN_TG, "/pilot_absent")
        upd = FakeUpdate(ADMIN_TG, ADMIN_NAME)
        await P.cmd_pilot_absent(upd, FakeCtx())
        rec.message(ADMIN_TG, upd.message.replies[-1][0])
        return upd.message.replies

    try:
        # ── Сцена 2a: счастливый путь ────────────────────────────────
        rec.scene("Сцена 2a. Счастливый путь: «✅ Всё в порядке»",
                  "Ключевая фраза: «Система справилась сама — "
                  "координатору остался только информационный апдейт».")
        await pilot_round()
        rec.note("⏱ проходит ~10 сек — тикает планировщик, родителю уходит "
                 "уведомление с кнопками (ALBION_NOTIFY_PARENT_DELAY_MIN)")
        await fast_forward()

        inc_id, _, nonce = await latest_state()
        rec.press(PARENT_TG, "✅ Всё в порядке")
        query = FakeQuery(f"resolve:{inc_id}:{nonce}:ok", FakeUser(PARENT_TG, PARENT_NAME))
        await H.handle_callback(FakeUpdate(PARENT_TG, PARENT_NAME, query=query), FakeCtx())
        if query.edits:
            rec.message(PARENT_TG, query.edits[-1], edited=True)
        rec.note("📝 исходное сообщение отредактировано (нативный паттерн Telegram): "
                 "кнопки исчезли, на их месте — результат; новых сообщений в чате нет")

        # ── Сцена 2b: «⏰ Опоздаем» ──────────────────────────────────
        rec.scene("Сцена 2b. «⏰ Опоздаем» — координатор в курсе, действий не требуется")
        await pilot_round()
        rec.note("⏱ родителю снова ушло уведомление о неявке")
        await fast_forward()

        inc_id, _, nonce = await latest_state()
        rec.press(PARENT_TG, "⏰ Опоздаем")
        query = FakeQuery(f"resolve:{inc_id}:{nonce}:late", FakeUser(PARENT_TG, PARENT_NAME))
        await H.handle_callback(FakeUpdate(PARENT_TG, PARENT_NAME, query=query), FakeCtx())
        if query.edits:
            rec.message(PARENT_TG, query.edits[-1], edited=True)

        # ── Сцена 2c: свободный текст ────────────────────────────────
        rec.scene(
            "Сцена 2c. Родитель пишет свободным текстом — смысл распознаётся",
            "«Родитель не учит команды — пишет как привык». С подключённым LLM-ключом "
            "понимаются любые формулировки; без ключа — встроенная ключевая эвристика. "
            "Координатору всегда уходит исходный текст родителя.",
        )
        await pilot_round()
        rec.note("⏱ родителю ушло уведомление с кнопками")
        await fast_forward()

        parent_text = "Мы опоздаем на 15 минут — пробки"
        upd = FakeUpdate(PARENT_TG, PARENT_NAME, text=parent_text)
        rec.free_text(PARENT_TG, parent_text)
        await H.handle_message(upd, FakeCtx())
        if upd.message.replies:
            rec.message(PARENT_TG, upd.message.replies[-1][0])

        # ── Сцена 2d: молчание → эскалация ───────────────────────────
        rec.scene(
            "Сцена 2d. Родитель молчит → эскалация координатору",
            "Management by exception: человек подключается ТОЛЬКО когда система "
            "не справилась сама. Уведомление со всеми данными для звонка + кнопки действия.",
        )
        await pilot_round()
        rec.note("⏱ родителю ушло уведомление… но родитель молчит")
        await fast_forward()

        rec.note("⏱ проходит время эскалации (ALBION_ESCALATE_DELAY_MIN) — "
                 "планировщик сам отправляет эскалацию координаторам")
        await fast_forward()  # вторая перемотка: срабатывает escalate

        inc_id, _, _ = await latest_state()
        rec.note(f"📝 кнопка «👤 Написать родителю» — прямой переход в чат с родителем "
                 f"(tg://user?id={PARENT_TG}): искать контакт не нужно")
        # Координатор закрывает ситуацию одной кнопкой прямо на эскалации.
        rec.press(COORD_TG, "✅ Закрыть ситуацию")
        query = FakeQuery(f"coord_resolve:{inc_id}:ok", FakeUser(COORD_TG, COORD_NAME),
                          message_text=f"🚨 Эскалация: инцидент #{inc_id}\nПричина: no response")
        await H.handle_callback(FakeUpdate(COORD_TG, COORD_NAME, query=query), FakeCtx())
        if query.edits:
            rec.message(COORD_TG, query.edits[-1], edited=True)
        elif query.answers and query.answers[-1][0]:
            rec.message(COORD_TG, query.answers[-1][0])

        # ── Сцена 2e: отмена урока ───────────────────────────────────
        rec.scene(
            "Сцена 2e. /cancel_lesson — мгновенные уведомления",
            "Репетитор заболел и отменяет урок: репетитор и координатор узнают сразу, "
            "с причиной. Неизвестный ID → честный «не найден», а не ложное «передано».",
        )
        rec.command(TUTOR_TG, "/cancel_lesson lesson_1 по болезни")
        upd = FakeUpdate(TUTOR_TG, TUTOR_NAME)
        await H.cmd_cancel_lesson(upd, FakeCtx(["lesson_1", "по", "болезни"]))
        rec.message(TUTOR_TG, upd.message.replies[-1][0])

        rec.command(TUTOR_TG, "/cancel_lesson unknown_lesson")
        upd = FakeUpdate(TUTOR_TG, TUTOR_NAME)
        await H.cmd_cancel_lesson(upd, FakeCtx(["unknown_lesson"]))
        rec.message(TUTOR_TG, upd.message.replies[-1][0])

        # ── Финал: статистика ────────────────────────────────────────
        rec.scene(
            "Финал. /incidents — метрики для клиента",
            "Ключевая фраза: «Из четырёх ситуаций три решил сам родитель одной кнопкой, "
            "а до человека дошла только одна — и та закрыта в один тап».",
        )
        rec.command(ADMIN_TG, "/incidents")
        upd = FakeUpdate(ADMIN_TG, ADMIN_NAME)
        await P.cmd_incidents(upd, FakeCtx())
        rec.message(ADMIN_TG, upd.message.replies[-1][0])

        return rec.render()
    finally:
        for etype, fn in subscribed:
            bus.unsubscribe(etype, fn)
        engine.repo, engine.scheduler = old_engine_repos
        settings.albion_admin_telegram_ids = old_admins
        if tmp:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", metavar="FILE", help="куда сохранить расшифровку (по умолчанию — stdout)")
    args = ap.parse_args()
    # run_demo() меняет cwd на временную папку — путь резолвим заранее.
    out_path = os.path.abspath(args.out) if args.out else None
    md = asyncio.run(run_demo())
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"✅ Расшифровка сохранена: {out_path} ({len(md)} символов)")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
