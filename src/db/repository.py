import json, aiosqlite, uuid
from datetime import datetime, timezone, timedelta

class Repository:
    def __init__(self, db_path: str = "albion.db"):
        self.db_path = db_path
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
            (tg, role, name, kw.get("username"), kw.get("phone"), kw.get("language","ru"))
        )).lastrowid

class IncidentRepository(Repository):
    async def create(self, **kw) -> int:
        cols = ", ".join(kw.keys()); ph = ", ".join("?" for _ in kw)
        return (await self._execute(f"INSERT INTO incidents ({cols}) VALUES ({ph})", tuple(kw.values()))).lastrowid
    async def get(self, iid: int) -> dict | None:
        return await self._fetchone("SELECT * FROM incidents WHERE id = ?", (iid,))
    async def update_status(self, iid: int, status: str, resolution: str | None = None) -> None:
        if resolution:
            await self._execute("UPDATE incidents SET status=?, resolved_at=?, resolution=? WHERE id=?",
                (status, datetime.now(timezone.utc).isoformat(), resolution, iid))
        else:
            await self._execute("UPDATE incidents SET status=? WHERE id=?", (status, iid))

class NotificationRepository(Repository):
    async def create(self, rid: int, type_: str, content: str, channel: str = "telegram") -> int:
        return (await self._execute(
            "INSERT INTO notifications (recipient_id,type,channel,content,status) VALUES (?,?,?,?,'queued')",
            (rid, type_, channel, content)
        )).lastrowid
    async def mark_sent(self, nid: int) -> None:
        await self._execute("UPDATE notifications SET status='sent', sent_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), nid))
    async def mark_failed(self, nid: int, error: str) -> None:
        await self._execute("UPDATE notifications SET status='failed' WHERE id=?", (nid,))

class WorkflowRepository(Repository):
    async def create(self, wtype: str, state: str = "pending", data: dict | None = None) -> int:
        return (await self._execute(
            "INSERT INTO workflow_instances (workflow_type,state,data) VALUES (?,?,?)",
            (wtype, state, json.dumps(data or {}))
        )).lastrowid
    async def update_state(self, wid: int, state: str, data: dict | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if data is not None:
            await self._execute("UPDATE workflow_instances SET state=?,data=?,updated_at=? WHERE id=?",
                (state, json.dumps(data), now, wid))
        else:
            await self._execute("UPDATE workflow_instances SET state=?,updated_at=? WHERE id=?", (state, now, wid))
    async def get(self, wid: int) -> dict | None:
        return await self._fetchone("SELECT * FROM workflow_instances WHERE id = ?", (wid,))

class LeadRepository(Repository):
    async def create(self, msg: str, extracted: dict | None = None, source: str = "telegram") -> int:
        return (await self._execute(
            "INSERT INTO leads (source,raw_message,extracted_data) VALUES (?,?,?)",
            (source, msg, json.dumps(extracted or {}))
        )).lastrowid
    async def get(self, lid: int) -> dict | None:
        row = await self._fetchone("SELECT * FROM leads WHERE id = ?", (lid,))
        if row and row.get("extracted_data"):
            row["extracted_data"] = json.loads(row["extracted_data"])
        return row

class ScheduledActionRepository(Repository):
    async def create(self, workflow_id: int, execute_at: str, action: str, payload: dict | None = None) -> str:
        aid = str(uuid.uuid4())[:8]
        await self._execute(
            "INSERT INTO scheduled_actions (id, workflow_id, execute_at, action, payload) VALUES (?,?,?,?,?)",
            (aid, workflow_id, execute_at, action, json.dumps(payload or {})),
        )
        return aid

    async def claim_pending(self, limit: int = 20) -> list[dict]:
        """Атомарно забирает просроченные задачи. Подчищает зависшие running."""
        # REAPER: возвращаем зависшие running обратно в pending (защита от падений)
        await self._execute(
            "UPDATE scheduled_actions SET status='pending', locked_until=NULL, attempts=attempts+1 WHERE status='running' AND locked_until < datetime('now') AND attempts < 3"
        )

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            # Выбираем кандидатов
            rows = await (await db.execute(
                "SELECT id FROM scheduled_actions WHERE status='pending' AND execute_at <= datetime('now') AND attempts < 3 LIMIT ?",
                (limit,),
            )).fetchall()
            ids = [r["id"] for r in rows]
            if not ids:
                return []
            claimed = []
            for aid in ids:
                cursor = await db.execute(
                    "UPDATE scheduled_actions SET status='running', attempts=attempts+1, locked_until=datetime('now','+2 minutes') WHERE id=? AND status='pending'",
                    (aid,),
                )
                if cursor.rowcount > 0:
                    claimed.append(aid)
            if not claimed:
                return []
            placeholders = ",".join("?" for _ in claimed)
            result = await (await db.execute(
                f"SELECT * FROM scheduled_actions WHERE id IN ({placeholders})", tuple(claimed),
            )).fetchall()
            await db.commit()
            return [dict(r) for r in result]

    async def mark_done(self, aid: str) -> None:
        await self._execute("UPDATE scheduled_actions SET status='done' WHERE id=?", (aid,))

    async def mark_failed(self, aid: str, error: str) -> None:
        await self._execute("UPDATE scheduled_actions SET status='failed', last_error=? WHERE id=?", (error[:500], aid))

    async def requeue(self, aid: str) -> None:
        await self._execute("UPDATE scheduled_actions SET status='pending', locked_until=NULL WHERE id=?", (aid,))

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

class IdempotencyRepository(Repository):
    async def exists(self, key: str) -> bool:
        row = await self._fetchone(
            "SELECT 1 FROM idempotency_keys WHERE key = ? AND created_at > datetime('now', '-1 day')", (key,))
        return row is not None
    async def save(self, key: str, handler: str, response: str | None = None) -> None:
        await self._execute("INSERT OR IGNORE INTO idempotency_keys (key, handler, response) VALUES (?, ?, ?)",
            (key, handler, response))
    async def cleanup_old(self, hours: int = 24) -> None:
        await self._execute(f"DELETE FROM idempotency_keys WHERE created_at < datetime('now', '-{hours} hours')")
