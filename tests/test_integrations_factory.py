"""Тесты реальной интеграции MeritHub: OAuth2/JWT клиент, фабрика, маппинги,
авто-неявка по webhook attendance."""

import base64
import hashlib
import hmac
import json

import pytest

from src.config import settings


# =====================================================================
# Фейковый httpx.AsyncClient (не зависит от версии httpx)
# =====================================================================
class _Resp:
    def __init__(self, status=200, json_body=None, text=""):
        self.status_code = status
        self._json = json_body
        self.text = text
        self.content = json.dumps(json_body).encode() if json_body is not None else b""

    def json(self):
        return self._json


def _default_router(url, body):
    if "/api/token" in url:
        return _Resp(200, {"access_token": "TOK123"})
    return _Resp(200, {"userId": "mh_7"})


class FakeAsyncClient:
    last_calls: list = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, headers=None, json=None, **kw):
        FakeAsyncClient.last_calls.append((method, str(url), dict(headers or {}), json))
        return _default_router(str(url), json)

    async def post(self, url, data=None, headers=None, **kw):
        FakeAsyncClient.last_calls.append(("POST", str(url), dict(headers or {}), data))
        return _default_router(str(url), data)


def _b64url_decode(s: str) -> bytes:
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


@pytest.fixture
def fake_httpx(monkeypatch):
    FakeAsyncClient.last_calls = []
    monkeypatch.setattr("src.integrations.merithub_client.httpx.AsyncClient", FakeAsyncClient)
    return FakeAsyncClient


# =====================================================================
# Фабрика (Vendor Agnostic)
# =====================================================================
def test_factory_returns_mock_without_credentials(monkeypatch):
    monkeypatch.setattr(settings, "merithub_client_id", None)
    monkeypatch.setattr(settings, "merithub_client_secret", None)
    from src.integrations.factory import get_merithub_service
    from src.integrations.merithub_mock import MockMeritHubService
    assert isinstance(get_merithub_service(), MockMeritHubService)


def test_factory_returns_real_client_with_credentials(monkeypatch):
    monkeypatch.setattr(settings, "merithub_client_id", "cid")
    monkeypatch.setattr(settings, "merithub_client_secret", "csec")
    from src.integrations.factory import get_merithub_service
    from src.integrations.merithub_client import MeritHubClient
    assert isinstance(get_merithub_service(), MeritHubClient)


def test_absence_workflow_uses_factory(monkeypatch):
    monkeypatch.setattr(settings, "merithub_client_id", None)
    monkeypatch.setattr(settings, "merithub_client_secret", None)
    from src.workflows.absence import AbsenceWorkflow
    from src.integrations.merithub_mock import MockMeritHubService
    wf = AbsenceWorkflow("nonexistent.db")
    assert isinstance(wf.merithub, MockMeritHubService)


