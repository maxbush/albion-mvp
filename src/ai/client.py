import json, logging, httpx
from src.config import settings
logger = logging.getLogger(__name__)

class LLMClient:
    def __init__(self):
        self.api_key = settings.openrouter_api_key
        self.base_url = settings.openrouter_base_url
        self.cheap_model = settings.llm_cheap_model
        self.http_client = httpx.AsyncClient(timeout=30.0)
        self._mock = not bool(self.api_key)
        if self._mock: logger.warning("AI: MOCK mode (no API key)")

    async def chat_cheap(self, messages):
        if self._mock: return self._mock_response(messages)
        try:
            resp = await self.http_client.post(f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.cheap_model, "messages": [{"role":"system","content":"You output JSON."}]+messages, "temperature":0.1})
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error("LLM: %s", e)
            return self._mock_response(messages)

    async def extract_entities(self, text):
        r = await self.chat_cheap([{"role":"user","content":f'Extract JSON from: "{text}". Fields: subject, grade_level, goal, is_lead.'}])
        try: return json.loads(r)
        except: return {"is_lead":False}

    async def classify_intent(self, text):
        r = await self.chat_cheap([{"role":"user","content":f'Classify intent: "{text}". Return JSON: intent (lead/cancellation/reschedule/absence_report/question/other), confidence.'}])
        try: return json.loads(r)
        except: return {"intent":"other","confidence":0.0}

    def _mock_response(self, msgs):
        t = msgs[-1]["content"].lower() if msgs else ""
        if "extract" in t: return json.dumps({"subject":"mathematics","grade_level":"9","is_lead":True})
        if "intent" in t or "classify" in t:
            if any(w in t for w in ["отмени","cancel"]): return json.dumps({"intent":"cancellation","confidence":0.95})
            if any(w in t for w in["absent","отсутств"]): return json.dumps({"intent":"absence_report","confidence":0.9})
            return json.dumps({"intent":"lead","confidence":0.6})
        return json.dumps({"intent":"other","confidence":0.5})

llm_client = LLMClient()
