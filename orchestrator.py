"""核心調度器：固定三回合的雙 Agent 詰問 + 主任技師彙整。

流程（不做開放式迴圈，回合數固定）：
    Round 1  Agent B 初審質疑（3～5 項）
    Round 2  Agent A 逐項回覆抗辯
    Round 3  Agent B 覆核終審（接受／部分接受／駁回 + 風險等級）
    彙整     Agent C 裁決，輸出無虞／重大疑義／待複核

每一回合結束後做一致性檢查（疑義編號是否全數回覆、分類是否不重不漏），
缺漏時補問一次，仍缺漏則以保守規則補齊並記錄於 notes，確保報告完整。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from agents import prompts as P
from agents import schemas as S
from agents.llm_client import LLMClient, schema_text
from utils.parser import StructuralSummary

AGENT_NAMES = {
    "A": "Agent A｜原設計工程師",
    "B": "Agent B｜外部審查專家",
    "C": "Agent C｜主任檢核技師",
}
TEMPERATURE = {"A": 0.4, "B": 0.3, "C": 0.1}
RISK_ORDER = {S.RiskLevel.PASS: 0, S.RiskLevel.MEDIUM: 1, S.RiskLevel.HIGH: 2}
BUCKET_RISK = {
    "no_concern_items": S.RiskLevel.PASS,
    "pending_review_items": S.RiskLevel.MEDIUM,
    "major_doubts": S.RiskLevel.HIGH,
}

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
    S.RiskLevel.PASS: "無安全疑慮",
    S.RiskLevel.MEDIUM: "需補充資料或修正後再確認",
    S.RiskLevel.HIGH: "不符規範或影響結構安全，必須修正",
}

ProgressCallback = Callable[[str, str], None]
MessageCallback = Callable[["DialogueMessage"], None]


@dataclass
class DialogueMessage:
    """審查對話中的一則發言（以疑義為單位），供介面即時顯示與報告保存。"""

    agent: str  # A／B／C
    stage: str  # round1／round2／round3／chief
    title: str
    body: str  # Markdown

    @property
    def speaker(self) -> str:
        return AGENT_NAMES[self.agent]

    def to_dict(self) -> dict:
        return {"agent": self.agent, "speaker": self.speaker, "stage": self.stage,
                "title": self.title, "body": self.body}


@dataclass
class ReviewSession:
    project_info: dict
    data_text: str
    findings: list[dict]
    provider: str = ""
    model: str = ""
    source: dict = field(default_factory=dict)  # 輸入檔名、雜湊、工作表與辨識欄位、初篩門檻
    started_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))
    round1: S.Round1Output | None = None
    round2: S.Round2Output | None = None
    round3: S.Round3Output | None = None
    chief: S.ChiefReport | None = None
    notes: list[str] = field(default_factory=list)
    transcript: list[DialogueMessage] = field(default_factory=list)

    @property
    def issue_ids(self) -> list[str]:
        return [i.issue_id for i in self.round1.issues] if self.round1 else []

    def threads(self) -> list[dict]:
        """以疑義為單位整理三回合內容，供 UI 與報告使用。"""
        resp = {r.issue_id: r for r in (self.round2.responses if self.round2 else [])}
        verd = {v.issue_id: v for v in (self.round3.verdicts if self.round3 else [])}
        return [
            {"issue": i, "response": resp.get(i.issue_id), "verdict": verd.get(i.issue_id)}
            for i in (self.round1.issues if self.round1 else [])
        ]

    def to_dict(self) -> dict:
        dump = lambda m: m.model_dump(mode="json") if m else None  # noqa: E731
        return {
            "project_info": self.project_info,
            "provider": self.provider,
            "model": self.model,
            "started_at": self.started_at,
            "source": self.source,
            "prechecks": self.findings,
            "round1_challenges": dump(self.round1),
            "round2_responses": dump(self.round2),
            "round3_verdicts": dump(self.round3),
            "chief_report": dump(self.chief),
            "transcript": [msg.to_dict() for msg in self.transcript],
            "notes": self.notes,
        }


def format_debate(session: ReviewSession, include_verdicts: bool = True) -> str:
    blocks = []
    for t in session.threads():
        i, r, v = t["issue"], t["response"], t["verdict"]
        lines = [
            f"### {i.issue_id} {i.title}（{i.category}）",
            f"- [B 質疑] {i.challenge}",
            f"  - 引用數據：{i.cited_data}",
            f"  - 算式：{i.formula}",
            f"  - 依據：{i.code_reference}",
        ]
        if r:
            lines += [
                f"- [A 回覆｜{r.stance.value}] {r.response}",
                f"  - 計算：{r.calculation or '—'}",
                f"  - 數據出處：{r.data_source}；規範依據：{r.code_reference}",
            ]
            if r.proposed_action:
                lines.append(f"  - 修正方案：{r.proposed_action}")
        if include_verdicts and v:
            lines.append(
                f"- [B 終審｜{v.decision.value}｜{v.risk_level.value}] {v.rationale}"
                + (f"；應辦事項：{v.required_action}" if v.required_action else "")
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


class ReviewOrchestrator:
    def __init__(
        self,
        client: LLMClient | dict[str, LLMClient],
        on_progress: ProgressCallback | None = None,
        on_message: MessageCallback | None = None,
    ):
        """client 可為單一 LLMClient（三個 Agent 共用），或 {"A":…, "B":…, "C":…} 分別指定。"""
        if isinstance(client, dict):
            self.clients = client
        else:
            self.clients = {"A": client, "B": client, "C": client}
        self.on_progress = on_progress or (lambda stage, msg: None)
        self.on_message = on_message or (lambda msg: None)

    def _say(self, s: ReviewSession, agent: str, stage: str, title: str, body: str) -> None:
        msg = DialogueMessage(agent, stage, title, body)
        s.transcript.append(msg)
        self.on_message(msg)

    # ------------------------------------------------------------------
    def run(self, summary: StructuralSummary, project_info: dict | None = None) -> ReviewSession:
        info = project_info or {}
        c = self.clients["B"]
        session = ReviewSession(
            project_info=info,
            data_text=summary.to_prompt_text(info),
            findings=[f.as_dict() for f in summary.findings],
            provider=c.provider,
            model=c.model,
            source={
                "file": summary.source_name,
                "sha256": summary.sha256,
                "sheets": [
                    {"name": sh.name, "rows": sh.n_rows, "mapped": sh.mapped} for sh in summary.sheets
                ],
                "limits": summary.limits,
            },
        )
        self.round1(session)
        self.round2(session)
        self.round3(session)
        self.chief(session)
        self.on_progress("done", "審查完成")
        return session

    def _call(self, agent: str, system: str, user: str, schema, task: str, payload: dict):
        return self.clients[agent].generate(
            system, user, schema, temperature=TEMPERATURE[agent], task=task, payload=payload,
        )

    # ------------------------------------------------------------------
    def round1(self, s: ReviewSession) -> None:
        self.on_progress("round1", f"Round 1｜{AGENT_NAMES['B']} 初審質疑中…")
        user = P.ROUND1_USER.format(data=s.data_text, schema=schema_text(S.Round1Output))
        out: S.Round1Output = self._call(
            "B", P.AGENT_B_SYSTEM, user, S.Round1Output, "round1", {"findings": s.findings},
        )
        # 統一編號，避免模型自行編號重複或跳號
        for n, issue in enumerate(out.issues, 1):
            issue.issue_id = f"Q{n}"
        s.round1 = out
        self._say(s, "B", "round1", "初審總體意見",
                  f"{out.summary}\n\n以下共 {len(out.issues)} 項疑義，請設計單位逐項回覆。")
        for i in out.issues:
            self._say(s, "B", "round1", f"{i.issue_id}　{i.title}", "\n\n".join([
                f"**疑義說明**：{i.challenge}",
                f"**檢核算式**：`{i.formula}`",
                f"**引用數據**：{i.cited_data}",
                f"**規範依據**：{i.code_reference}",
            ]))

    def round2(self, s: ReviewSession) -> None:
        self.on_progress("round2", f"Round 2｜{AGENT_NAMES['A']} 逐項回覆中…")
        issues_json = json.dumps(
            [i.model_dump(mode="json") for i in s.round1.issues], ensure_ascii=False, indent=1
        )
        user = P.ROUND2_USER.format(
            data=s.data_text, issues=issues_json, schema=schema_text(S.Round2Output)
        )
        payload = {"issues": [i.model_dump(mode="json") for i in s.round1.issues]}
        out: S.Round2Output = self._call("A", P.AGENT_A_SYSTEM, user, S.Round2Output, "round2", payload)

        missing = self._missing(s.issue_ids, [r.issue_id for r in out.responses])
        if missing:
            retry = self._call(
                "A", P.AGENT_A_SYSTEM,
                user + f"\n\n# 注意\n上次輸出遺漏 {', '.join(missing)}，請補齊全部疑義的回覆。",
                S.Round2Output, "round2", payload,
            )
            out = self._merge(out, retry, "responses")
            for qid in self._missing(s.issue_ids, [r.issue_id for r in out.responses]):
                out.responses.append(S.DesignerResponse(
                    issue_id=qid, stance=S.Stance.ADMIT,
                    response="（設計單位未就本項提出回覆，視同未能抗辯）",
                    data_source="—", code_reference="—",
                ))
                s.notes.append(f"Agent A 未回覆 {qid}，系統以「未能抗辯」補記。")
        out.responses = self._ordered(out.responses, s.issue_ids)
        s.round2 = out
        titles = {i.issue_id: i.title for i in s.round1.issues}
        for r in out.responses:
            parts = [f"**立場**：{r.stance.value}（{STANCE_MEANING[r.stance]}）", f"**回覆說明**：{r.response}"]
            if r.calculation:
                parts.append(f"**計算推導**：`{r.calculation}`")
            if r.proposed_action:
                parts.append(f"**修正方案**：{r.proposed_action}")
            parts.append(f"**數據出處**：{r.data_source}　**規範依據**：{r.code_reference}")
            self._say(s, "A", "round2", f"回覆 {r.issue_id}　{titles[r.issue_id]}", "\n\n".join(parts))

    def round3(self, s: ReviewSession) -> None:
        self.on_progress("round3", f"Round 3｜{AGENT_NAMES['B']} 覆核終審中…")
        user = P.ROUND3_USER.format(
            data=s.data_text, debate=format_debate(s, include_verdicts=False),
            schema=schema_text(S.Round3Output),
        )
        payload = {"responses": [r.model_dump(mode="json") for r in s.round2.responses]}
        out: S.Round3Output = self._call("B", P.AGENT_B_SYSTEM, user, S.Round3Output, "round3", payload)

        missing = self._missing(s.issue_ids, [v.issue_id for v in out.verdicts])
        if missing:
            retry = self._call(
                "B", P.AGENT_B_SYSTEM,
                user + f"\n\n# 注意\n上次輸出遺漏 {', '.join(missing)}，請補齊全部疑義的判定。",
                S.Round3Output, "round3", payload,
            )
            out = self._merge(out, retry, "verdicts")
            for qid in self._missing(s.issue_ids, [v.issue_id for v in out.verdicts]):
                out.verdicts.append(S.FinalVerdict(
                    issue_id=qid, decision=S.Decision.PARTIAL, risk_level=S.RiskLevel.MEDIUM,
                    rationale="（外審未提出終審判定，列為待複核）",
                ))
                s.notes.append(f"Agent B 未判定 {qid}，系統列為待複核。")
        out.verdicts = self._ordered(out.verdicts, s.issue_ids)
        s.round3 = out
        titles = {i.issue_id: i.title for i in s.round1.issues}
        for v in out.verdicts:
            parts = [
                f"**判定**：{v.decision.value}（{DECISION_MEANING[v.decision]}）",
                f"**風險等級**：{v.risk_level.value}（{RISK_MEANING[v.risk_level]}）",
                f"**判定理由**：{v.rationale}",
            ]
            if v.required_action:
                parts.append(f"**應辦事項**：{v.required_action}")
            self._say(s, "B", "round3", f"終審 {v.issue_id}　{titles[v.issue_id]}", "\n\n".join(parts))

    def chief(self, s: ReviewSession) -> None:
        self.on_progress("chief", f"彙整｜{AGENT_NAMES['C']} 裁決中…")
        user = P.CHIEF_USER.format(
            issue_ids="、".join(s.issue_ids), data=s.data_text,
            debate=format_debate(s), schema=schema_text(S.ChiefReport),
        )
        payload = {
            "issues": [i.model_dump(mode="json") for i in s.round1.issues],
            "verdicts": [v.model_dump(mode="json") for v in s.round3.verdicts],
        }
        report: S.ChiefReport = self._call("C", P.AGENT_C_SYSTEM, user, S.ChiefReport, "chief", payload)
        s.chief = self._normalize_report(report, s)
        c = s.chief
        lines = [f"**總結論**：{c.overall_conclusion.value}", c.executive_summary]
        for label, meaning, items in (
            ("重大疑義", "不符規範或影響結構安全，須修正後重新送審", c.major_doubts),
            ("待複核項目", "需補充資料或修正後再確認", c.pending_review_items),
            ("無虞項目", "疑義已解除", c.no_concern_items),
        ):
            if items:
                lines.append(f"**{label}**（{meaning}）：\n" + "\n".join(
                    f"- {i.issue_id}　{i.title}：{i.recommendation}" for i in items))
        self._say(s, "C", "chief", "最終裁決", "\n\n".join(lines))

    # ------------------------------------------------------------------
    def _normalize_report(self, report: S.ChiefReport, s: ReviewSession) -> S.ChiefReport:
        """確保每個疑義只出現在一個分類，且風險等級與總結論和分類一致。"""
        placed: dict[str, tuple[str, S.ReportItem]] = {}
        for bucket, risk in BUCKET_RISK.items():
            for item in getattr(report, bucket):
                if item.issue_id not in s.issue_ids:
                    s.notes.append(f"Agent C 輸出未知編號 {item.issue_id}，已忽略。")
                    continue
                prev = placed.get(item.issue_id)
                if prev and RISK_ORDER[BUCKET_RISK[prev[0]]] >= RISK_ORDER[risk]:
                    s.notes.append(f"{item.issue_id} 重複分類，保留較嚴重之「{prev[0]}」。")
                    continue
                if prev:
                    s.notes.append(f"{item.issue_id} 重複分類，保留較嚴重之「{bucket}」。")
                item.risk_level = risk
                placed[item.issue_id] = (bucket, item)

        issues = {i.issue_id: i for i in s.round1.issues}
        verdicts = {v.issue_id: v for v in s.round3.verdicts}
        risk_bucket = {v: k for k, v in BUCKET_RISK.items()}
        for qid in s.issue_ids:
            if qid in placed:
                continue
            v = verdicts[qid]
            bucket = risk_bucket[v.risk_level]
            placed[qid] = (bucket, S.ReportItem(
                issue_id=qid, title=issues[qid].title, risk_level=v.risk_level,
                finding=v.rationale, basis=issues[qid].code_reference,
                recommendation=v.required_action or "無",
            ))
            s.notes.append(f"Agent C 未分類 {qid}，依外審終審風險等級歸入「{bucket}」。")

        for bucket in BUCKET_RISK:
            setattr(report, bucket, [
                item for qid in s.issue_ids
                for b, item in [placed[qid]] if b == bucket
            ])

        expected = (
            S.OverallConclusion.RETURNED if report.major_doubts
            else S.OverallConclusion.CONDITIONAL if report.pending_review_items
            else S.OverallConclusion.APPROVED
        )
        # 總結論以分類結果為準，避免「有重大疑義卻同意備查」之類的矛盾
        if report.overall_conclusion != expected:
            s.notes.append(
                f"總結論由「{report.overall_conclusion.value}」調整為「{expected.value}」以符合分類結果。"
            )
            report.overall_conclusion = expected
        return report

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
