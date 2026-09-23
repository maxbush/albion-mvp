# 🏗️ ALBION Architecture Blueprint: LLM-Centric Design (LLM-Ready)

> **Назначение документа:** Архитектурная спецификация и пошаговый план реализации (Runbook), разработанные специально для написания и поддержки системы силами LLM (AI-агентов).
> 
> **Проблема классической архитектуры для LLM:** Большие монолитные файлы (1000+ строк), нетипизированные словари `dict[str, Any]`, скрытые побочные эффекты и сильная связанность с UI-фреймворком (python-telegram-bot) приводят к «галлюцинациям», потере контекста и случайным регрессиям при кодогенерации.
>
> **Решение:** Чистая гексагональная архитектура (Hexagonal / Ports & Adapters) с вертикальными слайсами (Vertical Slice Architecture), строгой статической типизацией (Pydantic v2 + MyPy) и правилом атомарных файлов (≤ 200 строк).

---

## 1. Фундаментальные принципы LLM-Centric разработки

Любая современная LLM (Claude, GPT, DeepSeek, Kimi, Qwen) работает в разы эффективнее, если кодовая база спроектирована по следующим 6 правилам:

### 1.1. Правило «≤ 200 строк на файл» (File Context Limit)
- Ни один файл не должен превышать 200–250 строк.
- **Почему:** LLM тратит минимум контекстного окна на чтение файла, видит его целиком без сжатия (lossless attention) и способна переписать его за один запрос без обрезки кода (`// ... rest of the code`).

### 1.2. Нулевая терпимость к нетипизированным `dict` (Strict Pydantic DTO)
- В бизнес-логике и событиях **запрещены** сырые `dict[str, Any]` и неявные ключи вроде `event.data.get("subClassId")`.
- Все входные, выходные данные и события описываются строгими моделями `Pydantic v2` / `dataclasses`.
- **Почему:** Тайпчекер (`mypy`/`pyright`) и Pydantic мгновенно ловят ошибки LLM на этапе синтаксического анализа, не дожидаясь рантайма.

### 1.3. Чистое ядро без зависимостей (Pure Domain Core)
- В папке `src/domain/` нет ни одного импорта внешних библиотек (`telegram`, `fastapi`, `aiosqlite`, `httpx`). Только Python 3.11+ stdlib и Pydantic.
- Вся математика (пересечение интервалов, таймзоны, 24-часовая политика отмен) — это чистые детерминированные функции: `f(inputs) -> output`.
- **Почему:** LLM пишет чистые функции со 100% точностью и без побочных эффектов.

### 1.4. Явные конечные автоматы (Explicit State Machines)
- Сложные сценарии (неявка ученика, согласование переноса) не пишутся через лапшу из 20 вложенных `if/elif/else`.
- Сценарий формализуется как таблица переходов: `State + Event -> NewState + List[Command]`.
- **Почему:** У LLM нет шанса пропустить или забыть ветку условия: таблица переходов исчерпывающа и проверяется матричным тестом.

### 1.5. Порты и адаптеры (Hexagonal / Protocols)
- Домен и Use Cases взаимодействуют с внешним миром только через `typing.Protocol`:
  - `MessengerPort` (отправка текста и кнопок — абстракция над TG и WhatsApp).
  - `LessonPlatformPort` (MeritHub, Airtable, Mock).
  - `RepositoryPort` (БД: SQLite / Postgres / In-Memory).
- **Почему:** Чтобы протестировать или заменить Telegram на WhatsApp или SQLite на Postgres, LLM пишет один изолированный класс-адаптер, не трогая ни строчки бизнес-логики.

### 1.6. Test Harness First (In-Memory TDD для LLM)
- На каждый порт создается `InMemoryFake` (в 30-50 строк кода).
- Все сценарии тестируются без поднятия сетевых сокетов, баз данных или моков httpx.
- **Скорость тестов:** 250+ тестов выполняются за 0.5–1 секунду.
- **Почему:** LLM запускает тесты после каждого изменения и сразу видит, решила ли она задачу или сломала контракт.

---

## 2. Целевая структура репозитория