# =====================================================================
# JWT + токен
# =====================================================================
def test_build_jwt_signature_and_payload():
    from src.integrations.merithub_client import MeritHubClient
    c = MeritHubClient("cid", "csec")
    jwt = c.build_jwt()
    h, p, s = jwt.split(".")
    # подпись = HMAC-SHA256(secret, header.payload)
    expect = base64.urlsafe_b64encode(
        hmac.new(b"csec", f"{h}.{p}".encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    assert s == expect
    payload = json.loads(_b64url_decode(p))
    assert payload["iss"] == "cid" and payload["expiry"] == 3600
    assert payload["aud"].endswith("/v1/cid/api/token")


@pytest.mark.asyncio
async def test_fetch_token_posts_jwt_and_grant(fake_httpx):
    from src.integrations.merithub_client import MeritHubClient
    c = MeritHubClient("cid", "csec")
    tok = await c._fetch_token()
    assert tok == "TOK123"
    method, url, headers, data = fake_httpx.last_calls[-1]
    assert method == "POST" and url.endswith("/v1/cid/api/token")
    assert data["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    assert data["assertion"].startswith("Bearer ")


@pytest.mark.asyncio
async def test_add_user_sends_bearer_and_body(fake_httpx):
    from src.integrations.merithub_client import MeritHubClient
    c = MeritHubClient("cid", "csec")
    resp = await c.add_user(client_user_id="u1", name="Миша", role="M")
    assert resp["userId"] == "mh_7"
    # первый вызов — токен, второй — add_user
    token_call, user_call = fake_httpx.last_calls[-2], fake_httpx.last_calls[-1]
    assert token_call[1].endswith("/api/token")
    m, url, headers, body = user_call
    assert m == "POST" and url.endswith("/v1/cid/users")
    assert headers["Authorization"] == "Bearer TOK123"
    assert body["clientUserId"] == "u1" and body["role"] == "M" and body["permission"] == "CJ"
    assert body["email"].startswith("u1@")


@pytest.mark.asyncio
async def test_schedule_class_url_uses_instructor(fake_httpx):
    from src.integrations.merithub_client import MeritHubClient
    c = MeritHubClient("cid", "csec")
    await c.schedule_class("instr_1", title="Math", start_time="2026-07-20T15:00:00+03:00", duration=60)
    m, url, headers, body = fake_httpx.last_calls[-1]
    assert m == "POST" and url.endswith("/v1/cid/instr_1")
    assert body["title"] == "Math" and body["duration"] == 60 and body["type"] == "oneTime"


@pytest.mark.asyncio
async def test_token_is_cached(fake_httpx):
    from src.integrations.merithub_client import MeritHubClient
    c = MeritHubClient("cid", "csec")
    await c._get_token()
    await c._get_token()  # второй раз — из кэша, без нового запроса токена
    token_calls = [call for call in fake_httpx.last_calls if call[1].endswith("/api/token")]
    assert len(token_calls) == 1
    c._token_exp = 0  # «истёк» → принудительное обновление
    await c._get_token()
    token_calls = [call for call in fake_httpx.last_calls if call[1].endswith("/api/token")]
    assert len(token_calls) == 2


def test_attended_user_ids():
    from src.integrations.merithub_client import MeritHubClient
    payload = {"attendance": [
        {"userId": "a", "totalTime": 300, "role": "host"},
        {"userId": "b", "totalTime": 0},          # не считаем присутствовавшим
        {"userId": "c", "totalTime": 120},
    ]}
    assert MeritHubClient.attended_user_ids(payload) == {"a", "c"}


def test_room_url():
    from src.integrations.merithub_client import MeritHubClient
    c = MeritHubClient("cid", "csec")
    assert c.room_url("LINK") == "https://live.merithub.com/info/room/cid/LINK"
    assert c.room_url("LINK", device_test=True).endswith("?devicetest=true")


def test_parse_schedule():
    from src.integrations.merithub_client import MeritHubClient
    info = MeritHubClient.parse_schedule({
        "classId": "C9", "hostLink": "HL",
        "commonLinks": {"commonHostLink": "HL", "commonParticipantLink": "PL"},
    })
    assert info == {"class_id": "C9", "host_link": "HL", "participant_link": "PL"}


def test_parse_user_links():
    from src.integrations.merithub_client import MeritHubClient
    assert MeritHubClient.parse_user_links({
        "users": [{"userId": "a", "userLink": "la"}, {"userId": "b", "userLink": "lb"}]
    }) == {"a": "la", "b": "lb"}


# =====================================================================
# Маппинги
# =====================================================================
@pytest.mark.asyncio
async def test_student_mapping_upsert(db_path):
    from src.db.repository import MeritHubStudentRepository
    r = MeritHubStudentRepository(db_path)
    await r.upsert("s1", name="Миша", parent_telegram_id="777")
    await r.upsert("s1", merithub_user_id="mh_1")  # дозаполнение
    s = await r.get_by_client_id("s1")
    assert s["merithub_user_id"] == "mh_1" and s["parent_telegram_id"] == "777"
    assert (await r.get_by_merithub_id("mh_1"))["client_user_id"] == "s1"


@pytest.mark.asyncio
async def test_enrollment_add_list(db_path):
    from src.db.repository import MeritHubEnrollmentRepository
    e = MeritHubEnrollmentRepository(db_path)
    await e.add("C1", "mh_1", client_user_id="s1", parent_telegram_id="777", student_name="Миша")
    await e.add("C1", "mh_2", client_user_id="s2", parent_telegram_id="888", student_name="Катя")
    rows = await e.list_by_class("C1")
    assert {r["merithub_user_id"] for r in rows} == {"mh_1", "mh_2"}


# =====================================================================
# Авто-неявка по webhook attendance
# =====================================================================
@pytest.mark.asyncio
async def test_dispatch_attendance_fires_for_absent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    from src.workflows.engine import engine
    from src.db.repository import (
        WorkflowRepository, ScheduledActionRepository, IncidentRepository,
        MeritHubStudentRepository, MeritHubEnrollmentRepository,
    )
    engine.repo = WorkflowRepository("albion.db")
    engine.scheduler = ScheduledActionRepository("albion.db")

    await MeritHubStudentRepository("albion.db").upsert(
        "s1", merithub_user_id="mh_s1", parent_telegram_id="777", name="Миша", role="student")
    await MeritHubEnrollmentRepository("albion.db").add(
        "C1", "mh_s1", client_user_id="s1", parent_telegram_id="777", student_name="Миша", role="student")
    # репетитор в том же классе — должен игнорироваться
    await MeritHubEnrollmentRepository("albion.db").add(
        "C1", "mh_t1", client_user_id="t1", parent_telegram_id=None, student_name="Анна", role="tutor")

    from src.api.webhook import _dispatch_attendance
    payload = {
        "classId": "C1", "requestType": "attendance",
        "attendance": [{"userId": "mh_t1", "role": "host", "totalTime": 300}],  # студента mh_s1 нет → неявка
    }
    await _dispatch_attendance(payload)

    incs = await IncidentRepository("albion.db")._fetchall("SELECT * FROM incidents")
    assert len(incs) == 1 and incs[0]["type"] == "absence"
    import json as _j
    wf = await WorkflowRepository("albion.db").get(1)
    assert _j.loads(wf["data"])["parent_telegram_id"] == "777"


@pytest.mark.asyncio
async def test_dispatch_attendance_no_enrollment_no_incident(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    from src.db.repository import IncidentRepository
    from src.api.webhook import _dispatch_attendance
    await _dispatch_attendance({"classId": "UNKNOWN", "requestType": "attendance", "attendance": []})
    assert await IncidentRepository("albion.db")._fetchall("SELECT * FROM incidents") == []
