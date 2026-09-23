"""Политика бесплатной/платной отмены и переноса (этап 1, чистая функция).

Никакого I/O: на вход — начало занятия и «сейчас», на выход — окно.
Один источник правды для текстов предупреждения и флага is_paid в
schedule_overrides. Граница включительно бесплатная: ровно free_hours
до занятия — ещё бесплатно.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class PolicyVerdict:
    window: str        # 'free' | 'paid'
    hours_left: float  # сколько часов осталось до занятия (может быть <0)
    free_hours: int    # порог, по которому судили

    @property
    def is_paid(self) -> bool:
        return self.window == "paid"


def evaluate_policy(
    lesson_start: datetime,
    *,
    now: datetime,
    free_hours: int,
) -> PolicyVerdict:
    """'free', если до занятия >= free_hours, иначе 'paid'."""
    hours_left = (lesson_start - now).total_seconds() / 3600
    return PolicyVerdict(
        window="paid" if hours_left < free_hours else "free",
        hours_left=round(hours_left, 2),
        free_hours=free_hours,
    )


def paid_warning_text(verdict: PolicyVerdict, kind: str) -> str:
    """Предупреждение для confirm-карточки. kind: 'cancel' | 'reschedule'."""
    if not verdict.is_paid:
        return ""
    what = "Отмена" if kind == "cancel" else "Перенос"
    return (
        f"⚠️ {what} менее чем за {verdict.free_hours} ч до занятия — "
        "по условиям она оплачивается.\n\n"
    )
