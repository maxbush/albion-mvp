# Этап 1 — бэклог задач для LLM-агентов

> Каждая карточка — самостоятельный PR. Формат карточки:
> **Цель · Зависит от · Прочитать · Можно менять · Контракт · Шаги · Приёмка · Не делать · Размер.**
> Размер: S ≤ 150 строк диффа, M ≤ 400, L — разбить (L в бэклоге нет).
> Часы — оценка **человеческого** времени на постановку, ревью и доводку
> (вместе с работой агента). Код пишет агент, отвечает за результат человек.

## Карта волн

```
Волна 0  Ограждения     T00 ─ T01 ─ T02 ─ T03
Волна 1  Данные         T04 ─ T05 ─ T06 ─ T07 ─ T08a ─ T08b
Волна 2  Сообщения      T09 ─ T10 ─ T11a ─ T11b ─ T12a ─ T12b ─ T12c ─ T13
Волна 3  WhatsApp       T14 ─ T15 ─ T16
Волна 4  Занятия        T17 ─ T18 ─ T19 ─ T20a ─ T20b ─ T21
Волна 5  Production     T22 ─ T23 ─ T24 ─ T25 ─ T26 ─ T27 ─ T28
```

Параллелить можно: волну 3 (T14) сразу после T10; T18/T19 (чистые функции)
в любой момент после T01; T25 (импорт) после T08b.

| Задача | Строка КП | Часы |
|---|---|---|
| T00–T03 | Масштабирование (подготовка) | 6 |
| T04–T07, T22–T24, T26–T27 | Масштабирование: БД, миграции, деплой, мониторинг | 30 |
| T08a–T16 | Интеграция WhatsApp, маршрутизация каналов | 60 |
| T17–T18 | Контроль пересечений perma (+ исправление напоминаний серий) | 18 |
| T19–T21 | Переносы и платные отмены | 26 |
| T25 | Миграция реальных данных | 10 |
| T28 + UAT | Обучение и приёмка | 5 |
| **Итого разработки** | | **~155 ч** |

155 ч укладываются в вилку КП (133–188 ч). Запас небольшой: главный риск —
T17 (материализация занятий), это работа, которой нет в КП отдельной строкой
(см. P2 в ARCHITECTURE).

---

## Волна 0 — ограждения

### T00 — Инструменты разработки и CI · S · 1.5 ч
- **Цель:** воспроизводимый прогон тестов и линтера локально и в GitHub Actions.
- **Зависит от:** —
- **Прочитать:** `requirements.txt`, `pytest.ini`, `tests/conftest.py`
- **Можно менять:** `requirements.txt`, `requirements-dev.txt` (новый), `pytest.ini`, `pyproject.toml` (новый), `.github/workflows/ci.yml` (новый)
- **Шаги:**
  1. Вынести `pytest`, `pytest-asyncio` в `requirements-dev.txt`, добавить `ruff`.
  2. `pyproject.toml`: конфиг ruff (правила `E,F,W,I,B,UP`, line-length 120). Существующие нарушения — через `extend-per-file-ignores` для старых файлов, не правкой кода.
  3. Маркеры pytest: `sqlite_only`, `slow`.
  4. CI: Python 3.11, `pip install`, `ruff check`, `pytest -q`.
- **Приёмка:** CI зелёный на PR; `ruff check src tests` → 0 ошибок.
- **Не делать:** переформатировать существующий код.

### T01 — Часы `src/core/clock.py` · M · 2 ч
- **Цель:** всё время идёт через одну точку, тесты управляют «сейчас» (P9).
- **Зависит от:** T00
- **Прочитать:** ARCHITECTURE §4.1, `src/scheduler/scheduler.py`, `src/db/repository.py` (класс `ScheduledActionRepository`), `grep -rn "datetime.now(" src`
- **Можно менять:** `src/core/*` (новый), `src/workflows/*.py`, `src/scheduler/*.py`, `src/db/repository.py`, `src/utils/recurrence.py`, `tests/conftest.py`
- **Контракт:** `now_utc()`, `to_db()`, `from_db()`; фикстура `fake_clock` (`set(dt)`, `advance(**timedelta_kwargs)`).
- **Шаги:** создать модуль и фикстуру; заменить `datetime.now(...)` в перечисленных пакетах; `org_now()` в recurrence строить от `now_utc()`.
- **Приёмка:** `tests/core/test_clock.py::test_fake_clock_controls_now`, `::test_to_db_roundtrip_utc`; в `tests/arch/baseline.json` число `datetime_now_outside_clock` уменьшилось минимум на 15.
- **Не делать:** трогать `src/bot/*` (это T11/T12).

### T02 — Архитектурные тесты и baseline · S · 2 ч
- **Цель:** правила слоёв из ARCHITECTURE §2.2 проверяются автоматически — агент не может ухудшить архитектуру.
- **Зависит от:** T00
- **Можно менять:** `tests/arch/test_boundaries.py`, `tests/arch/baseline.json` (новые)
- **Шаги:** статический анализ `src/` (ast + regex), по правилу на каждое ограничение: `datetime_now_outside_clock`, `telegram_import_outside_adapters`, `httpx_outside_integrations`, `raw_sql_outside_db`, `sqlite_isms`, `workflow_imports_bot`, `domain_impure_imports`, `telegram_id_in_workflows`, `notification_requested_publish`, `file_over_800_lines`. Текущее число нарушений пишется в `baseline.json`. Тест падает, если нарушений стало больше, и просит уменьшить baseline, если меньше (храповик).
- **Ориентир на `master@bc808fe`:** datetime.now — 26, raw SQL вне db — 34, SQLite-измы — 24, workflows→bot — 10, telegram_id в workflows — 79, прямые publish уведомлений — 24.
- **Приёмка:** `pytest tests/arch` зелёный; искусственное нарушение (добавить `datetime.now()` в workflow) роняет тест.

