# ALBION — Этап 1: целевая архитектура (вариант для исполнения LLM-агентами)

> Статус: предложение на ревью. Основано на `master@bc808fe` (241/241 тестов) и
> учитывает открытый PR #9 (абстракция каналов) и решения владельца, записанные в нём.
> Документ — **контракт** для задач в `docs/phase1/tasks/`. Если задача
> противоречит этому файлу — прав этот файл; если этот файл неполон — агент
> останавливается и задаёт вопрос (см. `AGENTS.md`).

---

## 0. Главная идея

LLM хорошо выполняет задачи, когда:

1. **Решения приняты заранее.** Бизнес-правила (окно бесплатной отмены, блок
   пересечений и т.д.) зафиксированы в `DECISIONS_NEEDED.md`, а не
   «додумываются» агентом.
2. **Контракты заданы кодом.** Типы, сигнатуры, таблицы описаны здесь дословно.
   Агент реализует контракт, а не проектирует его.
3. **Бизнес-логика — чистые функции.** Пересечения, политика отмен, развёртка
   серий — функции без IO в `src/domain/`. Их легко написать, протестировать и
   проверить на ревью.
4. **Границы проверяются тестами.** Архитектурные тесты (`tests/arch/`) падают,
   если агент импортирует `telegram` в workflow, пишет SQL вне `src/db/` или
   вызывает `datetime.now()` мимо часов.
5. **Задача маленькая.** ≤ ~400 строк диффа, ≤ 5 файлов «для чтения», чёткий
   Definition of Done в виде имён тестов.
6. **Внешний мир — через фейки.** WhatsApp, MeritHub, Telegram, часы — у всех
   есть fake-реализация, тесты не ходят в сеть.

---

## 1. Что в текущем коде мешает этапу 1 (проверено)

| # | Проблема | Где | Следствие для этапа 1 |
|---|---|---|---|
| P1 | Адресация людей по `telegram_id` (~230 мест), `users.telegram_id NOT NULL UNIQUE` | `db/models.py`, все workflows | Родитель «только с WhatsApp» не может существовать в системе. PR #9 это не решает: `resolve()` по-прежнему стартует от `telegram_id` |
| P2 | **Напоминания/чеки perma-серий создаются только на ближайшее занятие** | `bot/wizard.py:668-679` («полная рекуррентность — после демо») | Регулярные занятия — норма у клиента («идут, пока не отменили»). Со 2-й недели напоминаний нет. **Блокер production** |
| P3 | Два процесса (бот + uvicorn-ресивер) с in-memory шиной; в процессе вебхуков нет подписчиков | `api/webhook.py` публикует `LESSON_STARTED/COMPLETED` → 0 обработчиков | Реактивное закрытие чеков по вебхуку в проде не работает (в тестах — работает, т.к. один процесс). WhatsApp-вебхук унаследует ту же проблему |
| P4 | Отправка = синхронный вызов Telegram внутри обработчика шины с `sleep`-ретраями | `bot/handlers.py:1478` | Нет статусов доставки, нет выбора «шаблон/свободный текст» для WhatsApp, нет единой точки kill switch/метрик |
| P5 | Кнопки = строки `callback_data`, обработка — 600 строк в `handle_callback` Telegram | `bot/handlers.py:657-1283` | Ответ кнопкой из WhatsApp некуда передать без дублирования логики |
| P6 | Occurrence perma-серий вычисляются «на лету» | `utils/recurrence.py` | Перенести/отменить одно занятие, проверить пересечения, повесить напоминание — не на что (нет строки «занятие») |
| P7 | SQLite-измы: `julianday`, `json_extract`, `datetime('now')`, `lastrowid`; SQL вне `src/db` (34 места по счётчику `tests/arch`) | `db/repository.py`, `bot/pilot.py`, workflows | Переход на Postgres |
| P8 | Kill switch в памяти процесса | `bot/handlers.py:36` | Сбрасывается при рестарте |
| P9 | `datetime.now()` разбросан (26 мест) | везде | Логику по времени (окно 24ч, DST) нельзя надёжно тестировать |
| P10 | Демо/пилот в прод-коде; `trigger_absence` (бизнес-функция) живёт в `bot/pilot.py` | `bot/pilot.py`, `api/webhook.py:112` | Неправильные зависимости слоёв |

