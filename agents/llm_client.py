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
        self._source: dict[str, dict] = {}  # 疑義編號 -> 初篩結果或一般疑義
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
            self._source[qid] = f
            text = _issue_text(f)
            issues.append({
                "issue_id": qid, "title": text["title"], "category": text["category"],
                "cited_data": f"工作表「{f['sheet']}」{f.get('subject') or ''}：{f['detail']}",
                "challenge": text["challenge"], "formula": text["formula"], "code_reference": f["reference"],
            })
        for g in GENERIC_ISSUES:
            if len(issues) >= 3:
                break
            qid = f"Q{len(issues) + 1}"
            self._severity[qid] = "Info"
            self._source[qid] = g
            issues.append({"issue_id": qid, "title": g["title"], "category": g["category"],
                           "cited_data": "資料未提供（上傳的摘要表沒有此項資料）", "challenge": g["challenge"],
                           "formula": "—", "code_reference": g["reference"]})
        n_high = sum(1 for v in self._severity.values() if v == "High")
        summary = (f"本案上傳的結構摘要中，規則式初篩找到 {len(p.get('findings', []))} 項超出門檻或缺漏的數值。"
                   f"我從中挑出 {len(issues)} 項最需要釐清的疑義，其中 {n_high} 項已超過規範限值，可能影響結構安全，"
                   "需設計單位優先說明。（離線示範模式：內容由規則產生，未經語言模型推理）")
        return {"summary": summary, "issues": issues}

    def _mock_round2(self, p: dict) -> dict:
        out = []
        for it in p["issues"]:
            qid = it["issue_id"]
            reply = _designer_text(self._severity.get(qid, "Info"), self._source.get(qid, {}))
            out.append({"issue_id": qid, "data_source": it["cited_data"], "code_reference": it["code_reference"],
                        **reply})
        return {"responses": out}

    def _mock_round3(self, p: dict) -> dict:
        table = {
            "High": ("部分接受", "High",
                     "設計單位承認此項不符規範，提出的修正方向合理。不過目前送審的計算書仍未符合規範，"
                     "必須等修正後的計算書與檢核成果送達、確認數值已回到限值以內，才能解除疑義，因此風險等級維持 High。",
                     "提送修正版計算書，並附上修正後的檢核表，證明數值已符合規範限值。"),
            "Medium": ("部分接受", "Medium",
                       "設計單位的說明在規範上可以成立，但送審資料中沒有附上完整的推導過程，審查端無法直接複核。"
                       "此項不直接影響安全，但需補充資料後才能確認。",
                       "補充完整的計算推導或比較表，供審查複核。"),
            "Info": ("接受", "Pass",
                     "設計單位已說明作法依規範辦理，並指出計算書中對應的檢核位置，說明合理，此項疑義解除。", ""),
        }
        out = []
        for r in p["responses"]:
            decision, risk, why, todo = table[self._severity.get(r["issue_id"], "Info")]
            out.append({"issue_id": r["issue_id"], "decision": decision, "risk_level": risk,
                        "rationale": why, "required_action": todo})
        return {"verdicts": out}

    def _mock_chief(self, p: dict) -> dict:
        titles = {i["issue_id"]: i["title"] for i in p["issues"]}
        finding = {
            "High": "外審指出此項超過規範限值，設計單位已承認並提出修正方向，但修正成果尚未提出。"
                    "在數值回到規範限值以內之前，屬於影響結構安全的重大疑義。",
            "Medium": "此項數值仍在規範容許範圍內，但安全餘裕小或資料不完整。設計單位的說明可以成立，"
                      "需補充推導資料後再確認。",
            "Pass": "設計單位已說明依規範辦理並指出計算位置，外審接受，疑義解除。",
        }
        buckets = {"Pass": [], "Medium": [], "High": []}
        for v in p["verdicts"]:
            buckets[v["risk_level"]].append({
                "issue_id": v["issue_id"], "title": titles.get(v["issue_id"], v["issue_id"]),
                "risk_level": v["risk_level"], "finding": finding[v["risk_level"]],
                "basis": "外審終審判定、設計單位回覆及規則式初篩結果。",
                "recommendation": v["required_action"] or "無須辦理。",
            })
        n_h, n_m, n_p = len(buckets["High"]), len(buckets["Medium"]), len(buckets["Pass"])
        conclusion = "退回補正" if n_h else ("修正後同意" if n_m else "同意備查")
        next_step = {
            "退回補正": "由於仍有重大疑義，本案應退回設計單位，修正計算書後重新送審。",
            "修正後同意": "本案沒有重大疑義，待複核項目補充資料並經確認後即可同意。",
            "同意備查": "各項疑義均已解除，同意備查。",
        }[conclusion]
        summary = (f"本次審查共討論 {len(p['verdicts'])} 項疑義：重大疑義 {n_h} 項（目前不符規範或影響結構安全，必須修正）、"
                   f"待複核 {n_m} 項（需補充資料後確認）、無虞 {n_p} 項。{next_step}（離線示範模式）")
        return {
            "overall_conclusion": conclusion, "executive_summary": summary,
            "no_concern_items": buckets["Pass"], "major_doubts": buckets["High"],
            "pending_review_items": buckets["Medium"],
            "debate_observations": "離線示範模式的內容由規則產生，用於展示流程與報表格式，未經語言模型推理。",
        }


