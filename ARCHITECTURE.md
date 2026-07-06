# ALBION MVP — Архитектура и внутреннее устройство

> ⚠️ Этот файл для LLM-разработчиков и технических участников.
> Пользовательскую документацию см. в README.md.

---

## 📋 Ревизия

| Версия | Дата | Изменения |
|--------|------|-----------|
| 2.0 | 2026-07-06 | Scheduler → SQLite, Dead Letter Queue, Inline buttons, Kill Switch, WAL-mode, Idempotency TTL, NOTIFICATION_REQUESTED |

---

## 📐 Общая архитектура

```
                  ┌───────────────────────────────────────┐
                  │            Telegram Bot               │
                  │    (polling — локально, webhook — VPS) │
                  └──────────────┬────────────────────────┘
                                 │
                          сообщения / команды
                                 │
                                 ▼
                  ┌───────────────────────────────────────┐
                  │           Event Bus (pub/sub)          │
                  │         src/events/bus.py              │
                  │                                       │
                  │  + 10s timeout per handler             │
                  │  + Dead Letter Queue при ошибках       │
                  └──────────────┬────────────────────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                  ▼
   ┌──────────────────┐  ┌──────────────┐  ┌──────────────┐
   │  Workflow Engine  │  │   AI Layer   │  │  Scheduler   │
   │  (state machine)  │  │ (LLM клиент) │  │ (SQLite-based)│
   │ src/workflows/    │  │ src/ai/      │  │ src/scheduler/│
   └────────┬─────────┘  └──────┬───────┘  └──────┬───────┘
            │                   │                 │
            └───────────────────┼─────────────────┘
                                │
                                ▼
                  ┌───────────────────────────────────────┐
                  │       Integration Layer (Repository)   │
                  │  src/integrations/                     │
                  │                                       │
                  │  airtable_mock.py — Mock Airtable CRM  │
                  │  merithub_mock.py — Mock MeritHub     │
                  │  base.py          — Датаклассы/ABC    │
                  └──────────────┬────────────────────────┘
                                 │
                                 ▼
                  ┌───────────────────────────────────────┐
                  │       SQLite — состояние системы      │
                  │  src/db/                              │
                  │                                       │
                  │  WAL-mode + busy_timeout=5000         │
                  │                                       │
                  │  Таблицы:                             │
                  │  • users, incidents, notifications    │
                  │  • workflow_instances                 │
                  │  • leads, conversations               │
                  │  • scheduled_actions ★                │
                  │  • dead_letter_queue ★                │
                  │  • idempotency_keys (с TTL)           │
                  └───────────────────────────────────────┘
```

---

## 🔄 Event Bus — сердце системы

**Файл:** `src/events/bus.py`

In-memory pub/sub. Все компоненты общаются ТОЛЬКО через события.

### Ключевые изменения v2.0

| Аспект | Было | Стало |
|--------|------|-------|
| **Таймаут** | ∞ (висел навсегда) | 10 секунд (`asyncio.wait_for`) |
| **Ошибки** | логировались и терялись | → `SYSTEM_DLQ_ALERT` → DLQ handler записывает в БД |
| **Dead Letter Queue** | нет | Таблица `dead_letter_queue` + алерт координатору |

### События

```python
@dataclass
class Event:
    type: str
    data: dict
    timestamp: str = ...        # ISO 8601
    idempotency_key: str | None

class EventTypes:
    # Lessons
    LESSON_ABSENT / LESSON_CANCELLED / LESSON_RESCHEDULED

    # Messages
    MESSAGE_INCOMING / MESSAGE_CLASSIFIED

    # Leads
    LEAD_NEW

    # Notifications — честная стейт-машина
    NOTIFICATION_REQUESTED     # "отправь это сообщение"
    NOTIFICATION_DELIVERED     # "успешно отправлено"
    NOTIFICATION_FAILED        # "ошибка отправки"

    # Workflows
    WORKFLOW_STARTED / WORKFLOW_COMPLETED / WORKFLOW_FAILED

    # System
    SCHEDULER_TICK
    SYSTEM_DLQ_ALERT           # событие в DLQ
    SYSTEM_KILL_SWITCH         # смена уровня kill switch
```

