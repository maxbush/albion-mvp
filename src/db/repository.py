import json
import uuid
from datetime import datetime, timezone

import aiosqlite

from src.config import settings

# SQLite использует datetime('now') = UTC. Это правильно.
# execute_at/locked_until храним как RFC3339 ISO-текст с timezone.
# Для сравнения используем julianday(...), а не лексикографическое сравнение строк.


class Repository:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or settings.database_path

    async def _execute(self, sql: str, params: tuple = ()):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            c = await db.execute(sql, params)
            await db.commit()
            return c

    async def _fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(sql, params)).fetchone()
            return dict(row) if row else None

    async def _fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            return [dict(r) for r in await (await db.execute(sql, params)).fetchall()]


class UserRepository(Repository):
    async def get_by_telegram_id(self, tg: str) -> dict | None:
        return await self._fetchone("SELECT * FROM users WHERE telegram_id = ?", (tg,))

    async def create(self, tg: str, role: str, name: str, **kw) -> int:
        return (await self._execute(
            "INSERT INTO users (telegram_id,role,name,username,phone,language) VALUES (?,?,?,?,?,?)",
            (tg, role, name, kw.get("username"), kw.get("phone"), kw.get("language", "ru")),
        )).lastrowid

    async def get(self, uid: int) -> dict | None:
        return await self._fetchone("SELECT * FROM users WHERE id = ?", (uid,))

    # ── Расширения для пилота: раздача ролей по TG-аккаунтам ──────────
    async def get_by_username(self, username: str) -> dict | None:
        return await self._fetchone(
            "SELECT * FROM users WHERE lower(username) = lower(?)", (username.lstrip("@"),),
        )

    async def list_all(self) -> list[dict]:
        return await self._fetchall(
            "SELECT id, telegram_id, role, name, username, created_at FROM users ORDER BY role, name",
        )

    async def list_by_role(self, role: str) -> list[dict]:
        return await self._fetchall("SELECT * FROM users WHERE role = ? ORDER BY name", (role,))

    async def update_role(self, uid: int, role: str) -> None:
        await self._execute(
            "UPDATE users SET role=?, updated_at=? WHERE id=?",
            (role, datetime.now(timezone.utc).isoformat(), uid),
        )

    async def set_language(self, telegram_id: str, lang: str) -> None:
        """Язык интерфейса пользователя (ru/en). Иное значение игнорируется."""
        if lang not in ("ru", "en"):
            return
        await self._execute(
            "UPDATE users SET language=?, updated_at=? WHERE telegram_id=?",
            (lang, datetime.now(timezone.utc).isoformat(), str(telegram_id)),
        )

    async def set_role_by_telegram(
        self,
        tg: str,
        role: str,
        name: str | None = None,
        username: str | None = None,
    ) -> tuple[int, bool]:
        """Upsert: назначает роль по telegram_id, создаёт пользователя если его нет.

        Возвращает (user_id, created)."""
        existing = await self.get_by_telegram_id(tg)
        if existing:
            await self.update_role(existing["id"], role)
            if name or username:
                await self._execute(
                    "UPDATE users SET name=COALESCE(?,name), username=COALESCE(?,username) WHERE id=?",
                    (name, username, existing["id"]),
                )
            return existing["id"], False
        uid = await self.create(tg, role, name or f"Owner {tg}", username=username)
        return uid, True


class IncidentRepository(Repository):
    async def create(self, **kw) -> int:
        cols = ", ".join(kw.keys())
        ph = ", ".join("?" for _ in kw)
        return (await self._execute(f"INSERT INTO incidents ({cols}) VALUES ({ph})", tuple(kw.values()))).lastrowid

    async def get(self, iid: int) -> dict | None:
        return await self._fetchone("SELECT * FROM incidents WHERE id = ?", (iid,))

    async def update_status(self, iid: int, status: str, resolution: str | None = None) -> None:
        if resolution:
            await self._execute(
                "UPDATE incidents SET status=?, resolved_at=?, resolution=? WHERE id=?",
                (status, datetime.now(timezone.utc).isoformat(), resolution, iid),
            )
        else:
            await self._execute("UPDATE incidents SET status=? WHERE id=?", (status, iid))


