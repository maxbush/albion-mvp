# ALBION MVP — Архитектура и внутреннее устройство

> ⚠️ Этот файл для LLM-разработчиков и технических участников проекта.
> Пользовательскую документацию см. в README.md.

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
                  │  Любой компонент подписывается на       │
                  │  события и публикует свои              │
                  └──────────────┬────────────────────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                  ▼
   ┌──────────────────┐  ┌──────────────┐  ┌──────────────┐
   │  Workflow Engine  │  │   AI Layer   │  │  Scheduler   │
   │  (state machine)  │  │ (LLM клиент) │  │ (delayed job)│
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
                  │  users, conversations, incidents,     │
                  │  notifications, workflows, leads      │
                  └───────────────────────────────────────┘
```

---

## 🔄 Event Bus — сердце системы

**Файл:** `src/events/bus.py`

Это **in-memory pub/sub** (не Redis — пока не нужно). Все компоненты общаются ТОЛЬКО через события. Нет прямых вызовов между модулями.

### События (src/events/types.py)

```python
@dataclass
class Event:
    type: str                    # "lesson.absent", "lead.new", ...
    data: dict                   # полезная нагрузка
    timestamp: str = ...        # ISO формат
    idempotency_key: str | None  # для защиты от дублей

class EventTypes:
    LESSON_ABSENT = "lesson.absent"       # репетитор отметил отсутствие
    LESSON_CANCELLED = "lesson.cancelled" # занятие отменено
    MESSAGE_INCOMING = "message.incoming" # новое сообщение в Telegram
    MESSAGE_CLASSIFIED = "message.classified"  # AI классифицировал intent
    LEAD_NEW = "lead.new"                # новый лид создан
    NOTIFICATION_SENT = "notification.sent"    # отправить сообщение
    WORKFLOW_STARTED = "workflow.started"
    WORKFLOW_COMPLETED = "workflow.completed"
    WORKFLOW_FAILED = "workflow.failed"
    SCHEDULER_TICK = "scheduler.tick"    # тик от планировщика
```

### Как работает

```python
# Подписка
bus.subscribe(EventTypes.LESSON_ABSENT, my_handler)

# Публикация
await bus.publish(Event(EventTypes.LESSON_ABSENT, {"lesson_id": "..."}))

# Wildcard — ловит всё
bus.subscribe("*", log_all_events)
```

### Важно для LLM-разработчика
- Хендлеры выполняются **последовательно** в порядке подписки
- Если хендлер упал — другие продолжаются (try/except в bus.py)
- Для параллельного выполнения — `asyncio.gather` (пока не нужно в MVP)

---

## ⚙️ Workflow Engine — state machine

**Файлы:** `src/workflows/engine.py`, `src/workflows/absence.py`, `src/workflows/lead_capture.py`, `src/workflows/cancellation.py`

### Workflow Engine (engine.py)

Управляет жизненным циклом бизнес-процессов.

**Методы:**
```python
engine.start_workflow("absence_notification", data={})  # → workflow_id
engine.complete_workflow(wf_id, result={})
engine.fail_workflow(wf_id, error="...")
engine.schedule_delayed_action(wf_id, delay_min=5, action="notify_parent", action_data={})
```

**Состояния:** `pending → running → waiting → completed/failed`

**Как работает отложенное действие:**
1. Workflow пишет в `data._delayed` массив `{execute_at, action, data, workflow_id}`
2. Scheduler (src/scheduler/scheduler.py) тикает каждые 30 секунд
3. Находит просроченные действия и публикует `SCHEDULER_TICK`
4. Workflow handler получает тик и выполняет нужный action

### Сценарий #1: Absence → Notification (absence.py)

```
1. Событие: LESSON_ABSENT {lesson_id, reported_by}
2. AbsenceWorkflow.handle_lesson_absent():
   a. Получает lesson из MeritHub/Airtable
   b. Помечает absent в обоих сервисах
   c. Получает student (parent_telegram_id)
   d. Создаёт incident (статус: pending)
   e. Создаёт workflow (статус: running)
   f. Планирует notify_parent через 5 минут

3. Через 5 мин: SCHEDULER_TICK {action: "notify_parent"}
   a. Проверяет — не resolved ли инцидент?
   b. Если нет — публикует NOTIFICATION_SENT
      "Миша отсутствовал на занятии. Всё ли в порядке? /ok 1"
   c. Планирует escalate через 15 минут

4. Вариант А: Родитель пишет /ok 1
   → resolve_absence(1, "parent") → статус resolved

5. Вариант Б: Через 15 мин SCHEDULER_TICK {action: "escalate"}
   → статус escalated
   → координатору: "Эскалация: инцидент #1"
   → workflow completed
