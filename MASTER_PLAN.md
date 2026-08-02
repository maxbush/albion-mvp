# 🗺️ MASTER_PLAN v6 — ALBION MVP Sixth Audit (Round 9, Total Logic/UI-Sync/Dead-Code Audit)

> Создан: 2026-08-02 (Round 9 — тотальный аудит логики, UI/Backend синхронизации, оптимизации и мёртвого кода)
> Базовая линия на старте: **212/212 тестов passing** (коммит `e6e213e`)
> Метод E2E-проверки: интерфейс продукта = Telegram-бот (веб-UI нет, **Playwright неприменим** — прецедент зафиксирован в v3/v4). E2E = pytest-харнесс, эмулирующий Telegram-апдейты (`FakeUpdate`/`FakeBot`) с прогоном полных цепочек через event bus + SQLite scheduler. Каждая задача закрывается только с зелёной проверкой.

---

## 📜 История предыдущих раундов

- **Round 1–2:** хардкоды, demo-gating, idempotency, kill switch, timezone-fixes (95/95).
- **Round 3:** 15/15 задач, 120/120 тестов, org-timezone канон H4/P4.1.
- **Round 4:** UX-пакет U1–U7 (меню по ролям, честные ожидания, confirm опасных действий).
- **Round 5:** визарды координатора, perma-серии + occurrence-ядро, `wizard_state` в SQLite.
- **Round 6:** персональная отмена, `/lessons` с постоянными ссылками, i18n-слой, авто-сводка 07:30.
- **Round 7:** тотальный аудит логики — 19 задач закрыто (203/203).
- **Round 8:** логирование, права ролей, webhook-подписчики, мёртвые callback'и, UX (212/212).
- **Round 9 (этот план):** найден и воспроизведён **P0-баг семейства `data LIKE` (отмена чужого workflow)**,
  мёртвая кнопка визарда «➕ Ещё занятие», **нерабочий `--webhook`-режим бота** (бот глухой в prod),
  миссматчи help-карточки с правами ролей, семантическая ошибка `/ok`, зонный баг «сегодня»
  в `/today`, неидемпотентный attendance-webhook, дубль `SafeStreamHandler`, фантомная эмиссия
  событий без подписчиков, мусорные импорты.

---

## 🔴 P0 — Критические баги логики (доказаны репро)

- [x] **R9-1. LIKE-коллизия `incident_id` в JSON данных workflow — отмена ЧУЖОГО workflow**
  ✅ 2026-08-02. `WorkflowRepository.find_by_json` (json_extract, точное сравнение int/str). Заменены все 13 `data LIKE`-запросов (absence, handlers, pilot, cancellation, lesson_ops). 4 новых теста, репро-скрипт зелёный, 216/216.
  🐛 Воспроизведено: инцидент 5 и инцидент 55. `resolve_absence(5)` → запрос
  `WHERE data LIKE '%"incident_id": 5%'` матчит ОБА (LIKE-подстрока!), `ORDER BY id DESC`
  берёт workflow инцидента 55 и ОТМЕНЯЕТ ЕГО. Инцидент 55 молча теряет эскалацию,
  инцидент 5 остаётся с running-workflow.
  📍 Места: `src/workflows/absence.py` (строки 248, 419), `src/bot/handlers.py` (940),
  `src/bot/pilot.py` (867 — `_student_name`). Всего 14 мест `data LIKE` в коде —
  конвертируем в **точное сравнение `json_extract(data, '$.field') = ?`** через новый
  хелпер `WorkflowRepository.find_by_json_field(...)` (json_extract доступен в SQLite ≥3.38).
  🧪 E2E: тест-репро «инцидент 5 vs 55» (resolve_absence(5) не трогает workflow 55) +
  существующие сценарии ответа родителя/эскалации остаются зелёными.

- [ ] **R9-2. Мёртвая кнопка «➕ Ещё занятие» (`wz:sched:again`) в визарде `/schedule`**
  🐛 После успешного создания `WizardStateRepository.delete(chat_id)` удаляет состояние,
  а `handle_wz_callback` обрабатывает `wz:sched:again` ТОЛЬКО через `_load(chat_id)` →
  state=None → «Этот сценарий уже закрыт. Начните заново.» Кнопка с финальной карточки
  никогда не срабатывает (код-ветка `a == "again"` в `_sched_cb` недостижима).
  📍 `src/bot/wizard.py` (спец-кейс в `handle_wz_callback` до `_load`, по образцу
  `wz:person:toschedule`; ветка `a == "again"` в `_sched_cb` остаётся как fallback).
  🧪 E2E: полный цикл визарда → «Создать» → клик «➕ Ещё занятие» → открывается новый
  шаг выбора репетитора (не «сценарий закрыт»).

