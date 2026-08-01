"""Минимальный i18n-слой (пока два языка: ru — дефолт, en — тьюторы).

Решение (Вариант 3 аудита): родители и координаторы — русскоязычные; тексты
нуточно-тьюторских касаний локализованы. Матрица строк централизована здесь,
бизнес-логика не дублируется. Язык хранится в users.language, выставляется
при регистрации (tutor → en); смена языка пользователем — отдельной фичей после пилота.

Новые строки добавляются ТОЛЬКО сюда (оба языка), fallback — всегда 'ru'.
"""

import logging

from src.db.repository import UserRepository

logger = logging.getLogger(__name__)

T: dict[str, dict[str, str]] = {
    # ── Напоминание тьютору за T-N минут ──
    "tutor_reminder": {
        "ru": "🧑‍🏫 Через {mins} мин урок.{time_line}\nРепетитор: {tutor}\nУченики: {students}\n\nПодтвердите готовность.",
        "en": "🧑‍🏫 Lesson in {mins} min.{time_line}\nTutor: {tutor}\nStudents: {students}\n\nPlease confirm you're ready.",
    },
    "tutor_btn_ready": {"ru": "✅ Готов(а)", "en": "✅ Ready"},
    "tutor_btn_late": {"ru": "⏰ Опоздаю", "en": "⏰ Running late"},
    "tutor_btn_no_show": {"ru": "❌ Не смогу провести", "en": "❌ Can't teach"},
    "tutor_btn_tech": {"ru": "🛠 Проблема с платформой", "en": "🛠 Platform issue"},
    # ── Старт-чек тьютора ──
    "tutor_start_check": {
        "ru": "▶️ Время урока: {label}.\nУченики: {students}\n\nОтметьте статус старта урока.",
        "en": "▶️ Lesson time: {label}.\nStudents: {students}\n\nPlease mark the lesson start status.",
    },
    "tutor_btn_class_started": {"ru": "✅ Урок начался", "en": "✅ Lesson started"},
    "tutor_btn_student_absent": {"ru": "👤 Ученик не пришёл", "en": "👤 Student absent"},
    "tutor_btn_tech_short": {"ru": "🛠 Техпроблема", "en": "🛠 Tech issue"},
    # ── Ссылка на комнату для тьютора ──
    "tutor_link": {
        "ru": "📎 Ссылка на урок:\n🕐 {time}\n👥 Ученики: {students}\n🔗 {url}",
        "en": "📎 Lesson link:\n🕐 {time}\n👥 Students: {students}\n🔗 {url}",
    },
    # ── Уведомление тьютору об отмене ──
    "tutor_cancelled": {
        "ru": "📅 Отмена: {subject}\n{reason}",
        "en": "📅 Cancellation: {subject}\n{reason}",
    },
    # ── Подтверждения нажатий (checkin callbacks) ──
    "ack_ready": {"ru": "✅ Статус принят.", "en": "✅ Got it, status recorded."},
    "ack_late": {"ru": "⏰ Спасибо, отметили опоздание.", "en": "⏰ Thanks, noted you'll be late."},
    "ack_no_show": {"ru": "❌ Спасибо, отметили отсутствие.", "en": "❌ Thanks, noted the absence."},
    "ack_tech": {"ru": "🛠 Спасибо, отметили техпроблему.", "en": "🛠 Thanks, tech issue logged."},
    "ack_class_started": {"ru": "✅ Отлично, старт урока зафиксирован.", "en": "✅ Great, lesson start recorded."},
    "ack_student_absent": {"ru": "👤 Спасибо, отметили отсутствие ученика.", "en": "👤 Thanks, noted the student is absent."},
    # ── Ответы на free-text тьютора ──
    "ft_tutor_ready": {"ru": "✅ Спасибо! Отметили, что вы готовы.", "en": "✅ Thanks! Noted you're ready."},
    "ft_tutor_late": {
        "ru": "⏰ Спасибо! Отметили, что вы опоздаете. Координатор уведомлён.",
        "en": "⏰ Thanks! Noted you'll be late. The coordinator has been informed.",
    },
    "ft_tutor_no_show": {
        "ru": "❌ Спасибо! Отметили, что урок не состоится. Координатор уведомлён.",
        "en": "❌ Thanks! Noted the lesson won't happen. The coordinator has been informed.",
    },
    "ft_tutor_tech": {
        "ru": "🛠 Спасибо! Техпроблема зафиксирована. Координатор уведомлён.",
        "en": "🛠 Thanks! Tech issue logged. The coordinator has been informed.",
    },
    "ft_tutor_other": {
        "ru": "💬 Спасибо! Передали ответ координатору для ручной обработки.",
        "en": "💬 Thanks! We've passed your message to the coordinator.",
    },
    "ft_tutor_start_hint": {
        "ru": "💬 Спасибо! Передали ответ координатору для ручной обработки.\nДля быстрого ответа используйте кнопки из предыдущего сообщения.",
        "en": "💬 Thanks! Passed to the coordinator.\nFor a quick reply, please use the buttons in the previous message.",
    },
    # ── «Мои занятия» (/lessons) ──
    "lessons_empty": {
        "ru": "Ближайших занятий не вижу.\nКогда координатор добавит вас в расписание — они появятся здесь.",
        "en": "No upcoming lessons found.\nOnce the coordinator schedules you, they will show up here.",
    },
    "lessons_header_parent": {
        "ru": "📚 Ваши ближайшие занятия:",
        "en": "📚 Your upcoming lessons:",
    },
    "lessons_header_tutor": {
        "ru": "📚 Ваши ближайшие уроки (время: {org}):",
        "en": "📚 Your upcoming lessons (time: {org}):",
    },
    "lessons_link_hint": {
        "ru": "Ссылки постоянные: если потеряли — нажмите здесь заново в любой момент.",
        "en": "Links are permanent: if lost, just tap here again anytime.",
    },
}


def tr(key: str, lang: str = "ru", **fmt) -> str:
    """Строка по ключу и языку. Нет языка/ключа → ru; нет ru → сам ключ (заметно)."""
    variants = T.get(key)
    if not variants:
        logger.warning("i18n: unknown key %s", key)
        return key
    template = variants.get(lang) or variants.get("ru") or key
    try:
        return template.format(**fmt) if fmt else template
    except Exception:
        return template


async def lang_of(telegram_id: str) -> str:
    """Язык пользователя из БД. Незнакомец → ru (дефолт продукта)."""
    try:
        user = await UserRepository().get_by_telegram_id(str(telegram_id))
        lang = (user or {}).get("language") or "ru"
        return lang if lang in ("ru", "en") else "ru"
    except Exception:
        return "ru"
