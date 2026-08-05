import asyncio
import json
import logging

import httpx

from src.config import settings

logger = logging.getLogger(__name__)


class LLMClient:
    def __init__(self):
        self.api_key = settings.openrouter_api_key
        self.base_url = settings.openrouter_base_url
        self.cheap_model = settings.llm_cheap_model
        self.http_client = httpx.AsyncClient(timeout=30.0)
        self._mock = not bool(self.api_key)
        if self._mock:
            logger.warning("AI: MOCK mode (no API key)")

    async def chat_cheap(self, messages):
        if self._mock:
            return self._mock_response(messages)
        try:
            resp = await self.http_client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.cheap_model,
                    "messages": [{"role": "system", "content": "You output JSON."}] + messages,
                    "temperature": 0.1,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error("LLM: %s", e)
            return self._mock_response(messages)

    async def extract_entities(self, text):
        r = await self.chat_cheap([
            {"role": "user", "content": f'Extract JSON from: "{text}". Fields: subject, grade_level, goal, is_lead.'}
        ])
        try:
            return json.loads(r)
        except Exception:
            return {"is_lead": False}

    async def classify_intent(self, text):
        try:
            r = await asyncio.wait_for(self.chat_cheap([
                {"role": "user", "content": f'Classify intent: "{text}". Return JSON: intent (lead/cancellation/reschedule/absence_report/question/other), confidence.'}
            ]), timeout=8.0)
        except (asyncio.TimeoutError, Exception):
            logger.warning("LLM classify_intent timed out or failed for text=%.40s", text)
            return {"intent": "other", "confidence": 0.0}
        try:
            return json.loads(r)
        except Exception:
            return {"intent": "other", "confidence": 0.0}

    async def interpret_parent_reply(self, text: str) -> dict:
        """Понимает ответ родителя на сообщение о неявке/предурочном статусе.

        Возвращает JSON вида:
          {"status": "ok|no_show|late|other", "confidence": 0..1, "summary": "..."}
        """
        r = await self.chat_cheap([
            {
                "role": "user",
                "content": (
                    f'Interpret parent reply about a student absence: "{text}". '
                    'Return JSON with fields: '
                    'status (ok/no_show/late/other), confidence, summary. '
                    'Use no_show if parent says the student will miss class, '
                    'late if they will be late, ok if everything is fine/handled.'
                ),
            }
        ])
        try:
            parsed = json.loads(r)
            if parsed.get("status") not in {"ok", "no_show", "late", "other"}:
                parsed["status"] = "other"
            parsed.setdefault("confidence", 0.0)
            parsed.setdefault("summary", text[:120])
            return parsed
        except Exception:
            return self._heuristic_parent_reply(text)

    async def interpret_tutor_reply(self, text: str) -> dict:
        r = await self.chat_cheap([
            {
                "role": "user",
                "content": (
                    f'Interpret tutor reply about lesson readiness: "{text}". '
                    'Return JSON with fields: status (ready/late/no_show/tech/other), confidence, summary. '
                    'Use no_show if tutor cannot conduct class, tech for platform/device issues, '
                    'late if tutor will join late, ready if everything is on track.'
                ),
            }
        ])
        try:
            parsed = json.loads(r)
            if parsed.get("status") not in {"ready", "late", "no_show", "tech", "other"}:
                parsed["status"] = "other"
            parsed.setdefault("confidence", 0.0)
            parsed.setdefault("summary", text[:120])
            return parsed
        except Exception:
            return self._heuristic_tutor_reply(text)

    def _heuristic_parent_reply(self, text: str) -> dict:
        t = (text or "").lower().strip()
        late_words = ["опоз", "задерж", "late", "через ", "через", "будем через", "минут"]
        no_show_words = ["не прид", "не будет", "не смож", "боле", "забол", "пропуст", "cancel"]
        ok_words = ["всё ок", "все ок", "в порядке", "спасибо", "ok", "okay", "thanks", "добер", "подключ"]
        if any(w in t for w in no_show_words):
            return {"status": "no_show", "confidence": 0.85, "summary": text[:120]}
        if any(w in t for w in late_words):
            return {"status": "late", "confidence": 0.8, "summary": text[:120]}
        if any(w in t for w in ok_words):
            return {"status": "ok", "confidence": 0.75, "summary": text[:120]}
        return {"status": "other", "confidence": 0.4, "summary": text[:120]}

    def _heuristic_tutor_reply(self, text: str) -> dict:
        t = (text or "").lower().strip()
        # R7-9: тьюторы англоговорящие — словарики пополнены EN-формулировками.
        if any(w in t for w in ["не смогу", "не могу", "отмен", "не провед", "cancel",
                                "can't", "cannot", "can not", "won't", "no show", "no-show",
                                "sick", "unable", "have to skip", "absent"]):
            return {"status": "no_show", "confidence": 0.85, "summary": text[:120]}
        if any(w in t for w in ["тех", "интернет", "камера", "микрофон", "platform", "платформ",
                                "connection", "wifi", "wi-fi", "internet", "camera", "mic",
                                "microphone", "laptop", "computer", "vpn", "is down", "broken",
                                "issue with", "problem with"]):
            return {"status": "tech", "confidence": 0.85, "summary": text[:120]}
        if any(w in t for w in ["опоз", "задерж", "late", "через",
                                "running late", "behind", "delay", "stuck", "traffic",
                                "be there in", "minutes late", "few min"]):
            return {"status": "late", "confidence": 0.8, "summary": text[:120]}
        if any(w in t for w in ["готов", "ok", "в порядке", "буду", "на месте",
                                "ready", "all set", "confirmed", "confirm", "on track",
                                "good to go", "yes", "yep", "fine"]):
            return {"status": "ready", "confidence": 0.75, "summary": text[:120]}
        return {"status": "other", "confidence": 0.4, "summary": text[:120]}

    def _mock_response(self, msgs):
        full = msgs[-1]["content"].lower() if msgs else ""
        # Сканируем ТОЛЬКО текст пользователя (в первой паре кавычек), а не весь
        # промпт: шаблон сам содержит status-слова («ready», «late», «cannot conduct
        # class») — при полном скане все ответы схлопываются в один статус (R7-9;
        # латентный баг mock-режима).
        raw = msgs[-1]["content"] if msgs else ""
        quoted = raw.split('"')
        t = quoted[1].lower() if len(quoted) > 2 else full
        if "extract" in full:
            return json.dumps({"subject": "mathematics", "grade_level": "9", "is_lead": True})
        if "interpret parent reply" in full:
            if any(w in t for w in ["не прид", "не будет", "боле", "cancel"]):
                return json.dumps({"status": "no_show", "confidence": 0.9, "summary": "parent says student will miss class"})
            if any(w in t for w in ["опоз", "late", "через"]):
                return json.dumps({"status": "late", "confidence": 0.85, "summary": "parent says student will be late"})
            if any(w in t for w in ["в порядке", "ok", "спасибо", "подключ"]):
                return json.dumps({"status": "ok", "confidence": 0.8, "summary": "parent says everything is fine"})
            return json.dumps({"status": "other", "confidence": 0.4, "summary": "free text"})
        if "interpret tutor reply" in full:
            # R7-9: EN-слова — тьюторы англоговорящие.
            if any(w in t for w in ["не смогу", "не могу", "отмен", "cancel",
                                    "can't", "cannot", "can not", "won't", "unable"]):
                return json.dumps({"status": "no_show", "confidence": 0.9, "summary": "tutor cannot conduct class"})
            if any(w in t for w in ["тех", "интернет", "platform", "камера", "микрофон",
                                    "connection", "wifi", "wi-fi", "internet", "camera", "mic",
                                    "issue with", "problem with"]):
                return json.dumps({"status": "tech", "confidence": 0.88, "summary": "tutor has technical issue"})
            if any(w in t for w in ["опоз", "late", "через",
                                    "running late", "behind", "delay", "stuck", "traffic"]):
                return json.dumps({"status": "late", "confidence": 0.85, "summary": "tutor will be late"})
            if any(w in t for w in ["готов", "ok", "буду", "на месте",
                                    "ready", "all set", "confirmed", "on track"]):
                return json.dumps({"status": "ready", "confidence": 0.8, "summary": "tutor is ready"})
            return json.dumps({"status": "other", "confidence": 0.4, "summary": "free text"})
        if "intent" in full or "classify" in full:
            if any(w in t for w in ["отмени", "cancel"]):
                return json.dumps({"intent": "cancellation", "confidence": 0.95})
            if any(w in t for w in ["absent", "отсутств"]):
                return json.dumps({"intent": "absence_report", "confidence": 0.9})
            return json.dumps({"intent": "lead", "confidence": 0.6})
        return json.dumps({"intent": "other", "confidence": 0.5})


llm_client = LLMClient()
