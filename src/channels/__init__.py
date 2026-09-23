"""Каналы доставки уведомлений (Telegram, WhatsApp, email — этап 3).

Бизнес-логика публикует NOTIFICATION_REQUESTED с канал-нейтральной моделью
кнопок; конкретный ChannelSender отвечает за отображение в канал
(inline-клавиатура TG, quick-reply/CTA WA).
"""
