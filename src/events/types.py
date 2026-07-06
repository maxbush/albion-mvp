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
    LESSON_RESCHEDULED = "lesson.rescheduled"
    LESSON_STARTED = "lesson.started"
    LESSON_COMPLETED = "lesson.completed"

    # Messages
    MESSAGE_INCOMING = "message.incoming"
    MESSAGE_CLASSIFIED = "message.classified"

    # Leads
    LEAD_NEW = "lead.new"

    # Notifications — честная стейт-машина
    NOTIFICATION_REQUESTED = "notification.requested"
    NOTIFICATION_DELIVERED = "notification.delivered"
    NOTIFICATION_FAILED = "notification.failed"

    # Payments
    PAYMENT_RECEIVED = "payment.received"
    PAYMENT_LOW_BALANCE = "payment.low_balance"

    # Workflows
    WORKFLOW_STARTED = "workflow.started"
    WORKFLOW_COMPLETED = "workflow.completed"
    WORKFLOW_FAILED = "workflow.failed"

    # System
    SCHEDULER_TICK = "scheduler.tick"
    SYSTEM_DLQ_ALERT = "system.dlq_alert"
    SYSTEM_KILL_SWITCH = "system.kill_switch"