class NotificationRepository(Repository):
    async def create(self, rid: int, type_: str, content: str, channel: str = "telegram") -> int:
        return (await self._execute(
            "INSERT INTO notifications (recipient_id,type,channel,content,status) VALUES (?,?,?,?,'queued')",
            (rid, type_, channel, content),
        )).lastrowid

    async def mark_sent(self, nid: int) -> None:
        await self._execute(
            "UPDATE notifications SET status='sent', sent_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), nid),
        )

    async def mark_failed(self, nid: int, error: str) -> None:
        await self._execute("UPDATE notifications SET status='failed' WHERE id=?", (nid,))


class WorkflowRepository(Repository):
    async def create(self, wtype: str, state: str = "pending", data: dict | None = None) -> int:
        return (await self._execute(
            "INSERT INTO workflow_instances (workflow_type,state,data) VALUES (?,?,?)",
            (wtype, state, json.dumps(data or {})),
        )).lastrowid

    async def update_state(self, wid: int, state: str, data: dict | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if data is not None:
            await self._execute(
                "UPDATE workflow_instances SET state=?,data=?,updated_at=? WHERE id=?",
                (state, json.dumps(data), now, wid),
            )
        else:
            await self._execute(
                "UPDATE workflow_instances SET state=?,updated_at=? WHERE id=?",
                (state, now, wid),
            )

    async def update_data(self, wid: int, data: dict) -> None:
        await self._execute(
            "UPDATE workflow_instances SET data=?, updated_at=? WHERE id=?",
            (json.dumps(data), datetime.now(timezone.utc).isoformat(), wid),
        )

    async def get(self, wid: int) -> dict | None:
        return await self._fetchone("SELECT * FROM workflow_instances WHERE id = ?", (wid,))

    async def cancel(self, wid: int) -> None:
        await self._execute(
            "UPDATE workflow_instances SET state='cancelled', updated_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), wid),
        )


class LeadRepository(Repository):
    async def create(self, msg: str, extracted: dict | None = None, source: str = "telegram") -> int:
        return (await self._execute(
            "INSERT INTO leads (source,raw_message,extracted_data) VALUES (?,?,?)",
            (source, msg, json.dumps(extracted or {})),
        )).lastrowid

    async def get(self, lid: int) -> dict | None:
        row = await self._fetchone("SELECT * FROM leads WHERE id = ?", (lid,))
        if row and row.get("extracted_data"):
            row["extracted_data"] = json.loads(row["extracted_data"])
        return row

    async def list_recent(self, limit: int = 10) -> list[dict]:
        """R7-18: поверхность просмотра для /leads."""
        rows = await self._fetchall(
            "SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            if r.get("extracted_data"):
                try:
                    r["extracted_data"] = json.loads(r["extracted_data"])
                except Exception:
                    pass
        return rows

    async def count(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) as cnt FROM leads")
        return row["cnt"] if row else 0


SCHEDULED_LOCK_MINUTES = 5  # сколько минут даём на выполнение action


class ScheduledActionRepository(Repository):
    async def create(self, workflow_id: int, execute_at: str, action: str, payload: dict | None = None) -> str:
        aid = str(uuid.uuid4())[:8]
        await self._execute(
            "INSERT INTO scheduled_actions (id, workflow_id, execute_at, action, payload) VALUES (?,?,?,?,?)",
            (aid, workflow_id, execute_at, action, json.dumps(payload or {})),
        )
        return aid

    async def claim_pending(self, limit: int = 20) -> list[dict]:
        """Атомарно забирает просроченные задачи.

        REAPER: возвращаем зависшие running в pending (защита от падений).
        НЕ увеличиваем attempts — reaper не считается попыткой выполнения.
        Зомби с исчерпанными попытками (attempts >= 3) добиваем до failed,
        иначе строка висела бы в running вечно.
        """
        await self._execute(
            "UPDATE scheduled_actions "
            "SET status='failed', last_error='lock expired after max attempts', locked_until=NULL "
            "WHERE status='running' AND julianday(locked_until) < julianday('now') AND attempts >= 3"
        )
        await self._execute(
            "UPDATE scheduled_actions "
            "SET status='pending', locked_until=NULL "
            "WHERE status='running' AND julianday(locked_until) < julianday('now') AND attempts < 3"
        )

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT id FROM scheduled_actions "
                "WHERE status='pending' AND julianday(execute_at) <= julianday('now') AND attempts < 3 "
                "LIMIT ?",
                (limit,),
            )).fetchall()
            ids = [r["id"] for r in rows]
            if not ids:
                return []
            claimed = []
            for aid in ids:
                cursor = await db.execute(
                    f"UPDATE scheduled_actions SET status='running', attempts=attempts+1, "
                    f"locked_until=datetime('now','+{SCHEDULED_LOCK_MINUTES} minutes') "
                    "WHERE id=? AND status='pending'",
                    (aid,),
                )
                if cursor.rowcount > 0:
                    claimed.append(aid)
            if not claimed:
                return []
            placeholders = ",".join("?" for _ in claimed)
            result = await (await db.execute(
                f"SELECT * FROM scheduled_actions WHERE id IN ({placeholders})",
                tuple(claimed),
            )).fetchall()
            await db.commit()
            return [dict(r) for r in result]

    async def mark_done(self, aid: str) -> None:
        await self._execute(
            "UPDATE scheduled_actions SET status='done', locked_until=NULL, last_error=NULL WHERE id=?",
            (aid,),
        )

    async def mark_failed(self, aid: str, error: str) -> None:
        await self._execute(
            "UPDATE scheduled_actions SET status='failed', last_error=?, locked_until=NULL WHERE id=?",
            (error[:500], aid),
        )

    async def requeue(self, aid: str, error: str | None = None) -> None:
        await self._execute(
            "UPDATE scheduled_actions SET status='pending', locked_until=NULL, last_error=? WHERE id=?",
            ((error or "")[:500] or None, aid),
        )

    async def cancel_by_workflow(self, workflow_id: int, action: str | None = None) -> int:
        """Отменяет pending задачи по workflow_id. Если action указан — только конкретный action."""
        if action:
            await self._execute(
                "UPDATE scheduled_actions SET status='cancelled' "
                "WHERE workflow_id=? AND action=? AND status='pending'",
                (workflow_id, action),
            )
        else:
            await self._execute(
                "UPDATE scheduled_actions SET status='cancelled' "
                "WHERE workflow_id=? AND status='pending'",
                (workflow_id,),
            )
        return 0

    async def cleanup_old(self, hours: int = 24) -> None:
        await self._execute(
            "DELETE FROM scheduled_actions WHERE status='done' AND created_at < datetime('now', ?)",
            (f"-{hours} hours",),
        )


