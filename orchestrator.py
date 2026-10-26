"""核心調度器：角色輪替的多輪審查。

一輪（pass）＝固定三回合詰問＋主任技師彙整：
    Round 1  Agent B 初審質疑
    Round 2  Agent A 逐項回覆抗辯
    Round 3  Agent B 覆核終審＋具體解決方案（rc-member-designer 參數）
    彙整     Agent C 裁決並複核方案
每一回合開始前，該 Agent 先自行查閱規範原文（條號或關鍵字），只能引用查得的條文（固定執行，不可關閉）。
每一次 API 呼叫的實際 token 用量都記入 ReviewSession.usage，依模型單價換算費用（utils/pricing.py）。

三個模型輪流擔任 A／B／C（第 p 輪：A 用模型 p、B 用模型 p+1、C 用模型 p+2，循環），每一輪都建立全新的 client、
從頭開始審查，提示中不含其他輪的任何內容，因此同一個模型在不同輪次之間不會共用記憶。
所有輪次結束後，由第 1 輪主任技師所用的模型（同樣是全新的對話）做綜合裁決，並以保守原則校正：
風險取各輪最嚴者、任一輪的中高風險項目不得遺漏、總結論不得比任一輪寬鬆。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from agents import prompts as P
from agents import schemas as S
from agents.llm_client import Attachment, LLMClient, Usage, schema_text
from utils import rcmd
from utils.calcbook import CalcBook
from utils.latex import split_lines
from utils.pricing import USD_TWD, CostSummary, Price, summarize
from utils.specs import Clause, SpecLibrary, format_clauses, load_library, scope_of

AGENT_NAMES = {
    "A": "Agent A｜原設計工程師",
    "B": "Agent B｜外部審查專家",
    "C": "Agent C｜主任檢核技師",
    "F": "綜合裁決｜主任檢核技師",
}
ROLES = ("A", "B", "C")
TEMPERATURE = {"A": 0.4, "B": 0.3, "C": 0.1, "F": 0.1}
RISK_ORDER = {S.RiskLevel.LOW: 0, S.RiskLevel.MEDIUM: 1, S.RiskLevel.HIGH: 2}
CONCLUSION_ORDER = {S.OverallConclusion.APPROVED: 0, S.OverallConclusion.CONDITIONAL: 1, S.OverallConclusion.RETURNED: 2}
BUCKET_RISK = {
    "no_concern_items": S.RiskLevel.LOW,
    "pending_review_items": S.RiskLevel.MEDIUM,
    "major_doubts": S.RiskLevel.HIGH,
}
MAX_AUTO_CLAUSES = 30  # 計算書自行引用的條文，自動查出原文的上限
MAX_CLAUSE_CHARS = 60_000  # 每次提示中規範原文的字數上限

# 對話中附上白話解釋，讓非結構專業的讀者也看得懂各種立場與判定代表什麼
STANCE_MEANING = {
    S.Stance.ADMIT: "設計單位同意審查意見，並提出修正方案",
    S.Stance.CODE_BASIS: "設計單位認為現行作法符合規範，並指出依據條文",
    S.Stance.CALC_DEFENSE: "設計單位以計算推導說明設計足夠",
}
DECISION_MEANING = {
    S.Decision.ACCEPT: "回覆合理且有依據，此項疑義解除",
    S.Decision.PARTIAL: "回覆部分成立，但仍需補充資料或修正",
    S.Decision.REJECT: "回覆不成立，維持原疑義",
}
RISK_MEANING = {
    S.RiskLevel.LOW: "疑義解除或不影響結構安全",
    S.RiskLevel.MEDIUM: "需補充資料或修正後再確認",
    S.RiskLevel.HIGH: "不符規範或影響結構安全，必須修正",
}

ProgressCallback = Callable[[str, str], None]
MessageCallback = Callable[["DialogueMessage"], None]
UsageCallback = Callable[[dict], None]
ThinkingCallback = Callable[[dict, str], None]  # (思考段落紀錄, 新增文字)
ClientFactory = Callable[[], LLMClient]


def tex_block(formulas: list[str]) -> str:
    """Markdown 中的 LaTeX 區塊（Streamlit 以 KaTeX 顯示）。"""
    lines = [x for f in formulas for x in split_lines(f)]
    return "\n\n".join(f"$$\n{x.strip().strip('$')}\n$$" for x in lines)


@dataclass
class DialogueMessage:
    """審查對話中的一則發言，供介面即時顯示與報告保存。"""

    agent: str  # A／B／C／F
    stage: str  # round1／round2／round3／chief／final
    title: str
    body: str  # Markdown（算式為 $$…$$）
    pass_no: int = 1  # 第幾輪；綜合裁決為 0
    model: str = ""

    @property
    def speaker(self) -> str:
        return AGENT_NAMES[self.agent]

    def to_dict(self) -> dict:
        return {"pass": self.pass_no, "agent": self.agent, "speaker": self.speaker, "model": self.model,
                "stage": self.stage, "title": self.title, "body": self.body}


@dataclass
class PassResult:
    no: int
    roles: dict[str, str]  # A／B／C -> "provider / model"
    round1: S.Round1Output | None = None
    round2: S.Round2Output | None = None
    round3: S.Round3Output | None = None
    chief: S.ChiefReport | None = None
    clause_keys: list[str] = field(default_factory=list)  # 本輪提供給 Agent 的規範條文
    lookups: list[str] = field(default_factory=list)  # 各回合查閱紀錄

    @property
    def issue_ids(self) -> list[str]:
        return [i.issue_id for i in self.round1.issues] if self.round1 else []

    def threads(self) -> list[dict]:
        """以疑義為單位整理三回合內容，供 UI 與報告使用。"""
        resp = {r.issue_id: r for r in (self.round2.responses if self.round2 else [])}
        verd = {v.issue_id: v for v in (self.round3.verdicts if self.round3 else [])}
        chief = {i.issue_id: i for i in self.chief_items()}
        return [{"issue": i, "response": resp.get(i.issue_id), "verdict": verd.get(i.issue_id),
                 "chief": chief.get(i.issue_id), "adopted": self.adopted(i.issue_id)}
                for i in (self.round1.issues if self.round1 else [])]

    def chief_items(self) -> list[S.ReportItem]:
        c = self.chief
        return [*c.major_doubts, *c.pending_review_items, *c.no_concern_items] if c else []

    def adopted(self, qid: str) -> S.Solution | None:
        item = next((i for i in self.chief_items() if i.issue_id == qid), None)
        v = next((v for v in (self.round3.verdicts if self.round3 else []) if v.issue_id == qid), None)
        if not item or not v or not 1 <= item.adopted_solution <= len(v.solutions):
            return None
        return v.solutions[item.adopted_solution - 1]

    def to_dict(self) -> dict:
        dump = lambda m: m.model_dump(mode="json") if m else None  # noqa: E731
        return {"pass": self.no, "roles": self.roles, "round1_challenges": dump(self.round1),
                "round2_responses": dump(self.round2), "round3_verdicts": dump(self.round3),
                "chief_report": dump(self.chief), "clauses": self.clause_keys, "lookups": self.lookups}


@dataclass
class ReviewSession:
    calc: dict  # 計算書：檔名、SHA-256、頁數、標題、類型、模組、設計依據
    project_info: dict  # 報告表頭專案資料（取自計算書）
    model_slots: list[str] = field(default_factory=list)  # 三個模型槽位 "provider / model"
    passes: list[PassResult] = field(default_factory=list)
    final: S.ConsolidatedReport | None = None
    final_model: str = ""
    case_name: str = ""  # rc-member-designer 案件檔名
    adjusted_case: dict | None = None  # 套用最終方案後的案件檔
    adjusted_applied: list[str] = field(default_factory=list)
    adjusted_rejected: list[str] = field(default_factory=list)
    spec_source: str = ""
    usage: list[dict] = field(default_factory=list)  # 每一次 API 呼叫的實際 token 用量（含輪次、角色）
    thoughts: list[dict] = field(default_factory=list)  # 每一次 API 呼叫的思考摘要（輪次、角色、階段、模型、內容）
    prices: dict[str, Price] = field(default_factory=dict)  # 計費單價（模型 → 單價），未列者用預設值
    fx: float = USD_TWD  # 美元兌新台幣
    started_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))
    notes: list[str] = field(default_factory=list)
    transcript: list[DialogueMessage] = field(default_factory=list)

    @property
    def provider(self) -> str:
        return self.model_slots[0].split(" / ")[0] if self.model_slots else ""

    def cost(self) -> CostSummary:
        return summarize(self.usage, self.prices, self.fx,
                         datetime.strptime(self.started_at, "%Y-%m-%d %H:%M").date())

    def item_risk(self, source_id: str) -> S.RiskLevel | None:
        """P2-Q1 → 第 2 輪 Q1 在主任技師分類中的風險等級。"""
        try:
            p, q = source_id.upper().lstrip("P").split("-", 1)
            ps = self.passes[int(p) - 1]
        except (ValueError, IndexError):
            return None
        return next((i.risk_level for i in ps.chief_items() if i.issue_id == q), None)

    def _cost_dict(self) -> dict:
        c = self.cost()
        return {"usd": round(c.usd, 4), "twd": round(c.twd, 1), "fx": c.fx, "calls": c.calls,
                "missing_price": c.missing, "estimated": c.estimated,
                "by_model": [{**r.__dict__, "usd": None if r.usd is None else round(r.usd, 4)} for r in c.rows],
                "prices": {m: p.__dict__ for m, p in c.prices.items()}}

    def to_dict(self) -> dict:
        return {
            "calc_book": self.calc,
            "project_info": self.project_info,
            "model_slots": self.model_slots,
            "started_at": self.started_at,
            "spec_source": self.spec_source,
            "passes": [p.to_dict() for p in self.passes],
            "final_model": self.final_model,
            "final_report": self.final.model_dump(mode="json") if self.final else None,
            "rc_member_designer": {"case_file": self.case_name, "applied": self.adjusted_applied,
                                   "rejected": self.adjusted_rejected, "tool": rcmd.TOOL_URL},
            "usage": self.usage,
            "thoughts": self.thoughts,
            "cost": self._cost_dict(),
            "transcript": [m.to_dict() for m in self.transcript],
            "notes": self.notes,
        }


def _fmt_solution(n: int, sol: S.Solution) -> list[str]:
    lines = [f"    方案 {n}：{sol.label}｜{sol.approach}｜改動幅度：{sol.change_extent}"]
    for c in sol.changes:
        lines.append(f"      - {c.param_id or '（非工具欄位）'} {c.label}：{c.current} → {c.proposed} {c.unit}")
    lines += [f"      驗算：{f}" for f in sol.verification]
    if sol.other_method:
        lines.append(f"      其他作法：{sol.other_method}")
    return lines


def format_debate(ps: PassResult, include_verdicts: bool = True) -> str:
    blocks = []
    for t in ps.threads():
        i, r, v = t["issue"], t["response"], t["verdict"]
        lines = [
            f"### {i.issue_id} {i.title}（{i.category}，初判 {i.preliminary_risk.value}）",
            f"- [B 質疑] {i.challenge}",
            f"  - 計算書出處：{i.calc_ref}；引用數據：{i.cited_data}",
            *[f"  - 算式：{f}" for f in i.formulas],
            f"  - 依據：{'、'.join(i.code_clauses)}｜{i.code_quote}",
        ]
        if r:
            lines.append(f"- [A 回覆｜{r.stance.value}] {r.response}")
            lines += [f"  - 計算：{f}" for f in r.calculations]
            lines.append(f"  - 數據出處：{r.data_source}；規範依據：{'、'.join(r.code_clauses) or '—'}")
            if r.proposed_action:
                lines.append(f"  - 修正方案：{r.proposed_action}")
        if include_verdicts and v:
            lines.append(f"- [B 終審｜{v.decision.value}｜{v.risk_level.value}] {v.rationale}"
                         + (f"；應辦事項：{v.required_action}" if v.required_action else ""))
            lines += [f"  - 覆核算式：{f}" for f in v.check_formulas]
            for n, sol in enumerate(v.solutions, 1):
                lines += _fmt_solution(n, sol)
            if v.solutions:
                lines.append(f"  - 外審建議採用：方案 {v.recommended_solution}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


class _PassLibrary:
    """單一輪次內提供給 Agent 的規範條文（每輪重新建立，不跨輪共用）。"""

    def __init__(self, lib: SpecLibrary):
        self.lib, self.items = lib, {}

    def add(self, clauses: list[Clause]) -> list[Clause]:
        new = [c for c in clauses if c.key not in self.items]
        for c in new:
            self.items[c.key] = c
        return new

    def add_refs(self, refs: list[tuple[str, str]], children: int = 0) -> list[Clause]:
        out = []
        for code, cid in refs:
            out += self.add(self.lib.lookup(code, cid, max_children=children))
        return out

    def text(self, prefer: set[str] | None = None) -> str:
        prefer = prefer or set()
        order = [c for k, c in self.items.items() if k in prefer] + [c for k, c in self.items.items() if k not in prefer]
        picked, used = [], 0
        for c in order:
            if used + min(len(c.text), 1800) > MAX_CLAUSE_CHARS:
                continue
            picked.append(c)
            used += min(len(c.text), 1800)
        return format_clauses(picked) if picked else "（尚未查閱任何條文）"

    def listing(self) -> str:
        return "、".join(c.ref for c in self.items.values()) or "（無）"


class ReviewOrchestrator:
    def __init__(
        self,
        clients: LLMClient | ClientFactory | list[ClientFactory],
        passes: int = 3,
        on_progress: ProgressCallback | None = None,
        on_message: MessageCallback | None = None,
        library: SpecLibrary | None = None,
        on_usage: UsageCallback | None = None,
        on_thinking: ThinkingCallback | None = None,
    ):
        """clients：三個模型槽位的 client 產生器（每呼叫一次建立全新的 client）；
        也可給單一產生器或單一 LLMClient（三個槽位共用，測試與離線示範用）。"""
        if isinstance(clients, LLMClient):
            inst = clients
            factories = [lambda: inst]
        elif callable(clients):
            factories = [clients]
        else:
            factories = list(clients)
        self.slots = [factories[i % len(factories)] for i in range(3)]
        self.passes = max(1, int(passes))
        progress_cb = on_progress or (lambda stage, msg: None)
        self._stage = ""

        def progress(stage: str, text: str) -> None:
            self._stage = text.rstrip("…").removesuffix("中")  # 供思考紀錄標示階段
            progress_cb(stage, text)

        self.on_progress = progress
        self.on_thinking = on_thinking or (lambda entry, delta: None)
        self.on_message = on_message or (lambda msg: None)
        self.lib = library or load_library()
        self.on_usage = on_usage or (lambda record: None)

    @staticmethod
    def roles_for(pass_no: int) -> dict[str, int]:
        """第 pass_no 輪（1 起算）各角色使用的模型槽位：A＝p、B＝p+1、C＝p+2（循環）。"""
        return {r: (i + pass_no - 1) % 3 for i, r in enumerate(ROLES)}

    def _say(self, s: ReviewSession, agent, stage, title, body, pass_no, model="") -> None:
        msg = DialogueMessage(agent, stage, title, body, pass_no, model)
        s.transcript.append(msg)
        self.on_message(msg)

    # ------------------------------------------------------------------
    def _client(self, s: ReviewSession, slot: int, pass_no: int, role: str) -> LLMClient:
        """建立全新的 client，並把它每一次 API 呼叫的實際用量記入審查紀錄。"""
        c = self.slots[slot]()
        current: dict = {}

        def record(u: Usage) -> None:
            rec = {"pass": pass_no, "role": role, **u.__dict__}
            s.usage.append(rec)
            self.on_usage(rec)
            current.clear()  # 一次呼叫結束，下一段思考另起一筆

        def think(delta: str) -> None:
            if not current:
                current["entry"] = {"pass": pass_no, "role": role, "task": c._task, "stage": self._stage,
                                    "model": c.model, "text": ""}
                s.thoughts.append(current["entry"])
            current["entry"]["text"] += delta
            self.on_thinking(current["entry"], delta)

        c.on_usage, c.on_thinking = record, think
        return c

    def run(self, calc: CalcBook, case: dict | None = None, case_name: str = "",
            project_info: dict | None = None, prices: dict[str, Price] | None = None,
            fx: float = USD_TWD) -> ReviewSession:
        probe = [f() for f in self.slots]  # 只用來記錄模型名稱；審查時每輪另建新的 client
        session = ReviewSession(
            calc={"file": calc.filename, "sha256": calc.sha256, "kind": calc.kind, "pages": calc.n_pages,
                  "title": calc.title, "module": calc.module, "basis": calc.basis, "from_rcmd": calc.from_rcmd},
            project_info={**calc.facts, **{k: v for k, v in (project_info or {}).items() if v}},
            model_slots=[f"{c.provider} / {c.model}" for c in probe],
            case_name=case_name,
            spec_source=self.lib.source,
            prices=dict(prices or {}),
            fx=fx,
        )
        self._calc, self._case = calc, case
        self._module = (rcmd.case_module(case) if case else None) or calc.module or "pylon"
        self._scope = scope_of(calc.basis)
        self._att = [Attachment(calc.filename, calc.pdf)] if calc.pdf else []
        for p in range(1, self.passes + 1):
            session.passes.append(self._run_pass(session, p))
        self.final(session)
        self.on_progress("done", "審查完成")
        return session

    def _run_pass(self, s: ReviewSession, p: int) -> PassResult:
        idx = self.roles_for(p)
        clients = {r: self._client(s, i, p, r) for r, i in idx.items()}  # 每一輪、每一角色都是全新的 client
        ps = PassResult(no=p, roles={r: f"{c.provider} / {c.model}" for r, c in clients.items()})
        lib = _PassLibrary(self.lib)
        refs = self.lib.find_refs(self._calc.text())[:MAX_AUTO_CLAUSES]
        auto = lib.add_refs(refs)
        if auto:
            ps.lookups.append(f"計算書引用條文 {len(auto)} 段（自動查出原文）")
        self.round1(s, ps, clients["B"], lib)
        self.round2(s, ps, clients["A"], lib)
        self.round3(s, ps, clients["B"], lib)
        self.chief(s, ps, clients["C"], lib)
        ps.clause_keys = list(lib.items)
        for r, c in clients.items():
            if c.attachments_dropped:
                s.notes.append(f"第 {p} 輪 {AGENT_NAMES[r]}（{c.model}）不接受 PDF 附件，改以擷取文字審查。")
        return ps

    # ------------------------------------------------------------------
    def _call(self, client: LLMClient, agent: str, system: str, user: str, schema, task: str, payload: dict):
        return client.generate(system, user, schema, temperature=TEMPERATURE[agent], task=task, payload=payload,
                               attachments=self._att)

    def _common(self) -> dict:
        return {"facts": self._calc.facts_text() or "（計算書表頭未辨識）", "calc": self._calc.prompt_text()}

    def _lookup(self, s, ps, client, agent, lib: _PassLibrary, stage_key: str, stage: str, extra: str = "") -> None:
        self.on_progress(stage_key, f"第 {ps.no} 輪｜{stage}｜{AGENT_NAMES[agent]} 查閱規範原文中…")
        user = P.LOOKUP_USER.format(stage=stage, catalog=self.lib.catalog(self._scope), toc=self.lib.toc(self._scope),
                                    have=lib.listing(), extra=extra, schema=schema_text(S.LookupRequest),
                                    **self._common())
        req: S.LookupRequest = self._call(client, agent, {"A": P.AGENT_A_SYSTEM, "B": P.AGENT_B_SYSTEM,
                                                           "C": P.AGENT_C_SYSTEM}[agent], user, S.LookupRequest,
                                          "lookup", {"stage": stage_key})
        added, missing = [], []
        for r in req.clauses[:20]:
            got = self.lib.lookup(r.spec, r.clause)
            if not got:
                missing.append(f"[{r.spec}] {r.clause}")
            added += lib.add(got)
        for kw in req.keywords[:6]:
            added += lib.add(self.lib.search(kw, self._scope, k=3))
        note = f"{stage}：查閱 {len(added)} 段條文" + (f"；查無 {'、'.join(missing)}" if missing else "")
        ps.lookups.append(note)
        if added or missing:
            body = (req.reason + "\n\n" if req.reason else "") + "查得：" + ("、".join(c.ref for c in added[:15])
                                                                            + ("…" if len(added) > 15 else "") or "（無）")
            if missing:
                body += f"\n\n規範原文中查無：{'、'.join(missing)}"
            self._say(s, agent, stage_key, "查閱規範原文", body, ps.no, client.model)

    def _cite(self, s: ReviewSession, ps: PassResult, lib: _PassLibrary, who: str, texts: list[str]) -> set[str]:
        """核對引用條文：查無原文者記入系統紀錄；查得但尚未提供者補入本輪條文庫。回傳引用到的條文 key。"""
        keys = set()
        for t in texts:
            for code, cid in self.lib.find_refs(t):
                c = self.lib.get(code, cid)
                if c:
                    lib.add([c])
                    keys.add(c.key)
            for miss in self.lib.check_refs(t):
                note = f"第 {ps.no} 輪 {who} 引用「{miss}」，規範原文索引中查無此條，請技師核對。"
                if note not in s.notes:
                    s.notes.append(note)
        return keys

    # ------------------------------------------------------------------
    def round1(self, s: ReviewSession, ps: PassResult, client: LLMClient, lib: _PassLibrary) -> None:
        self._lookup(s, ps, client, "B", lib, "round1", "Round 1 初審質疑")
        self.on_progress("round1", f"第 {ps.no} 輪｜Round 1｜{AGENT_NAMES['B']} 初審質疑中…")
        user = P.ROUND1_USER.format(clauses=lib.text(), schema=schema_text(S.Round1Output), **self._common())
        out: S.Round1Output = self._call(client, "B", P.AGENT_B_SYSTEM, user, S.Round1Output, "round1",
                                         {"calc_text": self._calc.text(), "facts": self._calc.facts})
        for n, issue in enumerate(out.issues, 1):  # 統一編號，避免模型自行編號重複或跳號
            issue.issue_id = f"Q{n}"
        ps.round1 = out
        for f in out.project_facts:  # 表頭未辨識到的專案資料，由 Agent 從計算書補充
            if f.item and f.value and f.item not in s.project_info:
                s.project_info[f.item] = f.value
        self._cite(s, ps, lib, "外審", [c for i in out.issues for c in i.code_clauses])
        self._say(s, "B", "round1", "初審總體意見",
                  f"{out.summary}\n\n以下共 {len(out.issues)} 項疑義，請設計單位逐項回覆。", ps.no, client.model)
        for i in out.issues:
            self._say(s, "B", "round1", f"{i.issue_id}　{i.title}", "\n\n".join([
                f"**初判風險**：{i.preliminary_risk.value}",
                f"**疑義說明**：{i.challenge}",
                f"**檢核算式**：\n\n{tex_block(i.formulas)}",
                f"**引用數據**：{i.cited_data}（{i.calc_ref}）",
                f"**規範依據**：{'、'.join(i.code_clauses)}——{i.code_quote}",
            ]), ps.no, client.model)

    def round2(self, s: ReviewSession, ps: PassResult, client: LLMClient, lib: _PassLibrary) -> None:
        issues_json = json.dumps([i.model_dump(mode="json") for i in ps.round1.issues], ensure_ascii=False, indent=1)
        self._lookup(s, ps, client, "A", lib, "round2", "Round 2 回覆抗辯", f"\n## 外審疑義\n{issues_json}\n")
        self.on_progress("round2", f"第 {ps.no} 輪｜Round 2｜{AGENT_NAMES['A']} 逐項回覆中…")
        user = P.ROUND2_USER.format(clauses=lib.text(), issues=issues_json, schema=schema_text(S.Round2Output),
                                    **self._common())
        payload = {"issues": [i.model_dump(mode="json") for i in ps.round1.issues]}
        out: S.Round2Output = self._call(client, "A", P.AGENT_A_SYSTEM, user, S.Round2Output, "round2", payload)
        missing = self._missing(ps.issue_ids, [r.issue_id for r in out.responses])
        if missing:
            retry = self._call(client, "A", P.AGENT_A_SYSTEM,
                               user + f"\n\n# 注意\n上次輸出遺漏 {', '.join(missing)}，請補齊全部疑義的回覆。",
                               S.Round2Output, "round2", payload)
            out = self._merge(out, retry, "responses")
            for qid in self._missing(ps.issue_ids, [r.issue_id for r in out.responses]):
                out.responses.append(S.DesignerResponse(
                    issue_id=qid, stance=S.Stance.ADMIT, response="（設計單位未就本項提出回覆，視同未能抗辯）",
                    data_source="—"))
                s.notes.append(f"第 {ps.no} 輪 Agent A 未回覆 {qid}，系統以「未能抗辯」補記。")
        out.responses = self._ordered(out.responses, ps.issue_ids)
        ps.round2 = out
        self._cite(s, ps, lib, "設計單位", [c for r in out.responses for c in r.code_clauses])
        titles = {i.issue_id: i.title for i in ps.round1.issues}
        for r in out.responses:
            parts = [f"**立場**：{r.stance.value}（{STANCE_MEANING[r.stance]}）", f"**回覆說明**：{r.response}"]
            if r.calculations:
                parts.append(f"**計算推導**：\n\n{tex_block(r.calculations)}")
            if r.proposed_action:
                parts.append(f"**修正方案**：{r.proposed_action}")
            parts.append(f"**數據出處**：{r.data_source}　**規範依據**：{'、'.join(r.code_clauses) or '—'}")
            self._say(s, "A", "round2", f"回覆 {r.issue_id}　{titles[r.issue_id]}", "\n\n".join(parts), ps.no,
                      client.model)

    def round3(self, s: ReviewSession, ps: PassResult, client: LLMClient, lib: _PassLibrary) -> None:
        debate = format_debate(ps, include_verdicts=False)
        self._lookup(s, ps, client, "B", lib, "round3", "Round 3 覆核終審", f"\n## 辯論紀錄\n{debate}\n")
        self.on_progress("round3", f"第 {ps.no} 輪｜Round 3｜{AGENT_NAMES['B']} 覆核終審與研擬方案中…")
        user = P.ROUND3_USER.format(clauses=lib.text(), debate=debate, tool_url=rcmd.TOOL_URL,
                                    params=rcmd.catalog_text(self._module, self._case),
                                    schema=schema_text(S.Round3Output), **self._common())
        payload = {"issues": [i.model_dump(mode="json") for i in ps.round1.issues],
                   "responses": [r.model_dump(mode="json") for r in ps.round2.responses],
                   "case_values": (self._case or {}).get("values"), "module": self._module}
        out: S.Round3Output = self._call(client, "B", P.AGENT_B_SYSTEM, user, S.Round3Output, "round3", payload)

        missing = self._missing(ps.issue_ids, [v.issue_id for v in out.verdicts])
        no_fix = [v.issue_id for v in out.verdicts if v.risk_level != S.RiskLevel.LOW and not v.solutions]
        if missing or no_fix:
            ask = []
            if missing:
                ask.append(f"上次輸出遺漏 {', '.join(missing)}，請補齊全部疑義的判定。")
            if no_fix:
                ask.append(f"{', '.join(no_fix)} 為中風險或高風險，必須提出具體解決方案（solutions）。")
            retry = self._call(client, "B", P.AGENT_B_SYSTEM, user + "\n\n# 注意\n" + "\n".join(ask),
                               S.Round3Output, "round3", payload)
            got = {v.issue_id: v for v in retry.verdicts}
            out.verdicts = [got[v.issue_id] if v.issue_id in no_fix and got.get(v.issue_id, v).solutions else v
                            for v in out.verdicts]
            out = self._merge(out, retry, "verdicts")
            for qid in self._missing(ps.issue_ids, [v.issue_id for v in out.verdicts]):
                out.verdicts.append(S.FinalVerdict(
                    issue_id=qid, decision=S.Decision.PARTIAL, risk_level=S.RiskLevel.MEDIUM,
                    rationale="（外審未提出終審判定，列為待複核）"))
                s.notes.append(f"第 {ps.no} 輪 Agent B 未判定 {qid}，系統列為待複核。")
            for v in out.verdicts:
                if v.risk_level != S.RiskLevel.LOW and not v.solutions:
                    s.notes.append(f"第 {ps.no} 輪 Agent B 未就 {v.issue_id}（{v.risk_level.value}）提出解決方案。")
        out.verdicts = self._ordered(out.verdicts, ps.issue_ids)
        for v in out.verdicts:
            if not 0 <= v.recommended_solution <= len(v.solutions):
                v.recommended_solution = 1 if v.solutions else 0
            if v.solutions and v.recommended_solution == 0:
                v.recommended_solution = 1
            for n, sol in enumerate(v.solutions, 1):
                for c in sol.changes:
                    why = c.param_id and rcmd.validate_change(self._module, c.param_id, c.proposed, self._case)
                    if why:
                        s.notes.append(f"第 {ps.no} 輪 {v.issue_id} 方案 {n}：{why}（建議值「{c.proposed}」無法直接套用）。")
        ps.round3 = out
        self._cite(s, ps, lib, "外審", [x for v in out.verdicts for x in [v.rationale, *v.check_formulas]])
        titles = {i.issue_id: i.title for i in ps.round1.issues}
        for v in out.verdicts:
            parts = [
                f"**判定**：{v.decision.value}（{DECISION_MEANING[v.decision]}）",
                f"**風險等級**：{v.risk_level.value}（{RISK_MEANING[v.risk_level]}）",
                f"**判定理由**：{v.rationale}",
            ]
            if v.check_formulas:
                parts.append(f"**覆核算式**：\n\n{tex_block(v.check_formulas)}")
            if v.required_action:
                parts.append(f"**應辦事項**：{v.required_action}")
            for n, sol in enumerate(v.solutions, 1):
                parts.append(solution_markdown(sol, n, recommended=n == v.recommended_solution))
            self._say(s, "B", "round3", f"終審 {v.issue_id}　{titles[v.issue_id]}", "\n\n".join(parts), ps.no,
                      client.model)

    def chief(self, s: ReviewSession, ps: PassResult, client: LLMClient, lib: _PassLibrary) -> None:
        self.on_progress("chief", f"第 {ps.no} 輪｜彙整｜{AGENT_NAMES['C']} 裁決與複核方案中…")
        user = P.CHIEF_USER.format(issue_ids="、".join(ps.issue_ids), clauses=lib.text(), debate=format_debate(ps),
                                   params=rcmd.catalog_text(self._module, self._case),
                                   schema=schema_text(S.ChiefReport), **self._common())
        payload = {"issues": [i.model_dump(mode="json") for i in ps.round1.issues],
                   "verdicts": [v.model_dump(mode="json") for v in ps.round3.verdicts]}
        report: S.ChiefReport = self._call(client, "C", P.AGENT_C_SYSTEM, user, S.ChiefReport, "chief", payload)
        ps.chief = self._normalize_report(report, s, ps)
        c = ps.chief
        lines = [f"**本輪結論**：{c.overall_conclusion.value}", c.executive_summary]
        for label, meaning, items in (
            ("重大疑義（高風險）", "不符規範或影響結構安全，須修正後重新送審", c.major_doubts),
            ("待複核項目（中風險）", "需補充資料或修正後再確認", c.pending_review_items),
            ("無虞項目（低風險）", "疑義已解除", c.no_concern_items),
        ):
            if items:
                lines.append(f"**{label}**（{meaning}）：\n" + "\n".join(
                    f"- {i.issue_id}　{i.title}：{i.recommendation}"
                    + (f"（採用方案 {i.adopted_solution}：{ps.adopted(i.issue_id).label}）" if ps.adopted(i.issue_id) else "")
                    for i in items))
        self._say(s, "C", "chief", f"第 {ps.no} 輪裁決", "\n\n".join(lines), ps.no, client.model)

    # ------------------------------------------------------------------
    def final(self, s: ReviewSession) -> None:
        """綜合裁決：第 1 輪主任技師所用之模型，以全新對話彙整各輪結果。"""
        slot = self.roles_for(1)["C"]
        client = self._client(s, slot, 0, "F")
        s.final_model = f"{client.provider} / {client.model}"
        self.on_progress("final", f"綜合裁決｜主任檢核技師 彙整 {len(s.passes)} 輪結果中…")
        lib = _PassLibrary(self.lib)
        blocks, payload = [], []
        for ps in s.passes:
            items = []
            for it in ps.chief_items():
                sol = ps.adopted(it.issue_id)
                issue = next(i for i in ps.round1.issues if i.issue_id == it.issue_id)
                verdict = next(v for v in ps.round3.verdicts if v.issue_id == it.issue_id)
                for code, cid in self.lib.find_refs(" ".join(issue.code_clauses)):
                    lib.add([c for c in [self.lib.get(code, cid)] if c])
                items.append({"issue_id": it.issue_id, "title": it.title, "risk_level": it.risk_level.value,
                              "finding": it.finding, "recommendation": it.recommendation,
                              "code_clauses": issue.code_clauses, "solution_check": it.solution_check,
                              "solution": sol.model_dump(mode="json") if sol else None,
                              "other_solutions": [x.label + "｜" + x.change_extent for n, x in enumerate(verdict.solutions, 1)
                                                  if n != it.adopted_solution]})
            payload.append({"pass": ps.no, "conclusion": ps.chief.overall_conclusion.value, "items": items})
            blocks.append(f"### 第 {ps.no} 輪（結論：{ps.chief.overall_conclusion.value}）\n"
                          + json.dumps(items, ensure_ascii=False, indent=1))
        roles = "\n".join(f"- 第 {ps.no} 輪：" + "；".join(f"{AGENT_NAMES[r]}＝{m}" for r, m in ps.roles.items())
                          for ps in s.passes)
        user = P.FINAL_USER.format(n_passes=len(s.passes), roles=roles, facts=self._calc.facts_text(),
                                   tool_url=rcmd.TOOL_URL, params=rcmd.catalog_text(self._module, self._case),
                                   clauses=lib.text(), passes="\n\n".join(blocks),
                                   schema=schema_text(S.ConsolidatedReport))
        out: S.ConsolidatedReport = client.generate(P.FINAL_SYSTEM, user, S.ConsolidatedReport,
                                                    temperature=TEMPERATURE["F"], task="final",
                                                    payload={"passes": payload}, attachments=self._att)
        s.final = self._normalize_final(out, s)
        self._apply_case(s)
        f = s.final
        lines = [f"**綜合結論**：{f.overall_conclusion.value}", f.executive_summary]
        if f.cross_pass_notes:
            lines.append(f"**各輪一致性**：{f.cross_pass_notes}")
        for n, it in enumerate(f.items, 1):
            body = f"**{n}. {it.title}**（{it.risk_level.value}；來源 {'、'.join(it.source_ids)}）：{it.finding}"
            if it.final_solution:
                body += "\n\n" + solution_markdown(it.final_solution, None, recommended=True)
            if it.why_minimal:
                body += f"\n\n**為何改動最小**：{it.why_minimal}"
            lines.append(body)
        if f.tool_summary:
            lines.append(f"**rc-member-designer 調整總結**：{f.tool_summary}")
        if s.adjusted_applied:
            lines.append("**已寫入調整後案件檔**：\n" + "\n".join(f"- {x}" for x in s.adjusted_applied))
        self._say(s, "F", "final", "綜合裁決", "\n\n".join(lines), 0, client.model)

    # ------------------------------------------------------------------
    def _normalize_report(self, report: S.ChiefReport, s: ReviewSession, ps: PassResult) -> S.ChiefReport:
        """確保每個疑義只出現在一個分類，且風險等級與總結論和分類一致。"""
        tag = f"第 {ps.no} 輪"
        placed: dict[str, tuple[str, S.ReportItem]] = {}
        for bucket, risk in BUCKET_RISK.items():
            for item in getattr(report, bucket):
                if item.issue_id not in ps.issue_ids:
                    s.notes.append(f"{tag} Agent C 輸出未知編號 {item.issue_id}，已忽略。")
                    continue
                prev = placed.get(item.issue_id)
                if prev and RISK_ORDER[BUCKET_RISK[prev[0]]] >= RISK_ORDER[risk]:
                    s.notes.append(f"{tag} {item.issue_id} 重複分類，保留較嚴重之「{prev[0]}」。")
                    continue
                if prev:
                    s.notes.append(f"{tag} {item.issue_id} 重複分類，保留較嚴重之「{bucket}」。")
                item.risk_level = risk
                placed[item.issue_id] = (bucket, item)

        issues = {i.issue_id: i for i in ps.round1.issues}
        verdicts = {v.issue_id: v for v in ps.round3.verdicts}
        risk_bucket = {v: k for k, v in BUCKET_RISK.items()}
        for qid in ps.issue_ids:
            if qid in placed:
                continue
            v = verdicts[qid]
            bucket = risk_bucket[v.risk_level]
            placed[qid] = (bucket, S.ReportItem(
                issue_id=qid, title=issues[qid].title, risk_level=v.risk_level, finding=v.rationale,
                basis="、".join(issues[qid].code_clauses), recommendation=v.required_action or "無",
                adopted_solution=v.recommended_solution))
            s.notes.append(f"{tag} Agent C 未分類 {qid}，依外審終審風險等級歸入「{bucket}」。")

        for qid, (_, item) in placed.items():
            n = len(verdicts[qid].solutions)
            if not 0 <= item.adopted_solution <= n:
                s.notes.append(f"{tag} {qid} 採用方案序號 {item.adopted_solution} 不存在，改採外審建議方案。")
                item.adopted_solution = verdicts[qid].recommended_solution

        for bucket in BUCKET_RISK:
            setattr(report, bucket, [item for qid in ps.issue_ids for b, item in [placed[qid]] if b == bucket])

        expected = (
            S.OverallConclusion.RETURNED if report.major_doubts
            else S.OverallConclusion.CONDITIONAL if report.pending_review_items
            else S.OverallConclusion.APPROVED
        )
        if report.overall_conclusion != expected:  # 總結論以分類結果為準
            s.notes.append(f"{tag} 總結論由「{report.overall_conclusion.value}」調整為「{expected.value}」以符合分類結果。")
            report.overall_conclusion = expected
        return report

    def _normalize_final(self, out: S.ConsolidatedReport, s: ReviewSession) -> S.ConsolidatedReport:
        """綜合裁決的保守校正：來源編號有效、風險不低於來源、不遺漏任何中高風險項目、總結論不寬於任一輪。"""
        covered: set[str] = set()
        for it in out.items:
            ids = []
            for sid in it.source_ids:
                sid = sid.strip().upper().replace(" ", "")
                if s.item_risk(sid) is None:
                    s.notes.append(f"綜合裁決引用不存在的來源 {sid}，已忽略。")
                    continue
                ids.append(sid)
            it.source_ids = ids
            covered |= set(ids)
            worst = max((s.item_risk(x) for x in ids), key=RISK_ORDER.get, default=it.risk_level)
            if RISK_ORDER[worst] > RISK_ORDER[it.risk_level]:
                s.notes.append(f"綜合裁決「{it.title}」風險由{it.risk_level.value}提高為{worst.value}（取各輪最嚴者）。")
                it.risk_level = worst
            if it.final_solution is None and it.risk_level != S.RiskLevel.LOW:
                sol = self._source_solution(s, ids)
                if sol:
                    it.final_solution = sol
                    s.notes.append(f"綜合裁決「{it.title}」未指定最終方案，改採來源輪次主任技師採用之方案。")
        out.items = [it for it in out.items if it.source_ids]
        for ps in s.passes:
            for item in ps.chief_items():
                sid = f"P{ps.no}-{item.issue_id}"
                if item.risk_level == S.RiskLevel.LOW or sid in covered:
                    continue
                issue = next(i for i in ps.round1.issues if i.issue_id == item.issue_id)
                out.items.append(S.ConsolidatedItem(
                    title=item.title, source_ids=[sid], risk_level=item.risk_level, finding=item.finding,
                    code_clauses=issue.code_clauses, final_solution=ps.adopted(item.issue_id),
                    required_action=item.recommendation))
                s.notes.append(f"綜合裁決遺漏 {sid}（{item.risk_level.value}），系統依該輪結果補入。")
        out.items.sort(key=lambda it: -RISK_ORDER[it.risk_level])
        expected = (
            S.OverallConclusion.RETURNED if any(i.risk_level == S.RiskLevel.HIGH for i in out.items)
            else S.OverallConclusion.CONDITIONAL if any(i.risk_level == S.RiskLevel.MEDIUM for i in out.items)
            else S.OverallConclusion.APPROVED
        )
        worst_pass = max((ps.chief.overall_conclusion for ps in s.passes), key=CONCLUSION_ORDER.get)
        if CONCLUSION_ORDER[worst_pass] > CONCLUSION_ORDER[expected]:
            expected = worst_pass
        if out.overall_conclusion != expected:
            s.notes.append(f"綜合結論由「{out.overall_conclusion.value}」調整為「{expected.value}」"
                           "（依項目風險等級，且不寬於任一輪結論）。")
            out.overall_conclusion = expected
        return out

    @staticmethod
    def _source_solution(s: ReviewSession, ids: list[str]) -> S.Solution | None:
        ranked = sorted(ids, key=lambda x: -RISK_ORDER[s.item_risk(x)])
        for sid in ranked:
            p, q = sid.lstrip("P").split("-", 1)
            sol = s.passes[int(p) - 1].adopted(q)
            if sol:
                return sol
        return None

    def _apply_case(self, s: ReviewSession) -> None:
        """把最終方案中可對應工具欄位的參數變更寫入案件檔副本。"""
        if not self._case or not s.final:
            return
        chosen: dict[str, tuple[str, str]] = {}
        for it in s.final.items:  # 已依風險由高至低排序：同一欄位有不同建議值時，以較高風險項目為準
            for c in (it.final_solution.changes if it.final_solution else []):
                if not c.param_id:
                    continue
                if c.param_id in chosen and chosen[c.param_id][0] != c.proposed:
                    s.notes.append(f"「{c.param_id}」在多個項目有不同建議值（{chosen[c.param_id][0]}／{c.proposed}），"
                                   f"案件檔採「{chosen[c.param_id][1]}」之 {chosen[c.param_id][0]}，請於工具中重算確認。")
                    continue
                chosen[c.param_id] = (c.proposed, it.title)
        if not chosen:
            return
        new, applied, rejected = rcmd.apply_changes(self._case, [(k, v) for k, (v, _) in chosen.items()])
        s.adjusted_case, s.adjusted_applied, s.adjusted_rejected = new, applied, rejected
        for r in rejected:
            s.notes.append(f"調整後案件檔未套用：{r}")

    @staticmethod
    def _missing(expected: list[str], got: list[str]) -> list[str]:
        return [q for q in expected if q not in set(got)]

    @staticmethod
    def _merge(first, second, attr: str):
        have = {x.issue_id for x in getattr(first, attr)}
        getattr(first, attr).extend(x for x in getattr(second, attr) if x.issue_id not in have)
        return first

    @staticmethod
    def _ordered(items: list, ids: list[str]) -> list:
        """依疑義編號排序、去除重複與未知編號。"""
        by_id = {}
        for x in items:
            by_id.setdefault(x.issue_id, x)
        return [by_id[q] for q in ids if q in by_id]


def solution_markdown(sol: S.Solution, n: int | None, recommended: bool = False) -> str:
    head = f"**{'建議採用｜' if recommended else ''}{sol.label}**" + (f"（方案 {n}）" if n and str(n) not in sol.label else "")
    parts = [head, f"做法：{sol.approach}", f"改動幅度：{sol.change_extent}"]
    if sol.changes:
        rows = ["| rc-member-designer 欄位 | 參數 | 目前值 | 建議值 |", "|---|---|---|---|"]
        rows += [f"| `{c.param_id or '—'}` | {c.label} | {c.current} | **{c.proposed}** {c.unit} |" for c in sol.changes]
        parts.append("\n".join(rows))
    parts.append("修正後驗算：\n\n" + tex_block(sol.verification))
    if sol.tool_steps:
        parts.append(f"工具操作：{sol.tool_steps}")
    if sol.other_method:
        parts.append(f"其他作法：{sol.other_method}")
    return "\n\n".join(parts)
