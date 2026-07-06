<p align="center">
  <img src="https://img.shields.io/badge/status-MVP-yellow" alt="Status">
  <img src="https://img.shields.io/badge/python-3.13-blue" alt="Python">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  <img src="https://img.shields.io/badge/LLM-Claude%20%7C%20GPT%20%7C%20any-orange" alt="LLM">
</p>

<h1 align="center">🤖 ALBION MVP</h1>
<p align="center"><i>AI-ассистент для координации репетиторских занятий</i></p>

---

## 📋 Что это

ALBION автоматизирует **повторяющиеся задачи координатора** репетиторских услуг:

| Сценарий | Приоритет | Экономия времени |
|----------|-----------|-----------------|
| 🔔 **Оповещения о неявке** ученика → родителю | №1 | ~4 часа/день |
| 📥 **Захват заявок** (leads) с AI-извлечением | №2 | ~15 мин на заявку |
| 🔄 **Отмена/перенос** занятий | №3 | ~10 мин на обращение |

## 🏗 Архитектура

```
         Telegram Bot (Polling / Webhook)
                     │
               ┌─────▼─────┐
               │  Event    │
               │   Bus     │◄──── AI Layer (LLM via OpenRouter)
               └─────┬─────┘
                     │
          ┌──────────┼──────────┐
          ▼          ▼          ▼
   ┌──────────┐ ┌────────┐ ┌────────┐
   │ Workflow │ │Scheduler│ │  Logs  │
   │ Handlers │ │(delayed)│ │ JSON   │
   └────┬─────┘ └────────┘ └────────┘
        │
   ┌────▼────────────────────────────┐
   │      Integration Layer          │
   │  Airtable (mock) │ MeritHub     │
   │  Xero (mock)     │ SQLite       │
   └─────────────────────────────────┘
```

**Ключевая идея:** LLM — **сменяемый слой**, не ядро. Без OpenAI/Claude всё работает.

## 🚀 Быстрый старт (локально)

```bash
# 1. Склонировать
git clone <repo> && cd albion-mvp

# 2. Запустить (автоматически установит всё)
bash scripts/run.sh
# Или руками:
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Создать бота у @BotFather, получить токен
# 4. Записать токен в .env
echo "TELEGRAM_BOT_TOKEN=ваш_токен" >> .env

# 5. Запустить
python -m src.main
```

## 🤖 Команды бота

| Команда | Описание | Пример |
|---------|----------|--------|
| `/start` | Приветствие и справка | — |
| `/status` | Состояние системы | — |
| `/absent ID` | Отметить отсутствие | `/absent lesson_1` |
| `/ok ID` | Подтвердить что всё ок | `/ok 1` |

## 📁 Структура проекта

```
albion-mvp/
├── src/
│   ├── main.py              # 🚀 Точка входа
│   ├── config.py            # ⚙️ Конфиг из .env
│   ├── bot/handlers.py      # 💬 Telegram /start /absent /ok
│   ├── events/bus.py        # 📡 Event Bus (pub/sub)
│   ├── events/types.py      # 📦 Типы событий
│   ├── workflows/
│   │   ├── engine.py        # ⚙️ Workflow Engine
│   │   ├── absence.py       # Сценарий #1: неявка → уведомление
│   │   ├── lead_capture.py  # Сценарий #2: захват заявок
│   │   └── cancellation.py  # Сценарий #3: отмена/перенос
│   ├── ai/
│   │   ├── client.py        # 🧠 OpenRouter + mock fallback
│   │   └── classifier.py    # 🏷 Классификация намерений
│   ├── integrations/        # 🔌 Mock Airtable, MeritHub
│   ├── db/                  # 🗄 SQLite + Repository Pattern
│   └── scheduler/           # ⏰ Планировщик
├── tests/                   # 🧪 17 тестов
├── scripts/
│   ├── run.sh               # 🚀 Быстрый старт (Linux/Mac)
│   └── run.bat              # 🚀 Быстрый старт (Windows)
├── docker-compose.yml       # 🐳 Для прода
├── Dockerfile               # 🐳 Для прода
└── .env.example             # 🔑 Шаблон конфига
```

## 🧪 Запуск тестов

```bash
pytest tests/ -v
```

## 🐳 Для прода (когда будет готов)

```bash
docker-compose up -d
```

## 🛡 Безопасность (в MVP)

- ✅ Idempotency keys — защита от дублей webhook
- ✅ Rate limiting — 1 req/sec
- ✅ Pydantic валидация всех входов
- ✅ Prompt injection protection
- ✅ Mock-режим без API-ключей
- ✅ Structured JSON-логи для анализа

## 📊 Логирование

- **stdout** — человекочитаемые логи
- **albion.log** — JSON structured logs (1 MB ротация, 5 файлов)

Пример лога:
```json
{"timestamp": "2026-07-03T12:34:56Z", "level": "INFO", "module": "workflows.absence", "message": "Absence workflow: lesson=lesson_1 inc=1 wf=1"}
```