# ---------------------------------------------------------------------------
# 離線示範文字：依初篩類別產生完整、白話的說明
# ---------------------------------------------------------------------------
GENERIC_ISSUES = [
    {"title": "基礎支承假設未說明", "category": "邊界條件", "reference": "建築物基礎構造設計規範（條號待查）",
     "challenge": "分析模型若假設柱底完全固定（固接），但實際基礎與土壤會有些微轉動，柱底彎矩與上部位移都可能被低估。"
                  "上傳的資料沒有說明基礎支承的假設，請設計單位說明採用的邊界條件，以及是否考慮土壤彈簧或基礎轉動勁度。",
     "reply": "本案為筏式基礎，基礎版厚且勁度大，依規範慣例以固接模擬柱底；筏基另以土壤彈簧模型單獨分析，"
              "柱底反力與彎矩已傳入基礎檢核，相關計算詳計算書基礎章節。"},
    {"title": "意外扭矩未說明", "category": "載重組合", "reference": "建築物耐震設計規範及解說 §2.14",
     "challenge": "規範要求地震力需考慮質心偏移 5% 所造成的「意外扭矩」，用來涵蓋質量分布的不確定性。"
                  "若漏掉，建築外側的柱與梁受力可能被低估。上傳資料未說明是否已納入，請設計單位說明。",
     "reply": "分析模型已依規範將各層質心偏移 5% 建立意外扭矩的地震載重工況，並納入所有地震組合，相關設定詳計算書載重章節。"},
    {"title": "強柱弱梁檢核未提供", "category": "強度檢核", "reference": "土木401-112 §18.4.3.2",
     "challenge": "耐震設計要求柱的彎矩強度至少為梁的 1.2 倍（強柱弱梁），讓地震時由梁先降伏消能，避免柱破壞導致樓層崩塌。"
                  "上傳資料沒有這項檢核結果，請設計單位補充各梁柱接頭的檢核。",
     "reply": "各梁柱接頭已依規範檢核柱彎矩強度總和不小於梁的 1.2 倍，檢核結果列於計算書耐震細則章節。"},
]

_COMBO_WHY = {
    "1.4D": "這是只有靜載重時的基本組合，用來確認構件在自重下的強度。",
    "1.2D+1.6L": "這是一般使用狀態下控制最多構件的組合，缺少時梁、版的設計可能不保守。",
    "地震組合 (E)": "沒有地震組合就無法確認建築在地震時的安全性，這是耐震設計的核心。",
    "風力組合 (W)": "建築物需檢核風力作用；即使判斷地震力較大，也應提出比較說明風力不控制。",
    "0.9D±E 抗傾覆": "此組合把靜載重打折後加上地震力，用來檢核柱受拉、基礎抗拉拔與抗傾覆，缺少時可能低估受拉情形。",
}


def _issue_text(f: dict) -> dict:
    """依初篩類別寫出疑義標題、白話說明與算式。"""
    check, where, v, lim = f.get("check"), f.get("subject") or f["sheet"], f.get("value"), f.get("limit")
    if check == "drift" and v is not None:
        return {
            "title": f"{where} 層間位移角超過上限", "category": "位移角",
            "formula": f"θ = Δ／h = {v:.4f} > {lim}",
            "challenge": f"{where} 的層間位移角為 {v:.4f}（約 1/{1 / v:.0f}），超過規範上限 {lim}，超出約 {v / lim - 1:.0%}。"
                         "層間位移角是地震時上下樓層的水平位移差除以樓高，數值過大代表這一層的勁度不足，"
                         "地震時隔間牆、帷幕與管線容易損壞，也可能是軟層的徵兆。"
                         "請設計單位說明計算條件（是否已含扭轉效應、採用哪一組地震力），並提出改善方式。",
        }
    if check == "beam_rho" and v is not None:
        return {
            "title": f"{where} 梁鋼筋比超過上限", "category": "配筋比",
            "formula": f"ρ = {v:.2%} > {lim:.1%}",
            "challenge": f"{where} 梁的拉力鋼筋比 ρ（鋼筋面積占斷面的比例）為 {v:.2%}，超過耐震梁的上限 {lim:.1%}。"
                         "鋼筋放得太多，地震時梁無法靠鋼筋降伏來吸收能量（韌性不足），而且鋼筋過密也不容易澆置混凝土。"
                         "請設計單位說明是否可加大梁斷面或調整配筋。",
        }
    if check == "col_rho" and v is not None:
        low = v < lim
        return {
            "title": f"{where} 柱主筋比{'不足' if low else '過高'}", "category": "配筋比",
            "formula": f"ρg = {v:.2%} {'<' if low else '>'} {lim:.0%}",
            "challenge": f"{where} 柱的主筋比 ρg（主筋總面積占柱斷面的比例）為 {v:.2%}，"
                         f"{'低於規範下限' if low else '高於規範上限'} {lim:.0%}。"
                         + ("主筋太少時，柱的抗彎強度與韌性不足，地震時可能先於梁破壞。" if low
                            else "主筋太多時鋼筋擁擠，混凝土不易填實，也不利於耐震行為。")
                         + "請設計單位說明並調整配筋。",
        }
    if check == "dcr" and v is not None:
        if v > lim:
            return {
                "title": f"{where} 強度不足（D/C > 1）", "category": "強度檢核",
                "formula": f"D/C = {v:.2f} > {lim:.2f}",
                "challenge": f"{where} 的 D/C（需求／容量比，即構件承受的設計載重除以設計強度）為 {v:.2f}，大於 {lim:.2f}，"
                             f"表示設計載重已超過構件強度約 {v - 1:.0%}。這是直接影響結構安全的項目，"
                             "請設計單位說明控制的載重組合，並提出加大斷面或增加配筋的方案。",
            }
        return {
            "title": f"{where} 強度餘裕偏小", "category": "強度檢核",
            "formula": f"D/C = {v:.2f}（上限 {lim:.2f}）",
            "challenge": f"{where} 的 D/C（需求／容量比）為 {v:.2f}，雖然沒有超過 {lim:.2f}，但剩下的安全餘裕只有約 {lim - v:.0%}。"
                         "若施工誤差或實際載重稍微增加，就可能變成強度不足。請設計單位確認載重與分析假設是否足夠保守。",
        }
    if check == "combo":
        label = f.get("subject") or "部分組合"
        return {
            "title": f"缺少「{label}」載重組合", "category": "載重組合", "formula": f"載重組合表中未見 {label}",
            "challenge": f"上傳的載重組合表中沒有看到「{label}」。{_COMBO_WHY.get(label, '')}"
                         "請設計單位說明是否遺漏，或提出此組合不控制設計的理由。",
        }
    return {"title": f["item"][:20], "category": "其他", "formula": f["detail"],
            "challenge": f"{f['item']}：{f['detail']}。請設計單位說明是否符合規範要求。"}


