import logging, uuid

logger = logging.getLogger(__name__)


class MockMeritHubService:
    """Mock MeritHub API. Интерфейс СТРОГО повторяет реальный клиент
    (create-only вендор, стратегия «import & observe»): никаких get_lesson /
    mark_absent / cancel_lesson — их у MeritHubClient нет, раньше mock создавал
    ложный интерфейс (R7-10). Чтение занятий — локальная БД, статусы — webhook'и.
    """

    def __init__(self):
        self._classes = {}
        self._users = {}
        # ОТКЛЮЧЕНО (Round 3, решение владельца 2026-07-31): баланс не используется
        # продуктовым кодом (PAYMENT_* события нигде не публикуются). В будущем —
        # интеграция с Xero.
        # self._balances = {}

    async def add_user(self, **kw):
        cuid = kw.get("client_user_id", "")
        mh_id = f"mh_{cuid}"
        self._users[mh_id] = kw
        return {"userId": mh_id}

    async def update_user(self, merithub_user_id, **f):
        return {"userId": merithub_user_id}

    async def delete_user(self, merithub_user_id):
        return {}

    async def schedule_class(self, instructor_merithub_id, **kw):
        class_id = f"C{uuid.uuid4().hex[:6]}"
        hl = f"mock_host_{class_id}"
        pl = f"mock_part_{class_id}"
        self._classes[class_id] = {"instructor": instructor_merithub_id, **kw}
        return {
            "classId": class_id,
            "commonLinks": {
                "commonHostLink": hl,
                "commonParticipantLink": pl,
            },
        }

    async def edit_class(self, class_id, **kw):
        return {"classId": class_id}

    async def add_users_to_class(self, class_id, users):
        return {
            "users": [
                {"userId": u["userId"], "userLink": f"mock_{u['userId']}"}
                for u in users
            ],
        }

    async def remove_users_from_class(self, class_id, user_ids):
        return {}

    async def delete_class(self, class_id):
        self._classes.pop(class_id, None)
        return {}

    def room_url(self, link, device_test=False):
        return f"https://live.merithub.com/info/room/MOCK/{link}"

    @staticmethod
    def parse_schedule(resp):
        c = resp.get("commonLinks") or {}
        return {
            "class_id": str(resp.get("classId", "")),
            "host_link": c.get("commonHostLink", ""),
            "participant_link": c.get("commonParticipantLink", ""),
        }

    @staticmethod
    def parse_user_links(resp):
        return {
            u["userId"]: u["userLink"]
            for u in (resp.get("users") or [])
            if u.get("userId")
        }

    @staticmethod
    def attended_user_ids(p):
        return {
            str(a["userId"])
            for a in (p.get("attendance") or [])
            if int(a.get("totalTime", 0)) > 0
        }
