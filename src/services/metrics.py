"""Метрики в Prometheus-формате (PR6, PHASE1_ARCHITECTURE.md §6.4).

collect_metrics() собирает счётчики по таблицам: уведомления по
каналам/статусам, DLQ, очередь планировщика (размер + лаг), входящие
webhook'и, kill switch. Без внешних сервисов — текст exposition format,
отдаётся из /metrics webhook-процесса (доступ ограничивает Caddy).
"""

from datetime import datetime, timezone

from src.db.engine import connect


def _parse_iso(s: str):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


async def collect_metrics(dsn: str) -> str:
    """Prometheus exposition text. Ошибки БД → пустые ряды (метрика-здоровье
    самой метрики — успех парса, а не наличие данных)."""
    lines: list[str] = []
    emit = lines.append

    async with connect(dsn) as db:
        # notifications: sent/failed/queued по каналам
        emit("# HELP albion_notifications_total Уведомления по каналу и статусу")
        emit("# TYPE albion_notifications_total counter")
        for r in await db.fetchall(
                "SELECT channel, status, COUNT(*) AS n "
                "FROM notifications GROUP BY channel, status"):
            emit(f'albion_notifications_total{{channel="{r["channel"]}",'
                 f'status="{r["status"]}"}} {r["n"]}')

        # DLQ
        emit("# HELP albion_dlq_size Записей в dead_letter_queue")
        emit("# TYPE albion_dlq_size gauge")
        row = await db.fetchone("SELECT COUNT(*) AS n FROM dead_letter_queue")
        emit(f"albion_dlq_size {row['n'] if row else 0}")

        # scheduled_actions: очередь + лаг самой старой pending-задачи
        emit("# HELP albion_scheduled_actions_total Задачи планировщика по статусу")
        emit("# TYPE albion_scheduled_actions_total gauge")
        for r in await db.fetchall(
                "SELECT status, COUNT(*) AS n FROM scheduled_actions GROUP BY status"):
            emit(f'albion_scheduled_actions_total{{status="{r["status"]}"}} {r["n"]}')
        emit("# HELP albion_scheduled_pending_lag_seconds "
             "Возраст самой старой pending-задачи")
        emit("# TYPE albion_scheduled_pending_lag_seconds gauge")
        row = await db.fetchone(
            "SELECT MIN(execute_at) AS oldest FROM scheduled_actions "
            "WHERE status='pending'")
        lag = 0.0
        if row and row["oldest"]:
            dt = _parse_iso(row["oldest"])
            if dt:
                now = datetime.now(dt.tzinfo or timezone.utc)
                lag = max(0.0, (now - dt).total_seconds())
        emit(f"albion_scheduled_pending_lag_seconds {lag:.0f}")

        # webhook ingress
        emit("# HELP albion_webhook_events_total События webhook по подписи")
        emit("# TYPE albion_webhook_events_total counter")
        for r in await db.fetchall(
                "SELECT signature_ok, COUNT(*) AS n "
                "FROM webhook_events GROUP BY signature_ok"):
            emit(f'albion_webhook_events_total{{signature_ok="{r["signature_ok"]}"}} {r["n"]}')

        # workflows
        emit("# HELP albion_workflows_total Workflow-инстансы по статусу")
        emit("# TYPE albion_workflows_total gauge")
        for r in await db.fetchall(
                "SELECT state, COUNT(*) AS n FROM workflow_instances GROUP BY state"):
            emit(f'albion_workflows_total{{state="{r["state"]}"}} {r["n"]}')

        # kill switch level
        emit("# HELP albion_kill_switch_level Уровень kill switch (0/1/2)")
        emit("# TYPE albion_kill_switch_level gauge")
        row = await db.fetchone(
            "SELECT value FROM system_settings WHERE key='kill_switch_level'")
        emit(f"albion_kill_switch_level {row['value'] if row else 2}")

    return "\n".join(lines) + "\n"