Что **хорошо и сохраняется**: сценарии (absence, lesson_ops), планировщик в БД с
lock/retry/DLQ, идемпотентность через nonce, клиент MeritHub, визарды в БД,
i18n, 241 тест.

---

## 2. Целевая картина

### 2.1 Один процесс

```
                          ┌──────────────────── albion (uvicorn, 1 instance) ─────────────────────┐
Telegram ─POST /tg/webhook──▶│ adapters/inbound                                                      │
WhatsApp ─POST /wa/webhook──▶│   telegram (PTB process_update) · whatsapp · merithub                  │
MeritHub ─POST /mh/webhook──▶│        │ InboundMessage / action_id / DomainEvent                      │
                             │        ▼                                                               │
                             │ application:  actions.dispatch · workflows · planner · change_requests │
                             │        │                    ▲                                          │
                             │        ▼                    │ pure calls                               │
                             │ domain (pure): recurrence · overlaps · change_policy · phones          │
                             │        │                                                               │
                             │        ▼                                                               │
                             │ infra: db repos · outbox · scheduler loop · delivery worker ·          │
                             │        merithub client · channel senders (tg / wa)                      │
                             │ /health  /metrics(internal)                                            │
                             └──────────────────────────────┬─────────────────────────────────────────┘
                                                            ▼
                                                   Postgres 16 (prod) / SQLite (tests, dev)
```

- Один процесс → одна in-memory шина → проблема P3 исчезает без брокера.
- Фоновые циклы (scheduler, delivery worker, planner) стартуют в `lifespan` FastAPI.
- Dev-режим: `python -m src.main` (polling) остаётся для локальной работы.
- Масштаб: 100+ тьюторов ≈ 1.5k занятий/неделю ≈ 6–7k/мес ≈ 30–40k отложенных
  действий/мес (~1–2k в день). Это нагрузка на порядки ниже предела одного
  процесса и Postgres. Горизонтальное масштабирование **не делаем**.
  Postgres нужен ради эксплуатации (бэкапы, миграции, конкурентный доступ,
  PITR), а не ради производительности.

### 2.2 Слои и правила импорта (проверяются `tests/arch/`)

| Слой | Пакеты | Может импортировать | Не может |
|---|---|---|---|
| core | `src/core/` (clock, ids, errors) | stdlib | всё остальное |
| domain | `src/domain/` | `src/core`, stdlib | db, telegram, httpx, bus, settings* |
| messages | `src/messages/` | core, domain | db, telegram, httpx |
| db | `src/db/` | core, sqlalchemy | telegram, httpx, workflows |
| integrations | `src/integrations/`, `src/channels/` | core, db, messages, httpx | workflows, bot |
| application | `src/workflows/`, `src/actions/`, `src/services/` | всё выше | `telegram` (напрямую), SQL |
| adapters | `src/bot/`, `src/api/`, `src/app.py` | всё | — |

\* domain получает настройки параметрами (dataclass-политики), а не читает `settings`.

Дополнительные правила-тесты:

- `datetime.now(` и `datetime.utcnow(` — только в `src/core/clock.py`.
- `sqlalchemy.text(` / строки `SELECT|INSERT|UPDATE|DELETE` — только в `src/db/`.
- `from telegram` — только в `src/bot/`, `src/channels/telegram.py`, `src/app.py`, `src/main.py`.
- `httpx` — только в `src/integrations/`, `src/channels/`, `src/ai/`.
- Пользовательские тексты — только в `src/messages/catalog.py` (ru + en).

---

## 3. Модель данных (целевая)

Эволюция существующей схемы, без «большого переписывания». Реальные данные
приходят через импорт (задача T25), а не из SQLite MVP, поэтому переносить
старую базу не нужно. Новые и изменённые таблицы:

