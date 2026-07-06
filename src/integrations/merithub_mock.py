import logging
from datetime import datetime
from src.integrations.base import Lesson
logger = logging.getLogger(__name__)

class MockMeritHubService:
    def __init__(self):
        self._lessons = {}; self._balances = {}
        self._seed()
    def _seed(self):
        self._lessons["mh_lesson_1"] = Lesson("mh_lesson_1","student_1","tutor_1","mathematics",datetime(2026,7,4,15,0),datetime(2026,7,4,16,0))
        self._balances["student_1"] = 150.0; self._balances["student_2"] = 20.0
    async def get_lesson(self, lid): return self._lessons.get(lid)
    async def mark_absent(self, lid):
        if lid in self._lessons: self._lessons[lid].status = "absent"; return True
        return False
    async def cancel_lesson(self, lid, reason=""):
        if lid in self._lessons: self._lessons[lid].status = "cancelled"; return True
        return False
    async def get_balance(self, sid): return self._balances.get(sid, 0.0)
    async def check_low_balance(self, sid, threshold=60.0): return (await self.get_balance(sid)) < threshold