def _designer_text(severity: str, f: dict) -> dict:
    """原設計工程師的示範回覆：超限者承認缺失，偏小或缺漏者以計算說明，一般疑義以規範依據回覆。"""
    if "reply" in f:
        return {"stance": "規範依據抗辯", "response": f["reply"]}
    check, where = f.get("check"), f.get("subject") or f.get("sheet", "")
    if severity == "High":
        fix = {
            "drift": "加大該層柱斷面或增設剪力牆以提高樓層勁度，重新分析後檢附各層層間位移角檢核表。",
            "beam_rho": "加大梁深或梁寬以降低鋼筋比，必要時改為雙筋配置，重新檢核後檢附配筋表。",
            "col_rho": "調整柱主筋數量使主筋比回到規範範圍內，並重新檢核柱的軸力與彎矩強度。",
            "dcr": "加大斷面或增加配筋，重新檢核並檢附 D/C 不大於 1.0 的結果。",
        }.get(check, "依審查意見修正並重新檢核。")
        return {"stance": "承認缺失",
                "response": f"同意審查意見。經查 {where} 的數值確實超出規範限值，原設計未注意到此項，將於修正版計算書中改善。",
                "proposed_action": fix}
    if check == "combo":
        return {"stance": "計算推導抗辯",
                "response": f"「{where}」雖未列在送審的組合表中，但經比較，本案由其他組合控制設計，此組合不會改變構件尺寸與配筋。"
                            "將於修正版計算書補列此組合及比較結果，供審查確認。",
                "calculation": "以控制構件比較：該組合下之需求均小於已檢核之控制組合（詳修正版比較表）"}
    return {"stance": "計算推導抗辯",
            "response": f"{where} 的 D/C 雖接近 1.0，但仍小於 1.0，依規範「設計強度不小於需求強度」即屬合格。"
                        "載重與材料強度均依規範取值，分析假設並未放寬，因此設計足夠。",
            "calculation": f"φSn ≥ U：D/C = {f.get('value', 0):.2f} ≤ {f.get('limit', 1):.2f}"}


# ---------------------------------------------------------------------------
# 以使用者的 API Key 查詢可用模型
# ---------------------------------------------------------------------------
PROVIDER_LABEL = {"gemini": "Gemini", "openai": "OpenAI"}
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


def list_models(provider: str, api_key: str) -> list[str]:
    """用使用者的 API Key 向服務商查詢可用的文字模型；Key 無效或連線失敗時拋出 LLMError。"""
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
        raise LLMError(f"{label} 查詢模型失敗：{text[:200]}") from e
    raise LLMError(f"未知的 provider：{provider}")


def create_client(provider: str, model: str | None = None, api_key: str | None = None) -> LLMClient:
    provider = provider.lower()
    if provider == "openai":
        return OpenAIClient(model, api_key=api_key)
    if provider == "gemini":
        return GeminiClient(model, api_key=api_key)
    if provider == "mock":
        return MockClient(model)
    raise LLMError(f"未知的 provider：{provider}")


__all__ = ["LLMClient", "LLMError", "create_client", "list_models", "schema_text", "extract_json"]