```
src/
├── config.py                          # Pydantic Settings (.env)
│
├── domain/                            # СЛОЙ 1: ЧИСТЫЙ ДОМЕН (0 зависимостей)
│   ├── models/                        # Доменные сущности
│   │   ├── user.py                    # User, UserRole, ChannelPreference
│   │   ├── lesson.py                  # Lesson, ClassType, RecurrencePattern
│   │   └── incident.py                # Incident, IncidentType, Resolution
│   ├── policies/                      # Чистая бизнес-логика (детерминированная)
│   │   ├── cancellation_policy.py     # Правило 24 часов, платная/бесплатная отмена
│   │   ├── conflict_detector.py       # Интервалы [S, E), пересечения perma/onetime
│   │   └── timezone_engine.py         # Org timezone London, DST-сдвиги, dual-display
│   └── events/                        # Строгие DTO событий
│       ├── base.py                    # DomainEvent, EventMetadata
│       ├── lesson_events.py           # LessonCancelled, LessonAbsent, RescheduleRequested
│       └── message_events.py          # MessageIncoming, ButtonClicked
│
├── application/                       # СЛОЙ 2: ЮЗКЕЙСЫ (Оркестрация)
│   ├── ports/                         # Интерфейсы (Protocols)
│   │   ├── messenger_port.py          # IMessengerGateway (send_message, send_buttons)
│   │   ├── platform_port.py           # ILessonPlatform (create_class, get_status)
│   │   ├── repository_ports.py        # IUserRepository, ILessonRepository, IIncidentRepository
│   │   └── scheduler_port.py          # ISchedulerService (schedule_action, cancel_action)
│   └── use_cases/                     # Сценарии (1 файл = 1 use case ≤ 150 строк)
│       ├── absence/                   # Сценарий неявки
│       │   ├── handle_no_show.py      # Триггер неявки (от тьютора или вебхука)
│       │   ├── process_parent_reply.py# Обработка ответа родителя (кнопка / текст)
│       │   └── escalate_to_coord.py   # Эскалация по таймеру
│       ├── cancellation/              # Сценарий отмены и переноса
│       │   ├── request_cancellation.py# Расчет условий, показ предупреждения
│       │   ├── confirm_cancellation.py# Исполнение отмены (платная/бесплатная)
│       │   └── request_reschedule.py  # Запрос переноса, уведомление координатора
│       └── scheduling/                # Сценарий планирования
│           ├── validate_schedule.py   # Проверка конфликтов репетитора
│           └── create_class_flow.py   # Вызов платформы + сохранение в репозиторий
│
├── infrastructure/                    # СЛОЙ 3: АДАПТЕРЫ И ВНЕШНИЙ МИР
│   ├── database/                      # База данных
│   │   ├── connection.py              # SQLite aiosqlite, PRAGMA WAL, connection pool
│   │   ├── migrations/                # Версионированные миграции (001, 002...)
│   │   └── sqlite_repos.py            # Реализация repository_ports через SQL
│   ├── messaging/                     # Транспорт сообщений
│   │   ├── channel_router.py          # Маршрутизация Telegram vs WhatsApp
│   │   ├── telegram_gateway.py        # Реализация MessengerPort через Bot API
│   │   └── whatsapp_gateway.py        # Реализация MessengerPort через Meta Cloud API
│   ├── platforms/                     # Внешние платформы
│   │   ├── merithub_client.py         # Реальный OAuth2+JWT клиент MeritHub
│   │   └── platform_factory.py        # Real ↔ Mock переключатель
│   └── scheduler/                     # Планировщик
│       └── persistent_scheduler.py    # SQLite/Memory фоновый таймер с DLQ
│
└── entrypoints/                       # СЛОЙ 4: ТОЧКИ ВХОДА (Тонкие адаптеры UI/API)
    ├── tg_bot/                        # Telegram Bot UI
    │   ├── bot_app.py                 # Сборка Application
    │   ├── handlers/                  # Команды бота (тонкие: вызывают use cases)
    │   │   ├── common_handlers.py     # /start, /whoami, /status, /role
    │   │   └── lesson_handlers.py     # /cancel_lesson, /lessons, /schedule
    │   └── wizards/                   # Пошаговые UI-диалоги
    │       ├── schedule_wizard.py     # Пошаговый выбор репетитора/времени
    │       └── person_wizard.py       # Добавление ученика/тьютора
    ├── api/                           # FastAPI
    │   ├── app.py                     # Создание FastAPI приложения
    │   ├── routes_merithub.py         # POST /merithub/webhook
    │   ├── routes_whatsapp.py         # GET/POST /whatsapp/webhook
    │   └── routes_monitoring.py       # GET /health, GET /metrics
    └── cli/                           # CLI-утилиты
        └── seed_and_migrate.py        # Миграция реальных 18 учеников и координаторов
```

---

## 3. Спецификация контрактов (Typed Contracts)