```sql
-- users = ЕДИНАЯ таблица людей (P1). telegram_id становится необязательным.
ALTER users:
    telegram_id      TEXT NULL UNIQUE           -- было NOT NULL
    phone_e164       TEXT NULL UNIQUE           -- нормализованный +447..., ключ для WhatsApp
    email            TEXT NULL
    timezone         TEXT NULL                  -- только для отображения (D6)
    merithub_user_id TEXT NULL UNIQUE
    client_user_id   TEXT NULL UNIQUE           -- наш id в MeritHub
    status           TEXT NOT NULL DEFAULT 'active'   -- active|archived
    role: + 'admin'

guardians (student_user_id FK users, parent_user_id FK users, PRIMARY KEY(both))

-- из PR #9, расширяется:
user_channels (
    user_id        FK users NOT NULL,
    channel        TEXT NOT NULL,        -- telegram|whatsapp|email
    address        TEXT NOT NULL,        -- tg chat_id | E.164 | email
    is_preferred   BOOL DEFAULT false,
    verified_at    TIMESTAMPTZ NULL,
    last_inbound_at TIMESTAMPTZ NULL,    -- для 24-часового окна WhatsApp
    opted_out_at   TIMESTAMPTZ NULL,     -- «STOP» / отказ от канала
    PRIMARY KEY (channel, address)
)

-- Материализованные занятия (P2, P6). Одна строка = одно конкретное занятие.
lessons (
    id                 BIGINT PK,
    class_id           TEXT NOT NULL,          -- merithub_classes.class_id (серия или разовое)
    original_start_utc TIMESTAMPTZ NOT NULL,   -- слот по расписанию серии (ключ идемпотентности)
    start_utc          TIMESTAMPTZ NOT NULL,   -- фактическое время (после переноса)
    end_utc            TIMESTAMPTZ NOT NULL,
    tutor_user_id      FK users NULL,
    status             TEXT NOT NULL DEFAULT 'scheduled',
        -- scheduled|cancelled|cancelled_paid|rescheduled_out|completed|no_show
    room_class_id      TEXT NULL,              -- отдельный oneTime-класс MeritHub после переноса
    created_at, updated_at,
    UNIQUE (class_id, original_start_utc)
)
CREATE INDEX lessons_tutor_time ON lessons(tutor_user_id, start_utc);

lesson_participants (lesson_id FK, student_user_id FK, PRIMARY KEY(both))

change_requests (
    id BIGINT PK, lesson_id FK NOT NULL,
    kind TEXT NOT NULL,                 -- cancel|reschedule
    initiator_user_id FK, initiator_role TEXT,
    requested_at TIMESTAMPTZ, hours_before NUMERIC,
    outcome TEXT NOT NULL,              -- free|paid|too_late|not_allowed  (из domain.change_policy)
    status TEXT NOT NULL,               -- awaiting_confirm|confirmed|applied|rejected|expired
    new_start_utc TIMESTAMPTZ NULL, reason TEXT NULL,
    waived_by_user_id FK NULL           -- координатор снял платность
)

-- Outbox (P4): notifications превращается в очередь исходящих.
notifications (+ колонки):
    user_id            FK users              -- кому (вместо recipient telegram)
    message_key        TEXT NOT NULL         -- ключ каталога
    params_json        TEXT NOT NULL
    actions_json       TEXT NOT NULL DEFAULT '[]'
    channel            TEXT NULL             -- выбирается при отправке
    address            TEXT NULL
    status             TEXT NOT NULL         -- queued|sending|sent|delivered|read|failed|suppressed
    attempts INT, next_attempt_at TIMESTAMPTZ, last_error TEXT
    provider_message_id TEXT NULL UNIQUE     -- tg message_id / wamid
    dedup_key          TEXT NULL UNIQUE      -- защита от дублей
    workflow_id, lesson_id NULL

inbound_messages (channel, address, provider_message_id UNIQUE, user_id NULL,
                  kind text|action|status, payload_json, received_at)

system_settings (key TEXT PK, value TEXT, updated_at)   -- kill switch и т.п. (P8)

-- Вместо json_extract (P7): реальные колонки
workflow_instances  + lesson_id NULL, class_id NULL, user_id NULL  (индексы)
scheduled_actions   + lesson_id NULL (индекс)
```

Правила времени:

- В БД всё хранится в UTC (`TIMESTAMPTZ` в PG, ISO-8601 `+00:00` в SQLite).
  «Сейчас» передаётся в SQL параметром из `clock.now_utc()`, а не функцией СУБД.
