import logging, uuid
from datetime import datetime
from src.integrations.base import Lesson

logger = logging.getLogger(__name__)


class MockMeritHubService:
    def __init__(self):
        self._lessons = {}
        self._balances = {}
        self._classes = {}
        self._users = {}
        self._seed()

    def _seed(self):
        self._lessons["mh_lesson_1"] = Lesson(
            "mh_lesson_1", "student_1", "tutor_1", "mathematics",
            datetime(2026, 7, 4, 15, 0), datetime(2026, 7, 4, 16, 0),
        )
        self._balances["student_1"] = 150.0
        self._balances["student_2"] = 20.0

    async def get_lesson(self, lid):
        return self._lessons.get(lid)

    async def mark_absent(self, lid):
        if lid in self._lessons:
            self._lessons[lid].status = "absent"
            return True
        return False

    async def cancel_lesson(self, lid, reason=""):
        if lid in self._lessons:
            self._lessons[lid].status = "cancelled"
            return True
        return False

    async def get_balance(self, sid):
        return self._balances.get(sid, 0.0)

    async def check_low_balance(self, sid, threshold=60.0):
        return (await self.get_balance(sid)) < threshold

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
