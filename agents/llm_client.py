"""LLM API 呼叫與結構化輸出封裝。

支援三種 provider：
- openai：OpenAI Chat Completions（JSON mode）
- gemini：Google Gen AI SDK（application/json 輸出）
- mock：離線示範用，依規則式初篩結果產生確定性回覆，不需 API Key

所有 provider 都回傳經 Pydantic 驗證的物件；JSON 解析或驗證失敗時，會把錯誤訊息回饋給模型重試。
"""

from __future__ import annotations

import json
import os
import re
import time
from abc import ABC, abstractmethod
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODELS = {
    "openai": os.getenv("OPENAI_MODEL", "gpt-4.1"),
    "gemini": os.getenv("GEMINI_MODEL", "gemini-2.5-pro"),
    "mock": "mock-reviewer",
}


class LLMError(RuntimeError):
    pass


def schema_text(model: type[BaseModel]) -> str:
    return json.dumps(model.model_json_schema(), ensure_ascii=False, indent=1)


def extract_json(text: str) -> Any:
    """從模型輸出擷取 JSON（容忍 ```json 程式碼框或前後多餘文字）。"""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


class LLMClient(ABC):
    provider: str = ""

    def __init__(self, model: str | None = None, max_retries: int = 2):
        self.model = model or DEFAULT_MODELS[self.provider]
        self.max_retries = max_retries

    @abstractmethod
    def _complete(self, system: str, user: str, temperature: float) -> str:
        """送出一次請求，回傳純文字。"""

    def generate(
        self,
        system: str,
        user: str,
        schema: type[T],
        *,
        temperature: float = 0.3,
        task: str = "",
        payload: dict | None = None,
    ) -> T:
        """呼叫模型並以 schema 驗證輸出。task/payload 僅供 mock provider 使用。"""
        prompt = user
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                raw = self._complete(system, prompt, temperature)
                return schema.model_validate(extract_json(raw))
            except (json.JSONDecodeError, ValidationError) as e:
                last_err = e
                prompt = (
                    f"{user}\n\n# 上一次輸出未通過格式驗證\n錯誤：{str(e)[:1500]}\n"
                    "請修正後只輸出符合 Schema 的 JSON。"
                )
            except Exception as e:  # 網路／速率限制等暫時性錯誤
                last_err = e
                if attempt < self.max_retries:
                    time.sleep(2 ** attempt)
        raise LLMError(f"{self.provider} 呼叫失敗（已重試 {self.max_retries} 次）：{last_err}")


class OpenAIClient(LLMClient):
    provider = "openai"

    def __init__(self, model: str | None = None, api_key: str | None = None, **kw):
        super().__init__(model, **kw)
        from openai import OpenAI

        key = api_key or os.getenv("OPENAI_API_KEY")
        if not key:
            raise LLMError("未設定 OPENAI_API_KEY")
        self._client = OpenAI(api_key=key, base_url=os.getenv("OPENAI_BASE_URL") or None)
        self._send_temperature = True

    def _complete(self, system: str, user: str, temperature: float) -> str:
        from openai import BadRequestError

        kwargs: dict[str, Any] = dict(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
        )
        if self._send_temperature:
            kwargs["temperature"] = temperature
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except BadRequestError as e:
            # 部分推理模型不接受 temperature 參數
            if self._send_temperature and "temperature" in str(e):
                self._send_temperature = False
                kwargs.pop("temperature")
                resp = self._client.chat.completions.create(**kwargs)
            else:
                raise
        return resp.choices[0].message.content or ""


class GeminiClient(LLMClient):
    provider = "gemini"

    def __init__(self, model: str | None = None, api_key: str | None = None, **kw):
        super().__init__(model, **kw)
        from google import genai

        key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not key:
            raise LLMError("未設定 GEMINI_API_KEY")
        self._client = genai.Client(api_key=key)

    def _complete(self, system: str, user: str, temperature: float) -> str:
        from google.genai import types

        resp = self._client.models.generate_content(
            model=self.model,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                temperature=temperature,
                response_mime_type="application/json",
            ),
        )
        return resp.text or ""


