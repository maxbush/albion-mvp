from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class Tutor:
    id: str; name: str; subjects: list[str]
    telegram_id: str | None = None; is_active: bool = True

@dataclass
class Student:
    id: str; name: str; grade_level: str | None = None
    telegram_id: str | None = None; parent_telegram_id: str | None = None

@dataclass
class Lesson:
    id: str; student_id: str; tutor_id: str; subject: str
    start_time: datetime; end_time: datetime; status: str = "scheduled"

@dataclass
class Lead:
    id: str; raw_message: str; extracted_data: dict = field(default_factory=dict); status: str = "new"
