# 🗺️ MASTER_PLAN v3 — тотальный аудит ALBION MVP

> Создан: 2026-07-30
>
> Базовый commit: `32dbbcb` (`master`, синхронизирован с рабочей веткой)
>
> Статус: **ШАГ 1 завершён — аудит и планирование; реализация ожидает команды «ВНИЗ»**
>
> Этот файл — единственный источник истины для аудита. Старые Round 1/2 сохранены в истории Git.

---

## 1. Правила выполнения

1. Работа идёт строго сверху вниз: берётся первая задача `- [ ]`, меняется на `- [~]`, после доказанной проверки — на `- [x]`.
2. Одна атомарная задача = один самостоятельный commit. Нельзя смешивать рефакторинг, функциональность и документацию без необходимости.
3. Перед рискованным рефакторингом сначала фиксируется текущее ожидаемое поведение тестом.
4. Задача считается выполненной только после:
   - unit/integration regression-тестов;
   - релевантного E2E-сценария;
   - проверки логов приложения и browser/terminal console;
   - полного `pytest`, `ruff`, `bandit`, `pip-audit` либо явно зафиксированного временного исключения;
   - `git diff --check` и чистого рабочего дерева после commit.
5. В репозитории **нет обычного web frontend**. Пользовательский UI — Telegram, HTTP-интерфейс — FastAPI webhook. Поэтому E2E состоит из:
   - Playwright smoke для реально существующего FastAPI `/health` и webhook HTTP-контура;
   - application-level Telegram E2E через `python-telegram-bot` Update + fake Bot API transport;
   - реальный MeritHub staging smoke выполняется только владельцем без передачи секретов в чат.
6. Если задача требует решения из секции «🛑 Вмешательство человека», она помечается blocked и работа продолжается со следующей безопасной задачей.

---

## 2. Базовая диагностика

### Что есть в продукте

- Python 3.11+, Telegram UI, FastAPI receiver для MeritHub, SQLite scheduler/outbox-like notifications.
- Отдельного browser UI/frontend нет; «UI/Backend sync» означает прежде всего Telegram-команды, inline-кнопки, роли и ответы backend.
- Real MeritHub существует; Airtable, cancellation data и часть финансовой логики остаются mock.

### Автоматические проверки на старте аудита

| Проверка | Результат |
|---|---:|
| `pytest` | **95 passed** |
| Coverage | **56% overall** |
| `compileall` | OK |
| Ruff | **147 ошибок/предупреждений** |
| Bandit | **6 medium + 11 low** |
| pip-audit | **11 известных уязвимостей** (`python-dotenv`, `pytest`, транзитивный `starlette`) |
| CI | отсутствует |
| Playwright/E2E | отсутствует |

### Главные выводы

- Зелёные 95 тестов не означают production-ready: `main.py`, logging, classifier и DLQ имеют 0% coverage; Telegram handlers — 21%, lesson ops — 30%.
- Обнаружены реальные P0-риски: самостоятельное назначение coordinator, неавторизованные `/absent` и `/ok`, forged callbacks, повторная обработка attendance webhook, молчаливое завершение неизвестных scheduler actions, broken Telegram webhook mode, несогласованное создание схемы БД, destructive demo-команды в real-режиме.
- Документация расходится с кодом и сама с собой: 75/94/95 тестов, заявленные команды отсутствуют, mock-компоненты описаны как реальные.

---

## 3. Очередь работ

## 🔴 P0 — безопасность, потеря/искажение данных, неработающие critical flows

- [ ] **P0.0 — Создать воспроизводимый QA/E2E safety net** (`pyproject.toml`, `requirements-dev.txt`, `tests/e2e/`, `.github/workflows/ci.yml`): разделить runtime/dev зависимости; добавить Ruff/Bandit/pip-audit/coverage config; сделать Telegram Update harness; поднять FastAPI в тесте и добавить Playwright smoke для `/health` и webhook; зафиксировать текущие critical flows до рефакторинга. Gate: 95 старых тестов + новые E2E, без реальных Telegram/MeritHub вызовов.