### 3.1. Доменные модели (`src/domain/models/`)

```python
# src/domain/models/lesson.py
from datetime import datetime
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field

class ClassType(str, Enum):
    ONE_TIME = "oneTime"
    PERMA = "perma"

class Lesson(BaseModel):
    class_id: str
    title: str
    tutor_cuid: str
    class_type: ClassType = ClassType.ONE_TIME
    start_time: str                    # ISO 8601 строка в org timezone
    duration_min: int = 60
    schedule_days: List[int] = Field(default_factory=list) # 0=Sun..6=Sat
    end_date: Optional[str] = None
    host_link: Optional[str] = None
    participant_link: Optional[str] = None
```

```python
# src/domain/models/user.py
from enum import Enum
from typing import Optional
from pydantic import BaseModel

class UserRole(str, Enum):
    PARENT = "parent"
    TUTOR = "tutor"
    COORDINATOR = "coordinator"
    STUDENT = "student"

class ChannelPreference(str, Enum):
    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"
    AUTO = "auto"

class ContactUser(BaseModel):
    cuid: str
    name: str
    role: UserRole
    phone: Optional[str] = None
    telegram_id: Optional[str] = None
    email: Optional[str] = None
    timezone: str = "Europe/London"
    preferred_channel: ChannelPreference = ChannelPreference.AUTO
```

### 3.2. Доменная политика отмен (`src/domain/policies/cancellation_policy.py`)

```python
from datetime import datetime
from pydantic import BaseModel

class CancellationPolicyResult(BaseModel):
    is_paid: bool
    hours_left: float
    hours_left_str: str
    warning_message: str

def evaluate_cancellation_policy(
    lesson_dt: datetime,
    current_dt: datetime,
    deadline_hours: int = 24,
) -> CancellationPolicyResult:
    """Чистая функция: расчет статуса отмены и предупреждения по правилу 24ч."""
    hours_left = (lesson_dt - current_dt).total_seconds() / 3600.0
    is_paid = hours_left < deadline_hours

    if hours_left < 1.0:
        hours_str = f"{max(int(hours_left * 60), 1)} мин"
    elif hours_left < 24.0:
        hours_str = f"{hours_left:.1f} ч"
    else:
        d = int(hours_left // 24)
        h = int(hours_left % 24)
        hours_str = f"{d} дн {h} ч" if h else f"{d} дн"

    if is_paid:
        msg = (
            f"⚠️ Внимание: до урока осталось {hours_str} (менее {deadline_hours} ч).\n"
            f"По правилам центра, отмена менее чем за {deadline_hours}ч является ПЛАТНОЙ "
            f"(100% списание с баланса). Репетитор получит компенсацию.\n\n"
            f"Подтверждаете платную отмену?"
        )
    else:
        msg = (
            f"ℹ️ До урока осталось {hours_str} (более {deadline_hours} ч).\n"
            f"✅ Отмена бесплатная — списание с баланса производиться не будет.\n\n"
            f"Подтвердить отмену занятия?"
        )

    return CancellationPolicyResult(
        is_paid=is_paid,
        hours_left=hours_left,
        hours_left_str=hours_str,
        warning_message=msg,
    )
```

### 3.3. Доменная политика пересечений (`src/domain/policies/conflict_detector.py`)

```python
from typing import List, Optional
from pydantic import BaseModel
from src.domain.models.lesson import Lesson, ClassType

class ConflictDetails(BaseModel):
    conflicting_class_id: str
    conflicting_title: str
    existing_range: str
    proposed_range: str
    overlap_minutes: int
    message: str

def find_schedule_conflicts(
    existing_lessons: List[Lesson],
    target_tutor_cuid: str,
    proposed_class_type: ClassType,
    proposed_start_hhmm: str,          # "15:30"
    proposed_duration: int,            # 60
    proposed_days: List[int],          # [1, 3]
    proposed_date: Optional[str] = None, # "YYYY-MM-DD"
) -> List[ConflictDetails]:
    """Чистая функция: интервальное пересечение полуинтервалов [S, E)."""
    # Логика: max(S1, S2) < min(E1, E2) с проверкой общих дней недели.
    ...
```

### 3.4. Абстрактные порты (`src/application/ports/`)

```python
# src/application/ports/messenger_port.py
from typing import Protocol, List, Optional
from pydantic import BaseModel

class InlineButton(BaseModel):
    text: str
    callback_data: str

class IMessengerGateway(Protocol):
    async def send_text(
        self,
        recipient_id: str,
        text: str,
        channel: Optional[str] = None,
    ) -> bool:
        ...

    async def send_interactive(
        self,
        recipient_id: str,
        text: str,
        buttons: List[InlineButton],
        channel: Optional[str] = None,
    ) -> bool:
        ...
```