- Расписание задаётся в зоне организации (`ALBION_ORG_TIMEZONE`, D6) и
  переводится в UTC **для каждой даты отдельно** (DST: 16:00 London — это то
  15:00 UTC, то 16:00 UTC). На это есть отдельный тест.

---

## 4. Контракты (реализуются дословно)

### 4.1 Часы — `src/core/clock.py`

```python
def now_utc() -> datetime: ...                 # aware UTC; в тестах подменяется фикстурой fake_clock
def to_db(dt: datetime) -> str: ...            # нормализованный ISO UTC
def from_db(v: str | datetime) -> datetime: ...
```

### 4.2 Доступ к БД — `src/db/database.py`

SQLAlchemy 2.x **Core** (не ORM), async, SQL-строки через `text()` с именованными
параметрами. Драйверы: `sqlite+aiosqlite` (тесты/dev), `postgresql+asyncpg` (prod).

```python
class Database:
    def __init__(self, url: str): ...
    async def fetchone(self, sql: str, **params) -> dict | None: ...
    async def fetchall(self, sql: str, **params) -> list[dict]: ...
    async def execute(self, sql: str, **params) -> int: ...          # rowcount
    async def insert_returning_id(self, sql: str, **params) -> int: ...  # SQL с RETURNING id
    def transaction(self) -> AsyncContextManager["Database"]: ...

def get_db() -> Database: ...   # синглтон по settings.database_url; тесты подменяют
```

Разрешённый переносимый SQL: `:name`-параметры, `RETURNING`,
`INSERT ... ON CONFLICT (...) DO UPDATE/NOTHING`, `CURRENT_TIMESTAMP` только в
DEFAULT. Запрещено: `julianday`, `json_extract`, `datetime('now')`, `?`, `lastrowid`,
`INSERT OR REPLACE`.

Миграции: Alembic, типы `sa.*` в `op.create_table`. `init_db()` = `alembic upgrade head`.

### 4.3 Исходящие сообщения — `src/messages/`

```python
# src/messages/types.py
@dataclass(frozen=True)
class Action:
    id: str            # грамматика "prefix:arg1:arg2...", ≤ 64 байт (лимит Telegram callback_data)
    label_key: str     # ключ каталога; итоговая подпись ≤ 20 символов (лимит WhatsApp)

@dataclass(frozen=True)
class Link:
    label_key: str
    url: str

@dataclass(frozen=True)
class OutboundMessage:
    key: str                          # ключ каталога, напр. "absence.parent_warning"
    params: Mapping[str, str]
    actions: tuple[Action, ...] = ()  # WhatsApp: ≤3 → кнопки, 4..10 → list
    links: tuple[Link, ...] = ()

# src/messages/catalog.py
@dataclass(frozen=True)
class MessageSpec:
    key: str
    text: Mapping[str, str]           # {"ru": "...{student}...", "en": "..."}
    wa_template: str | None           # имя одобренного шаблона Meta (utility), None = только в окне
    wa_params: tuple[str, ...] = ()   # порядок параметров шаблона {{1}}, {{2}}...
    audience: frozenset[str] = frozenset()  # роли-получатели (для проверки)

CATALOG: dict[str, MessageSpec]
def render_text(key: str, params: Mapping[str, str], lang: str) -> str: ...  # KeyError → тест падает
```

Существующий `src/utils/i18n.py` переносится в каталог; `tr()` остаётся фасадом,
пока все вызовы не переедут.

### 4.4 Постановка в очередь — `src/services/notify.py`

```python
async def notify(user_id: int, msg: OutboundMessage, *,
                 workflow_id: int | None = None, lesson_id: int | None = None,
                 dedup_key: str | None = None) -> int | None: ...   # id в outbox; None если dedup
async def notify_role(role: str, msg: OutboundMessage, **kw) -> list[int]: ...
```

Workflows **никогда** не знают канал и адрес. `NOTIFICATION_REQUESTED` остаётся на
время миграции как совместимый вход (legacy payload → `notify`).

### 4.5 Каналы — `src/channels/` (развитие PR #9)