- [ ] **P0.1 — Закрыть privilege escalation в ролях** (`src/bot/handlers.py`, `src/bot/roles.py`, `tests/test_roles.py`, E2E Telegram): запретить self-registration/self-change в `coordinator` и `tutor` вне demo policy; оставить назначение privileged roles только admin-команде `/role`; скрыть недоступные кнопки; ввести единые helpers `require_admin/require_role/can_manage_ops`. Проверка: обычный пользователь не становится coordinator и не получает PII-алерты.

- [ ] **P0.2 — Авторизация команд и ownership callback** (`src/bot/handlers.py`, `src/workflows/absence.py`, `src/workflows/lesson_ops.py`, `src/db/repository.py`): `/absent` разрешить только назначенному tutor/admin, `/ok` — owner parent/coordinator/admin; для `resolve:` и `checkin:` сверять `query.from_user.id` с actor/parent из workflow; валидировать allowlist actions; заменить race `exists→save` атомарным idempotency claim. E2E: чужой аккаунт не может закрыть инцидент/подтвердить чужой урок, двойной callback создаёт один side effect.

- [ ] **P0.3 — Сделать attendance webhook идемпотентным и наблюдаемым** (`src/api/webhook.py`, `src/db/repository.py`, `tests/test_webhook.py`, Playwright/API E2E): вычислять стабильный event key (vendor id либо hash нормализованного payload), атомарно claim-ить событие до dispatch, не создавать повторные incidents/workflows; на dispatch failure не отвечать ложным `200 ok` без durable retry; скрывать `authorization`, API key и signature headers при сохранении; ограничить размер body и отклонять невалидный JSON. Проверка: 2 одинаковых/concurrent attendance POST → один incident.

- [ ] **P0.4 — Исправить scheduler routing и crash recovery** (`src/scheduler/scheduler.py`, `src/workflows/absence.py`, `src/workflows/lesson_ops.py`, `src/events/bus.py`, `src/db/repository.py`): обработчик должен явно сообщать «action handled»; неизвестная action не может считаться `done`; убрать fan-out, где все scheduler handlers получают все actions; third-attempt crash переводить из зависшего `running` в `failed`; добавить `ORDER BY execute_at`; cleanup task не должен размножаться при restart. E2E: typo action → failed/DLQ, known action → ровно один handler, restart → корректный retry.

- [ ] **P0.5 — Исправить инициализацию и конкурентный доступ к SQLite** (`src/config.py`, `src/db/migrations.py`, `src/db/repository.py`, `src/main.py`, `src/api/webhook.py`): `init_db` обязан использовать `settings.database_path`, включая абсолютные пути; каждый connection получает `busy_timeout`, `foreign_keys=ON`, row factory и согласованные PRAGMA; миграционные ошибки логируются и останавливают startup, а не проглатываются; добавить тест custom `DATABASE_URL`. Проверка: bot/webhook используют одну БД и не создают скрытый `./albion.db`.

- [ ] **P0.6 — Довести MeritHub transport/auth state machine** (`src/integrations/merithub_client.py`, `src/integrations/factory.py`, `tests/test_integrations_factory.py`): убрать утечку первых символов token в DEBUG; хранить auth **mode**, не старый token; после raw+Bearer 401 обязательно refresh один раз и затем raise `MeritHubError`, никогда не возвращать `{}` как успех; не затирать token, уже обновлённый concurrent coroutine; нормализовать prefixed Bearer; покрыть raw/Bearer/expiry/concurrency/401/409/list-response тестами. Проверка: `tokenNotFound` имеет детерминированный retry и честную ошибку.

- [ ] **P0.7 — Валидация и безопасная оркестрация `/mh_schedule`** (`src/bot/pilot.py`, `src/integrations/merithub_client.py`, `src/db/repository.py`, `tests/test_pilot.py`): до remote mutation проверить RFC3339 aware datetime, future time, duration bounds, tutor, всех students и parent mappings; не создавать пустой/частичный класс при missing students; различать validation и MeritHub errors; не считать real-mode успешным, если API не вернул настоящий ID/links; сделать retry/resume для 409 без дублирования класса. E2E: invalid input не вызывает API, retry после partial failure восстанавливает одну запись.

- [ ] **P0.8 — Оградить destructive demo-инструменты** (`src/bot/pilot.py`, `src/bot/handlers.py`, tests): `/seed10`, `/demo_reset`, demo callbacks и seed доступны только при `ALBION_DEMO_MODE=true` плюс admin; в real MeritHub seed не должен подменять ошибки fake `mh_*` IDs или перезаписывать реальные IDs; reset выполняется одной локальной транзакцией и не затрагивает production data. Проверка: demo=false → ноль DB/API mutations.

- [ ] **P0.9 — Починить реальный ручной absence flow и дедупликацию** (`src/bot/handlers.py`, `src/bot/pilot.py`, `src/workflows/absence.py`, repositories): `/absent <classId>` должен работать для локально сохранённых MeritHub classes/enrollments, а не искать real ID в mock Airtable; проверять assignment tutor→class; одинаковый source/class/student не создаёт параллельные активные incidents; команда отвечает success только если handler действительно создал incident. E2E: real-like class → уведомление правильному parent; unknown class → честная ошибка.

- [ ] **P0.10 — Исправить class-live workflow** (`src/workflows/lesson_ops.py`, `src/bot/handlers.py`, tests): ответ tutor `class_started` не должен отменять `class_live_check`; сделать live-check отдельным workflow/action либо выборочную отмену; проверка должна учитывать terminal valid statuses (`lv`, при допустимом опоздании `cp`) и контекст tutor response; создавать monitor даже без tutor Telegram. E2E: tutor подтвердил старт, но webhook `lv` не пришёл → coordinator alert; `lv` пришёл → alert отсутствует.

- [ ] **P0.11 — Устранить vulnerable dependency set** (`requirements.txt`, новый `requirements-dev.txt`, Dockerfile): обновить FastAPI/Starlette/python-dotenv/pytest и остальные библиотеки до совместимых безопасных версий, убрать pytest/неиспользуемый APScheduler из production image, зафиксировать hashes/lock strategy. Gate: `pip-audit` без известных runtime CVE, полный E2E на Python 3.11 и Docker target.

- [ ] **P0.12 — Убрать ложный production Telegram webhook mode** (`src/main.py`, `src/api/`, deployment docs/tests): сейчас `app.start()+set_webhook()` не поднимает HTTP endpoint для Telegram updates. После решения из human section либо реализовать единый FastAPI Telegram route с secret validation/update_queue, либо удалить `--webhook` и официально оставить polling. E2E: synthetic signed Telegram webhook реально достигает command handler либо режим недоступен и не рекламируется.

## 🟠 P1 — correctness основных сценариев и продуктовые миссматчи

- [ ] **P1.1 — Сделать kill switch безопасным и честным** (`src/bot/handlers.py`, notifications/workflows, config): состояние переживает restart; blocked notification получает статус `blocked`, а не остаётся `queued`; parent-block не превращается в ложную «не ответил» escalation; `/status` показывает effective mode. Нужна выбранная persistence policy из human section.

- [ ] **P1.2 — Реализовать работающий cancellation/reschedule UI→backend** (`src/workflows/cancellation.py`, `src/bot/handlers.py`, integrations): сейчас AI предлагает несуществующий `/cancel_lesson`, workflow всегда использует mock, tutor lookup ошибочно считает client tutor id Telegram ID. Добавить команды/confirm UI, factory integration, корректный mapping tutor/contact, incident/audit trail; либо удалить обещание reschedule до реализации. E2E Telegram: cancel command изменяет один lesson и уведомляет нужных участников.

- [ ] **P1.3 — Валидировать LLM output и исправить degraded UX** (`src/ai/client.py`, `src/ai/classifier.py`, `src/bot/handlers.py`): Pydantic schemas/allowlists/confidence threshold, JSON из code fences, ограничение prompt injection, circuit/degraded status; real API error не должен незаметно называться real AI; mock не классифицирует каждое бытовое сообщение как lead; пользователь получает финальный ответ вместо вечного «Обрабатываю...». E2E: malformed LLM response не падает и не создаёт ложную заявку.

- [ ] **P1.4 — Исправить timezone/date semantics** (`src/workflows/lesson_ops.py`, `src/bot/pilot.py`, config/tests): reject naive start, нормализовать instant в canonical timezone, исправить offset calculation (сейчас subtraction одинакового instant всегда даёт 0), `/today` и `/morning` определяют London-day через ZoneInfo, а не server local/string prefix; выводят London + user local. Проверка на DST и границу суток Minsk/London/Almaty.

- [ ] **P1.5 — Синхронизировать contacts с routing уведомлений** (`src/bot/pilot.py`, repositories): `/mh_contact ... tg` для student должен обновлять фактический `parent_telegram_id` и User mapping либо UI должен явно разделить learner/parent/tutor contacts; валидировать Telegram ID/email/phone/timezone; исключить orphan contacts. E2E: после одной документированной команды parent действительно получает reminder.

- [ ] **P1.6 — Исправить remote/local consistency MeritHub CRUD** (`src/bot/pilot.py`, repositories/integration service): parent role сейчас меняется до remote create, а «atomic» delete состоит из remote delete + трёх локальных commits; добавить transaction boundary, compensation или durable integration operation journal после решения human section; real API без ID считается failure. Проверка fault injection на каждом шаге без fake IDs и полусостояний.

- [ ] **P1.7 — Починить notification delivery semantics** (`src/bot/handlers.py`, `src/events/bus.py`, notifications schema/repo): согласовать `queued/sending/sent/failed/blocked`, сохранять error, исключить двойную отправку при timeout/cancel, не помещать network retry целиком под 10s EventBus timeout; метрики delivered/failed должны соответствовать факту. E2E fake Bot API: timeout после send не вызывает бесконтрольный duplicate.

- [ ] **P1.8 — Убрать хрупкие JSON `LIKE` queries** (`src/workflows/absence.py`, `src/workflows/lesson_ops.py`, repositories): pattern incident `1` может совпасть с `10`; заменить на `json_extract`/явные relation columns согласно human decision, добавить repository methods и индексы; сохранять workflow data при complete/fail вместо overwrite. Regression: incident 1 и 10, несколько check-in одного actor.

- [ ] **P1.9 — Устранить N+1 и private repository leakage** (`src/bot/pilot.py`, `src/bot/handlers.py`, repositories): `/mh_students`, `/today`, `/morning` делают per-row queries; добавить JOIN/aggregate query APIs, pagination/chunking и реальные row counts (`cancel_by_workflow` сейчас всегда 0); handlers больше не вызывают `_fetch*`/`_execute` напрямую.

- [ ] **P1.10 — Telegram UX/formatting/error boundary** (`src/bot/handlers.py`, `src/bot/pilot.py`, `src/bot/roles.py`): перейти с небезопасного dynamic Markdown на escaped HTML/plain text; chunk сообщений >4096; один раз отвечать callback query; добавить `Application.add_error_handler` с correlation id; тексты «координатор уведомлён» показывать только при delivered/queued recipient count; не логировать PII message body по умолчанию.

- [ ] **P1.11 — Webhook operations: retention, replay, readiness** (`src/api/webhook.py`, repositories, bot commands): bounded retention webhook events/DLQ, processing status и admin replay для failed event, readiness проверяет DB/schema, `/mh_events` не показывает secrets и различает unsigned/valid/invalid вместо misleading `signature_ok=1`.

- [ ] **P1.12 — Lifecycle и resource cleanup** (`src/main.py`, `src/ai/client.py`, scheduler/logging): закрывать shared `httpx.AsyncClient`, корректно cancel/await background tasks, не терять pending Telegram updates по умолчанию, не дублировать bus/log handlers при повторном startup, graceful shutdown polling/web server.

- [ ] **P1.13 — Привести imports TSV к обещанному UX** (`src/bot/pilot.py`): либо поддержать Telegram document upload, либо убрать это из инструкций; header detection вместо слепого `lines[1:]`; transaction, row-level validation/error report, timezone/email checks, проверка learner existence и защита от частичного импорта. E2E: файл с valid+invalid rows даёт точный итог.

## 🟡 P2 — завершение продукта, cleanup и поддерживаемость

- [ ] **P2.1 — Разобрать недовоплощённый MeritHub class management** (`merithub_client.py`, bot UI, DB): `edit_class`, `remove_users_from_class`, `delete_class` существуют только в backend. После решения human section либо добавить admin-команды с preview/confirm/audit, либо удалить неподдерживаемый контракт и документацию.

- [ ] **P2.2 — Утренняя сводка как реальная функция** (`src/bot/pilot.py`, scheduler/config): `/morning` сейчас только ручная команда, хотя описана как автоматическая. После выбора времени/таймзоны добавить daily scheduling с дедупликацией и recipient policy либо честно переименовать в manual report.

- [ ] **P2.3 — Удалить/реализовать мёртвые модели и события** (`src/db/models.py`, `src/events/types.py`, mocks/docs): `conversations`, ряд полей notifications/incidents/leads, payment events, lesson started/completed/rescheduled, `_demo_resolved`, unreachable `role_*` callbacks, mock balance и часть class APIs не участвуют в runtime. Удаление DB объектов — только после human approval; остальное удалить или покрыть реальным flow.

- [ ] **P2.4 — Repository/domain refactor** (`src/db/repository.py`, services): типизированные DTO/enums для roles/status/action, explicit repository APIs, shared transaction helper, constraints/validation, стабильные IDs; убрать generic `IncidentRepository.create(**kw)` и raw SQL из UI слоя.

- [ ] **P2.5 — Logging/observability cleanup** (`src/utils/logging.py`, all modules): исправить `+00:00Z`, duplicate timezone import, duplicate root handlers; structured context (`event_id`, `workflow_id`, `class_id`) без secrets/PII; заменить silent broad exceptions на точечные и наблюдаемые fallback.

- [ ] **P2.6 — Полный quality cleanup** (`src/`, `tests/`): довести Ruff до 0, Bandit medium/high до 0, удалить unused imports/variables, форматировать код, добавить typing gate; coverage ≥85% overall и ≥90% для auth/webhook/scheduler/MeritHub/workflows/handlers.

- [ ] **P2.7 — Deployment hardening** (`Dockerfile`, `docker-compose.yml`, scripts): единая Python version, non-root image, healthcheck, read-only code, `/data` volume вместо file mounts, отдельный webhook service при выбранной архитектуре, startup validation env, корректная Windows/Linux token check, backup/restore SQLite runbook.

- [ ] **P2.8 — Синхронизировать всю документацию с кодом** (`README.md`, `ARCHITECTURE.md`, `ALBION_GUIDE.md`, `PILOT.md`, `DEMO_RUNBOOK.md`, `ALBION_CONTEXT.md`): одна матрица команд/permissions/mock-vs-real; актуальные test/coverage badges; убрать заявления о несуществующем Xero/реальном Airtable/автоматических функциях; обновить troubleshooting и production security.