class DeadLetterQueueRepository(Repository):
    async def put(self, source: str, event_type: str | None, payload: dict, error: str) -> int:
        return (await self._execute(
            "INSERT INTO dead_letter_queue (source, event_type, payload, error) VALUES (?,?,?,?)",
            (source, event_type, json.dumps(payload), error[:1000]),
        )).lastrowid

    async def count(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) as cnt FROM dead_letter_queue")
        return row["cnt"] if row else 0


class WebhookEventRepository(Repository):
    """Хранилище захваченных вебхуков MeritHub (сырой payload + заголовки)."""

    async def save(self, event_type, signature_ok: int, headers: dict, raw, note: str | None = None) -> int:
        raw_s = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        raw_s = raw_s[:8000]
        if note:
            raw_s = raw_s + f"\n[note] {note}"
        return (await self._execute(
            "INSERT INTO webhook_events (event_type, signature_ok, headers, raw) VALUES (?,?,?,?)",
            (event_type, int(signature_ok), json.dumps(headers or {}, ensure_ascii=False), raw_s),
        )).lastrowid

    async def list_recent(self, limit: int = 10) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM webhook_events ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    async def count(self) -> int:
        r = await self._fetchone("SELECT COUNT(*) as cnt FROM webhook_events")
        return r["cnt"] if r else 0


