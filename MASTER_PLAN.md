# 🗺️ MASTER_PLAN v3 — ALBION MVP Third Audit (Round 3)

> Создан: 2026-07-31 (Round 3, полный ре-аудит)
> Базовая линия на старте: **95/95 тестов passing**
> Интерфейс продукта = Telegram-бот (нет веб-UI), поэтому E2E-проверки =
> pytest-эмуляция Telegram-апдейтов (FakeUpdate/FakeContext) + живой прогон
> через SQLite scheduler + FastAPI TestClient для webhook'ов.
> Playwright неприменим: веб-фронтенда в репозитории нет.

---

## 📜 История предыдущих раундов

- Round 1 (2026-07-2x): хардкоды удалены, demo-gating, idempotency, kill switch polish.
- Round 2 (2026-07-27): timezone-fix в `find_escalated_incident_for_parent`, dual-timezone display,
  `DEFAULT 'pending'`, import-команды, 11 новых тестов (94 итого).
- Round 3 (этот план): тотальный ре-аудит логики, UI/Backend синк, мёртвый код.

---

## 🔴 P0 — Баги корректности

- [x] **P0.1** `lesson_ops._format_dual_time` — суффикс `[+Nч к London]` **никогда не показывается**:
  `diff_hours = (user_time - london_time)` — это один и тот же instant, разница всегда 0.
  Проверено: `_format_dual_time('2026-07-28T15:00:00+00:00','Asia/Almaty')` → нет суффикса.
  План: считать разницу `utcoffset()` пользователя и London. Тест на off-by-offset.

- [x] **P0.2** `lesson_ops.schedule_class_coordination` — `class_live_check` планируется на
  **том же workflow** (`start_wid`), что и `tutor_start_check`. Когда репетитор отвечает
  на start-check, `record_checkin_response → _cancel_future_actions(wid)` **отменяет и
  live-check** → ветка «⚠️ урок не перешёл в live, но репетитор подтвердил старт»
  вообще недостижима. План: отдельный workflow `class_live_check` со скопированным контекстом
  (class_id/tutor/students/start_time) — именно на это уже рассчитан код `_check_class_live`
  (ищет tutor_start_check wf по class_id). E2E: tutor нажал «class_started» →
  через grace-период без статуса `lv` координатор получает алерт.

- [x] **P0.3** UI/Backend mismatch: `cancellation.handle_classified` отвечает пользователю
  «Укажите ID урока: /cancel_lesson <ID>» — но **команда `/cancel_lesson` не существует**
  (ни в setup_handlers, нигде). План: реализовать `/cancel_lesson <lesson_id> [причина...]`
  → публикует `LESSON_CANCELLED` + подтверждение; E2E тест полного флоу
  free-text «отмени урок» → подсказка → команда → уведомление репетитору/координаторам.

- [x] **P0.4** Markdown без `parse_mode` — пользователь видит служебные символы:
  `cmd_mh_students` (`*жирный*`, `` `код` `` выводятся буквально), `cmd_mh_contacts`
  (backticks буквально), `cmd_seed10` (финальная подсказка с backticks).
  План: добавить `parse_mode="Markdown"` + экранирование где надо; тесты на ответы.

- [x] **P0.5** Наивный `start_time` без таймзоны трактуется как **UTC** (`_schedule_at`,
  `_parse_dt`), хотя каноническая зона расписания в продукте — **Europe/London**
  (см. `/mh_schedule`, `schedule_class timezone="Europe/London"`). Летом (BST) напоминания
  уезжают на час. План: в точке планирования трактовать naive-время как Europe/London
  (с предупреждением в лог), aware-время не трогаем. Тест на naive London + корректный
  перевод в UTC.

## 🟡 P1 — Продукт/логика

- [x] **P1.1** Интент `absence_report` классифицируется AI-слоем, но **не имеет обработчика**:
  родитель/репетитор пишет «ученик не пришёл» — молчание. План: обработчик в
  `cancellation.py`-стиле → alert всем координаторам (ops_alert) с текстом и TG автора.

- [x] **P1.2** Протухшие check-in workflow'ы: `find_active_checkin` подхватывает ЛЮБОЙ
  'running' wf с nonce, даже недельной давности (например, юзер не был зарегистрирован
  в момент reminder → wf завис навсегда). Следующее обычное сообщение родителя попадёт
  в протухший check-in и закроет его. План: экспайр по `start_time + 3ч` (или updated_at):
  протухший wf → auto-complete `response_status='expired'` и пропуск. Тест.

- [ ] **P1.3** `/absent <id>` для реального class_id — тихий no-op: `handle_lesson_absent`
  не находит урок ни в MeritHub (метода `get_lesson` у real client нет), ни в mock Airtable,
  а юзеру уже отвечено «Зафиксировал отсутствие по ...». План: при неизвестном уроке
  и наличии `reported_by` — TG-уведомление отправителю «урок не найден». Тест.

