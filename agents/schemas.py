"""各回合結構化輸出的資料模型（Pydantic）。

LLM 的回覆一律要求為 JSON，並以這些模型驗證；驗證失敗會由 llm_client 帶著錯誤訊息重試。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class RiskLevel(str, Enum):
    PASS = "Pass"
    MEDIUM = "Medium"
    HIGH = "High"


class Stance(str, Enum):
    ADMIT = "承認缺失"
    CODE_BASIS = "規範依據抗辯"
    CALC_DEFENSE = "計算推導抗辯"


class Decision(str, Enum):
    ACCEPT = "接受"
    PARTIAL = "部分接受"
    REJECT = "駁回"


class OverallConclusion(str, Enum):
    APPROVED = "同意備查"
    CONDITIONAL = "修正後同意"
    RETURNED = "退回補正"


# ---------- Round 1：Agent B 初審質疑 ----------
class ReviewIssue(BaseModel):
    issue_id: str = Field(description="疑義編號，格式 Q1、Q2…")
    title: str = Field(description="疑義標題（20 字內，讓一般讀者看得出問題是什麼，例如「2F 層間位移角超過規範上限」）")
    category: str = Field(description="分類：邊界條件／載重組合／載重路徑／配筋比／位移角／強度檢核／安全係數／其他")
    cited_data: str = Field(description="引用之輸入數據（工作表、樓層、構件編號與數值）")
    challenge: str = Field(description="質疑內容：以完整句子說明檢查了什麼、數值與限值差多少、為什麼重要、要求設計單位說明或修正什麼")
    formula: str = Field(description="具體檢核算式，例如 ρ = As/(b·d) = 4520/(400×640) = 1.77%")
    code_reference: str = Field(description="質疑依據之規範條文")


class Round1Output(BaseModel):
    summary: str = Field(description="初審總體意見（150 字內，白話說明本案整體狀況與主要問題）")
    issues: list[ReviewIssue] = Field(min_length=3, max_length=5)


# ---------- Round 2：Agent A 回覆抗辯 ----------
class DesignerResponse(BaseModel):
    issue_id: str
    stance: Stance
    response: str = Field(description="回覆說明：以完整句子說明是否同意、理由與依據，讓非結構專業讀者也看得懂")
    calculation: str = Field(default="", description="計算推導（若為計算抗辯則必填）")
    data_source: str = Field(description="數據出處（工作表／欄位／樓層）")
    code_reference: str = Field(description="規範依據條文")
    proposed_action: str = Field(default="", description="承認缺失時之修正方案（具體做法與修正後要提出的檢核成果）")


class Round2Output(BaseModel):
    responses: list[DesignerResponse]


# ---------- Round 3：Agent B 覆核終審 ----------
class FinalVerdict(BaseModel):
    issue_id: str
    decision: Decision
    risk_level: RiskLevel
    rationale: str = Field(description="判定理由：說明為何接受／部分接受／駁回，以及此風險等級對結構安全的意義")
    required_action: str = Field(default="", description="要求設計單位辦理事項，無則留空")


class Round3Output(BaseModel):
    verdicts: list[FinalVerdict]


# ---------- 彙整：Agent C 主任檢核技師 ----------
class ReportItem(BaseModel):
    issue_id: str
    title: str
    risk_level: RiskLevel
    finding: str = Field(description="檢核結論：白話說明問題、雙方爭點與最後判斷")
    basis: str = Field(description="判定依據（規範條文／計算）")
    recommendation: str = Field(description="處理意見：設計單位接下來要做什麼")


class ChiefReport(BaseModel):
    overall_conclusion: OverallConclusion
    executive_summary: str = Field(description="總結意見（250 字內）：先講結論，再說明重大疑義與待複核項目為何重要，以及設計單位下一步")
    no_concern_items: list[ReportItem] = Field(default_factory=list, description="無虞項目")
    major_doubts: list[ReportItem] = Field(default_factory=list, description="重大疑義")
    pending_review_items: list[ReportItem] = Field(default_factory=list, description="待複核項目")
    debate_observations: str = Field(default="", description="對辯論過程之客觀觀察（例如論證品質、資料不足處）")