### Обработка ошибок

```python
async def publish(self, event: Event) -> None:
    for handler in handlers:
        try:
            await asyncio.wait_for(handler(event), timeout=10.0)
        except TimeoutError:
            await self._publish_alert(event, handler, "Timeout 10s")
        except Exception as e:
            await self._publish_alert(event, handler, str(e))

async def _publish_alert(self, event, handler, error):
    # Публикует SYSTEM_DLQ_ALERT → DLQ handler запишет в БД
    await bus.publish(Event(SYSTEM_DLQ_ALERT, {...}))
```

---

## ⚙️ Workflow Engine — state machine

**Файлы:** `src/workflows/engine.py`, `absence.py`, `lead_capture.py`, `cancellation.py`, `dlq_handler.py`

### Workflow Engine (engine.py)

Управление жизненным циклом. **Отложенные действия теперь через SQLite.**

```python
class WorkflowEngine:
    async def start_workflow(wtype, data) -> int
    async def complete_workflow(wid, result)
    async def fail_workflow(wid, error)
    async def schedule_action(wid, delay_min, action, payload) -> str  # ★
```

### Сценарий #1: Absence → Notification (absence.py)

```
1. LESSON_ABSENT {lesson_id}
   → Помечает absent в Airtable + MeritHub
   → Создаёт incident (status: pending)
   → Создаёт workflow (status: running)
   → scheduled_actions: notify_parent через 5 мин

2. SCHEDULER_TICK {action: "notify_parent"}
   → _check_incident_active(incident_id) ← ★ проверка!
   → Если resolved/escalated → пропускаем
   → NOTIFICATION_REQUESTED с callback_data для кнопки

3. Вариант А: Родитель нажал кнопку
   → callback_query "resolve:1:nonce"
   → resolve_absence(1, "parent")
   → editMessageText — убираем кнопки
   → workflow complete

4. Вариант Б: 15 мин тишины
   → SCHEDULER_TICK {action: "escalate"}
   → _check_incident_active() ← ★ ещё одна проверка!
   → NOTIFICATION_REQUESTED координатору
   → workflow complete (escalated)

5. Если хендлер упал:
   → SYSTEM_DLQ_ALERT → DLQ handler
   → workflow → failed
   → координатору: "⚠️ Ошибка обработки"
```

### Сценарий #2: Lead Capture (lead_capture.py)

```
1. Клиент пишет: "Ищу репетитора по математике"
2. MESSAGE_INCOMING → AI.classify_intent() → intent=lead
3. MESSAGE_CLASSIFIED → AI.extract_entities() → {subject, grade}
4. LEAD_NEW → Сохраняет в локальную БД + Airtable mock
5. NOTIFICATION_REQUESTED → координатору
```

### Сценарий #3: Cancellation (cancellation.py)

```
1. Клиент пишет об отмене → AI.classify → intent=cancellation
2. LESSON_CANCELLED → отмена в сервисах
3. NOTIFICATION_REQUESTED → репетитору + координатору
```

---

## 🧠 AI Layer (LLM)

**Файлы:** `src/ai/client.py`, `src/ai/classifier.py`

### LLMClient

```python
class LLMClient:
    async def extract_entities(text) -> dict      # subject, grade, goal, is_lead
    async def classify_intent(text) -> dict        # intent + confidence
```

**Mock-режим** (без `OPENROUTER_API_KEY`):
- По ключевым словам возвращает предопределённые JSON
- Все тесты проходят без интернета

**Когда AI в деле:** классификация, извлечение данных, (в будущем) генерация отчётов
**Когда AI НЕ в деле:** расчёты, workflow-переходы, бизнес-правила

