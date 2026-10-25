"""LLM API 呼叫與結構化輸出封裝。

支援四種 provider：
- openai：OpenAI Chat Completions（JSON mode）
- gemini：Google Gen AI SDK（application/json 輸出）
- claude：Anthropic Messages API
- mock：離線示範用，依計算書文字產生確定性回覆，不需 API Key

計算書 PDF 以附件（Attachment）原檔傳給模型：三家都能直接讀 PDF 的文字、算式、圖與表格版面。
每次呼叫都是獨立的單次請求（不帶歷史對話、不使用伺服器端記憶），同一個模型在不同輪次擔任不同角色時不會共用記憶。

所有 provider 都回傳經 Pydantic 驗證的物件；JSON 解析或驗證失敗時，會把錯誤訊息回饋給模型重試。
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

# 各服務商的旗艦模型（未指定模型時使用；介面上的預設值另依 PREFERRED_MODELS 從查得的清單中挑選）
DEFAULT_MODELS = {
    "gemini": os.getenv("GEMINI_MODEL", "gemini-4-argon"),
    "claude": os.getenv("CLAUDE_MODEL", "claude-opus-5-5"),
    "openai": os.getenv("OPENAI_MODEL", "gpt-6.1-sol"),
    "mock": "mock-reviewer",
}
# 介面預設模型：依序比對查得的模型清單，取第一個符合的（同一規則有多個時，優先不帶 preview／日期的正式版）
PREFERRED_MODELS = {
    "gemini": [r"^gemini-4(\.0)?-argon$", r"gemini-4.*argon", r"argon", r"^gemini-[\d.]+-pro$", r"gemini.*pro"],
    "claude": [r"^claude-opus-5-5$", r"claude-opus-5-5", r"^claude-opus", r"claude-fable", r"claude-sonnet"],
    "openai": [r"^gpt-6\.1-sol$", r"gpt-6\.1.*sol", r"gpt-[\d.]+-sol", r"-sol\b", r"^gpt-[\d.]+$"],
}


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class Attachment:
    """隨提示一起送出的檔案（目前用於計算書 PDF）。"""

    filename: str
    data: bytes
    mime: str = "application/pdf"

    @property
    def b64(self) -> str:
        return base64.b64encode(self.data).decode()


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


def default_model(provider: str, names: list[str]) -> str | None:
    """依 PREFERRED_MODELS 從模型清單挑預設值；都不符合時回傳 None（由呼叫端取清單第一個）。"""
    for pat in PREFERRED_MODELS.get(provider, []):
        hits = [n for n in names if re.search(pat, n)]
        if hits:
            stable = [n for n in hits if not re.search(r"preview|exp|-\d{4}|\d{8}", n)]
            return (stable or hits)[0]
    return None


class LLMClient(ABC):
    provider: str = ""

    def __init__(self, model: str | None = None, max_retries: int = 2):
        self.model = model or DEFAULT_MODELS[self.provider]
        self.max_retries = max_retries
        self.attachments_dropped = False  # 模型不接受 PDF 附件、改以擷取文字審查時為 True（記入報告）

    @abstractmethod
    def _complete(self, system: str, user: str, temperature: float, attachments: list[Attachment]) -> str:
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
        attachments: list[Attachment] | None = None,
    ) -> T:
        """呼叫模型並以 schema 驗證輸出。task/payload 僅供 mock provider 使用。"""
        prompt = user
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                raw = self._complete(system, prompt, temperature, list(attachments or []))
                return schema.model_validate(extract_json(raw))
            except (json.JSONDecodeError, ValidationError) as e:
                last_err = e
                prompt = (
                    f"{user}\n\n# 上一次輸出未通過格式驗證\n錯誤：{str(e)[:1500]}\n"
                    "請修正後只輸出符合 Schema 的 JSON。"
                )
            except LLMError:
                raise
            except Exception as e:  # 網路／速率限制等暫時性錯誤
                last_err = e
                if attempt < self.max_retries:
                    time.sleep(2 ** attempt)
        raise LLMError(f"{self.provider} 呼叫失敗（已重試 {self.max_retries} 次）：{last_err}")


def _rejects_files(err: Exception) -> bool:
    text = str(err).lower()
    return any(k in text for k in ("file", "pdf", "document", "mime", "inline_data", "unsupported"))


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

    def _complete(self, system: str, user: str, temperature: float, attachments: list[Attachment]) -> str:
        from openai import BadRequestError

        def content():
            if not attachments or self.attachments_dropped:
                return user
            files = [{"type": "file", "file": {"filename": a.filename, "file_data": f"data:{a.mime};base64,{a.b64}"}}
                     for a in attachments]
            return [*files, {"type": "text", "text": user}]

        def kwargs() -> dict[str, Any]:
            kw: dict[str, Any] = dict(
                model=self.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": content()}],
                response_format={"type": "json_object"},
            )
            if self._send_temperature:
                kw["temperature"] = temperature
            return kw

        for _ in range(3):
            try:
                resp = self._client.chat.completions.create(**kwargs())
                return resp.choices[0].message.content or ""
            except BadRequestError as e:
                if self._send_temperature and "temperature" in str(e):  # 部分推理模型不接受 temperature
                    self._send_temperature = False
                elif attachments and not self.attachments_dropped and _rejects_files(e):
                    self.attachments_dropped = True  # 不支援 PDF 附件的模型改用擷取文字
                else:
                    raise
        raise LLMError("OpenAI 請求參數無法調整至可接受的格式")


# 支援伺服器端拒答備援的模型：模型基於安全理由拒答時，API 會在同一次請求內改由其他模型接手
CLAUDE_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
# 非串流請求的上限（再高 SDK 會要求改用串流）；思考內容也計入，故取接近上限的值
CLAUDE_MAX_TOKENS = 20000


class ClaudeClient(LLMClient):
    provider = "claude"

    def __init__(self, model: str | None = None, api_key: str | None = None, **kw):
        super().__init__(model, **kw)
        import anthropic

        key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise LLMError("未設定 ANTHROPIC_API_KEY")
        self._client = anthropic.Anthropic(api_key=key)

    def _complete(self, system: str, user: str, temperature: float, attachments: list[Attachment]) -> str:
        # 新版 API 已不接受 temperature；思考模式維持模型預設
        blocks: list[dict] = []
        if not self.attachments_dropped:
            for a in attachments:
                # 同一輪內查閱與正式回覆會重複送同一份 PDF，加上快取標記減少重複計費
                blocks.append({"type": "document", "title": a.filename, "cache_control": {"type": "ephemeral"},
                               "source": {"type": "base64", "media_type": a.mime, "data": a.b64}})
        blocks.append({"type": "text", "text": user})
        kwargs = dict(model=self.model, max_tokens=CLAUDE_MAX_TOKENS, system=system,
                      messages=[{"role": "user", "content": blocks}])
        if self.model in CLAUDE_FALLBACK_MODELS:
            resp = self._client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        else:
            resp = self._client.messages.create(**kwargs)
        if resp.stop_reason == "refusal":
            category = getattr(resp.stop_details, "category", None) if resp.stop_details else None
            raise LLMError(f"Claude 拒絕回答此請求（類別：{category or '未提供'}）")
        if resp.stop_reason == "max_tokens":
            raise LLMError("Claude 回覆超過長度上限而被截斷")
        return "".join(b.text for b in resp.content if b.type == "text")


class GeminiClient(LLMClient):
    provider = "gemini"

    def __init__(self, model: str | None = None, api_key: str | None = None, **kw):
        super().__init__(model, **kw)
        from google import genai

        key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not key:
            raise LLMError("未設定 GEMINI_API_KEY")
        self._client = genai.Client(api_key=key)

    def _complete(self, system: str, user: str, temperature: float, attachments: list[Attachment]) -> str:
        from google.genai import errors, types

        def contents():
            if not attachments or self.attachments_dropped:
                return user
            return [*(types.Part.from_bytes(data=a.data, mime_type=a.mime) for a in attachments), user]

        config = types.GenerateContentConfig(system_instruction=system, temperature=temperature,
                                             response_mime_type="application/json")
        try:
            resp = self._client.models.generate_content(model=self.model, contents=contents(), config=config)
        except errors.ClientError as e:
            if not attachments or self.attachments_dropped or not _rejects_files(e):
                raise
            self.attachments_dropped = True
            resp = self._client.models.generate_content(model=self.model, contents=user, config=config)
        return resp.text or ""


# ---------------------------------------------------------------------------
# 離線示範
# ---------------------------------------------------------------------------
class MockClient(LLMClient):
    """離線示範：依計算書文字（rc-member-designer 計算書的「不合格項目」「未涵蓋」）產生確定性回覆。

    不保留任何狀態：每次回覆只依當次 payload 產生，與真實模型一樣不共用記憶。
    """

    provider = "mock"

    def __init__(self, model: str | None = None, delay: float = 0.0, **kw):
        super().__init__(model, **kw)
        self.delay = delay  # 每次呼叫的模擬延遲（秒），供介面示範逐回合顯示

    def _complete(self, system, user, temperature, attachments):  # pragma: no cover
        raise NotImplementedError

    def generate(self, system, user, schema, *, temperature=0.3, task="", payload=None, attachments=None):
        if self.delay:
            time.sleep(self.delay)
        builder = getattr(self, f"_mock_{task}", None)
        if builder is None:
            raise LLMError(f"Mock 不支援 task={task!r}")
        return schema.model_validate(builder(payload or {}))

    def _mock_lookup(self, p: dict) -> dict:
        if p.get("stage") != "round1":
            return {"clauses": [], "keywords": [], "reason": "已提供之條文足夠。"}
        return {"clauses": [{"spec": "01", "clause": "18.4.5"}, {"spec": "01", "clause": "18.4.3"}],
                "keywords": ["梁柱接頭 剪力強度"], "reason": "核對耐震柱橫向鋼筋、強柱弱梁與接頭剪力之規定。（離線示範）"}

    def _mock_round1(self, p: dict) -> dict:
        found = _mock_findings(p.get("calc_text", ""))
        issues = []
        for n, f in enumerate(found[:8], 1):
            issues.append({
                "issue_id": f"Q{n}", "title": f["title"], "category": f["category"], "calc_ref": f["calc_ref"],
                "cited_data": f["cited"], "challenge": f["challenge"], "formulas": f["formulas"],
                "code_clauses": f["clauses"], "code_quote": f["quote"], "preliminary_risk": f["risk"],
            })
        facts = [{"item": k, "value": v, "source": "第 1 頁表頭"} for k, v in (p.get("facts") or {}).items()]
        n_high = sum(1 for f in found if f["risk"] == "高風險")
        summary = (f"本計算書共 {len(issues)} 項需要釐清，其中 {n_high} 項經核對規範原文已不符規定，可能影響結構安全，"
                   "需設計單位優先說明與修正。（離線示範模式：內容由規則產生，未經語言模型推理）")
        return {"summary": summary, "project_facts": facts, "issues": issues}

    def _mock_round2(self, p: dict) -> dict:
        out = []
        for it in p["issues"]:
            risk = it["preliminary_risk"]
            base = {"issue_id": it["issue_id"], "data_source": it["calc_ref"], "code_clauses": it["code_clauses"],
                    "code_quote": it["code_quote"]}
            if risk == "高風險":
                out.append({**base, "stance": "承認缺失",
                            "response": "同意審查意見。經依規範原文重新核對，計算書中此項確實未符合規定，將於修正版計算書中改善。",
                            "proposed_action": "依外審意見調整配筋後，以 rc-member-designer 重新計算並檢附檢核彙總表。"})
            elif risk == "中風險":
                out.append({**base, "stance": "規範依據抗辯",
                            "response": "此項為計算書自列之「未涵蓋、須另行檢核」項目，本構件計算書僅涵蓋斷面設計，"
                                        "接頭與周邊構材之檢核另於整體結構計算書辦理，將補附相關檢核頁次。"})
            else:
                out.append({**base, "stance": "計算推導抗辯",
                            "response": "計算書已依規範計算並判定合格，代入計算書數值後需求小於容量。",
                            "calculations": it["formulas"]})
        return {"responses": out}

    def _mock_round3(self, p: dict) -> dict:
        values = p.get("case_values") or {}
        out = []
        for it in p["issues"]:
            risk = it["preliminary_risk"]
            if risk == "高風險":
                sols = _mock_solutions(it, values)
                out.append({"issue_id": it["issue_id"], "decision": "部分接受", "risk_level": "高風險",
                            "rationale": "設計單位承認此項不符規範，修正方向合理；但目前送審的計算書仍未符合規定，"
                                         "須以修正後的計算書證明數值回到規範限值以內，才能解除疑義，因此維持高風險。",
                            "check_formulas": it["formulas"],
                            "required_action": "依建議方案調整後重新計算，提送修正版計算書。",
                            "solutions": sols, "recommended_solution": 1 if sols else 0})
            elif risk == "中風險":
                out.append({"issue_id": it["issue_id"], "decision": "部分接受", "risk_level": "中風險",
                            "rationale": "說明可以成立，但送審資料中沒有附上對應檢核，審查端無法確認；此項不直接影響本構件斷面強度，"
                                         "需補充資料後再確認。",
                            "required_action": "補附對應之檢核計算與頁次。",
                            "solutions": [{"label": "方案 1：補充檢核", "approach": "於整體結構計算書補充本項檢核並標示頁次。",
                                           "changes": [], "verification": it["formulas"],
                                           "change_extent": "不需修改本構件設計，僅補充文件。",
                                           "other_method": "依所引用條文補充計算，並於修正版計算書附註中引用頁次。"}],
                            "recommended_solution": 1})
            else:
                out.append({"issue_id": it["issue_id"], "decision": "接受", "risk_level": "低風險",
                            "rationale": "設計單位的計算推導與規範原文一致，此項疑義解除。"})
        return {"verdicts": out}

    def _mock_chief(self, p: dict) -> dict:
        titles = {i["issue_id"]: i["title"] for i in p["issues"]}
        bucket_of = {"低風險": "no_concern_items", "中風險": "pending_review_items", "高風險": "major_doubts"}
        finding = {
            "高風險": "外審指出此項不符規範，設計單位已承認；外審提出的方案經複核可使該項回到規範限值以內，採用改動最小者。",
            "中風險": "此項不影響本構件斷面強度，但計算書未附檢核，需補充資料後再確認。",
            "低風險": "設計單位之計算與規範原文一致，疑義解除。",
        }
        out: dict[str, list] = {k: [] for k in bucket_of.values()}
        for v in p["verdicts"]:
            adopted = v.get("recommended_solution", 0)
            out[bucket_of[v["risk_level"]]].append({
                "issue_id": v["issue_id"], "title": titles.get(v["issue_id"], v["issue_id"]), "risk_level": v["risk_level"],
                "finding": finding[v["risk_level"]], "basis": "外審終審、設計單位回覆及所引規範原文。",
                "recommendation": v.get("required_action") or "無須辦理。", "adopted_solution": adopted,
                "solution_check": "建議值代入後符合所引條文；請於 rc-member-designer 重算確認。" if adopted else "",
            })
        n_h, n_m = len(out["major_doubts"]), len(out["pending_review_items"])
        conclusion = "退回補正" if n_h else ("修正後同意" if n_m else "同意備查")
        return {"overall_conclusion": conclusion,
                "executive_summary": f"本輪共審查 {len(p['verdicts'])} 項：高風險 {n_h} 項、中風險 {n_m} 項。"
                                     f"結論為「{conclusion}」。（離線示範模式）",
                **out, "debate_observations": "離線示範模式的內容由規則產生，用於展示流程與報表格式，未經語言模型推理。"}

    def _mock_final(self, p: dict) -> dict:
        merged: dict[str, dict] = {}
        rank = {"低風險": 0, "中風險": 1, "高風險": 2}
        for ps in p["passes"]:
            for it in ps["items"]:
                m = merged.setdefault(it["title"], {"title": it["title"], "source_ids": [], "risk_level": "低風險",
                                                    "finding": it["finding"], "code_clauses": it.get("code_clauses", []),
                                                    "final_solution": None, "why_minimal": "",
                                                    "required_action": it["recommendation"]})
                m["source_ids"].append(f"P{ps['pass']}-{it['issue_id']}")
                if rank[it["risk_level"]] > rank[m["risk_level"]]:
                    m["risk_level"] = it["risk_level"]
                if it.get("solution") and m["final_solution"] is None:
                    m["final_solution"] = it["solution"]
                    m["why_minimal"] = "各輪方案相同；此方案只調整一個欄位，斷面尺寸不變，為改動最小者。"
        items = list(merged.values())
        worst = max((rank[i["risk_level"]] for i in items), default=0)
        conclusion = ["同意備查", "修正後同意", "退回補正"][worst]
        return {"overall_conclusion": conclusion,
                "executive_summary": f"綜合 {len(p['passes'])} 輪審查，共 {len(items)} 項疑義，結論為「{conclusion}」。（離線示範模式）",
                "items": items, "cross_pass_notes": "離線示範模式各輪結果相同。",
                "tool_summary": "於 rc-member-designer 開啟調整後案件檔，確認「檢核彙總與總判定」各項均為 OK。"}


# ---------------------------------------------------------------------------
# 離線示範：從 rc-member-designer 計算書文字找出不合格與未涵蓋項目
# ---------------------------------------------------------------------------
def _items(text: str, head: str) -> list[str]:
    i = text.find(head)
    if i < 0:
        return []
    seg = text[i + len(head): i + len(head) + 1500]
    seg = re.split(r"\n(?:總判定依|設計\n|附註)|。\n", seg)[0]
    seg = re.sub(r"\s*\n\s*", "", seg)
    return [x.strip(" ；;。") for x in re.split(r"\(\d+\)", seg) if x.strip(" ；;。")]


def _page_of(text: str, needle: str) -> str:
    i = text.find(needle)
    pages = re.findall(r"<<第 (\d+) 頁>>", text[:i]) if i >= 0 else []
    return f"第 {pages[-1]} 頁" if pages else "計算書"


def _mock_findings(text: str) -> list[dict]:
    out = []
    page = _page_of(text, "檢核彙總與總判定")
    for item in _items(text, "不合格項目："):
        if "間距" in item or "圍束" in item:
            m = re.search(r"需求\s*s\s*=\s*([\d.]+)\s*cm.*?實務間距\s*([\d.]+)", item)
            need, s_min = (m.group(1), m.group(2)) if m else ("—", "—")
            out.append({"title": "柱端圍束筋間距無法滿足", "category": "耐震細則", "calc_ref": f"{page} 檢核彙總",
                        "cited": item[:120], "risk": "高風險",
                        "challenge": f"計算書顯示柱端加密區所需的橫向鋼筋間距為 {need} cm，已小於最小實務間距 {s_min} cm，"
                                     "代表以目前的箍筋號數無法配出規範要求的圍束鋼筋量（圍束筋用來包住混凝土核心，地震時避免柱端壓碎）。"
                                     "請設計單位說明並調整橫向鋼筋。",
                        "formulas": [rf"s_{{req}} = {need}\ \mathrm{{cm}} < s_{{min}} = {s_min}\ \mathrm{{cm}}",
                                     r"\frac{A_{sh}}{s\,b_c} \geq 0.3\left(\frac{A_g}{A_{ch}}-1\right)\frac{f'_c}{f_{yt}}"],
                        "clauses": ["土木401-112 §18.4.5.4", "土木401-112 表18.4.5.4"],
                        "quote": "特殊抗彎矩構架柱之橫向鋼筋用量應符合表18.4.5.4之規定。", "kind": "tie"})
        elif "強柱弱梁" in item:
            m = re.search(r"ΣM\s*=\s*2\s*×\s*([\d.]+)\s*=\s*([\d.]+)\s*[＜<]\s*1\.2ΣM\s*=\s*1\.2\s*×\s*([\d.]+)\s*=\s*([\d.]+)", item)
            mnc, smnc, mnb, need = m.groups() if m else ("—", "—", "—", "—")
            out.append({"title": "強柱弱梁檢核不符", "category": "耐震細則", "calc_ref": f"{page} 檢核彙總",
                        "cited": item[:160], "risk": "高風險",
                        "challenge": f"柱在接頭面的彎矩強度總和為 {smnc} tf·m，小於規範要求的梁彎矩強度總和之 6/5 倍（{need} tf·m）。"
                                     "強柱弱梁的目的是讓地震時梁先降伏消能、避免柱先破壞造成樓層崩塌；"
                                     "不符合時該柱的側向強度與勁度不得計入結構系統。請設計單位提高柱的彎矩強度。",
                        "formulas": [rf"\Sigma M_{{nc}} = 2 \times {mnc} = {smnc}\ \mathrm{{tf\cdot m}}",
                                     rf"\frac{{6}}{{5}}\Sigma M_{{nb}} = 1.2 \times {mnb} = {need}\ \mathrm{{tf\cdot m}}",
                                     rf"\Sigma M_{{nc}} = {smnc} < \frac{{6}}{{5}}\Sigma M_{{nb}} = {need}"],
                        "clauses": ["土木401-112 §18.4.3.2", "土木401-112 §18.4.3.3"],
                        "quote": "柱之彎矩強度應符合 ΣMnc ≥ (6/5)ΣMnb。", "kind": "flex"})
        else:
            out.append({"title": item[:18], "category": "其他", "calc_ref": f"{page} 檢核彙總", "cited": item[:160],
                        "risk": "高風險", "challenge": f"計算書判定不合格：{item}。請設計單位說明並修正。",
                        "formulas": [r"\mathrm{D/C} > 1.0"], "clauses": ["土木401-112 §22.4"], "quote": "（請對照原文）",
                        "kind": "other"})
    for item in _items(text, "本計算書未涵蓋、須另行檢核："):
        ref = re.search(r"§[\d.]+", item)
        out.append({"title": re.sub(r"（.*?）", "", item)[:18] + "未檢核", "category": "其他", "calc_ref": "計算書附註",
                    "cited": item, "risk": "中風險",
                    "challenge": f"計算書自列「{item}」為未涵蓋、須另行檢核之項目，但送審資料中沒有對應的檢核成果。"
                                 "請設計單位補充檢核或說明出處。",
                    "formulas": [r"V_u \leq \phi V_n"], "clauses": [f"土木401-112 {ref.group()}" if ref else "土木401-112 第 18 章"],
                    "quote": "（依所引條文）", "kind": "doc"})
    if not out:
        out.append({"title": "撓曲與軸力強度抽查", "category": "斷面強度", "calc_ref": page, "cited": "計算書檢核彙總",
                    "risk": "低風險", "challenge": "計算書各項檢核均判定合格，抽查控制組合之撓曲＋軸力需求／容量比，確認計算方式與規範一致。",
                    "formulas": [r"\mathrm{D/C} = \frac{P_u}{\phi P_n} \leq 1.0"], "clauses": ["土木401-112 §22.4"],
                    "quote": "（依所引條文）", "kind": "ok"})
    return out


def _bump(options: list[str], cur: str) -> str | None:
    return options[options.index(cur) + 1] if cur in options and options.index(cur) + 1 < len(options) else None


def _mock_solutions(issue: dict, values: dict) -> list[dict]:
    title = issue["title"]
    ties, bars = ["#3", "#4", "#5", "#6", "#7"], ["#4", "#5", "#6", "#7", "#8", "#9", "#10", "#11", "#12", "#14", "#16", "#18"]
    sols = []
    if "圍束" in title or "間距" in title:
        cur = values.get("tieSize", "#3")
        new = _bump(ties, cur) or "#5"
        sols.append({"label": f"方案 1：箍筋／繫筋 {cur} → {new}", "approach": "加大橫向鋼筋號數，提高每組箍筋提供的 A_sh，使所需間距回到實務範圍。",
                     "changes": [{"param_id": "tieSize", "label": "箍筋 / 繫筋號數", "current": cur, "proposed": new}],
                     "verification": [r"A_{sh} \propto A_b:\ \frac{A_{b,\#4}}{A_{b,\#3}} = \frac{1.267}{0.7133} = 1.78",
                                      r"s_{req,new} \approx 1.78 \times s_{req} \geq s_{min}"],
                     "change_extent": "僅改箍筋號數，斷面、主筋不變；橫向鋼筋單肢面積 +78%。",
                     "tool_steps": "柱斷面 → 橫向筋 / 耐震 →「箍筋 / 繫筋號數」改為建議值，重算後確認「橫向筋間距」為 OK。"})
        sols.append({"label": "方案 2：繫筋改為每一根主筋皆設", "approach": "增加繫筋肢數提高 A_sh。",
                     "changes": [{"param_id": "tieLayout", "label": "繫筋配置", "current": values.get("tieLayout", "code"),
                                  "proposed": "all"}],
                     "verification": [r"A_{sh} = n_{legs} A_b"], "change_extent": "增加繫筋肢數，施工較繁。",
                     "tool_steps": "柱斷面 →「繫筋配置」改為每一根主筋皆設。"})
    elif "強柱弱梁" in title or "彎矩" in title:
        cur = values.get("barSize", "#8")
        new = _bump(bars, cur) or cur
        n = values.get("nB", "3")
        sols.append({"label": f"方案 1：主筋 {cur} → {new}（根數不變）", "approach": "加大主筋號數提高柱的標稱彎矩強度 M_nc。",
                     "changes": [{"param_id": "barSize", "label": "主筋號數", "current": cur, "proposed": new}],
                     "verification": [r"\Sigma M_{nc} = 2 M_{nc} \geq \frac{6}{5}\Sigma M_{nb}",
                                      r"\frac{A_{b,\#9}}{A_{b,\#8}} = \frac{6.469}{5.067} = 1.28"],
                     "change_extent": "主筋號數加大一號、根數不變，鋼筋量 +28%，斷面不變。",
                     "tool_steps": "柱斷面 → 主筋 →「主筋號數」改為建議值，重算後確認「強柱弱梁」兩向皆為 OK。"})
        if n.isdigit():
            sols.append({"label": f"方案 2：每面主筋 {n} → {int(n) + 1} 根", "approach": "增加主筋根數。",
                         "changes": [{"param_id": "nB", "label": "B 向每面根數", "current": n, "proposed": str(int(n) + 1)},
                                     {"param_id": "nH", "label": "H 向每面根數", "current": values.get("nH", n),
                                      "proposed": str(int(values.get("nH", n)) + 1)}],
                         "verification": [r"\Sigma M_{nc} \geq \frac{6}{5}\Sigma M_{nb}"],
                         "change_extent": "主筋由 8 根增為 12 根，鋼筋量 +50%。",
                         "tool_steps": "柱斷面 → 主筋 → B、H 向每面根數各加 1。"})
    else:
        sols.append({"label": "方案 1：加大斷面或配筋", "approach": "依控制組合重新設計。", "changes": [],
                     "verification": [r"\mathrm{D/C} \leq 1.0"], "change_extent": "視重算結果而定。",
                     "other_method": "於 rc-member-designer 調整斷面或配筋後重算。"})
    return sols


# ---------------------------------------------------------------------------
# 以使用者的 API Key 查詢可用模型
# ---------------------------------------------------------------------------
PROVIDER_LABEL = {"gemini": "Gemini", "claude": "Claude", "openai": "OpenAI"}
# 不能用來對話／產生文字的模型（嵌入、影像、語音、即時串流、搜尋等）
_NON_TEXT = ("embed", "imagen", "image", "tts", "audio", "realtime", "transcribe", "whisper",
             "dall-e", "moderation", "aqa", "veo", "live", "search", "instruct")
_OPENAI_CHAT_PREFIX = ("gpt-", "o1", "o3", "o4", "chatgpt-")


def _is_text_model(name: str) -> bool:
    return not any(k in name.lower() for k in _NON_TEXT)


def filter_openai_models(ids: list[str]) -> list[str]:
    keep = [i for i in ids if i.startswith(_OPENAI_CHAT_PREFIX) and _is_text_model(i)]
    return sorted(set(keep), reverse=True)


def filter_gemini_models(models: list[tuple[str, list[str]]]) -> list[str]:
    """models：(名稱, supported_actions)。只保留支援 generateContent 的文字模型。"""
    keep = [name.removeprefix("models/") for name, actions in models
            if "generateContent" in (actions or []) and _is_text_model(name)]
    return sorted(set(keep), reverse=True)


def list_claude_models(api_key: str) -> list[str]:
    """Claude 的模型都能產生文字；只列仍在服務中的模型，新的排前面。"""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    try:
        models = list(client.models.list(lifecycle=["active"]))
    except anthropic.AuthenticationError as e:
        raise LLMError("Claude API Key 無效，請確認是否複製完整、是否已啟用。") from e
    except anthropic.PermissionDeniedError as e:
        raise LLMError("Claude API Key 沒有讀取模型清單的權限，請在 Claude Console 確認此 Key 的權限。") from e
    except anthropic.APIConnectionError as e:
        raise LLMError("無法連線到 Claude API，請稍後再試。") from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"Claude 查詢模型失敗（HTTP {e.status_code}）：{e.message[:200]}") from e
    models.sort(key=lambda m: m.created_at, reverse=True)
    return [m.id for m in models]


def list_models(provider: str, api_key: str) -> list[str]:
    """用使用者的 API Key 向服務商查詢可用的文字模型；Key 無效或連線失敗時拋出 LLMError。"""
    if provider == "claude":
        return list_claude_models(api_key)
    try:
        if provider == "openai":
            from openai import OpenAI

            client = OpenAI(api_key=api_key, base_url=os.getenv("OPENAI_BASE_URL") or None)
            return filter_openai_models([m.id for m in client.models.list()])
        if provider == "gemini":
            from google import genai

            client = genai.Client(api_key=api_key)
            return filter_gemini_models([(m.name or "", list(m.supported_actions or []))
                                         for m in client.models.list()])
    except Exception as e:
        label = PROVIDER_LABEL.get(provider, provider)
        text = str(e)
        if any(k in text for k in ("API key not valid", "API_KEY_INVALID", "Incorrect API key", "401")):
            raise LLMError(f"{label} API Key 無效，請確認是否複製完整、是否已啟用。") from e
        if "403" in text or "insufficient permissions" in text.lower():
            raise LLMError(f"{label} API Key 沒有讀取模型清單的權限；若是限制權限的 Key，請在後台開啟 Models 的讀取權限。") from e
        raise LLMError(f"{label} 查詢模型失敗：{text[:200]}") from e
    raise LLMError(f"未知的 provider：{provider}")


def create_client(provider: str, model: str | None = None, api_key: str | None = None) -> LLMClient:
    provider = provider.lower()
    if provider == "openai":
        return OpenAIClient(model, api_key=api_key)
    if provider == "gemini":
        return GeminiClient(model, api_key=api_key)
    if provider == "claude":
        return ClaudeClient(model, api_key=api_key)
    if provider == "mock":
        return MockClient(model)
    raise LLMError(f"未知的 provider：{provider}")


__all__ = ["Attachment", "LLMClient", "LLMError", "create_client", "default_model", "list_models", "schema_text",
           "extract_json"]