- [~] **R9-13. `notify_late_detail` хардкодит «репетитор» вместо фактического актора**
  🐛 Подтверждено владельцем (лог: 22:15:07 родитель нажал «⏰ Опоздаю» →
  22:15:11 координатору «ℹ️ Уточнение по опозданию репетитора» — ложь).
  `src/workflows/lesson_ops.py` `notify_late_detail` не смотрит `data.actor_type`.
  📍 Фикс: parent → «Ученик {student_name} опоздает на N мин» + заголовок
  «Уточнение по опозданию ученика»; tutor → «Репетитор {tutor_name} задержится на N мин»
  + прежний заголовок. Минуты форматировать из raw-значения кнопки (5/15/30+),
  не зависеть от языка нажавшего (координаторы — RU).
  🧪 E2E: parent-late → координатор видит «Ученик … опоздает на 15 мин»;
  tutor-late → «Репетитор … задержится на 5 мин» (R8-10 остаётся зелёным).

---

## 🟡 P1 — UI/Backend синхронизация и недовоплощённое

- [ ] **R9-3. Режим `--webhook` бота не поднимает сервер обновлений — бот глухой**
  🐛 `src/main.py` webhook-ветка делает `set_webhook(url=...)`, но никогда не запускает
  локальный приёмник Telegram-апдейтов (в PTB это `app.updater.start_webhook`).
  Telegram POSTит на URL, слушать некому → в прод-режиме бот молчит. `start_polling`
  вызывается только в dev-ветке.
  📍 Фикс: в `main.py` — `await app.updater.start_webhook(listen/port/url_path/secret_token,
  allowed_updates, drop_pending_updates)`; новые настройки `telegram_webhook_host`
  (default `0.0.0.0`) и `telegram_webhook_port` (default `8443`) в `src/config.py`
  и `.env.example`. Путь `url_path` выводить из `TELEGRAM_WEBHOOK_URL`.
  🧪 E2E: тест вспомогательной функции (мок `bot.set_webhook`/`updater.start_webhook`,
  проверка порта/хоста/пути/секрета).

- [ ] **R9-4. Help-карточка координатора обещает команды, которые не работают (dead-ends)**
  🐛 `_coordinator_help_text()` показывает секцию «*Владельцу:*» (`/kill_switch`, `/roles`,
  `/seed10`, `/mh_*`) ВСЕМ координаторам. Не-админ получает «⛔ Только владелец/админ».
  UI обещает → backend отказывает (миссматч, против принципа U1 «показываем только то,
  что реально доступно»).
  📍 `src/bot/handlers.py`: `_coordinator_help_text(admin: bool)` — секция владельца
  только для админов; `help_commands`-ветка передаёт флаг из `is_admin`.
  🧪 E2E: help для не-админа не содержит `/kill_switch` и `/roles`; help для админа —
  содержит. Обновить `test_r7_7` (проверка контракта: базовые команды остаются).

- [ ] **R9-5. `/ok <ID>`: координатор закрывает ситуацию, а история пишет «подтверждено родителем»**
  🐛 `cmd_ok` вызывает `resolve_absence(iid, ...)` без `resolution=` → дефолт
  `parent_confirmed` → в `/incidents` (список закрытых) отображается «подтверждено
  родителем», хотя закрыл координатор. Семантический миссматч UI/бизнес-логики.
  📍 `src/bot/handlers.py` `cmd_ok`: `resolution="coordinator_closed"`.
  🧪 E2E: `/ok` → `inc.resolution == "coordinator_closed"`; `/incidents` показывает
  «закрыто координатором».

- [ ] **R9-6. `/today`: два разных «сегодня» в одной команде (инциденты vs занятия)**
  🐛 Занятия фильтруются по `org_now().date()` (канон H4/P4.1), а инциденты — по
  `datetime.now()` (naive, серверная зона) → `created_at LIKE 'YYYY-MM-DD%'` по UTC.
  При сервере не в org-зоне (и около полуночи) списки расходятся: «занятий сегодня нет,
  инциденты сегодня есть» и наоборот.
  📍 `src/bot/pilot.py` `cmd_today`: UTC-границы org-дня
  (`org_now()` → начало/конец дня → `astimezone(utc)` → `created_at >= ? AND created_at < ?`).
  🧪 E2E: инцидент за 5 минут до полуночи org-зоны (после полуночи UTC) попадает
  в «сегодня», а не выпадает.

- [ ] **R9-7. Attendance-webhook не идемпотентен: ретраи плодят дубли уведомлений родителю**
  🐛 `_dispatch_attendance` → `trigger_absence` без проверки существующего активного
  сценария. Повторная доставка webhook (ретрай сети/мерithub) или пересечение
  с `tutor_start_check → student_absent` = ДВА инцидента по одному (класс, ученик) →
  родитель получает дубль уведомления, координатор — дубль эскалации.
  📍 `src/bot/pilot.py` `trigger_absence`: dedup-гейт по
  `json_extract(data,'$.lesson_ref')=? AND json_extract(data,'$.student_id')=? AND state='running'`
  для workflow `absence_notification`; исключение — `source="pilot_command"` (админ-демо,
  сброс через `/demo_reset`).
  🧪 E2E: два attendance-webhook по одному классу → 1 инцидент, 1 уведомление;
  повторный `/pilot_absent` по-прежнему работает.

---

## 🟢 P2 — Мёртвый код, оптимизация и чистота