class MeritHubStudentRepository(Repository):
    """Маппинг clientUserId ↔ merithubUserId ↔ родитель (TG)."""

    async def upsert(
        self,
        client_user_id: str,
        *,
        merithub_user_id: str | None = None,
        name: str | None = None,
        email: str | None = None,
        parent_telegram_id: str | None = None,
        timezone: str | None = None,
        country: str | None = None,
        role: str = "student",
    ) -> None:
        existing = await self._fetchone(
            "SELECT 1 FROM merithub_students WHERE client_user_id=?",
            (client_user_id,),
        )
        if existing:
            await self._execute(
                "UPDATE merithub_students SET "
                "merithub_user_id=COALESCE(?,merithub_user_id), "
                "name=COALESCE(?,name), "
                "email=COALESCE(?,email), "
                "parent_telegram_id=COALESCE(?,parent_telegram_id), "
                "timezone=COALESCE(?,timezone), "
                "country=COALESCE(?,country), "
                "role=COALESCE(?,role) "
                "WHERE client_user_id=?",
                (merithub_user_id, name, email, parent_telegram_id, timezone, country, role, client_user_id),
            )
        else:
            await self._execute(
                "INSERT INTO merithub_students "
                "(client_user_id, merithub_user_id, name, email, parent_telegram_id, timezone, country, role) "
                "VALUES (?,?,?,?,?,?,?,?)",
                # Дефолт зоны для display — зона организации (H4/P4.1), не хардкод.
                (client_user_id, merithub_user_id, name or client_user_id, email, parent_telegram_id,
                 timezone or settings.albion_org_timezone, country, role),
            )

    async def get_by_client_id(self, cuid: str) -> dict | None:
        return await self._fetchone("SELECT * FROM merithub_students WHERE client_user_id=?", (cuid,))

    async def get_by_client_ids(self, cuids: list[str]) -> dict[str, dict]:
        """Батч (R7-15): client_user_id → строка. Один запрос вместо N+1 в циклах."""
        ids = [str(c) for c in dict.fromkeys(cuids) if c]
        if not ids:
            return {}
        ph = ",".join("?" for _ in ids)
        rows = await self._fetchall(
            f"SELECT * FROM merithub_students WHERE client_user_id IN ({ph})", tuple(ids))
        return {r["client_user_id"]: r for r in rows}

    async def get_by_merithub_id(self, mh_id: str) -> dict | None:
        return await self._fetchone("SELECT * FROM merithub_students WHERE merithub_user_id=?", (mh_id,))

    async def list_all(self) -> list[dict]:
        return await self._fetchall("SELECT * FROM merithub_students ORDER BY name")

    async def list_by_role(self, role: str) -> list[dict]:
        """Ученики ('student') или репетиторы ('tutor') по имени — для пикеров визарда."""
        return await self._fetchall(
            "SELECT * FROM merithub_students WHERE role=? ORDER BY name", (role,))


class MeritHubClassRepository(Repository):
    """Метаданные классов MeritHub, созданных через ALBION.

    Нужны, чтобы позже можно было реально добавлять пользователей в класс через
    commonParticipantLink/commonHostLink, которые MeritHub возвращает только на
    этапе schedule_class.
    """

    async def upsert(
        self,
        class_id: str,
        *,
        host_link: str | None = None,
        participant_link: str | None = None,
        title: str | None = None,
        start_time: str | None = None,
        tutor_client_user_id: str | None = None,
        tutor_merithub_user_id: str | None = None,
        class_type: str | None = None,
        schedule_days: str | None = None,
        duration: int | None = None,
        timezone: str | None = None,
        end_date: str | None = None,
    ) -> None:
        existing = await self._fetchone("SELECT 1 FROM merithub_classes WHERE class_id=?", (class_id,))
        if existing:
            await self._execute(
                "UPDATE merithub_classes SET host_link=COALESCE(?,host_link), "
                "participant_link=COALESCE(?,participant_link), title=COALESCE(?,title), "
                "start_time=COALESCE(?,start_time), tutor_client_user_id=COALESCE(?,tutor_client_user_id), "
                "tutor_merithub_user_id=COALESCE(?,tutor_merithub_user_id), "
                "class_type=COALESCE(?,class_type), schedule_days=COALESCE(?,schedule_days), "
                "duration=COALESCE(?,duration), timezone=COALESCE(?,timezone), "
                "end_date=COALESCE(?,end_date) WHERE class_id=?",
                (
                    host_link,
                    participant_link,
                    title,
                    start_time,
                    tutor_client_user_id,
                    tutor_merithub_user_id,
                    class_type,
                    schedule_days,
                    duration,
                    timezone,
                    end_date,
                    class_id,
                ),
            )
        else:
            await self._execute(
                "INSERT INTO merithub_classes "
                "(class_id, host_link, participant_link, title, start_time, tutor_client_user_id, "
                "tutor_merithub_user_id, class_type, schedule_days, duration, timezone, end_date) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    class_id,
                    host_link,
                    participant_link,
                    title,
                    start_time,
                    tutor_client_user_id,
                    tutor_merithub_user_id,
                    class_type or "oneTime",
                    schedule_days,
                    duration,
                    timezone,
                    end_date,
                ),
            )

    async def get(self, class_id: str) -> dict | None:
        return await self._fetchone("SELECT * FROM merithub_classes WHERE class_id=?", (class_id,))

    async def get_many(self, class_ids: list[str]) -> dict[str, dict]:
        """Батч (R7-15): class_id → строка. Один запрос вместо N+1 в циклах."""
        ids = [str(c) for c in dict.fromkeys(class_ids) if c]
        if not ids:
            return {}
        ph = ",".join("?" for _ in ids)
        rows = await self._fetchall(
            f"SELECT * FROM merithub_classes WHERE class_id IN ({ph})", tuple(ids))
        return {r["class_id"]: r for r in rows}

    async def list_all(self) -> list[dict]:
        return await self._fetchall("SELECT * FROM merithub_classes ORDER BY created_at DESC")


