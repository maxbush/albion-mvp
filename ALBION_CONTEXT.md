# 📋 ALBION — Полная база знаний проекта

> Этот файл — единый источник истины для любой LLM или участника проекта.
> Содержит ВСЁ: клиент, бизнес, MeritHub, архитектура, решения, данные.
> Последнее обновление: 2026-07-27

---

## 1. О КЛИЕНТЕ: ALBION

### Что это
ALBION — учебный центр репетиторских услуг. Организуют индивидуальные онлайн-занятия между репетиторами и учениками (школьники, подготовка к экзаменам).

### Ключевые люди
- **Координаторы** — ведут клиентов, подбирают репетиторов, согласуют расписание, разбирают неявки
- **Финансовый директор** — счета, оплаты репетиторам
- **Репетиторы** — самозанятые, проводят занятия онлайн
- **Клиенты** — родители + ученики (в основном UK, Казахстан, Россия, ОАЭ, Франция, Австрия)

### Команда ALBION (контакты из выгрузки)
- Координаторы используют email `@albionconsult.co.uk`:
  - Victoria Eremayeva: `a.yeshmatova@albionconsult.co.uk`, +380505292480
  - Dmitriy Lazarev: `altysha79@yahoo.co.uk` (Ukraine)
  - Dilyara Mazhitova: `a.bashkirova@albionconsult.co.uk` (Kazakhstan)
  - Valeria Kaminsky: `albionconcierge@gmail.com` (UK)
- Контактный номер клиента: +44 7493 994501
- Vladimir — часто подбирает репетиторов и задаёт вопросы клиентам

### Цитаты клиента (прямая речь, важные для понимания контекста)

**Про боль №1 — неявки:**
> "Поиск отсутствующих на занятии. Отдельная большая боль: преподаватели пишут координатору, что ученика нет на уроке; на разбор таких ситуаций уходит до половины рабочего дня."

**Про расписание и часовые пояса:**
> "Это время мы даем клиенту в его местном времени. А сами еще в своем времени общаемся потому что тьюторы в этом времени. А бывает что у клиента тьюторы в 4х часовых зонах так как они в казахстане например. Тогда много ошибок может быть."

**Про создание уроков:**
> "Частично из-за user interface, частично из-за того что мы ручками делаем ошибки."
> "Урок был не правильно создан, и это никто не заметил потому что коммуникация была в 2х часовых зонах и тоже произошла накладка."
> "Это же расписание должно быть точно занесено в меритхабе. Как правило мы заносим его в британском времени."

**Про процесс создания урока:**
> "После того как лид отфильтровали, Владимир (чаще) или я (реже) задает клиенту вопросы:
> - Какие предметы нужны
> - В каком году ученик
> - Какой exam board по каждому предмету
> - Сколько часов по каждому предмету в неделю
> - Удобное время занятий (пн-пт и выходные)"
> "Из ответа мы формируем программу, выбираем преподавателей (и с ними в это время согласовываем желаемое клиентом время), предлагаем преподавателей и согласовываем с клиентом частоту/время занятий и стоимость."
> "Как правило новые клиенты хотят 1 пробный урок, иногда хотят попробовать 2 преподавателя, некоторые просто следуют рекомендации."
> "После оплаты или пробного урока или сразу депозита на несколько уроков — создаем в MeritHub вручную уроки."

**Про приоритеты:**
> "Координация почти полностью ручная (WhatsApp, MeritHub, Airtable, ИИ для расписаний). Работает, но плохо масштабируется — особенно расписания при мультипреподавателях, замены репетиторов, поиск отсутствующих, рассылка отчётности и контроль оплат."

---

## 2. ТЕКУЩИЙ WORKFLOW КЛИЕНТА ("как есть")