---

## 🗄 База данных (SQLite)

**Файлы:** `src/db/models.py`, `src/db/migrations.py`, `src/db/repository.py`

### Прагмы (v2.0)

```sql
PRAGMA journal_mode=WAL;         -- конкурентные чтения/записи
PRAGMA busy_timeout=5000;        -- ждать до 5с вместо ошибки lock
PRAGMA synchronous=NORMAL;       -- баланс скорости/безопасности
```

### Ключевые таблицы

```sql
-- ★ НОВАЯ: отложенные действия (вместо in-memory)
CREATE TABLE scheduled_actions (
    id TEXT PRIMARY KEY,          -- UUID[:8]
    workflow_id INTEGER,
    execute_at TIMESTAMP NOT NULL, -- когда выполнить
    action TEXT NOT NULL,          -- "notify_parent" / "escalate"
    payload TEXT DEFAULT '{}',    -- JSON
    status TEXT DEFAULT 'pending' CHECK(status IN ('pending','running','done','failed')),
    attempts INTEGER DEFAULT 0,   -- ≤ 3 попытки
    last_error TEXT,
    locked_until TIMESTAMP,       -- для claim-механизма
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_scheduled_pending ON scheduled_actions(status, execute_at);

-- ★ НОВАЯ: мёртвые события
CREATE TABLE dead_letter_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,          -- "event_bus" / "scheduler"
    event_type TEXT,
    payload TEXT DEFAULT '{}',
    error TEXT NOT NULL,
    attempts INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- idempotency keys (авто-чистка через 24 часа)
CREATE TABLE idempotency_keys (
    key TEXT PRIMARY KEY,
    handler TEXT NOT NULL,
    response TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_idempotency_created ON idempotency_keys(created_at);
```

### Repository Pattern

```python
class ScheduledActionRepository(Repository):
    async def create(wid, execute_at, action, payload) -> str
    async def claim_pending(limit=20) -> list[dict]  # ★ атомарный захват
    async def mark_done(aid)
    async def mark_failed(aid, error)
    async def requeue(aid)              # после временной ошибки
    async def cleanup_old(hours=24)      # удаление выполненных

class DeadLetterQueueRepository(Repository):
    async def put(source, event_type, payload, error) -> int
    async def count() -> int
```

### Claim-механизм (защита от дублей при падениях)

```sql
-- Шаг 1: выбираем кандидатов
SELECT id FROM scheduled_actions
WHERE status='pending' AND execute_at <= datetime('now') AND attempts < 3
LIMIT 20;

-- Шаг 2: атомарно забираем каждый
UPDATE scheduled_actions
SET status='running', attempts=attempts+1, locked_until=datetime('now','+2 minutes')
WHERE id=? AND status='pending';
-- Если rowcount == 1 — мы забрали, выполняем action
```

---

## 🔌 Integration Layer (Mock'и)

**Файлы:** `src/integrations/`

### Датаклассы (base.py)

```python
@dataclass class Tutor:     id, name, subjects, telegram_id
@dataclass class Student:   id, name, grade_level, parent_telegram_id
@dataclass class Lesson:    id, student_id, tutor_id, subject, start/end, status
@dataclass class Lead:      id, raw_message, extracted_data, status
```

### MockAirtableService / MockMeritHubService

In-memory с seed-данными (3 tutor, 2 student, 2 lesson). Методы: `get_*`, `mark_absent`, `cancel_lesson`, `check_low_balance`.

**Замена на реальные сервисы:**
1. Создать `src/integrations/airtable_real.py`
2. Реализовать те же методы через REST API
3. Заменить импорт в workflows

---

## 🤖 Telegram Bot

**Файл:** `src/bot/handlers.py`

Библиотека: `python-telegram-bot` v21+.

### Команды