- [ ] **P1.4** Scheduler reaper: зависшие `running` задачи с `attempts >= 3` и истёкшим
  lock **никогда не реанимируются и не помечаются failed** — зомби-строки. План: reaper
  помечает их `failed` с last_error='lock expired after max attempts'. Тест.

- [ ] **P1.5** Эскалация неявки координатору — сухая строка «🚨 Эскалация: инцидент #N
  (причина)» без ученика/занятия/родителя. План: обогатить (ученик, занятие с временем,
  parent TG, время инцидента), как в `notify_coordinators_parent_reply`. Тест.

## 🔵 P2 — Мёртвый код / оптимизация

- [ ] **P2.1** `_demo_resolved: set` в `handlers.py` — только `.discard()`/`.add()`,
  **никогда не читается**. Удалить.
- [ ] **P2.2** `MockMeritHubService.get_balance / check_low_balance` — не используются
  ничем, кроме собственного теста (`PAYMENT_*` события нигде не публикуются).
  **РЕШЕНИЕ ВЛАДЕЛЬЦА: закомментировать (НЕ удалять)** — в дальнейшем будет интеграция
  с Xero. Комментарий ссылается на это решение; тест-assert'ы тоже комментируются.
- [ ] **P2.3** `cmd_today`: `now = _dt.now().astimezone()` — неиспользуемая переменная.
- [ ] **P2.4** Дублирующиеся помощники «Ваши команды» в `cmd_start` и `handle_callback`
  (два разных списка команд, расходятся). Вынести в одну функцию `_coordinator_help()`.
- [ ] **P2.5** LIKE-поиск по JSON (`%"incident_id": N%`) — хрупко к формату json.dumps.
  Оставить как принятое ограничение MVP, задокументировать в кодовых комментариях ссылку
  на H3 (json_extract при росте). *Код не меняем — риск регрессий высок, тесты покрывают.*

## 🟢 P3 — Документация / полировка

- [ ] **P3.1** README: бейдж `tests-75/75` устарел (по факту 95+). Обновить + строка
  про новые команды Round 3.
- [ ] **P3.2** ALBION_GUIDE/DEMO_RUNBOOK: добавить `/cancel_lesson`, поведение
  absence_report, уточнить naive-time → Europe/London.

---

## 🛑 ТРЕБУЕТСЯ ВМЕШАТЕЛЬСТВО ЧЕЛОВЕКА

1. **H1 — Самоназначение роли coordinator (privilege escalation).** По кнопке
   «👨‍💼 Я координатор» в `/start` ЛЮБОЙ TG-аккаунт становится координатором и начинает
   получать эскалации, лиды и алерты (PII учеников). **РЕШЕНИЕ ВЛАДЕЛЬЦА (2026-07-31):
   для пилота оставляем как есть, для прода — отдельная задача убрать.** Не трогаем.
2. **H2 — Auto-dispatch attendance webhook без подписи.** Когда
   `MERITHUB_WEBHOOK_SECRET` пуст, любой POST на `/merithub/webhook` с requestType=
   `attendance` и локально известным classId вызовет поток неявок/уведомлений.
   Смягчено тем, что нужны локальные enrollments. Решение об авторском крюке — за
   владельцем продукта.
3. **H3 — SQLite connection-per-query + LIKE по JSON.** Для пилота ок. Для прода:
   пул коннектов, `json_extract`/отдельные колонки, индексы. Отложено сознательно.
4. **H4 (перенесено из Round 2) — Timezone при нескольких учениках** в одном классе:
   чей timezone в `schedule_class`? Сейчас всегда Europe/London + dual-display.

---

## Порядок выполнения (после команды «ВНИЗ»)

```
P0.1 dual-time diff                  → фикс + тест           ~10 мин
P0.2 class_live_check wf             → фикс + E2E            ~20 мин
P0.3 /cancel_lesson                  → команда + E2E         ~20 мин
P0.4 parse_mode sweep                → фикс + тесты          ~10 мин
P0.5 naive-time → Europe/London      → фикс + тест           ~10 мин
P1.1 absence_report handler          → фикс + тест           ~10 мин
P1.2 checkin expiry                  → фикс + тест           ~15 мин
P1.3 /absent unknown lesson notify   → фикс + тест           ~10 мин
P1.4 scheduler zombie reaper         → фикс + тест           ~10 мин
P1.5 escalation message enrich       → фикс + тест           ~10 мин
P2.1-P2.4 cleanup                    → рефактор + прогон     ~15 мин
P3.1-P3.2 docs                       → правки                ~10 мин
```

## Журнал выполнения

- 2026-07-31: разбор владельца применён (P2.2 → comment-out, H1 → не трогаем). Старт Round 3.
<!-- Сюда по ходу работы: отметки [~]/[x], ссылки на коммиты -->