```

### Сценарий #2: Lead Capture (lead_capture.py)

```
1. Клиент пишет: "Ищу репетитора по математике"
2. MESSAGE_INCOMING → AI.classify_intent() → {"intent": "lead"}
3. MESSAGE_CLASSIFIED → LeadCaptureWorkflow.handle_classified()
   a. AI.extract_entities() → {subject: "mathematics", grade_level: "9"}
   b. LEAD_NEW → handle_lead_new()
   c. Сохраняет в локальную БД + Airtable mock
   d. NOTIFICATION_SENT → координатору
```

### Сценарий #3: Cancellation (cancellation.py)

```
1. Клиент пишет об отмене → AI.classify → {"intent": "cancellation"}
2. LESSON_CANCELLED → handle_cancelled()
   a. Проверка: за сколько часов до урока?
   b. Отмена в обоих сервисах
   c. Уведомление репетитору + координатору
```

---

## 🧠 AI Layer (LLM)

**Файлы:** `src/ai/client.py`, `src/ai/classifier.py`

### LLMClient (client.py)

Абстракция над OpenRouter API. **Ключевая особенность — mock fallback.**

```python
class LLMClient:
    async def extract_entities(text) -> dict
        # Извлекает: subject, grade_level, goal, is_lead
        # Из сообщения: "Ищу репетитора по математике для 9 класса"
        #             → {"subject": "mathematics", "grade_level": "9", "is_lead": true}

    async def classify_intent(text) -> dict
        # Определяет: intent (lead/cancellation/absence_report/question/other)
        # confidence (0-1)
```

**Когда AI в деле:**
- Классификация намерений входящих сообщений
- Извлечение сущностей для заявок
- (в будущем) Генерация отчётов, перевод, ответы на вопросы

**Когда AI НЕ в деле:**
- Расчёт комиссий
- Сверка оплат
- Workflow transitions
- Бизнес-правила

**Mock-режим:** если `OPENROUTER_API_KEY` не указан — возвращает предопределённые JSON-ответы по ключевым словам. Все тесты проходят без интернета.

---

## 🗄 База данных (SQLite)

**Файлы:** `src/db/models.py`, `src/db/migrations.py`, `src/db/repository.py`

### Схема

```sql
users           -- telegram_id, role, name, language
conversations   -- user_id, role, content (история для AI)
workflow_instances  -- workflow_type, state, data (JSON)
incidents       -- lesson_ref, student/tutor/coordinator, type, status
notifications   -- recipient, type, channel, status
leads           -- source, raw_message, extracted_data (JSON), status
idempotency_keys -- key, handler (защита от дублей)
```

### Repository Pattern

Каждая таблица = отдельный класс репозитория:

```python
class UserRepository(Repository):
    async def get_by_telegram_id(tg) -> dict | None
    async def create(tg, role, name, **kw) -> int

class IncidentRepository(Repository):
    async def create(**kw) -> int
    async def get(id) -> dict | None
    async def update_status(id, status, resolution)
```

Важно: все репозитории принимают `db_path: str`. По умолчанию — `albion.db`, в тестах — временный файл.

---

## 🔌 Integration Layer (Mock'и)

**Файлы:** `src/integrations/`

### base.py — Датаклассы

```python
@dataclass
class Tutor:       id, name, subjects, telegram_id
@dataclass
class Student:     id, name, grade_level, parent_telegram_id
@dataclass
class Lesson:      id, student_id, tutor_id, subject, start/end_time, status
@dataclass
class Lead:        id, raw_message, extracted_data, status
```

### MockAirtableService

In-memory dict'ы с seed-данными (3 tutor'а, 2 student'а, 2 lesson'а).

Методы: `get_tutor`, `get_student`, `get_lesson`, `mark_absent`, `cancel_lesson`, `create_lead`

### MockMeritHubService

In-memory с seed: 1 урок, 2 баланса (150 и 20).

Методы: `get_lesson`, `mark_absent`, `cancel_lesson`, `get_balance`, `check_low_balance`

### Как заменить на реальные сервисы

1. Создать `src/integrations/airtable_real.py`
2. Реализовать те же методы, но через REST API
3. Заменить `MockAirtableService()` на `RealAirtableService()` в workflows
4. (В идеале) Ввести DI/abstract classes — но для MVP норм и прямое создание

---

## 🤖 Telegram Bot

**Файл:** `src/bot/handlers.py`

Использует `python-telegram-bot` v21+.

### Команды

```python
/start      → приветствие + справка
/status     → состояние системы (AI: mock/live, DB: ok, время)
/absent ID  → публикует LESSON_ABSENT
/ok ID      → вызывает resolve_absence(incident_id)
```

### Message handler

Любое текстовое сообщение → `MESSAGE_INCOMING` → AI классифицирует.

### Notification handler

Подписан на `NOTIFICATION_SENT` → отправляет сообщение пользователю через `app.bot.send_message()`.

### Polling vs Webhook

```python
# src/main.py
if webhook:
    await app.run_webhook(listen="0.0.0.0", port=8443)
else:
    await app.run_polling(drop_pending_updates=True)
