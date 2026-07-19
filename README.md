<p align="center">
  <img src="https://img.shields.io/badge/status-MVP-yellow" alt="Status">
  <img src="https://img.shields.io/badge/python-3.13-blue" alt="Python">
  <img src="https://img.shields.io/badge/tests-28/28-green" alt="Tests">
  <img src="https://img.shields.io/badge/LLM-Claude%20%7C%20GPT%20%7C%20any-orange" alt="LLM">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
</p>

<h1 align="center">🤖 ALBION MVP</h1>
<p align="center"><i>AI-ассистент для координации репетиторских занятий</i></p>

---

## 🎯 Что это

ALBION автоматизирует **повторяющиеся задачи координатора** репетиторских услуг:

| Сценарий | Приоритет | Экономия времени |
|----------|-----------|-----------------|
| 🔔 **Оповещения о неявке** ученика → родителю (с кнопкой подтверждения) | №1 | ~4 часа/день |
| 📥 **Захват заявок** (leads) с AI-извлечением | №2 | ~15 мин на заявку |
| 🔄 **Отмена/перенос** занятий | №3 | ~10 мин на обращение |

## ✨ Ключевые фичи v2.1

| Фича | Описание |
|------|----------|
| 🎬 **Интерактивное демо** | Кнопочный интерфейс с выбором роли, живым сценарием и отчётом |
| 🛡 **Scheduler в SQLite** | Никаких in-memory списков. Перезапуск бота не теряет отложенные уведомления |
| ⚰️ **Dead Letter Queue** | Упавшие события не теряются — пишутся в БД, координатор получает алерт |
| 🔘 **Inline кнопки** | Вместо `/ok 1` — кнопка *"✅ Всё в порядке"* под сообщением |
| 🔌 **Kill Switch** | 3 уровня: 0=всё выкл, 1=только координаторам, 2=полностью. Безопасный деплой |
| 📨 **Честные статусы уведомлений** | `requested → delivered / failed` |
| 🗄 **WAL-mode SQLite** | Нет ошибок `database is locked` при конкурентном доступе |
| ❌ **Отмена эскалаций** | При закрытии ситуации будущие уведомления отменяются |

---

## 🏗 Архитектура (схема)

```
         Telegram Bot (Polling / Webhook)
                     │
               ┌─────▼─────┐
               │  Event    │
               │   Bus     │◄──── AI Layer (LLM via OpenRouter)
               │ 10s timeout│      Mock fallback без ключа
               │  + DLQ    │
               └─────┬─────┘
                     │
          ┌──────────┼──────────┐
          ▼          ▼          ▼
   ┌──────────┐ ┌────────┐ ┌────────┐
   │ Workflow │ │Scheduler│ │  Logs  │
   │ Handlers │ │SQLite   │ │ JSON   │
   └────┬─────┘ │ based   │ └────────┘
        │       └────────┘
   ┌────▼────────────────────────────┐
   │      Integration Layer          │
   │  Airtable (mock) │ MeritHub     │
   │  Xero (mock)     │ SQLite       │
   └─────────────────────────────────┘
```

**LLM — сменяемый слой.** Без OpenAI/Claude всё работает (mock-режим).

---

## 🚀 Быстрый старт

```bash
# 1. Склонировать
git clone <repo> && cd albion-mvp

# 2. Запустить (автоматически)
bash scripts/run.sh
# Или руками:
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Создать бота у @BotFather, получить токен
# 4. Записать токен в .env
echo "TELEGRAM_BOT_TOKEN=ваш_токен" >> .env

# 5. Запустить
python -m src.main
```

---

## 🤖 Команды бота

| Команда | Описание | Пример |
|---------|----------|--------|
| `/start` | Приветствие (в демо-режиме — выбор роли) | — |
| `/status` | Состояние системы (AI, БД, Kill Switch) | — |
| `/absent ID` | Отметить отсутствие ученика | `/absent lesson_1` |
| `/mock_absent` | 🎬 Демо: absent через 10 секунд | — |
| `/ok ID` | Закрыть инцидент (если нет кнопки) | `/ok 1` |
| `/kill_switch 0\|1\|2` | 🔌 Режим отправки сообщений | `/kill_switch 1` |

**Inline-кнопки:** при уведомлении родителю бот прикрепляет кнопку *"✅ Всё в порядке"* — нажатие сразу закрывает инцидент.

---

## 🧪 Тесты

```bash
pytest tests/ -v
# 28 passed ✅
```

## 📁 Структура проекта

```
albion-mvp/
├── src/
│   ├── main.py              # 🚀 Точка входа
│   ├── config.py            # ⚙️ Конфиг из .env
│   ├── bot/handlers.py      # 💬 Telegram + inline кнопки + kill switch
│   ├── events/
│   │   ├── bus.py           # 📡 Event Bus (10s timeout, DLQ)
│   │   └── types.py         # 📦 Event + EventTypes
│   ├── workflows/
│   │   ├── engine.py        # ⚙️ Workflow Engine (SQLite scheduler)
│   │   ├── absence.py       # Сценарий #1: неявка → уведомление
│   │   ├── lead_capture.py  # Сценарий #2: захват заявок
│   │   ├── cancellation.py  # Сценарий #3: отмена/перенос
│   │   └── dlq_handler.py   # ⚰️ Dead Letter Queue handler
│   ├── ai/
│   │   ├── client.py        # 🧠 OpenRouter + mock fallback
│   │   └── classifier.py    # 🏷 Классификация намерений
│   ├── integrations/        # 🔌 Mock Airtable, MeritHub
│   ├── db/
│   │   ├── models.py        # 🗄 Schema (WAL, scheduled_actions, DLQ)
│   │   ├── repository.py    # 📦 Repository Pattern
│   │   └── migrations.py    # 📦 Инициализация
│   └── scheduler/           # ⏰ SQLite-based scheduler
├── tests/                   # 🧪 18 тестов
├── scripts/                 # 🚀 run.sh (Linux) + run.bat (Windows)
├── docker-compose.yml       # 🐳 Для прода
├── Dockerfile               # 🐳 Для прода
├── ARCHITECTURE.md          # 📖 Для LLM-разработчиков
└── README.md                # 📖 Этот файл
```

## 🐳 Для прода

```bash
docker-compose up -d
```

## 🛡 Безопасность

- ✅ **Scheduler в SQLite** — отложенные задачи переживают рестарт
- ✅ **Dead Letter Queue** — ни одно событие не теряется
- ✅ **Kill Switch (3 уровня)** — безопасный деплой новых фич
- ✅ **Inline кнопки с nonce** — защита от повторных нажатий
- ✅ **Idempotency TTL** — авто-чистка ключей через 24ч
- ✅ **WAL-mode** — нет `database is locked`
- ✅ **Mock-режим** — без API-ключей всё работает

## 📊 Логи

- **stdout** — человекочитаемые
- **albion.log** — JSON structured (ротация 1MB × 5 файлов)

```json
{"timestamp": "2026-07-03T12:34:56Z", "level": "INFO", "module": "workflows.absence", "message": "Absence: lesson=lesson_1 inc=1 wf=1"}
```