---

## 4. Спецификация стейт-машин (FSM)

### 4.1. Сценарий неявки (Absence State Machine)
```
       [ Триггер неявки ]
      (Tutor / MH Webhook)
               │
               ▼
     [ STATE: PROMPT_PARENT ] ── (1 мин) ──▶ [ Отправка кнопок родителю ]
               │                                      │
     ┌─────────┴─────────┐                            │
     │                   │                            ▼
  [ Ответ:           [ Ответ:                 [ STATE: AWAITING_REPLY ]
  ✅ Всё ок ]      ❌ Не придём ]                      │
     │                   │                       (Таймер 2 мин)
     │                   │                            │
     ▼                   ▼                            ▼
[ RESOLVED:         [ RESOLVED:              [ STATE: ESCALATED ]
  parent_ok ]       parent_absent ]                   │
                                              [ Алерт координаторам ]
                                                      │
                                                      ▼
                                             [ Координатор закрыл
                                                в 1 клик /ok ]
```

---

## 5. Пошаговый Execution Plan для LLM (Prompt-by-Prompt)

Если проект создается с нуля или рефакторится, LLM должна следовать **10 строго изолированным шагам**. Каждый шаг содержит:
1. Задачу (что создать).
2. Зависимости (какие файлы подать в контекст).
3. Проверочный тест (Acceptance Test).

---

### Шаг 1. Конфигурация и типизированные доменные модели
- **Файлы:** `src/config.py`, `src/domain/models/user.py`, `src/domain/models/lesson.py`, `src/domain/models/incident.py`.
- **Контекст для LLM:** Pydantic v2 правила, настройки из `.env`.
- **Критерий приёмки:** Модели валидируют корректные данные и выбрасывают `ValidationError` на некорректных.
- **Тест:** `tests/domain/test_models.py` (зелёный).

---

### Шаг 2. Чистые доменные политики (Pure Policies)
- **Файлы:**
  - `src/domain/policies/cancellation_policy.py` (правило 24ч, форматирование).
  - `src/domain/policies/conflict_detector.py` (пересечения интервалов $[S, E)$).
  - `src/domain/policies/timezone_engine.py` (London org zone, DST).
- **Контекст для LLM:** Только `src/domain/models/`. Внешние библиотеки запрещены.
- **Критерий приёмки:** Тесты на все краевые случаи: стык в стык (15:00-16:00 и 16:00-17:00 — нет конфликта), переход через полуночь, DST переключение GMT->BST.
- **Тест:** `tests/domain/test_policies.py` (зелёный, время выполнения < 0.05с).

---

### Шаг 3. Контракты портов и In-Memory Test Harness
- **Файлы:**
  - `src/application/ports/messenger_port.py`
  - `src/application/ports/platform_port.py`
  - `src/application/ports/repository_ports.py`
  - `src/application/ports/scheduler_port.py`
  - `tests/fakes/in_memory_gateways.py` (фейки всех портов).
- **Контекст для LLM:** `typing.Protocol`, чистые списки в памяти для записи вызовов.
- **Критерий приёмки:** Фейки реализуют все методы протоколов.

---

### Шаг 4. Use Case: Отмена и перенос занятий (Cancellation & Reschedule)
- **Файлы:**
  - `src/application/use_cases/cancellation/request_cancellation.py`
  - `src/application/use_cases/cancellation/confirm_cancellation.py`
  - `src/application/use_cases/cancellation/request_reschedule.py`
- **Контекст для LLM:** Шаги 1, 2, 3.
- **Критерий приёмки:**
  - Отмена >24ч -> `is_paid=False`, бесплатное сообщение родителям, репетитору и координатору.
  - Отмена <24ч -> `is_paid=True`, статус платной отмены, уведомление о списании.
  - Перенос -> создание инцидента, алерт координатору с кнопками.
- **Тест:** `tests/application/test_cancellation_use_cases.py` (через In-Memory Fakes).

---

### Шаг 5. Use Case: Неявка и эскалация (Absence Workflow)
- **Файлы:**
  - `src/application/use_cases/absence/handle_no_show.py`
  - `src/application/use_cases/absence/process_parent_reply.py`
  - `src/application/use_cases/absence/escalate_to_coord.py`
