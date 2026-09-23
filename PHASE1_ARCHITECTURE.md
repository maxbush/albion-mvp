# ALBION — Этап 1: архитектура доработок

> Статус: план на ревью. Основано на коде ветки main (241/241 тестов).
> Решение: развиваем существующую кодовую базу, переписывание не требуется.
> Документ дополняет DECISIONS.md — после утверждения пункты превращаются в ADR-записи.

---

## 1. Принципы

1. **Evolve, not rewrite.** Ядро (event bus → workflows → scheduler в БД → DLQ)
   остаётся. Все новые возможности — через существующие швы.
2. **БД — канал между процессами.** Уже так работает связка
   «webhook-ресивер ↔ бот»: процесс-писатель кладёт `scheduled_actions`,
   процесс-исполнитель забирает их `scheduler_loop`. WhatsApp-ресивер
   использует тот же паттерн — новых IPC не вводим.
3. **Координаторский UI остаётся в Telegram.** WhatsApp — канал для
   родителей/репетиторов (уведомления + простые ответы). Сложные сценарии
   (визарды, пикеры) в WhatsApp не переносим — там нет inline-клавиатур
   уровня Telegram.
4. **Канал уведомления — свойство получателя, не отправителя.** Роутинг
   по `user_channels` с fallback-цепочкой.
5. **Mock-first остаётся.** WhatsApp-клиент получает mock по образцу
   MeritHub: тесты и демо не требуют реального WABA.

## 2. Целевая картина процессов

```
Telegram ──webhook──▶ albion-bot (src.main --webhook)
                        ├─ bot handlers / визарды
                        ├─ scheduler_loop → scheduled_actions
                        ├─ workflows (bus)
                        └─ channels.send → TG API / WA Cloud API

MeritHub ──/merithub/webhook──┐
WhatsApp ──/whatsapp/webhook──┤
                              ▼
                     albion-web (FastAPI, src.api)
                        ├─ приём + verify подписи
                        ├─ webhook_events (сырой захват)
                        └─ запись в БД (scheduled_actions / inbound_messages)

                     Postgres (prod) / SQLite (dev, тесты)
```

Оба процесса уже есть и деплоятся по DEPLOY_VPS.md; добавляется только
второй endpoint в `albion-web` и Postgres вместо файла.

## 3. WhatsApp-канал

### 3.1 Новые модули

```
src/channels/
    base.py          # ChannelSender ABC + SendResult + кнопочная модель
    telegram.py      # перенос существующего send-пути из bot/handlers.py
    whatsapp.py      # Cloud API sender + mock
    router.py        # recipient → channel, fallback, outbound dispatch
    factory.py       # get_channel_sender(name) — mock по умолчанию
src/api/whatsapp_webhook.py  # mount в тот же FastAPI app
src/channels/inbound.py      # нормализация входящих → MESSAGE_INCOMING
```

### 3.2 Исходящие

Сейчас `NOTIFICATION_REQUESTED` обрабатывает один хендлер в
`bot/handlers.py`, который шлёт через `app.bot.send_message`. Меняется точка
доставки: хендлер → `router.send(recipient, text, buttons)`:

- `buttons` — нейтральная модель `[{id, label}]` (≤3 для WA, ≤100 для TG);
- роутер: `user_channels.preferred` → fallback `whatsapp → telegram`;
- ответ маппится в `NOTIFICATION_DELIVERED/FAILED` как сейчас;
- запись в `notifications` с реальным `channel` (колонка уже есть).

### 3.3 Кнопки и ответы в WhatsApp

Ограничения Cloud API: вне 24-часового окна — только **template messages**
(согласование шаблонов Meta занимает часы-дни — подаём заранее),
в окне — free-form + interactive (до 3 кнопок quick reply, списки до 10).