- [ ] **R9-8. `SafeStreamHandler` объявлен ДВАЖДЫ в `src/utils/logging.py`** — ⛔ **ОТКЛОНЕНО ВЛАДЕЛЬЦЕМ (проверено 2026-08-02)**
  ✅ Проверка: живой — ВТОРОЙ (pre-encode, строка 43+, под тестами R8-8), первый
  (строки 8–42, cp1251-ловля) — старый мёртвый дубликат. Задача предлагала удалить
  мёртвый первый, не трогая фикс. По решению владельца НЕ чинить (потенциальный
  конфликт с его рабочим процессом) — оставлено как есть, задача закрыта без изменений.
  🐛 Строки 8 и 43: первое определение (cp1251-докстринг) полностью затеняется вторым
  (pre-encode). Мёртвый код + путаница (два разных docstring у «одного» класса).
  📍 Удалить первое определение, оставить pre-encode версию (она под тестами R8-8).

- [ ] **R9-9. Фантомная эмиссия событий без подписчиков**
  🐛 `SYSTEM_KILL_SWITCH` (2 publish, 0 subscribe), `NOTIFICATION_DELIVERED` и
  `NOTIFICATION_FAILED` (2 publish, 0 subscribe). Статусы уведомлений и так персистятся
  в БД (`mark_sent`/`mark_failed`). По прецеденту R7-13 (удаление фантомов) — эмиссию
  убрать, типы из `src/events/types.py` удалить с комментарием. `WORKFLOW_*` оставить
  (подписчик в тестах, жизненный цикл).
  📍 `src/bot/handlers.py` (publish NOTIFICATION_DELIVERED/FAILED, publish
  SYSTEM_KILL_SWITCH в `cmd_kill_switch` и callback), `src/events/types.py`.
  🧪 E2E: после отправки уведомления статус в БД `sent`/`failed` (как раньше);
  `get_subscribed_events()` не содержит удалённых типов.

- [ ] **R9-10. Мусор в src/: неиспользуемые импорты и переменные (pyflakes)**
  📍 `src/bot/pilot.py:340` (unused `MeritHubClient`), `src/bot/wizard.py:35` (unused
  `is_admin`), `src/workflows/engine.py` (unused `json`), `src/integrations/airtable_mock.py`
  (unused `Lead`), `src/integrations/merithub_client.py` (unused `datetime`),
  лишние `global _kill_switch_level` в `src/bot/handlers.py` (3 шт. — только чтение),
  `f"Не смог прочитать нажатие."` без плейсхолдера (`pilot.py:846`),
  unused `labels` (`wizard.py:289`) и `flow` (`wizard.py:1066`), unused `learner_name`
  (`pilot.py:1278`).
  🧪 E2E: `pyflakes src/` — ноль предупреждений; полный прогон suite зелёный.

- [ ] **R9-11. N+1 в `/incidents`: per-incident LIKE-запрос имени + запрос класса**
  🐛 Для каждого активного инцидента (до 20) — отдельный `_student_name` LIKE-запрос
  и отдельный `MeritHubClassRepository().get()`. После R9-1 имена учеников можно брать
  одним батч-запросом (`json_extract` + `IN`), классы — `get_many()`.
  📍 `src/bot/pilot.py` `cmd_incidents`.
  🧪 E2E: те же данные на выходе, что и до оптимизации.

- [ ] **R9-12. README: устаревший бейдж тестов и статистика**
  📍 `README.md`: `tests-155/155` → актуальное число (212+n), секция «Что нового»
  — добавить строку Round 9 (или свести к актуальному состоянию).

---

## 🛑 ТРЕБУЕТСЯ ВМЕШАТЕЛЬСТВО ЧЕЛОВЕКА / ОТЛОЖЕНО ВЛАДЕЛЬЦЕМ

- **R8-1 (Отложено). Добавление роли «student» (🎓 Ученик) в UI-кнопки.** По решению владельца отложено: усложняет MVP.
- **R8-4 (Отложено). Воронка статусов лидов (`update_status`, `/lead_status`).** По решению владельца отложено.
- **H1 (П10). Саморегистрация координатора без аппрува.** Аппрув роли сознательно отложен владельцем.
- **H2 (D6). Включение `merithub_occurrences` в webhook-lookup** (subClassId → parent classId). Продакшен-сторона.
- **H3. EN-версия текстов требует вычитки носителем.**
- **H4. WhatsApp-мост.** Припарковано владельцем.
- **H5. `docker-compose` монтирует `albion.db` файлом.** При пустом файле Docker может создать каталог.
- **H6. Kill switch хранится in-memory** (после рестарта — уровень 2). Персист в БД — отдельной задачей.

---

## 🔧 Правило закрытия задачи

1. Статус `- [ ]` → `- [~]` → код → E2E-проверка (pytest-харнесс, полный прогон suite + новые тесты на функционал) → коммит атомарным сообщением → `- [x]` с датой.
2. Любое падение существующего теста = мой фикс неправ, а не тест устарел — если старое поведение признано дефектом, правлю тест И помечаю это явно в задаче/коммите.
3. Запрещено: глобальные переименования, изменение схемы БД вне миграционного паттерна, ослабление гейтов ролей.