class MockClient(LLMClient):
    """離線示範：依 payload（規則式初篩結果與前一回合輸出）產生合理的確定性回覆。"""

    provider = "mock"

    def __init__(self, model: str | None = None, delay: float = 0.0, **kw):
        super().__init__(model, **kw)
        self._severity: dict[str, str] = {}
        self.delay = delay  # 每次呼叫的模擬延遲（秒），供介面示範逐回合顯示

    def _complete(self, system: str, user: str, temperature: float) -> str:  # pragma: no cover
        raise NotImplementedError

    def generate(self, system, user, schema, *, temperature=0.3, task="", payload=None):
        payload = payload or {}
        if self.delay:
            time.sleep(self.delay)
        builder = getattr(self, f"_mock_{task}", None)
        if builder is None:
            raise LLMError(f"Mock 不支援 task={task!r}")
        return schema.model_validate(builder(payload))

    # ---- 各回合 ----
    def _mock_round1(self, p: dict) -> dict:
        issues = []
        for f in p.get("findings", [])[:5]:
            qid = f"Q{len(issues) + 1}"
            self._severity[qid] = f["level"]
            issues.append({
                "issue_id": qid,
                "title": f["item"][:20],
                "category": _category_of(f["item"]),
                "cited_data": f"工作表「{f['sheet']}」：{f['item']}",
                "challenge": f"{f['detail']}，請說明是否符合規範要求並提出檢討。",
                "formula": f["detail"],
                "code_reference": f["reference"],
            })
        generic = [
            ("基礎支承假設", "邊界條件", "模型採基礎固接，未見土壤彈簧或基礎轉動勁度之檢討。",
             "建築物基礎構造設計規範（條號待查）"),
            ("意外扭矩", "載重組合", "未見地震力考量 5% 意外偏心之說明。",
             "建築物耐震設計規範及解說 §2.14"),
            ("強柱弱梁檢核", "強度檢核", "未提供梁柱接頭 ΣMnc ≥ 1.2ΣMnb 之檢核結果。",
             "土木401-112 §18.4.3.2"),
        ]
        for title, cat, text, ref in generic:
            if len(issues) >= 3:
                break
            qid = f"Q{len(issues) + 1}"
            self._severity[qid] = "Info"
            issues.append({
                "issue_id": qid, "title": title, "category": cat, "cited_data": "資料未提供",
                "challenge": text, "formula": "—", "code_reference": ref,
            })
        return {"summary": f"初審共提出 {len(issues)} 項疑義（離線示範模式）。", "issues": issues}

    def _mock_round2(self, p: dict) -> dict:
        out = []
        for it in p["issues"]:
            sev = self._severity.get(it["issue_id"], "Info")
            if sev == "High":
                out.append({
                    "issue_id": it["issue_id"], "stance": "承認缺失",
                    "response": "經查屬實，將於修正版計算書調整。",
                    "data_source": it["cited_data"], "code_reference": it["code_reference"],
                    "proposed_action": "加大斷面或調整配筋後重新分析，並檢附修正後檢核表。",
                })
            elif sev == "Medium":
                out.append({
                    "issue_id": it["issue_id"], "stance": "計算推導抗辯",
                    "response": "該項數值仍在規範容許範圍內，餘裕雖小但滿足需求。",
                    "calculation": it["formula"], "data_source": it["cited_data"],
                    "code_reference": it["code_reference"],
                })
            else:
                out.append({
                    "issue_id": it["issue_id"], "stance": "規範依據抗辯",
                    "response": "本案已依規範相關條文辦理，相關檢核詳計算書附錄。",
                    "data_source": "計算書附錄", "code_reference": it["code_reference"],
                })
        return {"responses": out}

    def _mock_round3(self, p: dict) -> dict:
        table = {
            "High": ("部分接受", "High", "修正方案方向正確，但修正前不符規範，須檢附修正成果。"),
            "Medium": ("部分接受", "Medium", "計算可接受，惟請補充完整推導供複核。"),
            "Info": ("接受", "Pass", "回覆合理，疑義解除。"),
        }
        out = []
        for r in p["responses"]:
            d, risk, why = table[self._severity.get(r["issue_id"], "Info")]
            out.append({
                "issue_id": r["issue_id"], "decision": d, "risk_level": risk, "rationale": why,
                "required_action": "" if risk == "Pass" else "請於修正版計算書補正。",
            })
        return {"verdicts": out}

    def _mock_chief(self, p: dict) -> dict:
        titles = {i["issue_id"]: i["title"] for i in p["issues"]}
        buckets = {"Pass": [], "Medium": [], "High": []}
        for v in p["verdicts"]:
            buckets[v["risk_level"]].append({
                "issue_id": v["issue_id"], "title": titles.get(v["issue_id"], v["issue_id"]),
                "risk_level": v["risk_level"], "finding": v["rationale"],
                "basis": "依外審終審判定及規則式初篩結果。",
                "recommendation": v["required_action"] or "無",
            })
        conclusion = "退回補正" if buckets["High"] else ("修正後同意" if buckets["Medium"] else "同意備查")
        return {
            "overall_conclusion": conclusion,
            "executive_summary": (
                f"本次審查共 {len(p['verdicts'])} 項疑義：重大疑義 {len(buckets['High'])} 項、"
                f"待複核 {len(buckets['Medium'])} 項、無虞 {len(buckets['Pass'])} 項。（離線示範模式）"
            ),
            "no_concern_items": buckets["Pass"],
            "major_doubts": buckets["High"],
            "pending_review_items": buckets["Medium"],
            "debate_observations": "離線示範模式由規則產生，未經語言模型推理。",
        }


def _category_of(item: str) -> str:
    for key, cat in (("位移", "位移角"), ("配筋", "配筋比"), ("主筋", "配筋比"),
                     ("應力", "強度檢核"), ("載重組合", "載重組合")):
        if key in item:
            return cat
    return "其他"


def create_client(provider: str, model: str | None = None, api_key: str | None = None) -> LLMClient:
    provider = provider.lower()
    if provider == "openai":
        return OpenAIClient(model, api_key=api_key)
    if provider == "gemini":
        return GeminiClient(model, api_key=api_key)
    if provider == "mock":
        return MockClient(model)
    raise LLMError(f"未知的 provider：{provider}")


__all__ = ["LLMClient", "LLMError", "create_client", "schema_text", "extract_json"]