| Команда | Описание |
|---------|----------|
| `/start` | Приветствие + справка |
| `/status` | Состояние: AI, БД, Kill Switch |
| `/absent <ID>` | Отметить отсутствие ученика |
| `/mock_absent` | ★ Демо: absent через 10 секунд |
| `/ok <ID>` | ★ Запасной способ закрыть инцидент |
| `/kill_switch <0|1|2>` | ★ Управление отправкой |
| `/replay` | ★ (заглушка) перезапуск события из DLQ |

### Kill Switch (v2.0)

```python
_kill_switch_level = 2  # 0=выкл, 1=только координаторам, 2=полностью

def can_send(telegram_id: str | None) -> bool:
    if level == 2: return True
    if level == 1 and telegram_id and "coordinator" in str(telegram_id): return True
    return False
```

### Inline Buttons (v2.0)

Вместо `/ok 1` — под сообщением кнопка `✅ Всё в порядке`.

```python
# Формирование (в absence.py)
callback_data = f"resolve:{incident_id}:{nonce}"
kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Всё в порядке", callback_data=callback_data)]])

# Обработка (в bot/handlers.py)
async def handle_callback(upd, ctx):
    data = query.data  # "resolve:1:abc123"
    resolve_absence(inc_id)
    await query.edit_message_text("✅ Всё в порядке! 🙌")
```

**Особенности:**
- После нажатия — `editMessageText` убирает кнопки (нельзя нажать дважды)
- Nonce в callback_data для базовой защиты от подделки
- Если инцидент уже resolved — `query.answer()` с "уже закрыто"

### NOTIFICATION_REQUESTED → NOTIFICATION_DELIVERED (v2.0)

```python
# Публикация запроса
await bus.publish(Event(NOTIFICATION_REQUESTED, {
    "telegram_id": parent_tg, "message": "...",
    "callback_data": "resolve:1:abc",
}))

# В bot/handlers.py — реальная отправка
async def notif_handler(event):
    if not can_send(tg): return  # kill switch
    if callback_data:
        kb = InlineKeyboardMarkup(...)
        await app.bot.send_message(chat_id=tg, reply_markup=kb)
    else:
        await app.bot.send_message(chat_id=tg)
    # После успеха
    await bus.publish(Event(NOTIFICATION_DELIVERED, {...}))
    # При ошибке
    await bus.publish(Event(NOTIFICATION_FAILED, {...}))
```

### DLQ Alert → координатору

```python
# В bot/handlers.py
async def dlq_alert_handler(event):
    await app.bot.send_message(
        chat_id="coordinator_1",
        text=f"⚠️ *Системный алерт:* необработанное событие\n"
             f"Тип: `{event_type}`\nХендлер: `{handler}`\nОшибка: {error}",
    )

bus.subscribe(SYSTEM_DLQ_ALERT, dlq_alert_handler)
```

---

## ⏰ Scheduler (SQLite-based)

**Файл:** `src/scheduler/scheduler.py`

**Больше никаких in-memory списков.** Все отложенные задачи переживают рестарт.

```python
async def scheduler_loop(interval=30):
    while True:
        tasks = await ScheduledActionRepository().claim_pending(limit=20)
        for task in tasks:
            payload = json.loads(task["payload"])
            await bus.publish(Event(SCHEDULER_TICK, {
                "action": task["action"],
                "workflow_id": task["workflow_id"],
                "data": payload,
            }))
            # Если тик упал — action останется running
            # и через locked_until (2 мин) reaper его вернёт в pending
        await asyncio.sleep(interval)
```

**Безопасность:** `attempts < 3` — после трёх неудач статус → `failed`, запись → DLQ.

**Cleanup:** фоновая задача раз в час удаляет `done` задачи старше 24ч.

---

## 📊 Логирование

**Файл:** `src/utils/logging.py`

- **stdout** — человекочитаемый
- **albion.log** — JSON-structured (ротация 1MB × 5 файлов)

```json
{"timestamp": "2026-07-03T12:34:56.123Z", "level": "INFO", "module": "workflows.absence", "message": "Absence: lesson=lesson_1 inc=1 wf=1"}
```

