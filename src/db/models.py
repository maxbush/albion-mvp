SCHEMA_SQL = """
-- WAL mode for concurrent access
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id TEXT UNIQUE NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('parent','tutor','coordinator','student')),
    name TEXT NOT NULL,
    username TEXT,
    phone TEXT,
    language TEXT DEFAULT 'ru',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Таблица conversations УДАЛЕНА (R7-11): 0 ссылок в коде — история чата не
-- реализована как фича; существующие БД очищаются DROP'ом в init_db.

CREATE TABLE IF NOT EXISTS workflow_instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_type TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','running','completed','failed','cancelled')),
    data TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_ref TEXT,
    student_id INTEGER,
    tutor_id INTEGER,
    coordinator_id INTEGER,
    type TEXT NOT NULL CHECK(type IN ('absence','late','cancellation','other')),
    status TEXT NOT NULL DEFAULT 'pending',
    resolved_at TIMESTAMP,
    resolution TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_id INTEGER REFERENCES users(id),
    type TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'telegram',
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    sent_at TIMESTAMP,
    read_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT DEFAULT 'telegram',
    raw_message TEXT,
    extracted_data TEXT,
    status TEXT DEFAULT 'new',
    assigned_to INTEGER REFERENCES users(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY,
    handler TEXT NOT NULL,
    response TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_idempotency_created ON idempotency_keys(created_at);

CREATE TABLE IF NOT EXISTS scheduled_actions (
    id TEXT PRIMARY KEY,
    workflow_id INTEGER,
    execute_at TIMESTAMP NOT NULL,
    action TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER DEFAULT 0,
    last_error TEXT,
    locked_until TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_scheduled_pending ON scheduled_actions(status, execute_at);
CREATE INDEX IF NOT EXISTS idx_scheduled_workflow ON scheduled_actions(workflow_id, action);

CREATE TABLE IF NOT EXISTS dead_letter_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    event_type TEXT,
    payload TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL,
    attempts INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Захваченные вебхуки MeritHub (для отладки и настройки авто-обработчиков)
CREATE TABLE IF NOT EXISTS webhook_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT,
    signature_ok INTEGER NOT NULL DEFAULT 0,
    headers TEXT NOT NULL DEFAULT '{}',
    raw TEXT NOT NULL,
    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_webhook_received ON webhook_events(received_at);

-- Маппинг учеников/репетиторов MeritHub ↔ наш родитель (TG).
-- MeritHub НЕ отдаёт юзеров обратно через API, поэтому храним сами.
CREATE TABLE IF NOT EXISTS merithub_students (
    client_user_id TEXT PRIMARY KEY,
    merithub_user_id TEXT,
    name TEXT NOT NULL,
    email TEXT,
    parent_telegram_id TEXT,
    timezone TEXT DEFAULT 'Europe/London',
    country TEXT,
    role TEXT NOT NULL DEFAULT 'student',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mh_students_mh ON merithub_students(merithub_user_id);
CREATE INDEX IF NOT EXISTS idx_mh_students_tz ON merithub_students(timezone);

-- Метаданные классов MeritHub, созданных через ALBION.
-- Нужны, чтобы потом реально добавлять пользователей в класс: MeritHub
-- возвращает commonHostLink/commonParticipantLink только на этапе schedule_class.
-- class_type: 'oneTime' | 'perma' (регулярная серия; type менять нельзя — API).
-- schedule_days: JSON-список дней недели формата MeritHub (0=вс..6=сб), только perma.
CREATE TABLE IF NOT EXISTS merithub_classes (
    class_id TEXT PRIMARY KEY,
    host_link TEXT,
    participant_link TEXT,
    title TEXT,
    start_time TEXT,
    tutor_client_user_id TEXT,
    tutor_merithub_user_id TEXT,
    class_type TEXT NOT NULL DEFAULT 'oneTime',
    schedule_days TEXT,
    duration INTEGER,
    timezone TEXT DEFAULT 'Europe/London',
    end_date TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mh_classes_start ON merithub_classes(start_time);

-- Occurrences perma-серий из webhook'ов MeritHub (subClassId → родительский classId).
-- D6: таблица спроектирована сейчас, наполнение и fallback-lookup — после демо.
CREATE TABLE IF NOT EXISTS merithub_occurrences (
    sub_class_id TEXT PRIMARY KEY,
    parent_class_id TEXT NOT NULL,
    occurrence_date TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mh_occ_parent ON merithub_occurrences(parent_class_id);

-- Состояние кнопочных сценариев (визардов) координатора.
-- В SQLite, а не в памяти: перезапуск бота не должен молча убивать сценарий
-- (тот же принцип, что и у scheduler'а). expires_at — TTL неактивности.
CREATE TABLE IF NOT EXISTS wizard_state (
    chat_id TEXT PRIMARY KEY,
    flow TEXT NOT NULL,
    step TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    expires_at TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Контакты участников MeritHub в Telegram. Нужны для напоминаний tutor/parent.
CREATE TABLE IF NOT EXISTS merithub_contacts (
    client_user_id TEXT PRIMARY KEY,
    telegram_id TEXT,
    phone TEXT,
    email TEXT,
    name TEXT,
    country TEXT,
    city TEXT,
    role TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mh_contacts_tg ON merithub_contacts(telegram_id);
CREATE INDEX IF NOT EXISTS idx_mh_contacts_phone ON merithub_contacts(phone);

-- Последний статус класса по webhook classStatus.
CREATE TABLE IF NOT EXISTS merithub_class_status (
    class_id TEXT PRIMARY KEY,
    last_status TEXT,
    last_event_at TEXT,
    payload TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Зачисление в класс: нужно, чтобы по webhook attendance вычислить неявки
-- (зачисленные минус присутствовавшие).
CREATE TABLE IF NOT EXISTS merithub_enrollments (
    class_id TEXT NOT NULL,
    merithub_user_id TEXT NOT NULL,
    client_user_id TEXT,
    parent_telegram_id TEXT,
    student_name TEXT,
    role TEXT NOT NULL DEFAULT 'student',
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (class_id, merithub_user_id)
);
CREATE INDEX IF NOT EXISTS idx_mh_enroll_class ON merithub_enrollments(class_id);
"""
