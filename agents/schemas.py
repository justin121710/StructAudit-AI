"""各回合結構化輸出的資料模型（Pydantic）。

LLM 的回覆一律要求為 JSON，並以這些模型驗證；驗證失敗會由 llm_client 帶著錯誤訊息重試。
算式欄位（formulas、calculations、verification…）一律為 LaTeX 字串清單，與 rc-member-designer 計算書的 KaTeX 寫法相同。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class RiskLevel(str, Enum):
    LOW = "低風險"
    MEDIUM = "中風險"
    HIGH = "高風險"


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


LATEX_HINT = "LaTeX 算式清單（KaTeX 語法、不加 $，一條一行，代入數值與單位，中文說明不要寫進算式）"
CLAUSE_HINT = "依據條文（引用簡稱＋條號，例如「土木401-112 §18.4.3.2」「土木401-112 表18.4.5.4」）；只能引用已查閱原文者"


# ---------- 查閱規範原文（每一回合開始前） ----------
class ClauseRequest(BaseModel):
    spec: str = Field(description="規範代碼，見規範清單，例如 01（土木401-112）、02（建築物耐震設計規範）")
    clause: str = Field(description="條號或表號，例如 18.4.3.2、18.4.5、表18.4.5.4")


class LookupRequest(BaseModel):
    clauses: list[ClauseRequest] = Field(default_factory=list, max_length=20, description="要查閱的條文（最多 20 條）")
    keywords: list[str] = Field(default_factory=list, max_length=6,
                                description="不確定條號時的關鍵字（最多 6 個，例如「強柱弱梁」「圍束 橫向鋼筋」）")
    reason: str = Field(default="", description="為何需要這些條文（一句話）")


# ---------- Round 1：Agent B 初審質疑 ----------
class ProjectFact(BaseModel):
    item: str = Field(description="項目，例如 工程名稱、構件、斷面、材料強度、設計依據")
    value: str = Field(description="計算書中記載的內容（照錄）")
    source: str = Field(default="", description="出處頁次")


class ReviewIssue(BaseModel):
    issue_id: str = Field(description="疑義編號，格式 Q1、Q2…")
    title: str = Field(description="疑義標題（20 字內，讓一般讀者看得出問題是什麼，例如「柱端圍束筋間距無法滿足」）")
    category: str = Field(description="分類：材料／斷面強度／剪力扭矩／耐震細則／配筋細則／伸展搭接／載重組合／長細效應／其他")
    calc_ref: str = Field(description="計算書出處（頁次、章節或表格列，例如「第 15 頁 檢核彙總」）")
    cited_data: str = Field(description="引用之計算書數據（照錄數值與單位）")
    challenge: str = Field(description="質疑內容：以完整句子說明檢查了什麼、數值與規範要求差多少、為什麼重要、要求設計單位說明或修正什麼")
    formulas: list[str] = Field(min_length=1, description="檢核算式，" + LATEX_HINT)
    code_clauses: list[str] = Field(min_length=1, description=CLAUSE_HINT)
    code_quote: str = Field(description="條文要點（摘錄已查閱之原文，數值、係數不得改寫）")
    preliminary_risk: RiskLevel = Field(description="初判風險等級：低風險／中風險／高風險")


class Round1Output(BaseModel):
    summary: str = Field(description="初審總體意見（150 字內，白話說明本計算書整體狀況與主要問題）")
    project_facts: list[ProjectFact] = Field(default_factory=list,
                                             description="計算書中可辨識的專案資料（工程名稱、構件、斷面、材料、設計依據等）")
    issues: list[ReviewIssue] = Field(min_length=1, max_length=8)


# ---------- Round 2：Agent A 回覆抗辯 ----------
class DesignerResponse(BaseModel):
    issue_id: str
    stance: Stance
    response: str = Field(description="回覆說明：以完整句子說明是否同意、理由與依據，讓非結構專業讀者也看得懂")
    calculations: list[str] = Field(default_factory=list, description="計算推導（計算推導抗辯時必填），" + LATEX_HINT)
    data_source: str = Field(description="數據出處（計算書頁次／章節）")
    code_clauses: list[str] = Field(default_factory=list, description=CLAUSE_HINT)
    code_quote: str = Field(default="", description="引用條文要點（摘錄原文）")
    proposed_action: str = Field(default="", description="承認缺失時之修正方案（具體做法與修正後要提出的檢核成果）")


class Round2Output(BaseModel):
    responses: list[DesignerResponse]


# ---------- Round 3：Agent B 覆核終審＋具體解決方案 ----------
class ParamChange(BaseModel):
    param_id: str = Field(default="", description="rc-member-designer 輸入欄位 id（見欄位清單，例如 tieSize、nB）；非工具欄位留空")
    label: str = Field(description="參數名稱，例如「箍筋 / 繫筋號數」")
    current: str = Field(description="計算書目前值")
    proposed: str = Field(description="建議值（下拉欄位須為清單中的可選值，例如 #4；數值欄位只寫數字）")
    unit: str = Field(default="", description="單位")


class Solution(BaseModel):
    label: str = Field(description="方案名稱，例如「方案 1：箍筋改 #4」")
    approach: str = Field(description="做法說明：改什麼、為何可解決此疑義")
    changes: list[ParamChange] = Field(default_factory=list, description="參數變更（可直接在 rc-member-designer 調整者）")
    verification: list[str] = Field(min_length=1, description="以建議值重新檢核之算式，證明修正後符合規範，" + LATEX_HINT)
    change_extent: str = Field(description="改動幅度（例如「箍筋 #3→#4，單肢面積 +78%，斷面與主筋不變」）")
    tool_steps: str = Field(default="", description="在 rc-member-designer 的操作步驟（哪個分頁、哪個欄位、改成多少、重算後看哪一項）")
    other_method: str = Field(default="", description="無法以 rc-member-designer 調整時的其他作法（例如修改梁斷面、補充分析）")


class FinalVerdict(BaseModel):
    issue_id: str
    decision: Decision
    risk_level: RiskLevel
    rationale: str = Field(description="判定理由：說明為何接受／部分接受／駁回，以及此風險等級對結構安全的意義")
    check_formulas: list[str] = Field(default_factory=list, description="覆核算式，" + LATEX_HINT)
    required_action: str = Field(default="", description="要求設計單位辦理事項，無則留空")
    solutions: list[Solution] = Field(default_factory=list, max_length=3,
                                      description="中風險、高風險必須提出 1～3 個具體解決方案；低風險可留空")
    recommended_solution: int = Field(default=0, description="建議採用之方案序號（1 起算，取改動最小且符合規範者）；無方案填 0")


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
    adopted_solution: int = Field(default=0, description="採用外審方案之序號（1 起算；複核後均不成立或無須修正填 0）")
    solution_check: str = Field(default="", description="對採用方案之複核：數值是否正確、是否確實符合規範、是否為改動最小者")
    check_formulas: list[str] = Field(default_factory=list, description="複核算式，" + LATEX_HINT)


class ChiefReport(BaseModel):
    overall_conclusion: OverallConclusion
    executive_summary: str = Field(description="總結意見（250 字內）：先講結論，再說明重大疑義與待複核項目為何重要，以及設計單位下一步")
    no_concern_items: list[ReportItem] = Field(default_factory=list, description="無虞項目（低風險）")
    major_doubts: list[ReportItem] = Field(default_factory=list, description="重大疑義（高風險）")
    pending_review_items: list[ReportItem] = Field(default_factory=list, description="待複核項目（中風險）")
    debate_observations: str = Field(default="", description="對辯論過程之客觀觀察（例如論證品質、資料不足處）")


# ---------- 綜合裁決：彙整各輪（角色輪替）結果 ----------
class ConsolidatedItem(BaseModel):
    title: str = Field(description="疑義標題")
    source_ids: list[str] = Field(min_length=1, description="來源疑義，格式「P輪次-疑義編號」，例如 P1-Q2、P2-Q1")
    risk_level: RiskLevel = Field(description="風險等級：取各輪中最嚴重者（保守原則）")
    finding: str = Field(description="綜合結論：各輪判斷是否一致、最後認定")
    code_clauses: list[str] = Field(default_factory=list, description=CLAUSE_HINT)
    final_solution: Solution | None = Field(default=None, description="最終建議方案：各輪方案中改動最小且符合規範者（可合併調整）")
    why_minimal: str = Field(default="", description="為何此方案為改動最小且符合規範（與其他方案比較）")
    required_action: str = Field(default="", description="設計單位應辦事項")


class ConsolidatedReport(BaseModel):
    overall_conclusion: OverallConclusion
    executive_summary: str = Field(description="綜合結論（250 字內）")
    items: list[ConsolidatedItem] = Field(default_factory=list)
    cross_pass_notes: str = Field(default="", description="各輪（不同模型擔任不同角色）判斷之一致性與差異")
    tool_summary: str = Field(default="", description="rc-member-designer 調整總結：合併所有方案後要改哪些欄位、改完要確認哪些檢核")
