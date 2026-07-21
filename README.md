<p align="center">
  <img src="https://img.shields.io/badge/status-MVP-yellow" alt="Status">
  <img src="https://img.shields.io/badge/python-3.13-blue" alt="Python">
  <img src="https://img.shields.io/badge/tests-69/69-green" alt="Tests">
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

## ✨ Ключевые фичи v2.2

| Фича | Описание |
|------|----------|
| 🚀 **Демо-пилот** | `/pilot_seed` + `/pilot_absent` — прогон сценария неявки на **реальных TG-аккаунтах** владельцев |
| 👥 **Роли владельцев** | `/role` `/roles` `/whoami` — раздача ролей по TG-аккаунтам (координатор/репетитор/родитель) |
| 🔌 **Vendor Agnostic** | Реальный MeritHub подключается парой env-переменных (`src/integrations/factory.py`), без правки логики |
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
| `/whoami` | 🪪 Мой TG ID, username и роль | — |
| `/role <ID> <роль>` | 👑 Назначить роль (только владельцы) | `/role 123456789 tutor` |
| `/roles` | 👥 Список участников и ролей (только владельцы) | — |
| `/pilot_seed` | 🧪 Проверка готовности пилота (владельцы) | — |
| `/pilot_absent` | 🚀 Прогон сценария неявки на живых аккаунтах (владельцы) | — |
| `/mh_user <cuid> <TG> <имя>` | 🔗 Создать ученика в MeritHub + связать с родителем (владельцы) | `/mh_user s1 333333333 Миша` |
| `/mh_tutor <cuid> <имя>` | 🧑‍ Создать репетитора в MeritHub, role C (владельцы) | `/mh_tutor t1 Анна` |
| `/mh_enroll <classId> <cuid…>` | 📋 Зачислить в существующий класс (владельцы) | `/mh_enroll C1 s1 s2` |
| `/mh_schedule <tutor> <start> <min> <cuid…>` | 🗓 Создать класс + зачислить одной командой (владельцы) | `/mh_schedule t1 2026-07-20T15:00:00+03:00 60 s1` |
| `/mh_students` | 🔗 Список привязок MeritHub ↔ родитель (владельцы) | — |
| `/mh_events` | 🛰 Последние вебхуки MeritHub (владельцы) | — |
| `/status` | Состояние системы (AI, БД, Kill Switch) | — |
| `/absent ID` | Отметить отсутствие ученика | `/absent lesson_1` |
| `/mock_absent` | 🎬 Демо: absent через 10 секунд | — |
| `/ok ID` | Закрыть инцидент (если нет кнопки) | `/ok 1` |
| `/kill_switch 0\|1\|2` | 🔌 Режим отправки сообщений | `/kill_switch 1` |

**Inline-кнопки:** при уведомлении родителю бот прикрепляет кнопку *"✅ Всё в порядке"* — нажатие сразу закрывает инцидент.

---

## 🚀 Демо-пилот (реальные TG-аккаунты)

Пилот прогоняет MVP-сценарий **неявки ученика** на живых TG-аккаунтах владельцев:
репетитор отмечает неявку → родитель получает уведомление с кнопкой
«✅ Всё в порядке» → при молчании эскалация координатору (management by exception).

Пошаговый гайд — в **[PILOT.md](PILOT.md)**. Коротко:

```bash
# 1. .env: TELEGRAM_BOT_TOKEN, ALBION_ADMIN_TELEGRAM_IDS (TG ID владельцев)
# 2. Запуск (локально, polling):
python -m src.main
# 3. Каждый владелец: /start → /whoami (узнать свой TG ID)
# 4. Админ раздаёт роли:  /role <TG_ID> coordinator|tutor|parent
# 5. Проверка:            /pilot_seed
# 6. Прогон сценария:     /pilot_absent
```

Принцип **Vendor Agnostic**: реальный MeritHub подключается парой переменных
(`MERITHUB_CLIENT_ID`/`MERITHUB_CLIENT_SECRET`) — без правки бизнес-логики
(`src/integrations/factory.py`). Вебхук `requestType=attendance` автоматически
превращает неявку в MeritHub в уведомление родителя (`src/api/webhook.py`).

## 🧪 Тесты

```bash
pytest tests/ -v
# 69 passed ✅
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
├── tests/                   # 🧪 69 тестов
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