- `callback_data` вида `resolve:1:abc` → WA quick-reply button `id`; при
  ответе webhook приводит `button.id` обратно к той же грамматике —
  вся логика callback'ов (`handle_callback`, nonce, resolve) переиспользуется
  через тонкий адаптер `channels/inbound.py`.
- Списки (>3 опций, выбор занятия для отмены/переноса) → WA `list` message.
- Текстовые ответы → `MESSAGE_INCOMING` → существующий AI-классификатор.
  `users`/контакты WA находим по `phone` (поле уже есть в
  `merithub_contacts`, `users.phone`).

### 3.4 Таблица каналов

```sql
CREATE TABLE user_channels (
    user_id INTEGER NOT NULL,          -- users.id (или contacts PK — решить на реализации)
    channel TEXT NOT NULL CHECK(channel IN ('telegram','whatsapp')),
    address TEXT NOT NULL,             -- tg chat_id / E.164 phone
    is_preferred INTEGER DEFAULT 0,
    verified INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (channel, address)
);
```

Плюс `users.preferred_channel TEXT` — проще для MVP? Вариант B:
`merithub_contacts` расширить `preferred_channel`. Решение на старте
реализации: одна таблица `user_channels` чище для email-канала этапа 3.

### 3.5 Конфиг

```
WHATSAPP_TOKEN=                # system user token (permanent)
WHATSAPP_PHONE_NUMBER_ID=
WHATSAPP_BUSINESS_ACCOUNT_ID=
WHATSAPP_APP_SECRET=           # X-Hub-Signature-256 verify
WHATSAPP_VERIFY_TOKEN=         # GET-challenge verify
WHATSAPP_USE_MOCK=true         # без токена — mock по умолчанию
```

### 3.6 Известные продуктовые следствия

- Напоминания и алерты неявок = вне окна → **нужны approved-шаблоны**
  (напоминание, подтверждение неявки, перенос, платная отмена). Список
  шаблонов фиксируем в коде как реестр (`whatsapp_templates.py`),
  тексты согласуем с Meta до запуска.
- WhatsApp не позволяет «бесшовно ответить кнопкой через сутки» —
  сценарии с дедлайнами должны отправлять новый template, не править старый
  (у Telegram — `editMessageText`). Workflow-код не должен рассчитывать
  на редактирование: вводим `revision`-паттерн (новое сообщение
  «Актуальный статус: …») — уже частично так сделано.

## 4. Переносы и платные отмены

### 4.1 Модель данных

MeritHub — create-only (D4): класс нельзя сдвинуть через API. Перенос =
локальная правка расписания + новый класс в MeritHub при необходимости.

```sql
CREATE TABLE schedule_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    class_id TEXT NOT NULL,
    occurrence_date TEXT NOT NULL,     -- исходная дата (org-зона)
    action TEXT NOT NULL CHECK(action IN ('moved','cancelled')),
    new_date TEXT, new_time TEXT,      -- для moved
    reason TEXT,                        -- 'parent_request'/'tutor_request'/'paid_cancel'
    is_paid INTEGER DEFAULT 0,
    created_by TEXT,                    -- кто инициировал
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(class_id, occurrence_date)
);
```

`class_occurs_on` расширяется: занятие «есть» на дату d, если нет override
`cancelled/moved`; materialization-функция выдаёт перенесённые даты для
`moved`. Точка изменения одна — `recurrence.py` + `MeritHubClassRepository`.

### 4.2 Workflow reschedule

`src/workflows/reschedule.py`:

1. Запрос (кнопка в карточке занятия / команда / WA reply / NLU-интент
   «перенести») → `RESCHEDULE_REQUESTED`.
2. Проверка окна: до занятия ≥ `ALBION_RESCHEDULE_FREE_HOURS` (дефолт 24) —
   бесплатный перенос; меньше — платный/с предупреждением.
3. Платный сценарий: предупреждение инициатору («Перенос менее чем за N ч —
   оплачиваемый») + confirm-кнопка; при confirm → `is_paid=1`, инцидент
   типа `billing_flag` (или поле на override — реализация).
4. Перенос: override `moved` + при необходимости новый oneTime-класс через
   MeritHub API (общение в комнате требует ссылки → создаём класс-замену,
   ссылку шлём участникам). Решение «новая комната vs та же» —
   уточнить у клиента; технически новая проще и надёжнее.
5. Уведомления всем сторонам по их каналам + карточка координатору.

### 4.3 Платная отмена

Расширение `cancellation.py` (не отдельный модуль): в `handle_cancelled`
перед отменой — проверка окна (`ALBION_CANCEL_FREE_HOURS`), при платной —
ветка подтверждения с warning-текстом. Автоматические предупреждения:
scheduled_action `warn_cancel_deadline` на каждое занятие (за 26ч — если
клиент писал об отмене до этого момента… — уточнить у клиента триггер;
минимальная реализация: предупреждение при запросе отмены в платном окне).

## 5. Контроль пересечений perma-занятий

Сейчас `_sched_find_duplicate` ловит только «тот же tutor + тот же старт» —
без учёта длительности и пересечений серия×разовое. Заменяем на полноценную
проверку:

- Новый модуль `src/services/conflicts.py`: `find_conflicts(tutor_cuid,
  candidate) -> list[Conflict]`. Материализует окно 60 дней через
  `class_occurs_on`, сравнивает интервалы [start, start+duration).
- Точки вызова: визард `/schedule` (превью), `reschedule`, импорт данных.
- Политика: **блок или warn с override-кнопкой** — решить с клиентом
  (дефолт в плане: warn + «всё равно создать», дубли бывают легитимны —
  уже зафиксировано в коде).

## 6. Масштабирование (SQLite → Postgres, деплой, мониторинг)

### 6.1 Postgres

`DATABASE_URL` уже sqlalchemy-style. Слой `Repository` инкапсулирует
`_execute/_fetchone/_fetchall` — концентрация изменений там.

SQLite-измы, требующие диалекта (инвентаризация по коду):

| Идиома | Мест | Postgres |
|---|---|---|
| `?` placeholders | весь repository | `$1..$n` (asyncpg) или `%s` (psycopg) |
| `julianday(...) < julianday('now')` | claim_pending ×3 | `execute_at <= now()` (timestamptz) |
| `datetime('now', '-24 hours')` | cleanup | `now() - interval` |
| `json_extract(data,'$.f')=?` | find_by_json | `data->>'f'=?` (jsonb) |
| `datetime('now','+5 minutes')` | lock | `now()+interval` |
| `lastrowid` | inserts | `RETURNING id` |

Варианты: (a) свой dialect-слой поверх aiosqlite/asyncpg — минимум новых
зависимостей; (b) SQLAlchemy Core async — зрелый диалект + alembic из
коробки. **Рекомендация: (b)** — объём переписывания сопоставим, а миграции
и типобезопасность бесплатные. Тестовая матрица: SQLite (default, быстро)
+ Postgres (docker-compose service, CI).

Оценка риска: 707 строк repository + разрозненные прямые SQL в workflows —
основная статья часов пункта «масштабирование».

### 6.2 Миграции

alembic: baseline = текущая схема; дальше все `CREATE TABLE` из этого
плана — миграциями (в т.ч. `user_channels`, `schedule_overrides`,
`system_settings`, `inbound_messages`).

### 6.3 Мелкие prod-правки (входят в объём)

- kill switch → `system_settings` таблица (сейчас in-memory, рестарт
  сбрасывает — в коде есть честный комментарий об этом).
- `wizard_state` — уже в БД, ок.
- Лимиты: `claim_pending(limit=20)` — ок для сотен задач/день.
- Bus in-memory: при 100+ тьюторах событий сотни в день — не bottleneck;
  зафиксировать лимит в DECISIONS (per D3 уже есть «десятки классов/день»,
  пересмотреть формулировку).

### 6.4 Мониторинг