```python
@dataclass(frozen=True)
class DeliveryContext:
    lang: str
    window_open: bool          # WhatsApp: был входящий < 24ч назад
    reply_to_provider_id: str | None = None

@dataclass
class SendResult:              # из PR #9
    ok: bool
    channel: str
    message_id: str | None = None
    error: str | None = None
    retryable: bool = True     # новое: 4xx «номер не в WhatsApp» → False

class ChannelSender(Protocol):
    name: str                  # "telegram" | "whatsapp"
    async def send(self, address: str, msg: OutboundMessage, ctx: DeliveryContext) -> SendResult: ...

# src/channels/router.py
async def choose_channel(user_id: int) -> tuple[str, str] | None:
    """preferred (если есть sender и нет opted_out) → любой другой активный канал → None."""
```

Delivery worker (`src/services/delivery.py`) раз в N секунд берёт `queued`
записи, применяет kill switch (из `system_settings`), выбирает канал, вызывает
sender, пишет статус; ретраи с backoff 1м/5м/30м, затем `failed` + алерт.

### 4.6 Входящие и действия — `src/actions/`

```python
@dataclass(frozen=True)
class InboundMessage:
    channel: str
    address: str
    user_id: int | None
    kind: Literal["text", "action"]
    text: str | None
    action_id: str | None
    provider_message_id: str
    received_at: datetime

@dataclass
class ActionResult:
    reply: OutboundMessage | None = None   # ответ нажавшему
    close_buttons: bool = True             # Telegram: убрать клавиатуру у исходного сообщения
    toast: str | None = None               # Telegram: query.answer()

# src/actions/registry.py
def action(prefix: str): ...               # декоратор регистрации обработчика
async def dispatch(actor_user_id: int, action_id: str, *, channel: str) -> ActionResult: ...
```

Бизнес-ветки `handle_callback` (resolve, checkin, cancel_class, coord_*, …)
переезжают в `src/actions/*.py`. Telegram-хендлер и WhatsApp-вебхук становятся
тонкими адаптерами над `dispatch`. Визарды координатора (`wz:*`) остаются
Telegram-only (решение владельца: координаторский UI — Telegram).

### 4.7 Расписание — чистые функции `src/domain/`

```python
# src/domain/intervals.py
@dataclass(frozen=True)
class Interval:
    start_utc: datetime
    end_utc: datetime
    ref: str                # class_id / lesson_id / "candidate"

def overlaps(a: Interval, b: Interval, buffer: timedelta = timedelta(0)) -> bool: ...
def find_conflicts(candidates: Sequence[Interval], existing: Sequence[Interval],
                   buffer: timedelta = timedelta(0)) -> list[tuple[Interval, Interval]]: ...

# src/domain/recurrence.py  (перенос из utils/recurrence.py + новое)
def expand_series(days: Sequence[int], hhmm: tuple[int, int], duration_min: int,
                  tz: str, start: date, until: date) -> list[Interval]: ...   # дни в кодах MeritHub 0=вс

# src/domain/change_policy.py
@dataclass(frozen=True)
class ChangePolicy:
    free_hours: int = 24
    tutor_initiated_is_free: bool = True
    min_notice_hours_reschedule: int = 0   # 0 = переносить можно до начала

@dataclass(frozen=True)
class ChangeDecision:
    outcome: Literal["free", "paid", "too_late", "not_allowed"]
    hours_before: float
    warning_key: str | None      # ключ каталога для предупреждения

def evaluate_change(kind: Literal["cancel", "reschedule"], initiator_role: str,
                    lesson_start_utc: datetime, now_utc: datetime,
                    lesson_status: str, policy: ChangePolicy) -> ChangeDecision: ...

# src/domain/phones.py
def normalize_phone(raw: str, default_region: str = "GB") -> str | None: ...  # E.164 или None
```

### 4.8 Сервисы расписания — `src/services/schedule.py`

```python
async def materialize_class(class_id: str, until: date) -> int: ...   # идемпотентно; число новых lessons
async def run_planner(horizon_days: int = 28) -> None: ...            # ежедневно + после создания класса
async def tutor_conflicts(tutor_user_id: int, candidates: Sequence[Interval],
                          exclude_lesson_ids: set[int] = frozenset()) -> list[Conflict]: ...
async def schedule_lesson_actions(lesson_id: int) -> None: ...        # напоминания/чеки на КОНКРЕТНОЕ занятие
async def reschedule_lesson_actions(lesson_id: int) -> None: ...      # cancel + create
```