---

## 🔒 Безопасность (MVP)

1. **Kill Switch** — 3 уровня (0/1/2). Выкатываешь фичу с level=1, проверяешь час, переключаешь на 2
2. **Idempotency keys** — с TTL 24 часа, авто-чистка раз в час
3. **Claim-механизм** — атомарный захват задач, защита от дублей
4. **Dead Letter Queue** — упавшие события не теряются
5. **Rate limiting** — 1 req/sec (в middleware)
6. **Проверка статуса** — перед каждым действием `_check_incident_active()`
7. **WAL-mode** — нет `database is locked` при конкурентном доступе

---

## 🧪 Тестирование

**18 тестов** — все проходят.

| Файл | Тестов | Что проверяет |
|------|--------|---------------|
| test_event_bus.py | 4 | pub/sub, wildcard, исключения не ломают шину |
| test_absence_workflow.py | 5 | создание инцидента, mark absent, resolve, check_incident_active |
| test_lead_capture.py | 2 | создание лида, пустое сообщение |
| test_cancellation.py | 2 | отмена урока, несуществующий урок |
| test_mocks.py | 5 | Airtable/MeritHub корректность |

### Паттерн тестирования

```python
@pytest.mark.asyncio
async def test_absence_creates_incident(db_path):
    # db_path — temp .db с инициализированной схемой
    wf = AbsenceWorkflow(db_path)
    await wf.handle_lesson_absent(Event(LESSON_ABSENT, {...}))
    inc = await IncidentRepository(db_path).get(1)
    assert inc["status"] == "pending"
```

---

## 🚀 Запуск

### Локально (polling)

```bash
bash scripts/run.sh
# или
python -m src.main
```

### VPS/сервер (webhook)

```bash
python -m src.main --webhook
# нужен TELEGRAM_WEBHOOK_URL (публичный HTTPS URL)
```

### Docker

```bash
docker-compose up -d
```

---

## 🔧 Быстрый старт для LLM-разработчика

```bash
git clone <repo> && cd albion-mvp
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# создать бота у @BotFather, получить токен
echo "TELEGRAM_BOT_TOKEN=ваш_токен" >> .env
python -m src.main
# в Telegram: /status, /mock_absent, /kill_switch
```

---

## 🧩 Как добавить новый workflow

```python
# 1. src/workflows/my_feature.py
class MyWorkflow:
    async def handle_event(self, event):
        # логика
        await engine.schedule_action(wid, delay_min, "my_action", {})

async def register_handlers():
    bus.subscribe(EventTypes.SOME_EVENT, MyWorkflow().handle_event)

# 2. src/main.py
from src.workflows.my_feature import register_handlers as reg_my
await reg_my()

# 3. tests/test_my_feature.py
# 4. pytest tests/ -v
```

---

## 📈 Roadmap

| Этап | Содержание |
|------|-----------|
| ✅ MVP (сейчас) | Scheduler→SQLite, DLQ, Inline buttons, Kill Switch, 18 тестов |
| 🟡 Ближайшее | Замена mock'ов на реальные API (Airtable, MeritHub, Xero) |
| 🟡 | Web Dashboard для координатора |
| 🔵 Будущее | PostgreSQL, Redis, CI/CD |

---

## 💡 Key Decisions

| Решение | Почему |
|---------|--------|
| **Scheduler в SQLite, не Redis** | Нет внешних зависимостей в MVP, простота деплоя |
| **Event Bus последовательный** | Нет race conditions с SQLite, легче дебажить |
| **LLM — сменяемый слой** | Экономия токенов, graceful degradation при падении API |
| **Inline buttons вместо /ok** | UX: родители не пишут команды вручную |
| **Kill Switch трехуровневый** | Безопасный деплой: протестировать на координаторах → включить всем |
| **Mock'и без DI** | MVP: прямая зависимость проще, чем контейнеры |