class MeritHubContactRepository(Repository):
    """Связка client_user_id ↔ telegram_id / phone / email / country / city."""

    async def upsert(
        self,
        client_user_id: str,
        telegram_id: str | None = None,
        role: str = "student",
        name: str | None = None,
        phone: str | None = None,
        email: str | None = None,
        country: str | None = None,
        city: str | None = None,
    ) -> None:
        existing = await self._fetchone("SELECT 1 FROM merithub_contacts WHERE client_user_id=?", (client_user_id,))
        if existing:
            await self._execute(
                "UPDATE merithub_contacts SET "
                "telegram_id=COALESCE(?,telegram_id), "
                "phone=COALESCE(?,phone), "
                "email=COALESCE(?,email), "
                "name=COALESCE(?,name), "
                "country=COALESCE(?,country), "
                "city=COALESCE(?,city), "
                "role=COALESCE(?,role) "
                "WHERE client_user_id=?",
                (telegram_id, phone, email, name, country, city, role, client_user_id),
            )
        else:
            await self._execute(
                "INSERT INTO merithub_contacts "
                "(client_user_id, telegram_id, phone, email, name, country, city, role) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (client_user_id, telegram_id, phone, email, name, country, city, role),
            )

    async def get(self, client_user_id: str) -> dict | None:
        return await self._fetchone("SELECT * FROM merithub_contacts WHERE client_user_id=?", (client_user_id,))

    async def get_by_telegram(self, telegram_id: str) -> dict | None:
        return await self._fetchone(
            "SELECT * FROM merithub_contacts WHERE telegram_id=?", (str(telegram_id),))

    async def list_all(self) -> list[dict]:
        return await self._fetchall("SELECT * FROM merithub_contacts ORDER BY role, name")


class MeritHubClassStatusRepository(Repository):
    """Последний webhook-статус класса MeritHub (например lv/cp)."""

    async def upsert(self, class_id: str, last_status: str, payload: dict | None = None, event_time: str | None = None) -> None:
        payload_json = json.dumps(payload or {}, ensure_ascii=False)
        existing = await self._fetchone("SELECT 1 FROM merithub_class_status WHERE class_id=?", (class_id,))
        if existing:
            await self._execute(
                "UPDATE merithub_class_status SET last_status=?, last_event_at=?, payload=?, updated_at=? WHERE class_id=?",
                (last_status, event_time, payload_json, datetime.now(timezone.utc).isoformat(), class_id),
            )
        else:
            await self._execute(
                "INSERT INTO merithub_class_status (class_id, last_status, last_event_at, payload, updated_at) VALUES (?,?,?,?,?)",
                (class_id, last_status, event_time, payload_json, datetime.now(timezone.utc).isoformat()),
            )

    async def get(self, class_id: str) -> dict | None:
        row = await self._fetchone("SELECT * FROM merithub_class_status WHERE class_id=?", (class_id,))
        if row and row.get("payload"):
            try:
                row["payload"] = json.loads(row["payload"])
            except Exception:
                pass
        return row

    async def get_map(self, class_ids: list[str]) -> dict[str, dict]:
        """Батч (R7-15): class_id → row lightweight (без payload) для статусов в циклах."""
        ids = [str(c) for c in dict.fromkeys(class_ids) if c]
        if not ids:
            return {}
        ph = ",".join("?" for _ in ids)
        rows = await self._fetchall(
            f"SELECT class_id, last_status, last_event_at, updated_at "
            f"FROM merithub_class_status WHERE class_id IN ({ph})", tuple(ids))
        return {r["class_id"]: r for r in rows}


