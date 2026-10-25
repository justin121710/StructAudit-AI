import json

import pytest

from agents import schemas as S
from agents.llm_client import LLMClient, LLMError, MockClient, extract_json
from orchestrator import ReviewOrchestrator
from utils.parser import parse_structural_file
from utils.pdf_generator import generate_pdf, render_html


@pytest.fixture(scope="module")
def summary():
    return parse_structural_file("examples/sample_structure.xlsx")


class ScriptedClient(LLMClient):
    """依序回傳預先寫好的文字，用來模擬模型的各種不良輸出。"""

    provider = "mock"

    def __init__(self, replies):
        super().__init__("scripted", max_retries=1)
        self.replies = list(replies)
        self.prompts = []

    def _complete(self, system, user, temperature):
        self.prompts.append(user)
        return self.replies.pop(0)


def issue(n):
    return {"issue_id": f"X{n}", "title": f"疑義{n}", "category": "其他", "cited_data": "d",
            "challenge": "c", "formula": "f", "code_reference": "r"}


def test_extract_json_tolerates_fences():
    assert extract_json('說明\n```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('結果如下：{"a": {"b": 2}} 以上') == {"a": {"b": 2}}


def test_validation_error_is_fed_back_and_retried():
    bad = json.dumps({"summary": "s", "issues": [issue(1)]})  # 少於 3 項
    good = json.dumps({"summary": "s", "issues": [issue(1), issue(2), issue(3)]})
    c = ScriptedClient([bad, good])
    out = c.generate("sys", "user", S.Round1Output)
    assert len(out.issues) == 3
    assert "未通過格式驗證" in c.prompts[1]


def test_retry_exhausted_raises():
    c = ScriptedClient(["not json", "still not json"])
    with pytest.raises(LLMError):
        c.generate("sys", "user", S.Round1Output)


def test_mock_pipeline_end_to_end(summary):
    events = []
    s = ReviewOrchestrator(MockClient(), on_progress=lambda a, b: events.append(a)).run(summary)
    assert events == ["round1", "round2", "round3", "chief", "done"]
    assert 3 <= len(s.issue_ids) <= 5
    assert len(s.round2.responses) == len(s.round3.verdicts) == len(s.issue_ids)
    placed = [i.issue_id for b in (s.chief.no_concern_items, s.chief.major_doubts,
                                   s.chief.pending_review_items) for i in b]
    assert sorted(placed) == sorted(s.issue_ids)
    assert s.chief.overall_conclusion == S.OverallConclusion.RETURNED
    json.dumps(s.to_dict(), ensure_ascii=False)


def test_orchestrator_repairs_inconsistent_outputs(summary):
    r1 = {"summary": "s", "issues": [issue(1), issue(2), issue(3)]}
    resp = lambda q: {"issue_id": q, "stance": "規範依據抗辯", "response": "r",  # noqa: E731
                      "data_source": "d", "code_reference": "c"}
    r2_partial = {"responses": [resp("Q1"), resp("Q2")]}
    r2_retry = {"responses": [resp("Q2")]}  # 補問仍未回覆 Q3
    verdict = lambda q, risk: {"issue_id": q, "decision": "接受", "risk_level": risk,  # noqa: E731
                               "rationale": "ok"}
    r3 = {"verdicts": [verdict("Q1", "Pass"), verdict("Q2", "Medium"), verdict("Q3", "High")]}
    item = lambda q: {"issue_id": q, "title": "t", "risk_level": "Pass", "finding": "f",  # noqa: E731
                      "basis": "b", "recommendation": "r"}
    chief = {
        "overall_conclusion": "同意備查",  # 與分類矛盾，應被修正
        "executive_summary": "e",
        "no_concern_items": [item("Q1"), item("Q2")],
        "major_doubts": [item("Q2"), item("Q99")],  # Q2 重複、Q99 不存在、Q3 遺漏
        "pending_review_items": [],
    }
    client = ScriptedClient([json.dumps(x, ensure_ascii=False)
                             for x in (r1, r2_partial, r2_retry, r3, chief)])
    s = ReviewOrchestrator(client).run(summary)

    assert s.issue_ids == ["Q1", "Q2", "Q3"]  # 統一重新編號
    assert s.round2.responses[2].stance == S.Stance.ADMIT  # 未回覆者補記為未能抗辯
    assert [i.issue_id for i in s.chief.no_concern_items] == ["Q1"]
    assert [i.issue_id for i in s.chief.major_doubts] == ["Q2", "Q3"]
    assert all(i.risk_level == S.RiskLevel.HIGH for i in s.chief.major_doubts)
    assert s.chief.overall_conclusion == S.OverallConclusion.RETURNED
    assert len(s.notes) >= 4


@pytest.mark.parametrize("engine", ["weasyprint", "reportlab"])
def test_pdf_generation(summary, engine):
    if engine == "weasyprint":
        pytest.importorskip("weasyprint")
    s = ReviewOrchestrator(MockClient()).run(summary, {"案名": "測試案"})
    assert "測試案" in render_html(s)
    pdf, used = generate_pdf(s, engine)
    assert used == engine and pdf.startswith(b"%PDF")