```
1. ЗАПРОС: Клиент пишет в WhatsApp → Координатор уточняет предмет, уровень, цель, экзамен
2. ПОДБОР: Координатор ищет репетитора в Airtable/MeritHub по тегам
3. СОГЛАСОВАНИЕ: Связываются с репетитором → согласуют время → предлагают клиенту
4. ОПЛАТА: Финдиректор выставляет счёт → клиент платит (вперёд, банк. перевод)
5. СОЗДАНИЕ УРОКА: Координатор вручную создаёт урок в MeritHub
   - Ставит ставку (привязана к каждому занятию вручную — ОСНОВНОЙ ИСТОЧНИК ОШИБОК)
   - В британском времени
6. ПРОВЕДЕНИЕ: Только в MeritHub (видео + доска + запись)
7. НЕЯВКИ: Преподаватель пишет координатору → координатор ищет родителя → разбирается
8. ОТМЕНЫ: Через координатора. Правило: занятия идут постоянно пока явно не отменили
9. ОТЧЁТНОСТЬ: Рассылка участникам — вручную
10. ОПЛАТА РЕПЕТИТОРАМ: Еженедельно, задержка 1 неделя, сверка в Xero
```

### Инструменты клиента
- **WhatsApp** — основная коммуникация
- **MeritHub** — платформа для уроков (видео, доска, запись, расписание)
- **Airtable** — CRM (база репетиторов, теги по предметам). Сейчас вынесен за скобки проекта.
- **Xero** — бухгалтерия
- **ИИ** — для составления расписаний при нескольких репетиторах у одного ученика

---

## 3. MERITHUB — платформа для онлайн-уроков

### Структура Users в UI
| Раздел | Описание | Используем? |
|--------|----------|------------|
| **Admins** | Администраторы организации | Нет (у нас свои админы) |
| **Instructors** | Репетиторы (role=C в API) | ✅ Да |
| **Learners** | Ученики (role=M в API) | ✅ Да |
| **Subscribers** | Подписчики | Нет |
| **Parents/Clients** | Родители — почти пустая вкладка, только email, нет полноценной карточки | ❌ Нет |

> **Важно:** Координаторы ALBION хранят контакты родителей **прямо в карточке ученика** (Learner), а не в Parents/Clients.

### API MeritHub

**Авторизация:** OAuth2 + JWT (HS256, секрет = CLIENT_SECRET)
- Access token живёт 60 минут, обновляем за 5 минут до истечения
- Максимум 10 запросов токена в час

**Эндпоинты:**
```
POST /v1/{CLIENT_ID}/api/token          — получить access token
POST /v1/{CLIENT_ID}/users              — добавить юзера (C=instructor, M=learner)
PUT  /v1/{CLIENT_ID}/users/{USER_ID}    — обновить юзера
DELETE /v1/{CLIENT_ID}/users/{USER_ID}  — удалить юзера
POST /v1/{CLIENT_ID}/{INSTRUCTOR_ID}    — создать класс (schedule_class)
PUT  /v1/{CLIENT_ID}/{CLASS_ID}         — редактировать класс
POST /v1/{CLIENT_ID}/{CLASS_ID}/users   — добавить юзеров в класс
POST /v1/{CLIENT_ID}/{CLASS_ID}/removeuser — удалить из класса
DELETE /v1/{CLIENT_ID}/{CLASS_ID}       — удалить класс
```

**Webhooks (push от MeritHub к нам):**
```json
// classStatus — когда класс starts/ends
{"requestType": "classStatus", "classId": "...", "status": "lv|cp|cl|ex", "startTime": "..."}

// attendance — после окончания урока, кто присутствовал
{"requestType": "attendance", "classId": "...", "attendance": [
  {"userId": "...", "totalTime": 244, "startTime": "...", "endTime": "..."}
]}

// recording — когда запись готова
{"requestType": "recording", "classId": "...", "url": "..."}

// classFiles — файлы использованные на уроке
{"requestType": "classFiles", "Files": [...]}

// chats — логи чатов после урока
{"requestType": "chats", "chats": {"public": "url", "private": [...]}}
```

**Критические ограничения MeritHub:**
- ❌ НЕ отдаёт список юзеров через API (только создаёт)
- ❌ НЕ отдаёт список классов через API
- ❌ Parents/Clients API — практически отсутствует
- ✅ Отдаёт classId/userId при создании — мы храним их у себя
- ✅ Webhook attendance — позволяет считать неявки
- ✅ Webhook classStatus — позволяет детектить "урок не начался"

### Хосты
```
Service API:  https://serviceaccount1.meritgraph.com
Class API:    https://class1.meritgraph.com
Live rooms:   https://live.merithub.com/info/room/{CLIENT_ID}/{link}
```

---

