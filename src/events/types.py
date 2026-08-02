from dataclasses import dataclass, field
from datetime import datetime, timezone

@dataclass
class Event:
    type: str
    data: dict
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    idempotency_key: str | None = None

class EventTypes:
    # Lessons
    LESSON_ABSENT = "lesson.absent"
    LESSON_CANCELLED = "lesson.cancelled"
    # Публикуются webhook-диспетчером по факту classStatus (lv/cp) — метрики/DLQ.
    LESSON_STARTED = "lesson.started"
    LESSON_COMPLETED = "lesson.completed"
    # NOTE: LESSON_RESCHEDULED и PAYMENT_* убраны (R7-13) — непланируемые фичи,
    # вернём по решению о переносах/монетизации.

    # Messages
    MESSAGE_INCOMING = "message.incoming"
    MESSAGE_CLASSIFIED = "message.classified"

    # Leads
    LEAD_NEW = "lead.new"

    # Notifications — честная стейт-машина.
    # R9-9: NOTIFICATION_DELIVERED/FAILED удалены — публиковались в никуда
    # (0 подписчиков); статусы и так персистятся в БД (mark_sent/mark_failed).
    NOTIFICATION_REQUESTED = "notification.requested"

    # Payments — ОТКЛЮЧЕНО (Round 3, решение владельца): ждём интеграцию Xero.
    # PAYMENT_RECEIVED/PAYMENT_LOW_BALANCE удалены в R7-13 (фантомы, 0 publish).

    # Workflows
    WORKFLOW_STARTED = "workflow.started"
    WORKFLOW_COMPLETED = "workflow.completed"
    WORKFLOW_FAILED = "workflow.failed"

    # System
    # R9-9: SYSTEM_KILL_SWITCH удалён — публиковался в никуда (0 подписчиков);
    # уровень kill switch хранится в памяти процесса (H6).
    SCHEDULER_TICK = "scheduler.tick"
    SYSTEM_DLQ_ALERT = "system.dlq_alert"
