import logging
from datetime import datetime
from src.integrations.base import Tutor, Student, Lesson, Lead
logger = logging.getLogger(__name__)

class MockAirtableService:
    def __init__(self):
        self._tutors = {}; self._students = {}; self._lessons = {}; self._leads = {}
        self._id_counter = 0
        self._seed()
    def _seed(self):
        self._tutors["tutor_1"] = Tutor("tutor_1","Анна Петрова",["mathematics","physics"],"111111")
        self._tutors["tutor_2"] = Tutor("tutor_2","Иван Сидоров",["english","german"],"222222")
        self._students["student_1"] = Student("student_1","Миша Иванов","9","333333","parent_1")
        self._students["student_2"] = Student("student_2","Катя Смирнова","11","444444","parent_2")
        self._lessons["lesson_1"] = Lesson("lesson_1","student_1","tutor_1","mathematics",datetime(2026,7,4,15,0),datetime(2026,7,4,16,0))
        self._lessons["lesson_2"] = Lesson("lesson_2","student_2","tutor_2","english",datetime(2026,7,4,17,0),datetime(2026,7,4,18,0))
        logger.info("Mock Airtable: %d tutors, %d students, %d lessons", len(self._tutors), len(self._students), len(self._lessons))
    async def get_tutor(self, tid): return self._tutors.get(tid)
    async def get_student(self, sid): return self._students.get(sid)
    async def get_lesson(self, lid): return self._lessons.get(lid)
    async def mark_absent(self, lid, by=""):
        if lid in self._lessons: self._lessons[lid].status = "absent"; return True
        return False
    async def cancel_lesson(self, lid, reason=""):
        if lid in self._lessons: self._lessons[lid].status = "cancelled"; return True
        return False
    async def create_lead(self, lead):
        self._id_counter += 1
        lid = f"lead_{self._id_counter}"; lead.id = lid
        self._leads[lid] = lead
        return lid