## 4. РЕАЛЬНЫЕ ДАННЫЕ КЛИЕНТА

### 18 учеников (из выгрузки MeritHub)

| Имя | Timezone | Country | Родитель (Customer) |
|-----|----------|---------|---------------------|
| Ernest Mezheritsky | Europe/London | UK | Victoria Eremayeva |
| Alexandra Mironova | Asia/Dubai | UAE | — |
| Felix Stazhynski | Europe/London | UK | Daria Stazhynskaya |
| Roman Lazarev | Europe/London | UK | Dmitriy Lazarev |
| Artem Stoklitskiy | Europe/London | UK | Eva Kriss |
| Eva Kriss | Europe/Vienna | Austria | Victoria Eremayeva |
| Oleg Burylov | Europe/Moscow | Russia | — |
| Lion Lebedev | Europe/London | UK | Sofia Dimitrova |
| Makar Baranov | Europe/Moscow | Russia | — |
| Saveliy Koktysh | Europe/London | UK | Alexander Radostovets |
| Alexander Radostovets | Asia/Almaty | Kazakhstan | Dilyara Mazhitova |
| Marta Kaminsky | Europe/London | UK | Valeria Kaminsky |
| Slava Moscovoy | Europe/Moscow | Russia | — |
| Maria Onischuk | Europe/London | UK | Sergey Kolpakov |
| Jana Hoffmann | Europe/Paris | France | Alisa Fedoseev |
| Alisa Fedoseev | Europe/London | UK | — |
| Sergey Kolpakov | Europe/London | UK | — |
| Sofia Dimitrova | Europe/London | UK | — |

### 6 часовых поясов
- Europe/London (большинство) — **canonical**
- Europe/Moscow (+3ч)
- Asia/Almaty (+5ч)
- Asia/Dubai (+4ч)
- Europe/Vienna (+1ч)
- Europe/Paris (+1ч)

---

## 5. НАША РЕАЛИЗАЦИЯ: ALBION AI Assistant

### Что это
Telegram-бот + платформа автоматизации на Python. НЕ чат-бот, а **операционная система координации**.

### Стек
```
Python 3.11+ | python-telegram-bot | FastAPI (webhooks)
aiosqlite (WAL) | httpx | pydantic-settings
LLM: deepseek/deepseek-v4-flash (через OpenRouter, mock fallback)
```

### Архитектура (слои)
```
Telegram Bot (polling/webhook)
    ↓
Event Bus (pub/sub, 10s timeout, DLQ)
    ↓
Workflow Engine (state machine + SQLite scheduler)
    ↓
Integration Layer (Vendor Agnostic: mock ↔ real MeritHub)
    ↓
SQLite (WAL, авто-миграции)
```

### Ключевые модули

| Модуль | Файл | Назначение |
|--------|------|-----------|
| Bot handlers | `src/bot/handlers.py` | TG-команды, inline-кнопки, self-registration |
| Pilot/Demo | `src/bot/pilot.py` | /pilot_*, /mh_*, /seed10, /import_*, /morning |
| Roles | `src/bot/roles.py` | /role, /whoami, notify_all_coordinators() |
| Absence workflow | `src/workflows/absence.py` | Неявка → parent notification → escalate |
| Lesson ops | `src/workflows/lesson_ops.py` | Pre-lesson reminders, tutor start check, class live check |
| MeritHub client | `src/integrations/merithub_client.py` | OAuth2+JWT, schedule, attendance |
| Webhook receiver | `src/api/webhook.py` | FastAPI, classStatus + attendance dispatch |
| Scheduler | `src/scheduler/scheduler.py` | SQLite-based, claim/retry/DLQ |
| DB Repository | `src/db/repository.py` | 12 репозиториев (users, incidents, workflows, etc.) |

### Реализованные сценарии

#### Сценарий 1: Неявка ученика (главная боль)
```
Источник (3 варианта):
  - /pilot_absent (демо)
  - Tutor нажимает "👤 Ученик не пришёл" (tutor_start_check)
  - MeritHub attendance webhook (после урока)

Флоу:
  1. Через 1 мин → parent получает 3 кнопки: ✅/❌/⏰
  2. Parent отвечает (кнопка или текст) → AI интерпретирует → координатор получает outcome
  3. Parent молчит 2 мин → эскалация координатору
  4. Parent отвечает после эскалации → инцидент закрывается + координатор видит поздний ответ
```