```

---

## ⏰ Scheduler

**Файл:** `src/scheduler/scheduler.py`

In-memory список отложенных действий. Тикает каждые 30 секунд.

```python
_scheduled = [
    {
        "id": "act_0_1748950000.0",
        "execute_at": 1748950000.0,  # unix timestamp
        "action_type": "notify_parent",
        "data": {"workflow_id": 1, "incident_id": 1}
    }
]
```

При тике: `SCHEDULER_TICK {action, data, workflow_id}` → workflow handler получает и исполняет.

---

## 📊 Логирование

**Файл:** `src/utils/logging.py`

Два потока:
- **stdout** — человекочитаемый (`2026-07-03 12:34:56 | INFO | workflows.absence | ...`)
- **albion.log** — JSON-structured (ротация 1MB × 5 файлов)

```json
{"timestamp": "2026-07-03T12:34:56.123Z", "level": "INFO", "module": "workflows.absence", "message": "Absence: lesson=lesson_1 inc=1 wf=1"}
```

---

## 🔒 Безопасность (MVP)

1. **Idempotency keys** — каждый webhook/команда может иметь ключ; повторные вызовы игнорируются
2. **Rate limiting** — 1 запрос/сек на пользователя (в middleware)
3. **Pydantic validation** — все входные данные валидируются
4. **No secrets in code** — `.env`, в `.gitignore`
5. **Prompt injection** — системный промпт + выходная валидация (базовая)

---

## 🧪 Тестирование

**Файлы:** `tests/`

### Структура

| Файл | Тестов | Что проверяет |
|------|--------|--------------|
| test_event_bus.py | 4 | pub/sub, wildcard, исключения |
| test_absence_workflow.py | 4 | создание инцидента, mark absent, resolve |
| test_lead_capture.py | 2 | создание лида, пустое сообщение |
| test_cancellation.py | 2 | отмена урока, несуществующий урок |
| test_mocks.py | 5 | Airtable: tutor, student, mark_absent, lead; MeritHub: lesson, absent, balance |

### Паттерн тестирования

```python
@pytest.mark.asyncio
async def test_absence_creates_incident(db_path):
    # 1. Создаём workflow с временной БД
    wf = AbsenceWorkflow(db_path)

    # 2. Публикуем событие (не через bus, а напрямую)
    await wf.handle_lesson_absent(Event(EventTypes.LESSON_ABSENT, {...}))

    # 3. Проверяем состояние в БД
    inc = await IncidentRepository(db_path).get(1)
    assert inc["status"] == "pending"
```

**Фикстура db_path:** создаёт временный файл .db, инициализирует схему, после теста удаляет.

---

## 🚀 Запуск

### Локально (polling)

```bash
python -m src.main
# или
bash scripts/run.sh
```

### VPS/сервер (webhook)

```bash
python -m src.main --webhook
# нужен TELEGRAM_WEBHOOK_URL в .env (публичный HTTPS URL)
```

### Docker

```bash
docker-compose up -d
```

---

## 🧩 Как добавить новый workflow

1. Создать `src/workflows/new_feature.py`
2. В классе-воркфлоу определить хендлеры событий
3. В `register_handlers()` подписаться на события
4. Вызвать `register_handlers()` в `src/main.py`
5. Написать тесты в `tests/`

**Пример — добавление Payment Workflow:**

```python
# workflows/payment.py
class PaymentWorkflow:
    async def handle_payment(self, event):
        # логика проверки оплаты
        ...

async def register_handlers():
    bus.subscribe("payment.received", PaymentWorkflow().handle_payment)
```

```python
# main.py — добавить строку
from src.workflows.payment import register_handlers as reg_payment
await reg_payment()
```

---

## 💡 Key Decisions для LLM-разработчика

### Почему Event Bus, а не прямой вызов?

- **Тестируемость**: каждый handler можно вызвать изолированно
- **Расширяемость**: новый handler = subscribe, не трогая старый код
- **Graceful degradation**: при падении одного компонента остальные работают

### Почему mock'и, а не реальные API?

- MVP должен работать без интернета и ключей
- Разработка и тестирование не зависят от внешних сервисов
- Замена mock → real — это просто импорт другого класса

### Почему LLM — сменяемый слой, а не ядро?

- Стоимость: не платить за токены на бизнес-логике
- Доступность: система работает даже при падении API
- Гибкость: Claude ↔ GPT ↔ локальная Llama без переписывания

### Почему SQLite, а не PostgreSQL?

- MVP: zero config, один файл, легко переносить
- На 1000 клиентов и 10000 занятий SQLite справляется отлично
- Миграция на Postgres = поменять строчку в config.py

---

## 📈 Что дальше (пост-MVP)

1. Замена mock'ов на реальные API (Airtable, MeritHub, Xero)
2. Inline-кнопки в Telegram (вместо `/ok 1`)
3. Веб-дашборд для координатора
4. PostgreSQL вместо SQLite
5. Redis вместо in-memory scheduler
6. CI/CD (GitHub Actions — тесты)
7. OpenRouter с реальным ключом