- **Контекст для LLM:** Модели, порты, FSM неявки.
- **Критерий приёмки:** Полный цикл от триггера до ответа родителя кнопкой или эскалации при молчании. Одноразовый `nonce`.
- **Тест:** `tests/application/test_absence_use_cases.py`.

---

### Шаг 6. WhatsApp Meta Cloud API и Channel Router
- **Файлы:**
  - `src/infrastructure/messaging/whatsapp_gateway.py` (HTTP клиент Meta Graph API v20.0).
  - `src/infrastructure/messaging/channel_router.py` (маршрутизация Telegram vs WhatsApp по `preferred_channel` / номеру).
- **Контекст для LLM:** Meta Cloud API спецификация: интерактивные кнопки (до 3 шт), шаблоны, нормализация телефонов E.164.
- **Критерий приёмки:** Отправка текстовых и интерактивных сообщений, корректная деградация при отсутствии одного из каналов.
- **Тест:** `tests/infrastructure/test_whatsapp_gateway.py`.

---

### Шаг 7. База данных SQLite с WAL и версионированными миграциями
- **Файлы:**
  - `src/infrastructure/database/connection.py` (PRAGMA WAL, busy_timeout=10000, 64MB cache).
  - `src/infrastructure/database/migrations/runner.py` (таблица `schema_migrations`, версионированные шаги 001..005).
  - `src/infrastructure/database/sqlite_repos.py` (реализация репозиториев через aiosqlite).
- **Критерий приёмки:** Идемпотентный запуск миграций, индексы для 100+ тьюторов.
- **Тест:** `tests/infrastructure/test_sqlite_repos.py`.

---

### Шаг 8. MeritHub клиент и вебхуки
- **Файлы:**
  - `src/infrastructure/platforms/merithub_client.py` (OAuth2 + JWT клиент, create-only модель).
  - `src/entrypoints/api/routes_merithub.py` (приёмник `classStatus` и `attendance`).
  - `src/entrypoints/api/routes_whatsapp.py` (верификация hub.challenge + приём сообщений).
  - `src/entrypoints/api/routes_monitoring.py` (`/health` и `/metrics`).
- **Критерий приёмки:** Обработка attendance генерирует события неявки; GET webhook верифицируется; `/health` возвращает 200.
- **Тест:** `tests/entrypoints/test_api_routes.py`.

---

### Шаг 9. Telegram Bot UI (Тонкие обработчики)
- **Файлы:**
  - `src/entrypoints/tg_bot/handlers/common_handlers.py`
  - `src/entrypoints/tg_bot/handlers/lesson_handlers.py`
  - `src/entrypoints/tg_bot/wizards/schedule_wizard.py`
- **Контекст для LLM:** Обработчики ТОЛЬКО валидируют входящий Update Telegram, передают параметры в соответствующий Use Case и отправляют ответ. Никакой прямой работы с SQL в UI!
- **Критерий приёмки:** Все команды (`/start`, `/status`, `/schedule`, `/cancel_lesson`) работают и покрыты тестами с FakeBot/FakeUpdate.
- **Тест:** `tests/entrypoints/test_tg_handlers.py`.

---

### Шаг 10. Миграция данных и Production сборка
- **Файлы:**
  - `src/cli/seed_and_migrate.py` (18 учеников, таймзоны, 4 координатора).
  - `docker-compose.yml`, `Dockerfile`.
- **Критерий приёмки:** Скрипт миграции успешно накатывает реальные данные без дубликатов при повторных запусках. Контейнеры запускаются и проходят healthcheck.
- **Тест:** Сквозной интеграционный прогон `tests/test_e2e_full_flow.py`.

---

## 6. Codegen Guidelines для AI-агентов (Правила чистого кода)

При генерации кода любая LLM обязана соблюдать следующие системные ограничения:

1. **Explicit Imports:** Запрещены wildcard-импорты (`from module import *`).
2. **Type Annotations:** Все функции обязаны иметь type hints для аргументов и возвращаемого значения (включая `-> None`).
3. **No Magic Strings:** Константы, имена очередей, события и статусы объявляются в `Enum`.
4. **Error Handling:** Ошибки оборачиваются в доменные исключения (`LessonNotFoundError`, `TutorConflictError`), а не сырые `Exception` или `KeyError`.
5. **No Framework Leakage:** В `domain/` и `application/` запрещено импортировать классы Telegram (`Update`, `Context`, `InlineKeyboardMarkup`).
6. **Zero-Warning Rule:** Код обязан проходить линтеры `flake8` / `ruff` и статический анализатор без предупреждений.
