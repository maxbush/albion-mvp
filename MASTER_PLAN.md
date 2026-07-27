# 🗺️ MASTER_PLAN — ALBION MVP Total Audit & Fix

> Создан: 2026-07-27
> Статус: ✅ Выполнен (кроме P3.1 — требует решения человека)
> Тесты: 89/89 passing

---

## 📊 Текущее состояние

- **Строк кода:** ~6400 (src + tests)
- **Файлов source:** 22 .py
- **Файлов tests:** 11 .py
- **Коммитов:** 6
- **Тестов:** 78 passing

---

## 🔴 P0 — Критичные (логика, баги)

- [x] **P0.1** `cancellation.py` — хардкод `"222222"` и `"coordinator_1"`
  - Файл: `src/workflows/cancellation.py:27-28`
  - Проблема: Уведомления об отмене уходят на захардкоженные TG ID, а не на реальных репетиторов/координаторов
  - Фикс: Использовать `get_coordinator_ids()` + брать tutor TG из UserRepository/BД

- [x] **P0.2** `lead_capture.py` — хардкод `"coordinator_1"`
  - Файл: `src/workflows/lead_capture.py:23`
  - Проблема: Новые заявки уходят только на coordinator_1
  - Фикс: Использовать `get_coordinator_ids()` как в absence.py

- [x] **P0.3** `_demo_tick_handler` подписан на ВСЕ SCHEDULER_TICK события в проде
  - Файл: `src/bot/handlers.py:768-774`
  - Проблема: В prod-режиме каждый тик шедулера триггерит `_demo_tick_handler`, который пытается отправить `demo_notify` на `coordinator_1`. Это мусорный handler.
  - Фикс: Подписывать только при `settings.albion_demo_mode`

- [x] **P0.4** `notif_handler`: дублирование `buttons` + `cb_data`
  - Файл: `src/bot/handlers.py:811-814` + `src/workflows/absence.py:246-252`
  - Проблема: `_notify_parent` публикует и `buttons` (3 кнопки), и `cb_data` (одна кнопка "✅ Всё в порядке"). В `notif_handler` приоритет у `buttons`, но `cb_data` всё равно присутствует в payload — может запутать.
  - Фикс: Убрать `callback_data` из NOTIFICATION_REQUESTED payload в absence.py (buttons уже содержат все нужные callback_data)

---

## 🟡 P1 — Высокий приоритет (мёртвый код, архитектура)

- [x] **P1.1** `_reset_demo` — мёртвая функция
  - Файл: `src/bot/handlers.py:128-137`
  - Никогда не вызывается. Удалить.

- [x] **P1.2** `reap_stuck` — мёртвый метод
  - Файл: `src/db/repository.py:252-254`
  - Помечен DEPRECATED, всегда возвращает 0. Удалить.

- [x] **P1.3** `map_lesson` — мёртвый метод
  - Файл: `src/integrations/merithub_client.py:300-315`
  - Комментарий "не используется". Удалить.

- [x] **P1.4** `_demo_waiting_messages` и `_demo_resolved` — глобальный мутабельный стейт
  - Файл: `src/bot/handlers.py:37-40`
  - Проблема: Не переживает рестарт, не чистится, один chat_id может перезаписать другой.
  - Фикс: Оставлено — используется только в demo solo mode (cmd_mock_demo + _demo_solo_absence), которые теперь gated под demo_mode.

- [x] **P1.5** `cmd_mock_absent` + `cmd_mock_demo` — конфликт с реальным pilot flow
  - Файлы: `src/bot/handlers.py:279-296`
  - Фикс: Оба gated под `settings.albion_demo_mode`.

- [x] **P1.6** `cmd_status` раскрывает модель LLM любому пользователю
  - Файл: `src/bot/handlers.py:244-257`
  - Фикс: AI-инфо и Kill Switch показываются только admin'ам.

- [x] **P1.7** Неиспользуемые импорты в `cancellation.py`
  - Удалены datetime, NotificationRepository. Также aiosqlite из handlers.py.

- [x] **P1.8** `from __future__ import annotations` в `lesson_ops.py`
  - Удалён.

---

## 🔵 P2 — Тесты (покрытие критичных путей)

- [x] **P2.1** Тест: post-escalation button press ✅
- [x] **P2.2** Тест: duplicate button press (different button after resolve) ✅
- [x] **P2.3** Тест: self-registration flow ✅
- [x] **P2.4** Тест: `find_escalated_incident_for_parent` ✅
- [x] **P2.5** Тест: cmd_seed10 ✅
- [x] **P2.6** Тест: cmd_demo_reset ✅
- [x] **P2.7** Тест: cmd_incidents, cmd_today, cmd_morning ✅
- [x] **P2.8** Тест: cmd_mh_contact + merithub_contacts with phone/email ✅

---

## 🟢 P3 — Низкий приоритет (оптимизация, cleanup)

- [ ] **P3.1** SQLite: connection per query
  - Файл: `src/db/repository.py`
  - Каждый `_execute`/`_fetchone`/`_fetchall` создаёт новое подключение через `aiosqlite.connect`
  - Для MVP ОК (WAL + busy_timeout), но в проде нужен connection pool.
  - **🛑 ТРЕБУЕТСЯ ВМЕШАТЕЛЬСТВО ЧЕЛОВЕКА:** архитектурное изменение, влияет на все тесты. Оставить для прода.

- [x] **P3.2** `_show_demo_report` — мёртвый для non-demo
  - Файл: `src/bot/handlers.py:606-647`
  - Gated под demo_mode.

- [x] **P3.3** Дублирование координаторского паттерна
  - Вынесен в общий helper `notify_all_coordinators()` в `src/bot/roles.py`.
  - Используется в absence.py (2 места) и lesson_ops.py (1 место).
  - Убрано ~30 строк дублированного кода.

---

## 🛑 ТРЕБУЕТСЯ ВМЕШАТЕЛЬСТВО ЧЕЛОВЕКА

### Архитектурные решения, которые нужно согласовать:

1. **SQLite → PostgreSQL**: когда переходим? Текущий Repository pattern позволяет, но нужно переписать тесты и conftest.

2. **Connection pool**: нужно ли для MVP или ок как есть?

3. **Cancellation/Lead workflows**: нужны ли они в текущем виде? Они полностью хардкоднут mock-данные. Для MVP-демо неявок они не нужны. Можно:
   - (a) Оставить как есть (они не мешают)
   - (b) Отключить регистрацию в `main.py`
   - (c) Переписать на реальные данные

4. **Demo mode**: оставить или полностью убрать? Сейчас `ALBION_DEMO_MODE=false` по умолчанию и demo-флоу не активируются.

---

## ✅ Выполненные задачи

_Задачи, завершённые до создания MASTER_PLAN:_

- [x] Parent absence flow v2 (3 кнопки + free text)
- [x] Tutor quick-action flow v1
- [x] Pre-lesson reminders за 15 минут
- [x] Coordinator outcome notifications
- [x] MeritHub hook reconnaissance (classStatus + attendance)
- [x] Button-first UX
- [x] Class not live workflow (с контекстом tutor)
- [x] Contacts с phone/email
- [x] Self-registration через /start
- [x] Post-escalation button press fix
- [x] Duplicate button press fix
- [x] Late free text after escalation fix
- [x] Morning digest / Today / Incidents
- [x] seed10 / demo_reset
- [x] Security audit (4 critical fixes)

---

## 📋 Порядок выполнения

```
P0.1 → P0.2 → P0.3 → P0.4  (критические баги, ~30 мин)
P1.1 → P1.8                 (мёртвый код, ~20 мин)
P2.1 → P2.8                 (тесты, ~40 мин)
P3.2 → P3.3                 (cleanup, ~15 мин)
```

P3.1 (connection pool) — отложено до решения человека.
