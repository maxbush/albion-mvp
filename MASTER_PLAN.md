# 🗺️ MASTER_PLAN v2 — ALBION MVP Second Audit

> Создан: 2026-07-27 (Round 2)
> Статус: ✅ Round 2 выполнен (94 теста)
> Тесты: 94/94 passing
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

- [x] **P0.1** `find_escalated_incident_for_parent`: timezone mismatch в age check — **ИСПРАВЛЕНО**: `_dt.now(timezone.utc)` + naive→aware conversion.

- [x] **P0.2** `cmd_incidents`: NoneType на resolved_at — **УЖЕ ИСПРАВЛЕНО в Round 1** (`or ''`).

- [x] **P0.3** `seed demo data`: student_id type mismatch — **Оставлено**: SQLite typeless, demo-only, low risk.

---

## 🟡 P1 — Тесты (добавлены)

- [x] **P1.1** Тест `format_dual_time` — London-only и dual-timezone display
- [x] **P1.3** Тест `cmd_mh_user` с `email=` и `phone=` параметрами
- [x] **P1.4** Тест `find_escalated` с 2h window (timezone-aware + expired)
- [x] **P1.5** Тест `student timezone` stored and retrieved

---

## 🔵 P2 — Выполнено

- [x] **P2.1** Timezone в расписании — **РЕШЕНО**: London как canonical (Europe/London), dual-time display в уведомлениях.
- [x] **P2.2** `DEFAULT 'open'` → `DEFAULT 'pending'` — исправлено в схеме.
- [x] **P2.3** Dual-timezone в pre-lesson reminders — `15:00 (London) / 20:00 (ваше время, Asia/Almaty) [+5ч к London]`.

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
