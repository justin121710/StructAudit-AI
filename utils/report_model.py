"""報表資料模型：PDF（HTML 範本／ReportLab）與 Excel 共用同一份內容，確保輸出一致。

版面依 rc-member-designer 計算書慣例：
表頭（標題＋兩欄資料表＋程式版本／雜湊）→ 編號章節 → 檢核彙總與總判定 → 附註 → 簽核欄（設計／校核／審核）。
判定用語亦一致：OK／注意／NG；風險等級以中文表示（低風險／中風險／高風險）；算式為 LaTeX。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from agents import schemas as S
from orchestrator import AGENT_NAMES, PassResult, ReviewSession
from utils import rcmd
from utils.specs import Clause, load_library
from version import __version__

REPORT_TITLE = "結構計算書審查報告"
REVIEW_METHOD = "AI 詰問審查：外審質疑 → 設計回覆 → 外審終審與解決方案（三回合）＋ 主任檢核技師裁決；三個模型輪流擔任角色"
SIGN_ROLES = ("設計", "校核", "審核")
JUDGE = {S.RiskLevel.LOW: "OK", S.RiskLevel.MEDIUM: "注意", S.RiskLevel.HIGH: "NG"}
LEVEL = {"OK": "ok", "注意": "warn", "NG": "bad"}
BUCKET_LABEL = {
    "major_doubts": "重大疑義（高風險）",
    "pending_review_items": "待複核項目（中風險）",
    "no_concern_items": "無虞項目（低風險）",
}
UNCOVERED = [
    "審查僅依送審計算書（PDF）與所引規範原文，未讀取結構分析模型檔",
    "圖說與計算書之一致性（配筋、斷面、材料標示）",
    "計算書以外之構件、接頭與基礎",
    "建議方案之最終數值須於 rc-member-designer 重新計算確認",
]


@dataclass
class SummaryRow:
    no: int
    title: str
    sources: str
    risk: str  # 低風險／中風險／高風險
    judge: str  # OK／注意／NG
    solution: str
    ref: str


@dataclass
class FinalItemView:
    no: int
    title: str
    risk: str
    judge: str
    sources: list[str]
    finding: str
    clauses: list[str]
    solution: S.Solution | None
    why_minimal: str
    required_action: str


@dataclass
class PassView:
    no: int
    roles: list[tuple[str, str]]  # (角色, 模型)
    conclusion: str
    round1_summary: str
    threads: list[dict]
    buckets: list[tuple[str, str, list[S.ReportItem]]]
    executive_summary: str
    observations: str
    lookups: list[str]


@dataclass
class ReportModel:
    title: str
    report_no: str
    project_name: str
    meta: list[tuple[str, str, bool]]  # (項目, 內容, 是否跨兩欄)
    source_rows: list[tuple[str, str]]
    role_rows: list[list[str]]  # 輪次, A, B, C
    final_model: str
    executive_summary: str
    cross_pass_notes: str
    items: list[FinalItemView]
    tool_summary: str
    case_name: str
    adjusted_applied: list[str]
    adjusted_rejected: list[str]
    passes: list[PassView]
    summary_rows: list[SummaryRow]
    verdict_text: str
    verdict_level: str  # ok／warn／bad
    verdict_items: list[str]
    clauses: list[Clause]
    notes: list[str]
    tool_url: str = rcmd.TOOL_URL
    uncovered: list[str] = field(default_factory=lambda: list(UNCOVERED))
    sign_roles: tuple[str, ...] = SIGN_ROLES


def report_number(session: ReviewSession) -> str:
    stamp = datetime.strptime(session.started_at, "%Y-%m-%d %H:%M").strftime("%Y%m%d-%H%M")
    return f"SR-{stamp}"


def trace_text(session: ReviewSession) -> str:
    sha = session.calc.get("sha256", "")
    return f"v{__version__}｜計算書 SHA-256 {sha[:16] or '—'}"


def model_text(session: ReviewSession) -> str:
    slots = session.model_slots
    if len(set(slots)) == 1:
        return slots[0] if slots else "—"
    return "；".join(f"模型 {n}：{m}" for n, m in enumerate(slots, 1))


def verdict(conclusion: S.OverallConclusion, items: list[FinalItemView]) -> tuple[str, str, list[str]]:
    if conclusion == S.OverallConclusion.RETURNED:
        hits = [i.title for i in items if i.risk == S.RiskLevel.HIGH.value]
        return f"總判定：{conclusion.value}（NG），高風險 {len(hits)} 項", "bad", hits
    if conclusion == S.OverallConclusion.CONDITIONAL:
        hits = [i.title for i in items if i.risk == S.RiskLevel.MEDIUM.value]
        return f"總判定：{conclusion.value}（注意），中風險 {len(hits)} 項", "warn", hits
    return f"總判定：{conclusion.value}（OK）", "ok", []


def _cited_clauses(session: ReviewSession) -> list[Clause]:
    lib = load_library()
    texts = []
    for ps in session.passes:
        texts += [c for i in ps.round1.issues for c in i.code_clauses]
        texts += [c for r in ps.round2.responses for c in r.code_clauses]
    if session.final:
        texts += [c for it in session.final.items for c in it.code_clauses]
    seen, out = set(), []
    for code, cid in lib.find_refs(" ".join(texts)):
        c = lib.get(code, cid)
        if c and c.key not in seen:
            seen.add(c.key)
            out.append(c)
    return out


def _pass_view(ps: PassResult) -> PassView:
    return PassView(
        no=ps.no, roles=[(AGENT_NAMES[r], m) for r, m in ps.roles.items()],
        conclusion=ps.chief.overall_conclusion.value, round1_summary=ps.round1.summary, threads=ps.threads(),
        buckets=[(k, v, getattr(ps.chief, k)) for k, v in BUCKET_LABEL.items()],
        executive_summary=ps.chief.executive_summary, observations=ps.chief.debate_observations, lookups=ps.lookups)


def build_model(session: ReviewSession) -> ReportModel:
    if session.final is None:
        raise ValueError("審查尚未完成，無法產生報告")
    info = {k: v for k, v in session.project_info.items() if v}
    calc = session.calc
    project = info.get("工程名稱") or info.get("案名") or ""
    member = info.get("構件", "")

    meta: list[tuple[str, str, bool]] = [("報告編號", report_number(session), False), ("產生時間", session.started_at, False)]
    skip = {"工程名稱", "案名", "程式版本／雜湊", "產生時間"}
    for k, v in info.items():
        if k not in skip:
            meta.append((f"計算書{k}" if k in ("單位",) else k, str(v), len(str(v)) > 26))
    meta += [
        ("審查方式", REVIEW_METHOD, True),
        ("語言模型", model_text(session), True),
        ("綜合裁決", session.final_model, False),
        ("審查輪數", f"{len(session.passes)} 輪（角色輪替）", False),
        ("程式版本／雜湊", trace_text(session), True),
    ]

    source_rows = [
        ("計算書", f"{calc.get('file', '—')}（{calc.get('pages', 0)} 頁；{calc.get('title') or '—'}）"),
        ("計算書 SHA-256", calc.get("sha256", "—")),
    ]
    if info.get("程式版本／雜湊"):
        source_rows.append(("計算書程式版本", info["程式版本／雜湊"]))
    source_rows.append(("rc-member-designer 案件檔", session.case_name or "未提供（解決方案以欄位名稱表示）"))
    source_rows.append(("規範原文", f"rc-member-designer specs/（{session.spec_source}）"))

    role_rows = [[f"第 {ps.no} 輪", ps.roles.get("A", ""), ps.roles.get("B", ""), ps.roles.get("C", "")]
                 for ps in session.passes]

    f = session.final
    items = [FinalItemView(no=n, title=it.title, risk=it.risk_level.value, judge=JUDGE[it.risk_level],
                           sources=it.source_ids, finding=it.finding, clauses=it.code_clauses,
                           solution=it.final_solution, why_minimal=it.why_minimal, required_action=it.required_action)
             for n, it in enumerate(f.items, 1)]
    summary_rows = [SummaryRow(no=i.no, title=i.title, sources="、".join(i.sources), risk=i.risk, judge=i.judge,
                               solution=i.solution.label if i.solution else "—", ref="、".join(i.clauses) or "—")
                    for i in items]
    text, level, vitems = verdict(f.overall_conclusion, items)
    return ReportModel(
        title=REPORT_TITLE,
        report_no=report_number(session),
        project_name=project or (f"構件 {member}" if member else ""),
        meta=meta,
        source_rows=source_rows,
        role_rows=role_rows,
        final_model=session.final_model,
        executive_summary=f.executive_summary,
        cross_pass_notes=f.cross_pass_notes,
        items=items,
        tool_summary=f.tool_summary,
        case_name=session.case_name,
        adjusted_applied=list(session.adjusted_applied),
        adjusted_rejected=list(session.adjusted_rejected),
        passes=[_pass_view(ps) for ps in session.passes],
        summary_rows=summary_rows,
        verdict_text=text,
        verdict_level=level,
        verdict_items=vitems,
        clauses=_cited_clauses(session),
        notes=list(session.notes),
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