- [ ] **P2.9 — Финальный release gate 9.5/10** (весь repo): clean clone → install → migrations → unit/integration → Telegram E2E → Playwright/FastAPI E2E → Docker smoke → restart/recovery → security scans; ноль незаблокированных P0/P1, чистый Git, changelog и итоговый архитектурный отчёт.

---

## 4. 🛑 ТРЕБУЕТСЯ ВМЕШАТЕЛЬСТВО ЧЕЛОВЕКА

Эти решения не блокируют старт безопасных задач P0.0–P0.11; при достижении зависимой задачи она будет помечена blocked.

1. **Webhook trust policy MeritHub.** Сейчас при заданном secret запрос **без auth header всё равно принимается**, потому что документация MeritHub не гарантирует подпись. Нужно выбрать:
   - строгая подпись/token (предпочтительно, если MeritHub реально умеет);
   - explicit `ALLOW_UNSIGNED=true` + непредсказуемый path + reverse-proxy rate/IP policy;
   - оставить public unsigned (не рекомендуется: forged attendance создаёт инциденты).

2. **Telegram production transport.** Нужен ли реальный Telegram webhook в этом MVP? Если да — объединяем/разделяем FastAPI routes и настраиваем публичный ingress; если нет — удаляем сломанный `--webhook`, оставляем polling.

3. **DB migration vNext.** Для качественной модели желательно изменить схему:
   - `incidents.student_id/tutor_id` из INTEGER в TEXT;
   - отдельные `escalated_at/status_changed_at` вместо использования `resolved_at` для escalation;
   - relation columns workflow↔incident/class/actor вместо JSON LIKE;
   - notification `error/blocked/sending`;
   - integration operation journal;
   - enrollment unique room link.
   Нужны backup policy и подтверждение миграции существующей production SQLite.

4. **MeritHub links contract.** API возвращает unique instructor `hostLink`, commonHost/commonParticipant links и unique links после enrollment. Сейчас одна колонка `host_link` смешивает смыслы, student links теряются и никому не показываются. Подтвердить, должны ли ALBION хранить и рассылать индивидуальные join URLs.

5. **Role model.** Достаточна ли одна mutable роль на Telegram account, или один человек может одновременно быть admin+tutor+parent/coordinator? Для production предпочтительны capabilities/many-to-many; это DB/API change.

6. **Canonical scheduling timezone.** Документы говорят Europe/London, примеры передают `+03:00`. Подтвердить: ввод всегда London local, любой RFC3339 instant с нормализацией в London либо timezone tutor.

7. **Remote/local consistency policy.** Для необратимых MeritHub create/delete выбрать: durable operation journal/outbox (надёжнее) либо best-effort compensation (проще для MVP).

8. **Автоматическая morning digest policy.** Нужны London time отправки, дни недели и recipients; иначе функция остаётся ручной.

9. **Удаление legacy DB объектов.** Подтвердить, что `conversations` и неиспользуемые payment/event/data поля не нужны ближайшему roadmap, прежде чем удалять schema/API.

10. **Real staging verification.** Для финального MeritHub E2E владелец должен сам запустить smoke с credentials в локальном `.env`/CI secret store. Секреты не отправлять в чат и не коммитить.

---

## 5. Definition of Done: качество 9.5/10

- Все незаблокированные P0/P1 закрыты отдельными проверенными commits.
- `pytest` 100% green; coverage ≥85% overall, critical modules ≥90%.
- Telegram E2E и Playwright/FastAPI E2E green, browser/terminal console без новых errors.
- Ruff = 0; Bandit medium/high = 0; pip-audit runtime = 0 known vulnerabilities.
- Нет self-escalation, forged callback, duplicate webhook incident, silent scheduler success, fake real IDs и destructive demo в production.
- Документация, Telegram UI, permissions и backend contracts совпадают.
- Docker smoke и restart/recovery проходят на чистой БД и на мигрированной копии.
- Рабочее дерево чистое; каждый логический фикс имеет отдельный понятный commit.