`LessonOpsWorkflow.schedule_class_coordination` превращается в
`schedule_lesson_actions(lesson_id)` и вызывается планировщиком для каждого
занятия в горизонте (исправляет P2). Данные для напоминания читаются из
`lessons`/`users` в момент срабатывания, а не копируются в JSON workflow.

Вебхуки MeritHub сопоставляются с занятием: `classId` (+ `subClassId`/`startTime`)
→ ближайший `lessons.start_utc` в окне ±3ч для этого `class_id` (или
`room_class_id`).

---

## 5. Ключевые потоки

### 5.1 Напоминание родителю в WhatsApp (вне окна)

```
planner ──▶ schedule_lesson_actions(L) ──▶ scheduled_actions(lesson.remind_parent @ start-15m)
scheduler tick ──▶ lesson_ops.remind_parent(L) ──▶ notify(parent_id, OutboundMessage(
        key="lesson.parent_reminder", params={...},
        actions=(Action("checkin:<wid>:<nonce>:ready","btn.ready"), …late, …no_show)))
delivery worker ──▶ choose_channel → ("whatsapp", "+44…") ; window_open = False
WhatsAppSender ──▶ template "lesson_parent_reminder_ru" + quick-reply payloads = action ids
Meta ──status webhook──▶ outbox: sent → delivered → read
```

### 5.2 Родитель нажал кнопку в WhatsApp

```
POST /wa/webhook (подпись X-Hub-Signature-256 ок) ──▶ inbound_messages (dedup по wamid)
  button payload / button_reply.id = "checkin:<wid>:<nonce>:no_show"
  address → user_channels → user_id ; last_inbound_at = now (окно открыто 24ч)
──▶ actions.dispatch(user_id, "checkin:…:no_show", channel="whatsapp")
──▶ та же бизнес-логика, что и для Telegram-кнопки (nonce, идемпотентность)
──▶ ActionResult.reply → notify(...) → свободный текст (окно открыто)
```

### 5.3 Отмена в платном окне

```
Родитель: «Отменить занятие» → выбор занятия (TG inline / WA list)
──▶ change_requests.create → domain.evaluate_change(...) = paid (осталось 9ч < 24ч)
──▶ reply: предупреждение «Отмена менее чем за 24 ч оплачивается» + [Подтвердить] [Не отменять]
──▶ confirm → lessons.status = cancelled_paid ; reschedule_lesson_actions (снять напоминания)
──▶ notify(tutor), notify_role(coordinator, карточка + [Снять оплату]), инцидент/флаг для финансов
```

### 5.4 Создание серии с проверкой пересечений (жёсткий блок)

```
Визард /schedule → preview → expand_series(days, time, duration, org_tz, today, today+26w)
──▶ tutor_conflicts(tutor, candidates) → есть конфликты → карточка «Пересечение: пн 16:00 с …»
    кнопка «Создать» не показывается, предлагается изменить время
──▶ нет конфликтов → MeritHub schedule_class → materialize_class → schedule_lesson_actions
```

---

## 6. WhatsApp: ограничения, заложенные в дизайн

| Ограничение Meta | Как учтено |
|---|---|
| Сообщения, которые инициирует бизнес вне 24ч-окна, отправляются только одобренными шаблонами | `MessageSpec.wa_template`; `window_open` считается по `user_channels.last_inbound_at`; сообщение без шаблона вне окна → `suppressed` + алерт координатору, а в тестах — ошибка каталога |
| Кнопки ответа: ≤3, подпись ≤20 символов, id ≤256 | `Action.label` проверяется тестом каталога; >3 действий → interactive list (≤10) |
| Кнопки quick reply в шаблоне несут payload | payload = `Action.id`, поэтому грамматика та же, что в Telegram |
| Ошибка «вне окна» (re-engagement) при свободном тексте | sender один раз повторяет отправку шаблоном, если он есть |
| Подпись вебхука `X-Hub-Signature-256` (HMAC app secret) и GET-верификация | обязательны; без подписи → 401 |
| Лимит получателей до верификации бизнеса (250 уникальных в сутки) | верификация Meta Business — задача человека H1 до запуска |
| С 1.10.2026 Meta тарифицирует и сообщения внутри окна (service/utility) | отправляем меньше сообщений: подсказки встроены в напоминания, отдельные сообщения не шлём; метрика «шаблонов в месяц» есть в `/metrics` |
| Языки | шаблоны отдельно на ru и en → в каталоге имя шаблона по языку |