class MeritHubEnrollmentRepository(Repository):
    """Зачисление в класс (для вычисления неявок по webhook attendance)."""

    async def add(
        self,
        class_id: str,
        merithub_user_id: str,
        *,
        client_user_id: str | None = None,
        parent_telegram_id: str | None = None,
        student_name: str | None = None,
        role: str = "student",
    ) -> None:
        await self._execute(
            "INSERT OR REPLACE INTO merithub_enrollments "
            "(class_id, merithub_user_id, client_user_id, parent_telegram_id, student_name, role) "
            "VALUES (?,?,?,?,?,?)",
            (class_id, merithub_user_id, client_user_id, parent_telegram_id, student_name, role),
        )

    async def list_by_class(self, class_id: str) -> list[dict]:
        return await self._fetchall("SELECT * FROM merithub_enrollments WHERE class_id=?", (class_id,))

    async def list_by_classes(self, class_ids: list[str]) -> dict[str, list[dict]]:
        """Батч (R7-15): class_id → [зачисления]. Один запрос вместо N+1 на карточках."""
        ids = [str(c) for c in dict.fromkeys(class_ids) if c]
        if not ids:
            return {}
        ph = ",".join("?" for _ in ids)
        rows = await self._fetchall(
            f"SELECT * FROM merithub_enrollments WHERE class_id IN ({ph})", tuple(ids))
        out: dict[str, list[dict]] = {cid: [] for cid in ids}
        for r in rows:
            out.setdefault(r["class_id"], []).append(r)
        return out


class IdempotencyRepository(Repository):
    async def exists(self, key: str) -> bool:
        row = await self._fetchone(
            "SELECT 1 FROM idempotency_keys WHERE key = ? AND created_at > datetime('now', '-1 day')",
            (key,),
        )
        return row is not None

    async def save(self, key: str, handler: str, response: str | None = None) -> None:
        await self._execute(
            "INSERT OR IGNORE INTO idempotency_keys (key, handler, response) VALUES (?, ?, ?)",
            (key, handler, response),
        )

    async def cleanup_old(self, hours: int = 24) -> None:
        await self._execute(f"DELETE FROM idempotency_keys WHERE created_at < datetime('now', '-{hours} hours')")


class WizardStateRepository(Repository):
    """Состояние кнопочных сценариев (визардов) координатора.

    В SQLite (не в памяти процесса): перезапуск бота не убивает сценарий молча —
    тот же принцип, что и у scheduler'а. expires_at — TTL неактивности (UTC iso).
    """

    async def get(self, chat_id: str) -> dict | None:
        return await self._fetchone("SELECT * FROM wizard_state WHERE chat_id=?", (str(chat_id),))

    async def save(self, chat_id: str, flow: str, step: str, data: dict, expires_at: str) -> None:
        import json as _json
        from datetime import datetime as _dt, timezone as _tz
        payload = _json.dumps(data, ensure_ascii=False)
        now = _dt.now(_tz.utc).isoformat()
        existing = await self.get(chat_id)
        if existing:
            await self._execute(
                "UPDATE wizard_state SET flow=?, step=?, data=?, expires_at=?, updated_at=? "
                "WHERE chat_id=?",
                (flow, step, payload, expires_at, now, str(chat_id)),
            )
        else:
            await self._execute(
                "INSERT INTO wizard_state (chat_id, flow, step, data, expires_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (str(chat_id), flow, step, payload, expires_at, now),
            )

    async def delete(self, chat_id: str) -> None:
        await self._execute("DELETE FROM wizard_state WHERE chat_id=?", (str(chat_id),))

    async def list_expired(self, now_iso: str) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM wizard_state WHERE expires_at < ?", (now_iso,))
