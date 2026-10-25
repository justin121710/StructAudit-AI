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
    # 對話紀錄：B 初審意見＋逐項質疑 → A 逐項回覆 → B 逐項終審 → C 裁決
    n = len(s.issue_ids)
    assert [(x.agent, x.stage) for x in s.transcript] == (
        [("B", "round1")] * (n + 1) + [("A", "round2")] * n + [("B", "round3")] * n + [("C", "chief")]
    )
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


def test_model_filters_keep_text_models_only():
    from agents.llm_client import filter_gemini_models, filter_openai_models

    ids = ["gpt-4.1", "gpt-4o-mini", "o3", "text-embedding-3-large", "whisper-1", "dall-e-3",
           "gpt-4o-realtime-preview", "gpt-3.5-turbo-instruct", "tts-1", "gpt-4.1"]
    assert filter_openai_models(ids) == ["o3", "gpt-4o-mini", "gpt-4.1"]
    models = [("models/gemini-2.5-pro", ["generateContent", "countTokens"]),
              ("models/gemini-2.5-flash", ["generateContent"]),
              ("models/text-embedding-004", ["embedContent"]),
              ("models/imagen-3.0-generate-002", ["predict"]),
              ("models/gemini-2.5-flash-preview-tts", ["generateContent"])]
    assert filter_gemini_models(models) == ["gemini-2.5-pro", "gemini-2.5-flash"]


def test_per_agent_models_recorded_in_report(summary):
    from utils.report_model import build_model

    shared = MockClient()  # 示範 client 需共用狀態；這裡只改模型名稱來模擬三個不同模型
    clients = {}
    for agent, name in (("A", "gemini-2.5-flash"), ("B", "gemini-2.5-pro"), ("C", "gpt-4.1")):
        c = MockClient()
        c.model, c._severity, c._source = name, shared._severity, shared._source
        clients[agent] = c
    s = ReviewOrchestrator(clients).run(summary)
    assert s.agent_models == {"A": "mock / gemini-2.5-flash", "B": "mock / gemini-2.5-pro", "C": "mock / gpt-4.1"}
    meta = {k: v for k, v, _ in build_model(s).meta}
    assert "A 原設計工程師：mock / gemini-2.5-flash" in meta["語言模型"]
    assert "C 主任檢核技師：mock / gpt-4.1" in meta["語言模型"]


def test_claude_client_request_shape(monkeypatch):
    from types import SimpleNamespace as NS

    from agents.llm_client import ClaudeClient

    calls = []

    def reply(stop="end_turn", text='{"ok": 1}'):
        return NS(stop_reason=stop, stop_details=NS(category="cyber") if stop == "refusal" else None,
                  content=[NS(type="thinking", thinking=""), NS(type="text", text=text)])

    class FakeMessages:
        def __init__(self, beta):
            self.beta = beta

        def create(self, **kw):
            calls.append((self.beta, kw))
            return FakeMessages.next_reply

    fake = NS(messages=FakeMessages(False), beta=NS(messages=FakeMessages(True)))
    c = ClaudeClient("claude-opus-5-5", api_key="k")
    c._client = fake
    FakeMessages.next_reply = reply()
    assert c._complete("sys", "user", 0.3) == '{"ok": 1}'
    beta, kw = calls[-1]
    assert beta and kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert "temperature" not in kw and kw["system"] == "sys" and kw["max_tokens"] == 16000

    c.model = "claude-haiku-4-5"  # 不支援備援的模型改走一般端點
    c._complete("sys", "user", 0.3)
    beta, kw = calls[-1]
    assert not beta and "fallbacks" not in kw

    FakeMessages.next_reply = reply("refusal")
    with pytest.raises(LLMError, match="拒絕"):
        c._complete("sys", "user", 0.3)