- `/health` есть; добавить `/metrics` (Prometheus-формат, без внешних
  сервисов): notifications sent/failed по каналам, DLQ size,
  scheduled_actions pending-lag (max age), webhook ingress count/errors.
- Алертинг существующий (DLQ → координаторы) + внешний uptime-check на
  `/health` (UptimeRobot/аналог — бесплатный, вне scope кода).
- Бэкап: `pg_dump` в cron/systemd-timer + ротация; runbook в DEPLOY_VPS.

### 6.5 Deploy

docker-compose: `postgres` (volume), `bot`, `web`, `caddy` (TLS + роутинг
`/tg`, `/merithub/webhook`, `/whatsapp/webhook`, `/metrics` — только внутр.).
DEPLOY_VPS.md дополняется секциями Postgres и WhatsApp.

## 7. Миграция реальных данных

`scripts/import_data.py`: CSV (ученики, тьюторы, расписания) → upsert в
`merithub_students`/`merithub_contacts`/`merithub_classes`/`enrollments`.
Dry-run + отчёт (строк, конфликтов, без телефона/TG). Формат источника —
файл от клиента, колонки зафиксировать в шаблоне.

## 8. Production webhooks

- MeritHub: URL регистрируется в их админке (DEPLOY_VPS уже описывает).
- WhatsApp: Meta App Dashboard → webhook URL + verify token, подписка
  `messages`; шаблоны — через WABA Manager. **Lead time: создание WABA,
  привязка номера, approval шаблонов — вне нашего кода, начать первым
  делом** (критический путь этапа).

## 9. Порядок работ (PR-разбивка)

| # | PR | Содержание | Зависит от |
|---|----|-----------|------------|
| 1 | `channels-abstraction` | `src/channels/` + перенос TG-отправителя, `user_channels`, роутер (без изменения поведения) | — |
| 2 | `whatsapp` | Cloud API sender/mock, webhook endpoint, verify, адаптер callback↔quick-reply, шаблонный реестр | 1 |
| 3 | `postgres` | SQLAlchemy-слой/диалект, alembic, compose, тест-матрица | — (параллельно с 2) |
| 4 | `reschedule-payments` | `schedule_overrides`, reschedule-workflow, paid-cancel ветка | 1 (каналы) |
| 5 | `overlap-control` | `services/conflicts.py`, интеграция в визард/reschedule | 4 для reschedule-точки |
| 6 | `monitoring` | `/metrics`, kill switch в БД, runbook | 3 (для system_settings миграции) |
| 7 | `data-import` | `import_data.py` + шаблон CSV | 3 |

Каждый PR — зелёные тесты; новые сценарии — через существующий
pytest-харнесс (FakeUpdate/FakeBot) + FakeWhatsAppClient по образцу
`merithub_mock`.

## 10. Решения владельца (подтверждено)

1. **WABA:** номер есть, аккаунт WABA надо создавать → интеграция пишется
   на mock-first (как MeritHub); в проде включается env-переменными.
   Задача владельца: создание WABA + подача шаблонов на approval —
   начать как можно раньше (критический путь, вне нашего кода).
2. **Пересечения:** жёсткий блок — конфликтующее занятие не создаётся,
   координатору показывается конфликт и предлагается другое время.
3. **Бесплатное окно:** 24 часа (дефолт, уточняется у клиента) —
   `ALBION_CANCEL_FREE_HOURS=24`, `ALBION_RESCHEDULE_FREE_HOURS=24`.
4. **Каналы родителей:** preferred per user, оба канала живут — дефолт
   плана принят.
5. **Перенос:** новая комната MeritHub (API create-only) — дефолт принят.

## 11. Открытый вопрос (не блокирует старт)

- Поведение в платном окне отмены/переноса — блок или confirm «согласен
  оплатить»: берём confirm-ветку (предупреждение + подтверждение), если
  клиент скажет иначе — одна настройка workflow.