Версия Graph API — настройка `WHATSAPP_API_VERSION` (по умолчанию `v26.0`).

---

## 7. Надёжность, безопасность, наблюдаемость

- **Идемпотентность входящих:** `inbound_messages.provider_message_id UNIQUE`,
  `webhook_events` для MeritHub, nonce на действиях (как сейчас).
- **Идемпотентность исходящих:** `notifications.dedup_key`
  (`"remind_parent:{lesson_id}:{user_id}"`).
- **Планировщик:** сохраняется (lock, retry, DLQ). В PG claim делается через
  `UPDATE … WHERE id IN (SELECT … FOR UPDATE SKIP LOCKED) RETURNING *`,
  в SQLite — текущий механизм `locked_until`. Диалект выбирается внутри репозитория.
- **Kill switch:** `system_settings['kill_switch']`, применяется в delivery worker
  (единственная точка отправки).
- **PII:** телефоны и email маскируются в логах (`+44******501`); сырые
  payload'ы вебхуков хранятся 30 дней (задача очистки).
- **Секреты:** только через `settings`; в `.env.example` — пустые значения.
- **Наблюдаемость:** `/health` (БД, возраст последнего тика планировщика, лаг
  outbox); `/metrics` в формате Prometheus (outbox по статусам/каналам, DLQ,
  лаг планировщика, число вебхуков по источникам, число WA-шаблонов за месяц);
  heartbeat-ping на внешний dead-man URL (healthchecks.io или аналог); алерты
  админу в Telegram: DLQ > 0, outbox failed > N за час, планировщик стоит > 5 мин.
- **Бэкапы:** ежедневный `pg_dump` → внешнее S3-совместимое хранилище, ротация
  14/8 (дни/недели), ежемесячная проверка восстановления по runbook.

---

## 8. Чего этап 1 сознательно НЕ делает (защита объёма)

- Самостоятельный выбор слота родителем (нужна модель доступности тьюторов).
  Перенос — это заявка, новое время назначает координатор (с проверкой пересечений).
- Выставление счетов/Xero (D11). Платная отмена = флаг, уведомление и отчёт.
- Визарды координатора в WhatsApp. Координаторы работают в Telegram.
- Email (этап 3), дайджест и таск-лист (этап 2), хотя outbox и каталог к ним готовы.
- Горизонтальное масштабирование, брокеры сообщений, Kubernetes.

---

## 9. Отличия от плана в PR #9 (`PHASE1_ARCHITECTURE.md`)

| Тема | PR #9 | Этот вариант | Почему |
|---|---|---|---|
| Абстракция каналов | `ChannelSender.send(address, text, buttons)` | Принимается как основа, `send` получает `OutboundMessage` + `DeliveryContext` | WhatsApp вне окна нужен ключ шаблона, а не готовый текст |
| Идентичность | `user_channels` при `users.telegram_id NOT NULL`, resolve от `telegram_id` | `users` — единая таблица людей, `telegram_id` nullable, адресация по `user_id` | Иначе родитель «только с WhatsApp» не может существовать |
| Занятия perma | Вычисление на лету + `schedule_overrides` | Материализованная таблица `lessons` | Исправляет P2 (напоминания только на 1-е занятие), делает перенос, пересечения и платность операциями над строкой |
| Процессы | Два процесса, общая БД | Один процесс | Исправляет P3: события вебхуков сейчас уходят в пустоту |
| Отправка | Синхронно из обработчика шины | Outbox + delivery worker | Статусы доставки, выбор шаблона, kill switch и метрики в одной точке |
| Callback'и WhatsApp | Адаптер к `handle_callback` | Вынос бизнес-веток в `actions.dispatch` | `handle_callback` принимает Telegram `Update`, WhatsApp его не создаст |
| Postgres | SQLAlchemy Core + Alembic | То же, плюс правило «now — параметром» и реальные колонки вместо `json_extract` | Портируемый SQL без диалектных веток |