#### Сценарий 2: Pre-lesson reminders
```
За 15 мин до урока:
  Parent: "⏰ Через 15 мин занятие. 🕐 15:00 (London) / 20:00 (ваше время, Asia/Almaty)"
  Tutor:  "🧑‍🏫 Через 15 мин урок."

В момент начала урока:
  Tutor: "▶️ Время урока. [✅ Начался] [👤 Ученик не пришёл] [🛠 Техпроблема]"

Через 5 мин после старта:
  Если classStatus ≠ lv → "🚨 Урок не перешёл в live" (с контекстом)
```

#### Сценарий 3: Class not live detection
```
Умные сообщения в зависимости от контекста:
  - Tutor подтвердил готовность → "техпроблема / ученик не подключился"
  - Tutor отметил absent → "уведомление уже отправлено"
  - Tutor не ответил → "репетитор не подключился / неявка"
```

### Команды бота (полный список)

```
ОБЩИЕ:          /start /status /whoami
РОЛИ:           /role /roles
ИНЦИДЕНТЫ:      /absent /ok /incidents
МЕМОРИТУБ:     /mh_user /mh_tutor /mh_schedule /mh_enroll /mh_students /mh_events /mh_contact /mh_contacts
ИМПОРТ:         /import_learners /import_customers
ДЕМО:           /pilot_seed /pilot_absent /seed10 /demo_reset
МОНИТОРИНГ:     /today /morning /kill_switch
```

---

## 6. ПРИНЯТЫЕ РЕШЕНИЯ

| Решение | Почему |
|---------|--------|
| **London canonical** | Клиент создаёт уроки в британском времени. Все занятия в MeritHub → `Europe/London` |
| **Dual-time display** | `"15:00 (London) / 20:00 (ваше время, Asia/Almaty) [+5ч к London]"` |
| **Не использовать Parents/Clients API** | Вкладка пустая, нет карточки. Контакты родителя храним у себя |
| **Telegram-first** | WhatsApp — на следующем этапе (Twilio). Слой коммуникаций абстрагирован |
| **Vendor Agnostic** | MeritHub/Airtable/LLM — любые можно заменить одной env-переменной |
| **SQLite для MVP** | Нет внешних зависимостей. WAL + busy_timeout. Connection pool — для прода |
| **Event Bus** | Все компоненты общаются через события. 10s timeout, DLQ, идемпотентность |
| **Self-registration** | /start → 3 кнопки → роль. Не нужен /whoami → copy ID → admin /role |
| **AI only for NLU** | LLM интерпретирует текст. Бизнес-логика — детерминированная |
| **Kill Switch 3 уровня** | 0=off, 1=coordinators only, 2=full. Безопасный деплой |
| **Tutor start check → auto-absence** | Одна кнопка "ученик не пришёл" → auto-flow. Без free-text absent |

---

## 7. ПРИОРИТЕТЫ КЛИЕНТА (Roadmap)

| # | Задача | Статус |
|---|--------|--------|
| 1 | **Неявки** — автоматизация поиска отсутствующих | ✅ MVP готов |
| 2 | **Расписания** — создание и пересборка при заменах | ⏳ Partial (создание есть, пересборка нет) |
| 3 | **Контроль оплат** — напоминания о низком балансе | ⏳ Not started |
| 4 | **Захват лидов** — воронка продаж | ⏳ Basic (lead_capture workflow есть, но не используется) |
| 5 | **Отчётность** — сводки по занятиям и прогрессу | ⏳ Not started |
| 6 | **Сверка комиссий** — автоматический расчёт | ⏳ Not started |
| 7 | **Многоязычная поддержка** — перевод | ⏳ Not started |
| 8 | **WhatsApp** — через Twilio | ⏳ Not started |

---

## 8. СТРУКТУРА РЕПОЗИТОРИЯ

