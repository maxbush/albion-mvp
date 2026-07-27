# 🗺️ MASTER_PLAN v2 — ALBION MVP Second Audit

> Создан: 2026-07-27 (Round 2)
> Статус: Готов к выполнению
> Тесты: 89/89 passing
> Коммитов: 4 (+squashed P0-P3 fixes)

---

## 📊 Текущее состояние (после Round 1)

Все задачи первого аудита (P0–P3) выполнены и включены в кодовую базу:
- ✅ Хардкоды удалены из cancellation.py и lead_capture.py
- ✅ _demo_tick_handler gated под demo_mode
- ✅ callback_data убран из _notify_parent payload
- ✅ Мёртвый код удалён (_reset_demo, reap_stuck, map_lesson)
- ✅ cmd_status скрывает model от non-admin
- ✅ cmd_mock_* gated под demo_mode
- ✅ notify_all_coordinators helper извлечён
- ✅ 11 тестов добавлены (edge cases + demo commands)
- ✅ Timezone/country добавлены в модель
- ✅ Import commands (import_learners, import_customers)

---

## 🔴 P0 — Баги (логика, correctness)

- [ ] **P0.1** `find_escalated_incident_for_parent`: timezone mismatch в age check
  - Файл: `src/workflows/absence.py` ~line 155
  - Проблема: `_dt.now()` (naive) − `resolved_at` (aware +00:00) = TypeError → caught by except → **2-hour time window silently bypassed**. Любой эскалированный инцидент находится навечно, а не 2 часа.
  - Фикс: `_dt.now(timezone.utc)` вместо `_dt.now()`

- [ ] **P0.2** `cmd_incidents`: `inc.get('resolved_at', '')[:16]` → `TypeError: 'NoneType'` если resolved_at=None
  - Файл: `src/bot/pilot.py` ~line 652
  - Проблема: `inc.get('resolved_at', '')` возвращает `''` когда ключ есть но значение None. `None[:16]` = TypeError.
  - **Wait** — уже исправлено в Round 1. Проверить.
  - Статус: ✅ Проверить и закрыть если уже fixed.

- [ ] **P0.3** `seed_demo data`: `student_id="student_1"` (string) для INTEGER колонки
  - Файл: `src/bot/handlers.py:136`
  - Проблема: incidents.student_id объявлен как INTEGER в схеме, но demo создаёт со строкой "student_1". SQLite допускает (typeless), но это semantic error.
  - Фикс: Использовать числовое значение или поменять схему на TEXT.
  - Приоритет: Low — только demo mode.

---

## 🟡 P1 — Отсутствие тестов

- [ ] **P1.1** Тест: `cmd_import_learners` — парсинг TSV и загрузка в БД
  - Файл: `tests/test_pilot.py` или `tests/test_edge_cases_and_commands.py`
  - Сценарий: подать TSV текст как reply → проверить что ученики созданы с правильными timezone/country/email

- [ ] **P1.2** Тест: `cmd_import_customers` — парсинг TSV привязок родитель→ученик
  - Сценарий: подать TSV → проверить merithub_contacts

- [ ] **P1.3** Тест: `cmd_mh_user` с `email=` и `phone=` параметрами
  - Сценарий: `/mh_user s01 333 Алиса email=p@ex.com phone=+44123` → проверить merithub_contacts

- [ ] **P1.4** Тест: `find_escalated_incident_for_parent` с timezone-aware resolved_at
  - Убедиться что age check работает правильно с aware datetimes

- [ ] **P1.5** Тест: `/mh_students` показывает timezone и parent contacts

---

## 🔵 P2 — Архитектурные улучшения (безопасные)

- [ ] **P2.1** Timezone не используется при создании расписания
  - Файлы: `src/bot/pilot.py` (cmd_mh_schedule), `src/integrations/merithub_client.py`
  - Проблема: Храним timezone в merithub_students, но `/mh_schedule` не передаёт timezone ученика в MeritHub API. MeritHub принимает `timeZoneId` параметр — мы хардкодим дефолт.
  - Фикс: Брать timezone из первого ученика или репетитора и передавать в schedule_class.
  - **🛑 ТРЕБУЕТСЯ ВМЕШАТЕЛЬСТВО:** Нужна бизнес-логика — чей timezone использовать при нескольких учениках в разных зонах? (London vs Almaty = 5 часов разницы)

- [ ] **P2.2** `incidents.status DEFAULT 'open'` vs `status='pending'` в коде
  - Файл: `src/db/models.py:44`
  - Проблема: Схема дефолтит 'open', но весь код создаёт с явным 'pending'. DEFAULT 'open' — мёртвый.
  - Фикс: Поменять DEFAULT на 'pending' для консистентности.

- [ ] **P2.3** Показать timezone ученика в pre-lesson reminder
  - Файлы: `src/workflows/lesson_ops.py`
  - Проблема: Родитель получает напоминание "через 15 мин занятие", но не видит в каком часовом поясе. Если parent_tg в Лондоне, а урок создан в UTC+5, может быть путаница.
  - Фикс: Показывать "Занятие в 15:00 (Europe/London)" с timezone из merithub_students.

---

## 🟢 P3 — Cleanup / Polish

- [ ] **P3.1** Обновить ALBION_GUIDE.md: добавить /import_learners, /import_customers, timezone info
- [ ] **P3.2** Обновить DEMO_RUNBOOK.md: добавить секцию "Импорт реальных данных"

---

## 🛑 ТРЕБУЕТСЯ ВМЕШАТЕЛЬСТВО ЧЕЛОВЕКА

1. **Timezone при нескольких учениках:** Если в занятии ученик из London и ученик из Almaty — какой timezone использовать в MeritHub `schedule_class`? Варианты:
   - (a) Timezone репетитора (логично — он ведёт урок)
   - (b) Timezone организации (дефолт ALBION)
   - (c) Всегда UTC, конвертировать при показе

2. **SQLite connection per query:** Для MVP ок, для прода — pool. Отложено.

---

## Порядок выполнения

```
P0.1 (timezone bug fix)          → 5 мин
P0.2 (verify already fixed)      → 2 мин
P0.3 (demo student_id type)      → 5 мин
P1.1-P1.5 (tests)                → 30 мин
P2.1 (timezone in scheduling)    → ⏳ ждёт решения
P2.2 (DEFAULT 'pending')         → 5 мин
P2.3 (timezone in reminders)     → 15 мин
P3.1-P3.2 (docs update)          → 10 мин
──────────────────────────────────────
Итого: ~72 мин (без P2.1)
```
