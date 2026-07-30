"""Реальный клиент MeritHub Virtual Classroom API (OAuth2 + JWT).

Контракт — по официальной документации MeritHub:
  auth:   JWT(HS256, secret=CLIENT_SECRET) -> POST /api/token -> access token (60 мин)
  users:  {service_host}/v1/{CLIENT_ID}/users            (POST/PUT/DELETE)
  class:  {class_host}/v1/{CLIENT_ID}/{INSTRUCTOR_ID}    (POST schedule)
          {class_host}/v1/{CLIENT_ID}/{CLASS_ID}/users   (POST add users)
          {class_host}/v1/{CLIENT_ID}/{CLASS_ID}/removeuser
          {class_host}/v1/{CLIENT_ID}/{CLASS_ID}         (PUT edit / DELETE)
  links:  {live_host}/info/room/{CLIENT_ID}/{link}

ВАЖНО: MeritHub НЕ отдаёт список юзеров/классов через API — возвращённые
UserId/ClassId нужно хранить у себя (см. таблицы merithub_students /
merithub_enrollments и src/integrations/factory.py).

httpx-transport можно подменить в тестах (MockTransport) — сеть не нужна.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

# Логировать тело запросов/ответов (по умолчанию выкл — включает шум).
# Можно включить через MERITHUB_LOG_PAYLOAD=true в .env
_LOG_PAYLOAD = settings.merithub_log_payload

GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"
DEFAULT_SERVICE_HOST = "https://serviceaccount1.meritgraph.com"
DEFAULT_CLASS_HOST = "https://class1.meritgraph.com"
DEFAULT_LIVE_HOST = "https://live.merithub.com"
DEFAULT_IMG = "https://hst.meritgraph.com/theme/img/png/avtr.png"


class MeritHubError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class MeritHubClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        service_host: str = DEFAULT_SERVICE_HOST,
        class_host: str = DEFAULT_CLASS_HOST,
        live_host: str = DEFAULT_LIVE_HOST,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.service_host = service_host.rstrip("/")
        self.class_host = class_host.rstrip("/")
        self.live_host = live_host.rstrip("/")
        self.timeout = timeout
        self._transport = transport
        self._token: str | None = None
        self._token_exp: float = 0.0
        self._token_lock = asyncio.Lock()
        # Кеш успешного формата авторизации для Class API
        self._class_auth_mode: str | None = None

    # ── JWT + access token ────────────────────────────────────────────
    def build_jwt(self) -> str:
        header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
        payload = _b64url(json.dumps({
            "aud": f"{self.service_host}/v1/{self.client_id}/api/token",
            "iss": self.client_id,
            "expiry": 3600,
        }, separators=(",", ":")).encode())
        sig = hmac.new(self.client_secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
        return f"{header}.{payload}.{_b64url(sig)}"

    async def _fetch_token(self) -> str:
        url = f"{self.service_host}/v1/{self.client_id}/api/token"
        data = {"assertion": f"Bearer {self.build_jwt()}", "grant_type": GRANT_TYPE}
        logger.info("MeritHub Auth: POST %s", url)
        async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as c:
            r = await c.post(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
        if r.status_code >= 400:
            logger.error("MeritHub Auth: %d %s", r.status_code, r.text[:200])
            raise MeritHubError(f"token {r.status_code}: {r.text[:300]}", r.status_code)
        body = r.json() if r.content else {}
        token = body.get("access_token") or body.get("token") or (body.get("data") or {}).get("access_token")
        if not isinstance(token, str) or not token.strip():
            logger.error("MeritHub Auth: no access_token in response: %s", str(body)[:300])
            raise MeritHubError(f"token response has no access_token: {str(body)[:300]}")
        token = token.strip()
        logger.info("MeritHub Auth: token obtained (exp=%dmin)", 55)
        return token

    async def _get_token(self) -> str:
        """Double-check locking — не более одного _fetch_token при конкурентном доступе."""
        if self._token and time.time() < self._token_exp:
            return self._token
        async with self._token_lock:
            if not self._token or time.time() >= self._token_exp:
                self._token = await self._fetch_token()
                self._token_exp = time.time() + 55 * 60
            return self._token

    @staticmethod
    def _bearer(token: str) -> str:
        """Безопасно добавляет 'Bearer ' если его нет."""
        t = token.strip()
        return t if t.lower().startswith("bearer ") else f"Bearer {t}"

    def _is_class_api(self, url: str) -> bool:
        return url.startswith(f"{self.class_host}/")

    # ── общий запрос с авто-обновлением токена на 401 ─────────────────
    async def _request(self, method: str, url: str, json_body: dict | None = None) -> dict:
        is_class = self._is_class_api(url)

        # Определяем форматы заголовков для этого запроса
        if is_class and self._class_auth_mode:
            # Используем закешированный успешный формат
            auth_values = [self._class_auth_mode]
        elif is_class:
            # Class API: сначала голый токен, потом Bearer (fallback)
            auth_values = [None, None]  # будут заполнены ниже
        else:
            # Users API: только Bearer
            auth_values = [None]

        for attempt in range(2):
            token = await self._get_token()
            bearer_val = self._bearer(token)

            if is_class and not self._class_auth_mode:
                auth_values = [token, bearer_val]

            for auth_val in auth_values:
                if auth_val is None:
                    auth_val = bearer_val
                headers = {"Authorization": auth_val, "Content-Type": "application/json"}
                logger.debug("MeritHub API: trying Authorization: %s...%s",
                             auth_val[:20], " (class mode)" if is_class else "")
                async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as c:
                    r = await c.request(method, url, headers=headers, json=json_body)
                status = r.status_code
                body_preview = r.text[:200].replace("\n", " ")

                if status == 401:
                    if is_class and not self._class_auth_mode:
                        # Пробуем следующий формат авторизации
                        continue
                    # Все форматы перепробованы — инвалидируем токен и пробуем заново
                    if attempt == 0:
                        logger.warning("MeritHub API: 401 on %s %s - refreshing token", method, url)
                        self._token = None
                        self._class_auth_mode = None
                        break  # выходим из цикла форматов, переходим к следующей попытке
                    logger.error("MeritHub API: %s %s -> %d %s", method, url, status, body_preview)
                    raise MeritHubError(f"{method} {url} -> {status}: {r.text[:300]}", status)

                if status == 409:
                    # Class already exists — пытаемся извлечь classId из ответа
                    body = r.json() if r.content else {}
                    if body.get("classId"):
                        logger.info("MeritHub API: %s %s -> 409 (class exists, id=%s)", method, url, body["classId"])
                        return body
                    logger.error("MeritHub API: %s %s -> 409 %s", method, url, body_preview)
                    raise MeritHubError(f"409 Conflict: {body}", 409)

                # Успех
                if is_class:
                    # Запоминаем успешный формат для Class API
                    self._class_auth_mode = auth_val
                logger.info("MeritHub API: %s %s -> %d", method, url, status)
                if _LOG_PAYLOAD and json_body:
                    logger.debug("MeritHub API payload: %s", json.dumps(json_body, ensure_ascii=False)[:500])
                return r.json() if r.content else {}

            if attempt == 0 and is_class and not self._class_auth_mode:
                # Все форматы перепробовали, токен инвалидирован — повторим с новым токеном
                continue
            break

        return {}

    @staticmethod
    def _extract_id(resp: dict, *keys: str) -> str | None:
        for k in keys:
            if resp.get(k):
                return str(resp[k])
        data = resp.get("data")
        if isinstance(data, dict):
            for k in keys:
                if data.get(k):
                    return str(data[k])
        return None

    # ── Users ─────────────────────────────────────────────────────────
    async def add_user(
        self,
        *,
        client_user_id: str,
        name: str,
        role: str = "M",
        email: str | None = None,
        permission: str | None = None,
        title: str = "",
        img: str = DEFAULT_IMG,
        lang: str = "en",
        timezone: str = "Europe/London",
        desc: str = "",
    ) -> dict:
        """role: 'C' = репетитор/creator, 'M' = ученик. Возвращает ответ MeritHub
        (с MeritHub UserId — извлекай через _extract_id(resp,'userId','id'))."""
        body = {
            "name": name, "title": title, "img": img, "desc": desc, "lang": lang,
            "clientUserId": client_user_id,
            "email": email or f"{client_user_id}@albion.local",
            "role": role,
            "timeZone": timezone,
            "permission": permission or ("CC" if role == "C" else "CJ"),
        }
        return await self._request("POST", f"{self.service_host}/v1/{self.client_id}/users", body)

    async def update_user(self, merithub_user_id: str, **fields) -> dict:
        return await self._request("PUT", f"{self.service_host}/v1/{self.client_id}/users/{merithub_user_id}", fields)

    async def delete_user(self, merithub_user_id: str) -> dict:
        return await self._request("DELETE", f"{self.service_host}/v1/{self.client_id}/users/{merithub_user_id}")

    # ── Classes ──────────────────────────────────────────────────────
    async def schedule_class(
        self,
        instructor_merithub_id: str,
        *,
        title: str,
        start_time: str,
        duration: int,
        type: str = "oneTime",
        timezone: str = "Europe/London",
        layout: str = "CR",
        status: str = "up",
        description: str = "",
        schedule: list[int] | None = None,
        total_classes: int | None = None,
        end_date: str | None = None,
        recording: bool = True,
        recording_download: bool = False,
        av_mode: str = "",
    ) -> dict:
        body = {
            "title": title, "startTime": start_time, "duration": duration, "lang": "en",
            "timeZoneId": timezone, "description": description, "type": type,
            "access": "private", "login": False, "layout": layout, "status": status,
            "recordingDownload": recording_download,
            "recording": {"record": recording, "autoRecord": False, "recordingControl": True},
            "participantControl": {"write": False, "audio": False, "video": False},
            "whiteboard": {"asyncMode": False},
            "avMode": av_mode,
        }
        if end_date:
            body["endDate"] = end_date
        if schedule is not None:
            body["schedule"] = schedule
        if total_classes is not None:
            body["totalClasses"] = total_classes
        return await self._request("POST", f"{self.class_host}/v1/{self.client_id}/{instructor_merithub_id}", body)

    async def edit_class(
        self,
        class_id: str,
        *,
        title: str,
        start_time: str,
        duration: int,
        timezone: str = "Europe/London",
        description: str = "",
        layout: str = "CR",
        login: bool = False,
        schedule: list[int] | None = None,
        end_date: str | None = None,
        recording: bool = True,
        recording_download: bool = False,
    ) -> dict:
        body = {
            "title": title,
            "startTime": start_time,
            "duration": duration,
            "lang": "en",
            "timeZoneId": timezone,
            "description": description,
            "login": login,
            "layout": layout,
            "recordingDownload": recording_download,
            "recording": {"record": recording, "autoRecord": False, "recordingControl": True},
            "participantControl": {"write": False, "audio": False, "video": False},
        }
        if end_date:
            body["endDate"] = end_date
        if schedule is not None:
            body["schedule"] = schedule
        return await self._request("PUT", f"{self.class_host}/v1/{self.client_id}/{class_id}", body)

    async def add_users_to_class(self, class_id: str, users: list[dict]) -> dict:
        """users: [{"userId","userLink","userType":"su"}]. userLink = commonParticipantLink
        для ученика / commonHostLink для репетитора (из ответа schedule_class)."""
        return await self._request(
            "POST", f"{self.class_host}/v1/{self.client_id}/{class_id}/users", {"users": users})

    async def remove_users_from_class(self, class_id: str, user_ids: list[str]) -> dict:
        return await self._request(
            "POST", f"{self.class_host}/v1/{self.client_id}/{class_id}/removeuser", {"users": user_ids})

    async def delete_class(self, class_id: str) -> dict:
        return await self._request("DELETE", f"{self.class_host}/v1/{self.client_id}/{class_id}")

    # ── Ссылки для открытия комнаты ───────────────────────────────────
    def room_url(self, link: str, device_test: bool = False) -> str:
        url = f"{self.live_host}/info/room/{self.client_id}/{link}"
        return url + "?devicetest=true" if device_test else url

    # ── парсинг ответов (формы не показаны в доке явно → защитный разбор) ──
    @staticmethod
    def parse_schedule(resp: dict) -> dict:
        common = resp.get("commonLinks") or resp.get("common_links") or {}
        if not isinstance(common, dict):
            common = {}
        return {
            "class_id": str(resp.get("classId") or resp.get("class_id") or resp.get("id") or ""),
            "host_link": common.get("commonHostLink") or resp.get("hostLink") or resp.get("host_link") or "",
            "participant_link": (
                common.get("commonParticipantLink") or resp.get("commonParticipantLink")
                or common.get("commonParticipantlink") or ""
            ),
        }

    @staticmethod
    def parse_user_links(resp) -> dict:
        """add_users_to_class → {merithub_user_id: unique_userLink}.

        Поддерживает:
        - прямой список [{"userId":..., "userLink":...}, ...]
        - {"users": [...]}
        - {"data": {"users": [...]}}
        - {"data": [...]}
        """
        out: dict = {}
        users = None

        if isinstance(resp, list):
            users = resp
        elif isinstance(resp, dict):
            users = resp.get("users") or None
            if users is None and isinstance(resp.get("data"), dict):
                users = resp["data"].get("users") or None
            if users is None and isinstance(resp.get("data"), list):
                users = resp["data"]

        if not isinstance(users, list):
            return out

        for u in users:
            if not isinstance(u, dict):
                continue
            uid = u.get("userId") or u.get("user_id")
            link = u.get("userLink") or u.get("user_link")
            if uid and link:
                out[str(uid)] = link
        return out

    # ── маппинг webhook-поля attendee → «присутствовал ли» ────────────
    @staticmethod
    def attended_user_ids(attendance_payload: dict) -> set[str]:
        """Из webhook requestType=attendance достаёт userId тех, кто реально был
        (totalTime > 0). Отсутствующих в списке нет — их вычисляет вызывающий код
        как (зачисленные - присутствовавшие)."""
        ids = set()
        for a in attendance_payload.get("attendance", []) or []:
            uid = a.get("userId")
            if not uid:
                continue
            try:
                present = int(a.get("totalTime", 0) or 0) > 0
            except (TypeError, ValueError):
                present = bool(a.get("startTime"))
            if present:
                ids.add(str(uid))
        return ids