```
albion-mvp/
├── src/
│   ├── main.py                    # Точка входа (polling / webhook)
│   ├── config.py                  # Pydantic settings (.env)
│   ├── bot/
│   │   ├── handlers.py            # TG-команды, callbacks, self-registration
│   │   ├── pilot.py               # Демо, MeritHub, импорт, мониторинг
│   │   └── roles.py               # Роли, notify_all_coordinators()
│   ├── workflows/
│   │   ├── absence.py             # Сценарий неявки (главный)
│   │   ├── lesson_ops.py          # Pre-lesson, tutor start, class live
│   │   ├── cancellation.py        # Отмена/перенос
│   │   ├── lead_capture.py        # Захват лидов
│   │   ├── engine.py              # Workflow Engine + scheduler
│   │   └── dlq_handler.py         # Dead Letter Queue
│   ├── ai/
│   │   ├── client.py              # LLM (interpret_parent_reply, classify, extract)
│   │   └── classifier.py          # Intent classifier
│   ├── integrations/
│   │   ├── merithub_client.py     # OAuth2+JWT клиент MeritHub
│   │   ├── factory.py             # Vendor Agnostic switch (mock ↔ real)
│   │   ├── airtable_mock.py       # Mock Airtable
│   │   └── merithub_mock.py       # Mock MeritHub
│   ├── api/
│   │   └── webhook.py             # FastAPI: classStatus + attendance dispatch
│   ├── db/
│   │   ├── models.py              # SQL schema (12 таблиц)
│   │   ├── repository.py          # 12 репозиториев
│   │   └── migrations.py          # Auto-migrations (ALTER TABLE)
│   ├── scheduler/
│   │   └── scheduler.py           # SQLite-based scheduler loop
│   └── events/
│       ├── bus.py                  # Event Bus (pub/sub + DLQ)
│       └── types.py               # Event + EventTypes
├── tests/                          # 94 tests
├── ALBION_CONTEXT.md               # ← Этот файл
├── ALBION_GUIDE.md                 # Полная инструкция по использованию
├── DEMO_RUNBOOK.md                 # Сценарий демо для клиента
├── MASTER_PLAN.md                  # Аудит и трекер задач
├── ARCHITECTURE.md                 # Техническая архитектура
└── README.md                       # Quick start
```

### Таблицы БД
```
users                    — TG пользователи + роли
incidents                — инциденты (absence, late, cancellation)
workflow_instances       — state machine workflow'ов
scheduled_actions        — отложенные действия (scheduler)
notifications            — очередь уведомлений
leads                    — захваченные заявки
idempotency_keys         — защита от дублей
dead_letter_queue        — упавшие события
webhook_events           — захваченные webhook'и MeritHub
merithub_students        — маппинг учеников (cuid ↔ mh_id ↔ parent_tg ↔ timezone)
merithub_classes         — метаданные классов (class_id ↔ links ↔ start_time)
merithub_contacts        — контакты (phone, email, country, city)
merithub_class_status    — последний webhook-статус класса (lv/cp)
merithub_enrollments     — зачисление в классы
```

---

## 9. ДЕМО: КАК ПОКАЗЫВАТЬ КЛИЕНТУ

### Подготовка (5 мин)
```bash
python -m src.main
# В Telegram: /start → "Я координатор"
# /role <PARENT_TG> parent
```

### Прогон (15 мин)
```
1. /pilot_absent → parent нажимает ✅ → координатор не отвлекается
2. /demo_reset → /pilot_absent → parent пишет "опоздаем" → AI понимает
3. /demo_reset → /pilot_absent → parent молчит → эскалация
4. /incidents → статистика
5. (MeritHub) /seed10 → /mh_schedule → /mh_events → авто-неявки
```

### Ключевые фразы для клиента
- *"Координатор не тратит ни секунды — система сама разобралась"*
- *"Родитель не учит команды — пишет как привык. Система понимает."*
- *"Координатор подключается ТОЛЬКО когда система не справилась сама"*
- *"Мы не ждём, пока проблема случится — мы предотвращаем"*
- *"Это боль №1. Следующие: расписания, оплаты, лиды"*

---

## 10. ТЕКУЩИЕ МЕТРИКИ

```
Тесты:        94/94 passing
Строк кода:   ~7200 (src + tests)
Файлов src:   22 .py
Коммитов:     6
Таблиц БД:    14
Репозиториев: 12
Команд бота:  25+
Workflow'ов:  6 (absence, prelesson_parent, prelesson_tutor, tutor_start_check, cancellation, lead_capture)
```