### T03 — Вынос демо/пилота из прод-пути · M · 2.5 ч
- **Цель:** бизнес-функции не живут в `bot/pilot.py`; демо-команды выключаемы (P10).
- **Зависит от:** T00
- **Прочитать:** `grep -n "def \|register_pilot_handlers" src/bot/pilot.py`, `src/api/webhook.py:105-145`, `src/workflows/lesson_ops.py:325-345`, `src/main.py`
- **Можно менять:** `src/bot/pilot.py`, `src/workflows/absence.py`, `src/api/webhook.py`, `src/workflows/lesson_ops.py`, `src/main.py`, `src/config.py`, `.env.example`, тесты
- **Шаги:**
  1. `trigger_absence` перенести в `src/workflows/absence.py` (в `pilot.py` оставить реэкспорт, чтобы тесты не ломались).
  2. Настройка `ALBION_ENABLE_DEV_COMMANDS` (по умолчанию `false`): при `false` не регистрировать `pilot_*`, `mock_*`, `demo_*`, `seed10`, `mh_*` (кроме `/mh_events`), `/kill_switch` оставить.
  3. `seed_demo_data` вызывается только при `ALBION_DEMO_MODE=true` (уже так, проверить).
- **Приёмка:** `tests/test_dev_commands_flag.py::test_dev_commands_hidden_by_default`, `::test_dev_commands_enabled_by_flag`; `baseline.json`: `workflow_imports_bot` уменьшилось.
- **Не делать:** удалять команды (координаторы используют часть `mh_*` при импорте — решим в T25).

---

## Волна 1 — данные

### T04 — Слой БД на SQLAlchemy Core · M · 5 ч
- **Цель:** один переносимый слой доступа к SQLite и Postgres (P7).
- **Зависит от:** T01
- **Прочитать:** ARCHITECTURE §4.2, `src/db/repository.py`, `src/db/migrations.py`, `tests/conftest.py`
- **Можно менять:** `src/db/database.py` (новый), `src/db/repository.py`, `src/config.py`, `requirements.txt` (+`sqlalchemy[asyncio]>=2.0`, `asyncpg`), `tests/conftest.py`
- **Контракт:** класс `Database` и функция `get_db()` из §4.2. `Repository.__init__(db_path)` сохраняет сигнатуру (тесты передают путь) и строит URL `sqlite+aiosqlite:///{path}`.
- **Шаги:**
  1. Реализовать `Database`: при подключении к SQLite выполнять `PRAGMA journal_mode=WAL`, `busy_timeout`, `foreign_keys=ON`.
  2. Переписать `_execute/_fetchone/_fetchall` на `Database`, `?` → `:name`.
  3. Заменить SQLite-измы в `repository.py`: `julianday(x) <= julianday('now')` → `x <= :now` (параметр `to_db(now_utc())`); `datetime('now','-24 hours')` → параметр; `lastrowid` → `RETURNING id`; `INSERT OR REPLACE` → `ON CONFLICT DO UPDATE`.
  4. `find_by_json` пока оставить (удаляется в T06), но изолировать диалект внутри него.
- **Приёмка:** все существующие тесты зелёные; `tests/db/test_database.py::test_insert_returning_id`, `::test_transaction_rollback`; `baseline.json`: `sqlite_isms` ≤ 4 (остаются только `find_by_json` и 2 места в `bot/pilot.py` — их убирает T06).
- **Не делать:** ORM-модели, изменения схемы.

### T05 — Alembic и прогон тестов на Postgres · M · 4 ч
- **Цель:** версионированные миграции; тот же набор тестов проходит на PG.
- **Зависит от:** T04
- **Прочитать:** `src/db/models.py`, `src/db/migrations.py`, `docker-compose.yml`
- **Можно менять:** `alembic.ini`, `src/db/alembic/**` (новый), `src/db/migrations.py`, `src/db/models.py`, `docker-compose.dev.yml` (новый), `tests/conftest.py`, `.github/workflows/ci.yml`, `requirements.txt` (+`alembic`)
- **Шаги:**
  1. Baseline-миграция `0001_initial`: текущая схема, записанная через `op.create_table` с переносимыми типами (`sa.BigInteger` identity, `sa.DateTime(timezone=True)`, `sa.Text`). Мёртвые таблицы и ALTER-миграции из `migrations.py` не переносить.
  2. `init_db(db_path=None)` = `alembic upgrade head` (программно). Сигнатуру сохранить.
  3. `docker-compose.dev.yml`: `postgres:16` с healthcheck.
  4. Фикстура БД: если задан `ALBION_TEST_DATABASE_URL` → PG (схема на каждый тест: `CREATE SCHEMA test_<uuid>` + `search_path`), иначе SQLite во временном файле.
  5. CI: второй job с сервисом postgres, прогон `-m "not sqlite_only"`.
- **Приёмка:** оба CI-job'а зелёные; `tests/db/test_migrations.py::test_upgrade_head_from_empty`, `::test_downgrade_base`.
- **Не делать:** менять бизнес-таблицы (это T07/T08/T17).

### T06 — SQL только в `src/db`, колонки вместо `json_extract` · M · 3.5 ч
- **Цель:** убрать сырой SQL из `bot/`/`workflows/` (34 места, из них 18 в `bot/pilot.py`) и поиск по JSON.
- **Зависит от:** T05
- **Прочитать:** `grep -rn "_execute\|_fetch" src --include=*.py | grep -v src/db/`, `WorkflowRepository.find_by_json`, все вызовы `find_by_json`
- **Можно менять:** `src/db/**`, `src/bot/pilot.py`, `src/bot/handlers.py` (только строки с SQL), `src/workflows/*.py`, новая миграция
- **Шаги:**
  1. Миграция: `workflow_instances` + `class_id TEXT`, `lesson_ref TEXT`, `actor_user_id BIGINT`, `parent_telegram_id TEXT` (временно, удалится в T12a) + индексы; backfill из `data`.
  2. `WorkflowRepository.create(...)` заполняет колонки из `data`; `find_by_json(field, value, ...)` → `find(class_id=..., ...)` с явными полями.
  3. Каждый сырой SQL из `bot/`/`workflows/` → именованный метод репозитория.
- **Приёмка:** `baseline.json`: `raw_sql_outside_db` = 0, `sqlite_isms` = 0; `tests/db/test_workflow_repo.py::test_find_by_class_id_uses_column`.
- **Не делать:** менять поведение команд.

### T07 — `system_settings` и постоянный kill switch · S · 1.5 ч
- **Зависит от:** T05
- **Прочитать:** `src/bot/handlers.py:30-66` (kill switch), `cmd_kill_switch`, `grep -rn kill_switch src tests`
- **Можно менять:** `src/db/**`, `src/services/settings_store.py` (новый), `src/bot/handlers.py` (kill switch), тесты
- **Контракт:** `async get_setting(key, default) / set_setting(key, value)`; кэш в памяти на 5 с.
- **Приёмка:** `tests/services/test_settings_store.py::test_kill_switch_survives_restart` (новый экземпляр читает значение из БД); старые тесты kill switch зелёные.

### T08a — Идентичность: схема и нормализация телефонов · M · 3 ч
- **Цель:** `users` — единая таблица людей; телефон E.164; каналы (P1). Включает PR #9 (`user_channels`).
- **Зависит от:** T05; **смержить или перенести код PR #9** (`src/channels/*`, `UserChannelRepository`)
- **Прочитать:** ARCHITECTURE §3, PR #9 diff, `src/db/models.py` (users, merithub_students, merithub_contacts)
- **Можно менять:** `src/db/**`, `src/domain/phones.py` (новый), `src/channels/*`, `requirements.txt` (+`phonenumbers`)
- **Контракт:** `normalize_phone(raw, default_region="GB") -> str | None`; миграция по §3 (`users` + `guardians` + `user_channels` + индексы).
- **Шаги:** миграция (в PG сделать `telegram_id DROP NOT NULL`; в SQLite — `batch_alter_table`); `UserRepository`: `get_by_phone`, `get_by_channel(channel, address)`, `create_person(role, name, *, telegram_id=None, phone=None, ...)`; `UserChannelRepository` (из PR #9) + `touch_inbound(channel, address, at)`, `opt_out(...)`.
- **Приёмка:** `tests/domain/test_phones.py` (UK `07700 900123` → `+447700900123`, KZ `8 701 …` → `+7701…`, мусор → None, 10 кейсов); `tests/db/test_identity.py::test_user_without_telegram`, `::test_channel_unique_address`.
- **Не делать:** менять места, где создаются пользователи в боте (T08b).

### T08b — Идентичность: backfill и связь MeritHub-таблиц · M · 3 ч
- **Зависит от:** T08a
- **Прочитать:** `MeritHubStudentRepository`, `MeritHubContactRepository`, `MeritHubEnrollmentRepository`, `bot/wizard.py:1000-1030` (создание ученика/тьютора)
- **Можно менять:** `src/db/**`, `src/bot/wizard.py` (только сохранение персоны), `src/bot/roles.py` (регистрация), новая миграция
- **Шаги:**
  1. Миграция: `merithub_students.user_id`, `merithub_contacts.user_id`, `merithub_enrollments.student_user_id`, `parent_user_id` (FK users) + backfill: для каждого контакта/родителя найти или создать `users` по `telegram_id`/телефону, завести `user_channels` (telegram, при наличии телефона — whatsapp **без** preferred).
  2. Визард «новый ученик/тьютор» и `/start`-регистрация пишут в `users` + `user_channels` + связи.
- **Приёмка:** `tests/db/test_identity_backfill.py::test_backfill_links_parent_by_telegram`, `::test_backfill_idempotent`; существующие тесты визарда зелёные.

---

## Волна 2 — сообщения

### T09 — Каталог сообщений · M · 3 ч
- **Зависит от:** T00
- **Прочитать:** ARCHITECTURE §4.3, `src/utils/i18n.py`, `grep -rn '"message": f"' src/workflows`
- **Можно менять:** `src/messages/**` (новый), `src/utils/i18n.py` (фасад)
- **Контракт:** `Action`, `Link`, `OutboundMessage`, `MessageSpec`, `CATALOG`, `render_text`.
- **Шаги:** перенести строки `i18n.T` в `CATALOG`; добавить спецификации для всех текстов workflows (absence, lesson_ops, cancellation) — пока только тексты, вызовы меняются в T12; `tr()` = фасад над каталогом.
- **Приёмка:** `tests/messages/test_catalog.py::test_every_spec_has_ru_and_en`, `::test_placeholders_match_between_languages`, `::test_action_labels_fit_whatsapp_20_chars`, `::test_render_missing_param_raises`.

### T10 — Outbox, `notify()`, delivery worker, TelegramSender · M · 5 ч
- **Цель:** единственная точка отправки (P4). Поведение для пользователя не меняется.
- **Зависит от:** T07, T08a, T09
- **Прочитать:** ARCHITECTURE §4.4–4.5, `bot/handlers.py:1478-1525` (`notif_handler`), PR #9 `channels/telegram.py`, `router.py`, `NotificationRepository`
- **Можно менять:** `src/services/notify.py`, `src/services/delivery.py` (новые), `src/channels/*`, `src/db/**` (миграция outbox по §3), `src/bot/handlers.py` (`notif_handler`), `src/main.py`
- **Шаги:**
  1. Миграция `notifications` (+колонки §3, индекс `(status, next_attempt_at)`).
  2. `notify()` / `notify_role()`; legacy-адаптер: подписчик `NOTIFICATION_REQUESTED` превращает `{telegram_id, message, buttons}` в запись outbox (ключ `legacy.raw`, текст в params) — старые workflows работают без правок.
  3. `DeliveryWorker.run_once(limit=50)`: claim (`FOR UPDATE SKIP LOCKED` в PG, `locked_until` в SQLite) → kill switch → `choose_channel` → sender → статус; ретраи 60/300/1800 с, затем `failed` + событие DLQ.
  4. `TelegramSender.send(address, msg, ctx)`: render, `Action` → callback-кнопка, `Link` → url-кнопка; ошибки `Forbidden`/`chat not found` → `retryable=False`.
- **Приёмка:** `tests/services/test_delivery.py::test_queued_to_sent`, `::test_retry_backoff_then_failed`, `::test_kill_switch_level1_suppresses_non_coordinators`, `::test_non_retryable_error_fails_immediately`, `::test_dedup_key_prevents_duplicate`; все старые тесты, перехватывающие `NOTIFICATION_REQUESTED`, зелёные.
- **Не делать:** WhatsApp; менять тексты.

### T11a — Реестр действий + checkin/resolve · M · 4 ч
- **Цель:** бизнес-логика кнопок не зависит от Telegram (P5).
- **Зависит от:** T10
- **Прочитать:** ARCHITECTURE §4.6, `bot/handlers.py` ветки `resolve:`, `checkin:`, `checkin_late_time:`, `resolve_late_time:` (строки 977–1283)
- **Можно менять:** `src/actions/**` (новый), `src/bot/handlers.py` (эти ветки), тесты
- **Шаги:** `@action(prefix)` + `dispatch()`; перенести 4 ветки в `src/actions/checkin.py`, `src/actions/absence.py`; в `handle_callback` ветка → `result = await dispatch(...)` + отрисовка `ActionResult` (edit/answer). Проверка прав (роль, nonce) — внутри действия.
- **Приёмка:** существующие тесты этих кнопок зелёные без правок; `tests/actions/test_dispatch.py::test_checkin_action_channel_agnostic` (вызов `dispatch` без Telegram-объектов), `::test_unknown_prefix_returns_error_result`.

### T11b — Остальные действия · M · 3 ч
- **Зависит от:** T11a
- **Можно менять:** `src/actions/**`, `src/bot/handlers.py`
- **Шаги:** перенести `cancel_class:`, `cancel_yes:`, `coord_cancel_class:`, `coord_keep_class:`, `coord_resolve:`, `killswitch:`. Остаются в Telegram: `wz:*`, `register_*`, `demo_*`.
- **Приёмка:** старые тесты зелёные; `tests/test_handlers_size.py::test_handle_callback_under_150_lines` (через `inspect.getsource`).

### T12a — Absence workflow: адресация по user_id · M · 3 ч
- **Зависит от:** T10, T08b
- **Прочитать:** `src/workflows/absence.py`, `trigger_absence`, тесты `test_absence_workflow.py`, `test_parent_circuit.py`
- **Можно менять:** `src/workflows/absence.py`, `src/actions/absence.py`, `src/messages/catalog.py`, тесты (только фикстуры создания пользователей)
- **Шаги:** данные workflow хранят `parent_user_id` (не telegram_id); отправка через `notify(... OutboundMessage)`; кнопка «Написать родителю» в карточке координатора = `Link`, который строит helper `contact_link(user_id)` по каналам **родителя**: preferred telegram → `tg://user?id=…`, whatsapp → `https://wa.me/<digits>`, нет каналов → без кнопки.
- **Приёмка:** старые тесты зелёные; `tests/workflows/test_absence_whatsapp_parent.py::test_parent_without_telegram_gets_warning` (родитель только с whatsapp, fake sender получил сообщение с 3 actions).

### T12b — LessonOps: адресация по user_id · M · 4 ч
- **Зависит от:** T12a
- **Прочитать:** `src/workflows/lesson_ops.py` (весь, 920 строк — допускается), тесты `test_round3_flows.py`, `test_r9_fixes.py` (grep по lesson_ops)
- **Можно менять:** `src/workflows/lesson_ops.py`, `src/actions/checkin.py`, `src/messages/catalog.py`, тесты-фикстуры
- **Приёмка:** старые тесты зелёные; `tests/arch`: в `src/workflows/lesson_ops.py` 0 вхождений `telegram_id` (было 40), baseline уменьшен.

### T12c — Cancellation, lead, fallback, notify_all_coordinators · S · 2 ч
- **Зависит от:** T12b
- **Приёмка:** `baseline.json`: `telegram_id_in_workflows` = 0 (кроме legacy-адаптера); удалён legacy-адаптер `NOTIFICATION_REQUESTED`, если на событие не осталось publish'ей.

### T13 — Один процесс: FastAPI + Telegram webhook + циклы · M · 3.5 ч
- **Цель:** события вебхуков доходят до обработчиков (P3).
- **Зависит от:** T10
- **Прочитать:** `src/main.py`, `src/api/webhook.py`, `DEPLOY_VPS.md` §1, §5
- **Можно менять:** `src/app.py` (новый), `src/main.py`, `src/api/*.py`, `tests/test_webhook.py`, новый тест
- **Шаги:**
  1. `src/app.py`: `create_app()`; `lifespan` → `init_db`, `register_all`, PTB `Application.initialize/start` без updater, `set_webhook`, фоновые задачи (scheduler, delivery, wizard expiry, cleanup, planner) с корректной отменой при shutdown.
  2. `POST /tg/webhook`: проверка `X-Telegram-Bot-Api-Secret-Token` → `Update.de_json` → `application.process_update` (в фоне, ответ 200 сразу).
  3. MeritHub-ресивер монтируется как router `/mh/webhook` (старый путь из настройки — alias).
  4. `python -m src.main` (polling) остаётся для dev.
- **Приёмка:** `tests/api/test_single_process.py::test_merithub_classstatus_reaches_lesson_ops` (POST classStatus `lv` через TestClient → `class_live_check` закрыт — сейчас в проде это не работает), `::test_tg_webhook_rejects_bad_secret`, `::test_background_tasks_cancelled_on_shutdown`.

---

## Волна 3 — WhatsApp

### T14 — WhatsApp Cloud API клиент и sender · M · 5 ч
- **Зависит от:** T10 (можно параллельно с T11–T13)
- **Прочитать:** ARCHITECTURE §4.5, §6; `src/integrations/merithub_client.py` (стиль клиента и mock); документация Meta: messages (text, interactive button/list, template)
- **Можно менять:** `src/integrations/whatsapp_client.py`, `src/integrations/whatsapp_fake.py`, `src/channels/whatsapp.py` (новые), `src/config.py`, `.env.example`
- **Контракт:**
  ```python
  class WhatsAppClient:
      async def send_text(self, to: str, body: str, reply_to: str | None = None) -> str   # wamid
      async def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]]) -> str  # (id,title) ≤3
      async def send_list(self, to: str, body: str, button: str, rows: list[tuple[str, str]]) -> str  # ≤10
      async def send_template(self, to: str, name: str, lang: str, body_params: list[str],
                              button_payloads: list[str] = ()) -> str
  class WhatsAppError(Exception): code: int; retryable: bool; outside_window: bool
  ```
  Настройки: `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_WABA_ID`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_API_VERSION=v26.0`. Без токена фабрика возвращает fake.
- **Логика `WhatsAppSender.send`:** `ctx.window_open` → свободный текст/interactive (≤3 actions → кнопки, 4–10 → list, >10 → ошибка каталога); окно закрыто → `spec.wa_template` (нет шаблона → `SendResult(ok=False, retryable=False, error="no_template_outside_window")`); ошибка «вне окна» (код 131047) при свободном тексте → одна повторная попытка шаблоном.
- **Приёмка:** `tests/channels/test_whatsapp_sender.py` — ≥8 кейсов (окно открыто/закрыто, 3/5 кнопок, нет шаблона, 131047-фолбэк, 4xx невалидный номер → не ретраить, 5xx → ретраить, Link → текст со ссылкой); тесты клиента через `httpx.MockTransport` сверяют JSON тела запросов с примерами Meta.

### T15 — WhatsApp вебхук: входящие, кнопки, статусы · M · 5 ч
- **Зависит от:** T11b, T13, T14
- **Прочитать:** ARCHITECTURE §4.6, §5.2; `src/api/webhook.py` (стиль), документация Meta: webhooks messages/statuses
- **Можно менять:** `src/api/whatsapp.py` (новый), `src/adapters/wa_inbound.py` (новый), `src/db/**` (`inbound_messages`), `src/bot/handlers.py` (вынести общую обработку свободного текста в `src/services/inbound_text.py`)
- **Шаги:**
  1. `GET /wa/webhook`: `hub.mode=subscribe` + verify token → вернуть `hub.challenge`.
  2. `POST /wa/webhook`: HMAC SHA-256 `X-Hub-Signature-256` от **сырого** тела по app secret; неверная подпись → 401; ответ 200 быстро, обработка в фоне.
  3. `messages[]`: dedup по `id`; `touch_inbound` (открывает окно); адрес → `user_channels` → `user_id`. Неизвестный номер → вежливый ответ из каталога + карточка координатору «Неизвестный номер пишет в WhatsApp» (кнопка «Привязать» — ссылка на команду `/link_wa <phone>` в Telegram; команду сделать здесь же).
  4. `interactive.button_reply.id` / `interactive.list_reply.id` / `button.payload` (кнопка шаблона) → `actions.dispatch`.
  5. `text.body` → тот же путь, что свободный текст в Telegram (классификатор, пересылка координатору).
  6. `statuses[]` → outbox по `provider_message_id`: `sent/delivered/read/failed` (failed → алерт).
  7. Текст «STOP»/«СТОП» → `opt_out` + подтверждение.
- **Приёмка:** `tests/api/test_whatsapp_webhook.py`: `::test_verify_challenge`, `::test_bad_signature_401`, `::test_button_reply_dispatches_action`, `::test_template_button_payload_dispatches_action`, `::test_duplicate_message_ignored`, `::test_status_delivered_updates_outbox`, `::test_unknown_number_alerts_coordinator`, `::test_stop_opts_out`, `::test_inbound_opens_window` (фикстуры — реальные примеры payload из документации Meta, лежат в `tests/fixtures/wa/`).

### T16 — Реестр шаблонов WhatsApp и пакет для подачи в Meta · S · 3 ч
- **Зависит от:** T14, T12b
- **Можно менять:** `src/messages/catalog.py`, `scripts/wa_templates_export.py` (новый), `docs/whatsapp_templates.md` (генерируется)
- **Шаги:**
  1. Для каждого сообщения, которое инициирует система и которое уходит родителю/тьютору (напоминание, старт-чек, предупреждение о неявке, отмена, перенос, платная отмена, ссылка на урок), заполнить `wa_template` + `wa_params` (категория utility, ru и en).
  2. Скрипт печатает markdown/JSON для подачи: имя, категория, язык, тело с `{{n}}`, примеры значений, кнопки quick reply.
- **Приёмка:** `tests/messages/test_wa_templates.py::test_every_system_initiated_message_has_template`, `::test_template_params_subset_of_placeholders`, `::test_template_names_snake_case_unique`; `docs/whatsapp_templates.md` в PR → **задача H2 для человека**.

---

## Волна 4 — занятия

### T17 — Материализация занятий и планировщик · M · 8 ч (разрешено 2 PR: схема+сервис, затем переключение workflows)
- **Цель:** напоминания и чеки для **каждого** занятия серии (P2, P6).
- **Зависит от:** T12b, T13
- **Прочитать:** ARCHITECTURE §3 (`lessons`), §4.7–4.8; `src/utils/recurrence.py`; `bot/wizard.py:545-680` (`_sched_submit`); `LessonOpsWorkflow.schedule_class_coordination`; `api/webhook.py` dispatch'и
- **Можно менять:** `src/domain/recurrence.py`, `src/domain/intervals.py`, `src/services/schedule.py` (новые), `src/utils/recurrence.py` (реэкспорт), `src/db/**`, `src/workflows/lesson_ops.py`, `src/bot/wizard.py` (только вызов после создания), `src/api/webhook.py`, `src/app.py`
- **Шаги:**
  1. Миграция `lessons`, `lesson_participants`; `scheduled_actions.lesson_id`.
  2. `expand_series` (чистая функция; DST-безопасно: локальное время → UTC для каждой даты).
  3. `materialize_class(class_id, until)` — идемпотентно по `(class_id, original_start_utc)`; учитывает `end_date` серии.
  4. `schedule_lesson_actions(lesson_id)` — бывший `schedule_class_coordination`, но с `lesson_id`; данные для текста читаются в момент срабатывания.
  5. `run_planner(horizon)` — ежедневно в 03:00 (org tz) + сразу после создания класса.
  6. Вебхуки MeritHub: `classId`(+`startTime`) → `lessons` (±3 ч); статусы `lv`/`cp` обновляют `lessons.status`.
  7. Backfill: существующие `merithub_classes` материализуются при первом запуске планировщика.
- **Приёмка:** `tests/domain/test_recurrence.py::test_expand_series_dst_london` (16:00 London = 15:00 UTC в октябре до перехода и 16:00 UTC после), `::test_expand_series_respects_end_date`; `tests/services/test_planner.py::test_second_week_of_series_gets_reminders` (fake_clock +7 дней → напоминание для 2-го занятия отправлено), `::test_planner_idempotent`, `::test_webhook_lv_maps_to_lesson`.

### T18 — Контроль пересечений у тьютора · M · 4 ч
- **Зависит от:** T17 (для материализованных занятий); сама функция `find_conflicts` — после T01
- **Прочитать:** ARCHITECTURE §4.7, §5.4; `bot/wizard.py` `_sched_find_duplicate`, `_sched_preview`
- **Можно менять:** `src/domain/intervals.py`, `src/services/schedule.py`, `src/bot/wizard.py` (превью и submit), `src/messages/catalog.py`, `src/config.py`
- **Шаги:** `find_conflicts` (полуоткрытые интервалы: занятие 16:00–17:00 и 17:00–18:00 **не** пересекаются при buffer=0); `tutor_conflicts` — кандидаты `expand_series` на `ALBION_OVERLAP_HORIZON_WEEKS` против `lessons` тьютора (status ∈ scheduled) + неразвёрнутого хвоста серий через `expand_series`; визард: при конфликте превью показывает до 5 конфликтов (дата/время/кто), кнопки «Создать» нет, есть «Изменить время»; повторная проверка в `submit` (гонки).
- **Приёмка:** `tests/domain/test_intervals.py` (≥10 кейсов: касание, вложение, серия×разовое, разные дни, DST-граница, buffer); `tests/test_wizard_schedule.py::test_overlap_blocks_creation`, `::test_adjacent_lessons_allowed`, `::test_overlap_rechecked_on_submit`.

### T19 — Политика переносов и отмен (чистая функция) · S · 2 ч
- **Зависит от:** T01
- **Прочитать:** ARCHITECTURE §4.7 (`change_policy`), DECISIONS_NEEDED Q1–Q5, Q17
- **Можно менять:** `src/domain/change_policy.py` (новый), `src/config.py`, `.env.example`
- **Приёмка:** `tests/domain/test_change_policy.py` — табличный тест ≥12 строк: родитель за 30 ч → free; за 23.9 ч → paid; тьютор за 2 ч → free; после начала → too_late; занятие уже cancelled → not_allowed; граница ровно 24 ч → free; перенос по тем же правилам; `min_notice_hours_reschedule`.

### T20a — Заявки на отмену (родитель/тьютор) с платным окном · M · 5 ч
- **Зависит от:** T17, T19, T11b
- **Прочитать:** ARCHITECTURE §5.3; `src/workflows/cancellation.py`; `src/actions/*cancel*`; `cmd_cancel_lesson`
- **Можно менять:** `src/workflows/change_requests.py` (новый), `src/workflows/cancellation.py`, `src/actions/change.py` (новый), `src/bot/handlers.py` (`cmd_cancel_lesson`), `src/db/**`, `src/messages/catalog.py`
- **Шаги:** выбор занятия из `lessons` (ближайшие 14 дней, до 10 → WA list); `evaluate_change`; free → подтверждение → применить; paid → предупреждение + [Подтвердить отмену] [Не отменять] → `cancelled_paid`; `reschedule_lesson_actions` снимает напоминания; уведомления тьютору/родителям/координатору (карточка + [Снять оплату]); истечение подтверждения через 30 мин (`expired`).
- **Приёмка:** `tests/workflows/test_change_requests.py::test_parent_free_cancel`, `::test_parent_paid_cancel_requires_confirm`, `::test_paid_cancel_confirm_marks_lesson_and_removes_reminders`, `::test_coordinator_waives_fee`, `::test_confirm_expires`, `::test_whatsapp_parent_flow_via_dispatch` (всё через `dispatch`, без Telegram).

### T20b — Перенос занятия · M · 5 ч
- **Зависит от:** T20a, T18
- **Шаги:** заявка «Перенести» (родитель/тьютор) + пожелание текстом → карточка координатору с [Назначить время]; визард координатора (дата → время) с `tutor_conflicts(exclude_lesson_ids={L})`; применить: `lessons.start_utc` = новое время, `status` остаётся `scheduled`, создать oneTime-класс в MeritHub (`room_class_id`), зачислить участников, разослать новую ссылку; `reschedule_lesson_actions`; платность по `evaluate_change` (как в T20a). Поведение в MeritHub для исходного слота серии — по H3/Q11.
- **Приёмка:** `::test_reschedule_moves_reminders`, `::test_reschedule_blocked_by_conflict`, `::test_reschedule_creates_room_and_sends_links` (fake MeritHub), `::test_late_reschedule_is_paid`.

### T21 — Автоматические предупреждения · S · 2 ч
- **Зависит от:** T20a
- **Шаги:** в напоминание, если до занятия > `free_hours`, добавить строку «Бесплатно отменить/перенести можно до {deadline}» (в часовом поясе получателя); в ответ на запрос в платном окне — предупреждение с точной суммой часов. Отдельных сообщений не шлём (Q14).
- **Приёмка:** `tests/workflows/test_change_warnings.py::test_reminder_contains_free_deadline_in_user_tz`, `::test_no_deadline_line_inside_paid_window`.

---

## Волна 5 — production

### T22 — Мониторинг и алерты · M · 4 ч
- **Зависит от:** T13
- **Можно менять:** `src/api/ops.py` (новый), `src/services/monitoring.py` (новый), `src/app.py`, `requirements.txt` (+`prometheus-client`)
- **Шаги:** `/health` (БД `SELECT 1`, возраст последнего тика планировщика < 2 мин, самый старый queued в outbox < 5 мин → 200/503); `/metrics` (только с `127.0.0.1` или по токену); heartbeat GET на `HEALTHCHECK_PING_URL` каждую минуту; алерты админам (Telegram) с дебаунсом 30 мин: DLQ вырос, outbox failed > 5/час, WA-вебхук с неверной подписью > 10/час, планировщик стоит.
- **Приёмка:** `tests/api/test_ops.py::test_health_503_when_scheduler_stale`, `::test_metrics_requires_token`, `tests/services/test_monitoring.py::test_alert_debounced`.

### T23 — Деплой: Docker Compose + Caddy · M · 4 ч
- **Зависит от:** T13, T05
- **Можно менять:** `Dockerfile`, `docker-compose.yml`, `deploy/**` (новый), `DEPLOY.md` (новый; `DEPLOY_VPS.md` пометить устаревшим)
- **Шаги:** multi-stage Dockerfile (non-root, `python:3.11-slim`, healthcheck); compose: `app` (uvicorn, 1 воркер), `postgres:16` (volume, healthcheck), `caddy` (TLS, пути `/tg/webhook`, `/wa/webhook`, `/mh/webhook`, `/health`; `/metrics` не наружу); `migrate` one-shot (`alembic upgrade head`) перед `app`; `.env.production.example`; runbook: первичная установка, обновление (`pull → migrate → up -d`), откат, ротация логов.
- **Приёмка:** `docker compose config` валиден (проверка в CI); смоук в CI: `docker compose up -d postgres app` + `curl /health` = 200 (с fake-каналами).

### T24 — Бэкапы и проверка восстановления · S · 2 ч
- **Зависит от:** T23
- **Шаги:** сервис `backup` в compose (cron: `pg_dump -Fc` → `rclone`/`aws s3 cp` в S3-совместимое хранилище, ротация 14 дней + 8 недель); `scripts/restore_check.sh` (поднимает временный PG, восстанавливает последний дамп, проверяет число строк ключевых таблиц); раздел runbook.
- **Приёмка:** CI-job: backup → restore_check на тестовой БД зелёный.

### T25 — Импорт реальных данных · M · 5 ч
- **Зависит от:** T08b, T17; **вход от человека:** H4 (выгрузка клиента)
- **Можно менять:** `scripts/import_data.py`, `src/services/importer.py`, `docs/import_templates/*.csv` (новые)
- **Шаги:** шаблоны CSV (`tutors.csv`, `students.csv`, `parents.csv`, `classes.csv`, `enrollments.csv`) с описанием колонок; `--dry-run` по умолчанию: валидация (телефоны через `normalize_phone`, таймзоны, дни недели, существование ссылок), отчёт (создано/обновлено/ошибки/нет каналов/**пересечения у тьюторов**); `--apply` в одной транзакции; идемпотентно (повторный запуск ничего не дублирует); после применения — `run_planner`.
- **Приёмка:** `tests/services/test_importer.py::test_dry_run_reports_invalid_phone`, `::test_apply_idempotent`, `::test_import_detects_tutor_overlap`, `::test_import_links_parent_channels`.

### T26 — Персональные данные в логах и хранение · S · 2 ч
- **Зависит от:** T15
- **Шаги:** logging-фильтр, маскирующий E.164, email, TG-токены; очистка `webhook_events`/`inbound_messages` старше 30 дней (`ALBION_RAW_RETENTION_DAYS`) и outbox старше 180 дней; payload'ы в логах — только на уровне DEBUG.
- **Приёмка:** `tests/utils/test_log_masking.py::test_phone_masked`, `::test_email_masked`; `tests/services/test_retention.py::test_old_raw_webhooks_deleted`.

### T27 — Нагрузочный прогон «100+ тьюторов» · S · 3 ч
- **Зависит от:** T17, T10
- **Шаги:** `scripts/load_seed.py`: 120 тьюторов, 600 учеников, 1500 занятий в неделю (70% perma); тест с `fake_clock`, прогоняющий неделю тиками по 30 с (планировщик + delivery с fake-каналами).
- **Приёмка:** `tests/load/test_week_simulation.py` (маркер `slow`, в CI — ночной job): все напоминания отправлены вовремя (±1 тик), p95 тика < 500 мс на PG, 0 записей в DLQ; вывод записывается в `docs/phase1/LOAD_REPORT.md`.

### T28 — Документация для координаторов и сценарий приёмки · S · 3 ч
- **Зависит от:** все
- **Шаги:** обновить `USER_MANUAL.md` (WhatsApp у родителей, отмены/переносы, пересечения, привязка номера); `docs/phase1/UAT.md` — чек-лист приёмки (25–30 шагов с ожидаемым результатом) для сессии с координаторами; архивировать устаревшие документы MVP в `docs/archive/`.
- **Приёмка:** ревью человеком; каждый шаг UAT ссылается на тест или ручную проверку.

---

## Как запускать агента на задачу (шаблон промпта)

```
Ты работаешь в репозитории ALBION. Выполни задачу {Txx} из docs/phase1/BACKLOG.md.
Перед началом прочитай AGENTS.md, docs/phase1/ARCHITECTURE.md (разделы {§…}),
docs/phase1/DECISIONS_NEEDED.md и файлы из поля «Прочитать».
Сначала напиши план (≤10 пунктов) и список файлов, которые изменишь; жди подтверждения.
Затем реализуй: сначала тесты из «Приёмки» (красные), потом код (зелёные).
Перед PR: python -m pytest -q, ruff check src tests, проверь tests/arch.
Опиши PR по шаблону из AGENTS.md. Не выходи за рамки карточки.
```

## Чек-лист ревью для человека (5–10 минут на PR)
1. Дифф в пределах «Можно менять»? Размер ≤ M?
2. Тесты из «Приёмки» есть **и проверяют суть** (не `assert True`, не мок самого тестируемого)?
3. Нет ли удалённых или ослабленных старых тестов (`git diff --stat tests/`)?
4. `baseline.json` не вырос?
5. Бизнес-правила — из `DECISIONS_NEEDED` или вынесены в настройки?
6. Время — через clock, SQL — в `src/db`, тексты — в каталоге?
7. Для задач с внешним API: тело запроса сверено с документацией (фикстуры с примерами Meta/MeritHub)?
