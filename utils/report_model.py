"""報表資料模型：PDF（HTML 範本／ReportLab）與 Excel 共用同一份內容，確保三種輸出一致。

版面依 rc-member-designer 計算書慣例：
表頭（標題＋兩欄資料表＋程式版本／雜湊）→ 編號章節 → 檢核彙總與總判定 → 附註 → 簽核欄（設計／校核／審核）。
判定用語亦一致：OK／注意／NG。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from agents import schemas as S
from orchestrator import ReviewSession
from version import __version__

REPORT_TITLE = "結構計算書審查報告"
CODE_BASIS = "建築物混凝土結構設計規範（土木401-112）＋ 建築物耐震設計規範及解說（113 年版）＋ 建築技術規則建築構造編"
REVIEW_METHOD = "AI 雙重詰問審查：外審質疑 → 設計回覆 → 外審終審（三回合）＋ 主任檢核技師彙整裁決"
SIGN_ROLES = ("設計", "校核", "審核")

JUDGE = {S.RiskLevel.PASS: "OK", S.RiskLevel.MEDIUM: "注意", S.RiskLevel.HIGH: "NG"}
PRECHECK_JUDGE = {"High": "NG", "Medium": "注意", "Info": "參考"}
LEVEL = {"OK": "ok", "注意": "warn", "NG": "bad", "參考": "info"}
BUCKET_LABEL = {
    "major_doubts": "重大疑義",
    "pending_review_items": "待複核項目",
    "no_concern_items": "無虞項目",
}

LIMIT_ROWS = [
    # key, 項目, 採用（格式）, 出處
    ("drift_max", "層間相對側向位移角", "θ ≤ {v:.4f}", "建築物耐震設計規範及解說 §2.16.1"),
    ("beam_rho_max", "梁拉力鋼筋比", "ρ ≤ {v:.1%}", "土木401-112 §18.3.3.1"),
    ("col_rho_min", "柱縱向鋼筋比下限", "ρ_g ≥ {v:.1%}", "土木401-112 §18.4.4.1"),
    ("col_rho_max", "柱縱向鋼筋比上限", "ρ_g ≤ {v:.1%}", "土木401-112 §18.4.4.1（一般柱 §10.6.1.1 為 8%）"),
    ("dcr_max", "強度需求／容量比", "D/C ≤ {v:.2f}（超過判 NG）", "土木401-112 §9.5.1.1、§10.5.1.1"),
    ("dcr_warn", "強度餘裕警示", "D/C > {v:.2f} 判注意", "工程判斷"),
]
COMBO_ROW = ("載重組合完整性", "檢查 1.4D、1.2D+1.6L、含 E、含 W、0.9D 是否存在", "土木401-112 §5.3.1（表 5.3.1）")

UNCOVERED = [
    "結構模型本身（邊界條件、勁度修正、剛性樓板）僅能就上傳之摘要表推論，未讀取分析模型檔",
    "圖說與計算書之一致性（配筋、斷面、材料標示）",
    "基礎、擋土與地下室外牆設計",
    "施工性、鋼筋排列與續接細節",
]


@dataclass
class SummaryRow:
    issue_id: str
    title: str
    category: str
    decision: str  # B 終審：接受／部分接受／駁回
    risk: str  # Pass／Medium／High
    judge: str  # OK／注意／NG
    bucket: str  # 主任技師分類
    ref: str


@dataclass
class ReportModel:
    title: str
    report_no: str
    project_name: str
    meta: list[tuple[str, str, bool]]  # (項目, 內容, 是否跨兩欄)
    source_rows: list[tuple[str, str]]
    limit_rows: list[tuple[str, str, str]]
    prechecks: list[dict]
    threads: list[dict]
    round1_summary: str
    buckets: list[tuple[str, str, list[S.ReportItem]]]  # (key, 標題, 項目)
    executive_summary: str
    observations: str
    summary_rows: list[SummaryRow]
    verdict_text: str
    verdict_level: str  # ok / warn / bad
    verdict_items: list[str]
    notes: list[str]
    criteria: dict[str, str] = field(default_factory=dict)  # 初篩類別 -> 判定標準文字
    uncovered: list[str] = field(default_factory=lambda: list(UNCOVERED))
    sign_roles: tuple[str, ...] = SIGN_ROLES


def report_number(session: ReviewSession) -> str:
    stamp = datetime.strptime(session.started_at, "%Y-%m-%d %H:%M").strftime("%Y%m%d-%H%M")
    return f"SR-{stamp}"


def trace_text(session: ReviewSession) -> str:
    sha = session.source.get("sha256", "")
    return f"v{__version__}｜輸入 SHA-256 {sha[:12] or '—'}"


def model_text(session: ReviewSession) -> str:
    """三個 Agent 用同一個模型時只寫一次，否則逐一列出。"""
    models = session.agent_models or {"B": f"{session.provider} / {session.model}"}
    if len(set(models.values())) == 1:
        return next(iter(models.values()))
    roles = {"A": "A 原設計工程師", "B": "B 外部審查專家", "C": "C 主任檢核技師"}
    return "；".join(f"{roles[k]}：{models[k]}" for k in ("A", "B", "C") if k in models)


def verdict(chief: S.ChiefReport) -> tuple[str, str, list[str]]:
    c = chief.overall_conclusion
    if c == S.OverallConclusion.RETURNED:
        items = [f"{i.issue_id} {i.title}" for i in chief.major_doubts]
        return f"總判定：{c.value}（NG），重大疑義 {len(items)} 項", "bad", items
    if c == S.OverallConclusion.CONDITIONAL:
        items = [f"{i.issue_id} {i.title}" for i in chief.pending_review_items]
        return f"總判定：{c.value}（注意），待複核 {len(items)} 項", "warn", items
    return f"總判定：{c.value}（OK）", "ok", []


def build_model(session: ReviewSession) -> ReportModel:
    if session.chief is None:
        raise ValueError("審查尚未完成，無法產生報告")
    info = {k: v for k, v in session.project_info.items() if v}
    chief = session.chief

    meta: list[tuple[str, str, bool]] = [("報告編號", report_number(session), False)]
    meta += [(k, str(v), False) for k, v in info.items() if k not in ("案名", "備註")]
    if info.get("備註"):
        meta.append(("備註", str(info["備註"]), True))
    meta += [
        ("審查依據", CODE_BASIS, True),
        ("審查方式", REVIEW_METHOD, True),
        ("語言模型", model_text(session), len(set(session.agent_models.values())) > 1),
        ("產生時間", session.started_at, False),
        ("程式版本／雜湊", trace_text(session), True),
    ]

    src = session.source
    source_rows = [("輸入檔案", src.get("file", "—"))]
    for sh in src.get("sheets", []):
        mapped = "、".join(f"{v}→{k}" for k, v in sh["mapped"].items()) or "未辨識（全表提供參考）"
        source_rows.append((f"工作表「{sh['name']}」", f"{sh['rows']} 列；{mapped}"))
    source_rows.append(("SHA-256", src.get("sha256", "—")))

    lim = src.get("limits", {})
    limit_rows = [(item, fmt.format(v=lim[key]), ref) for key, item, fmt, ref in LIMIT_ROWS if key in lim]
    limit_rows.append(COMBO_ROW)
    fmt = {key: (fmt.format(v=lim[key]), ref) for key, _, fmt, ref in LIMIT_ROWS if key in lim}
    criteria = {
        "drift": fmt.get("drift_max", ("", ""))[0],
        "beam_rho": fmt.get("beam_rho_max", ("", ""))[0],
        "col_rho": "、".join(x for x in (fmt.get("col_rho_min", ("",))[0], fmt.get("col_rho_max", ("",))[0]) if x),
        "dcr": "；".join(x for x in (fmt.get("dcr_max", ("",))[0], fmt.get("dcr_warn", ("",))[0]) if x),
        "combo": COMBO_ROW[1],
    }

    prechecks = [{**f, "judge": PRECHECK_JUDGE.get(f["level"], f["level"])} for f in session.findings]

    bucket_of = {
        i.issue_id: key for key in BUCKET_LABEL for i in getattr(chief, key)
    }
    verdicts = {v.issue_id: v for v in session.round3.verdicts}
    item_risk = {i.issue_id: i.risk_level for key in BUCKET_LABEL for i in getattr(chief, key)}
    summary_rows = []
    for issue in session.round1.issues:
        risk = item_risk.get(issue.issue_id, verdicts[issue.issue_id].risk_level)
        summary_rows.append(SummaryRow(
            issue_id=issue.issue_id, title=issue.title, category=issue.category,
            decision=verdicts[issue.issue_id].decision.value, risk=risk.value, judge=JUDGE[risk],
            bucket=BUCKET_LABEL.get(bucket_of.get(issue.issue_id, ""), "—"), ref=issue.code_reference,
        ))

    text, level, items = verdict(chief)
    return ReportModel(
        title=REPORT_TITLE,
        report_no=report_number(session),
        project_name=info.get("案名", ""),
        meta=meta,
        source_rows=source_rows,
        limit_rows=limit_rows,
        prechecks=prechecks,
        threads=session.threads(),
        round1_summary=session.round1.summary,
        buckets=[(k, v, getattr(chief, k)) for k, v in BUCKET_LABEL.items()],
        executive_summary=chief.executive_summary,
        observations=chief.debate_observations,
        summary_rows=summary_rows,
        verdict_text=text,
        verdict_level=level,
        verdict_items=items,
        notes=list(session.notes),
        criteria=criteria,
    )


def meta_rows(meta: list[tuple[str, str, bool]]) -> list[list[tuple[str, str]]]:
    """表頭兩欄式排版：每列兩項，跨欄項目自成一列（與 rc-member-designer rpHead 相同規則）。"""
    rows, row = [], []
    for k, v, wide in meta:
        if wide:
            if row:
                rows.append(row)
                row = []
            rows.append([(k, v)])
            continue
        row.append((k, v))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows
