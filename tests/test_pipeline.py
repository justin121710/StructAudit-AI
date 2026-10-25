import json
from types import SimpleNamespace as NS

import pytest

from agents import schemas as S
from agents.llm_client import Attachment, LLMClient, LLMError, MockClient, default_model, extract_json
from orchestrator import ReviewOrchestrator
from utils.calcbook import read_calcbook
from utils.pdf_generator import generate_pdf, render_html
from utils.rcmd import load_case


@pytest.fixture(scope="module")
def calc():
    return read_calcbook("examples/sample_C1_calc.pdf")


@pytest.fixture(scope="module")
def case():
    return load_case(open("examples/sample_C1_case.json", "rb").read())


@pytest.fixture(scope="module")
def session(calc, case):
    return ReviewOrchestrator(MockClient, passes=3).run(calc, case, "sample_C1_case.json")


class ScriptedClient(LLMClient):
    """依序回傳預先寫好的文字，用來模擬模型的各種不良輸出。"""

    provider = "mock"

    def __init__(self, replies):
        super().__init__("scripted", max_retries=1)
        self.replies = list(replies)
        self.prompts = []

    def _complete(self, system, user, temperature, attachments):
        self.prompts.append(user)
        return self.replies.pop(0)


def issue(n, risk="中風險"):
    return {"issue_id": f"X{n}", "title": f"疑義{n}", "category": "其他", "calc_ref": "p1", "cited_data": "d",
            "challenge": "c", "formulas": ["a=b"], "code_clauses": ["土木401-112 §18.4.3.2"], "code_quote": "q",
            "preliminary_risk": risk}


def test_extract_json_tolerates_fences():
    assert extract_json('說明\n```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('結果如下：{"a": {"b": 2}} 以上') == {"a": {"b": 2}}


def test_validation_error_is_fed_back_and_retried():
    bad = json.dumps({"summary": "s", "issues": []})  # 至少 1 項
    good = json.dumps({"summary": "s", "issues": [issue(1)]})
    c = ScriptedClient([bad, good])
    out = c.generate("sys", "user", S.Round1Output)
    assert len(out.issues) == 1 and out.issues[0].preliminary_risk == S.RiskLevel.MEDIUM
    assert "未通過格式驗證" in c.prompts[1]


def test_retry_exhausted_raises():
    c = ScriptedClient(["not json", "still not json"])
    with pytest.raises(LLMError):
        c.generate("sys", "user", S.Round1Output)


def test_risk_levels_are_chinese():
    assert [r.value for r in S.RiskLevel] == ["低風險", "中風險", "高風險"]


def test_mock_pipeline_with_role_rotation(session):
    assert len(session.passes) == 3 and session.final is not None
    for ps in session.passes:
        stages = [(m.agent, m.stage) for m in session.transcript if m.pass_no == ps.no]
        assert stages[0] == ("B", "round1") and stages[-1] == ("C", "chief")
        assert {"round1", "round2", "round3", "chief"} <= {st for _, st in stages}
        assert [i.title for i in ps.round1.issues][:2] == ["柱端圍束筋間距無法滿足", "強柱弱梁檢核不符"]
        assert ps.clause_keys and "01:18.4.3.2" in ps.clause_keys  # Agent 查閱的規範原文
    assert session.transcript[-1].stage == "final" and session.transcript[-1].pass_no == 0
    assert session.final.overall_conclusion == S.OverallConclusion.RETURNED
    assert [i.risk_level for i in session.final.items[:2]] == [S.RiskLevel.HIGH] * 2
    assert session.final.items[0].source_ids == ["P1-Q1", "P2-Q1", "P3-Q1"]
    # 最終方案寫回 rc-member-designer 案件檔
    assert session.adjusted_case["values"]["tieSize"] == "#4" and session.adjusted_case["values"]["barSize"] == "#9"
    assert "$$" in next(m.body for m in session.transcript if m.stage == "round1" and m.title.startswith("Q1"))
    json.dumps(session.to_dict(), ensure_ascii=False)


def test_rotation_assigns_each_model_every_role():
    roles = [ReviewOrchestrator.roles_for(p) for p in (1, 2, 3)]
    assert roles[0] == {"A": 0, "B": 1, "C": 2}
    for r in "ABC":
        assert sorted(x[r] for x in roles) == [0, 1, 2]


class TaggedMock(MockClient):
    """每個 client 實例在疑義標題加上自己的標記，並記下收到的每一個提示，用來檢查各輪是否共用記憶。"""

    log: list = []
    count = 0

    def __init__(self, slot):
        super().__init__(f"model-{slot}")
        TaggedMock.count += 1
        self.tag = f"<inst{TaggedMock.count}>"

    def generate(self, system, user, schema, *, task="", payload=None, **kw):
        TaggedMock.log.append((self.tag, task, user))
        out = super().generate(system, user, schema, task=task, payload=payload, **kw)
        if task == "round1":
            for i in out.issues:
                i.title += self.tag
        return out


def test_passes_do_not_share_memory(calc):
    TaggedMock.log, TaggedMock.count = [], 0
    factories = [lambda n=n: TaggedMock(n) for n in range(3)]
    s = ReviewOrchestrator(factories, passes=2).run(calc)
    assert s.model_slots == ["mock / model-0", "mock / model-1", "mock / model-2"]
    assert s.passes[1].roles == {"A": "mock / model-1", "B": "mock / model-2", "C": "mock / model-0"}
    tag1 = s.passes[0].round1.issues[0].title[-7:]
    tag2 = s.passes[1].round1.issues[0].title[-7:]
    assert tag1 != tag2
    calls = [(tag, task, user) for tag, task, user in TaggedMock.log]
    pass2_instances = {tag for tag, task, user in calls if tag2 in user or tag == tag2}
    # 第 2 輪的任何一次呼叫都看不到第 1 輪的內容；只有綜合裁決同時看到兩輪
    for tag, task, user in calls:
        if task != "final" and tag in pass2_instances:
            assert tag1 not in user
    final_prompt = next(user for _, task, user in calls if task == "final")
    assert tag1 in final_prompt and tag2 in final_prompt
    # 每一輪每個角色都是新的 client（3 個記錄用＋2 輪×3 角色＋綜合裁決 1 個）
    assert TaggedMock.count == 3 + 6 + 1


def test_orchestrator_repairs_inconsistent_outputs(calc):
    r1 = {"summary": "s", "issues": [issue(1, "低風險"), issue(2), issue(3, "高風險")]}
    resp = lambda q: {"issue_id": q, "stance": "規範依據抗辯", "response": "r", "data_source": "d"}  # noqa: E731
    r2_partial = {"responses": [resp("Q1"), resp("Q2")]}
    r2_retry = {"responses": [resp("Q2")]}  # 補問仍未回覆 Q3
    sol = {"label": "方案 1", "approach": "a", "changes": [{"param_id": "tieSize", "label": "箍筋", "current": "#3",
                                                          "proposed": "#9"}], "verification": ["a=b"], "change_extent": "小"}
    verdict = lambda q, risk, sols=(): {"issue_id": q, "decision": "接受", "risk_level": risk,  # noqa: E731
                                        "rationale": "ok", "solutions": list(sols), "recommended_solution": 7}
    r3 = {"verdicts": [verdict("Q1", "低風險"), verdict("Q2", "中風險"), verdict("Q3", "高風險", [sol])]}
    r3_retry = {"verdicts": [verdict("Q2", "中風險")]}  # 補問仍未提出方案
    item = lambda q: {"issue_id": q, "title": "t", "risk_level": "低風險", "finding": "f",  # noqa: E731
                      "basis": "b", "recommendation": "r", "adopted_solution": 5}
    chief = {"overall_conclusion": "同意備查", "executive_summary": "e",  # 與分類矛盾，應被修正
             "no_concern_items": [item("Q1"), item("Q2")], "major_doubts": [item("Q2"), item("Q99")],
             "pending_review_items": []}
    final = {"overall_conclusion": "同意備查", "executive_summary": "e",
             "items": [{"title": "合併", "source_ids": ["P1-Q2", "P9-Q1"], "risk_level": "低風險", "finding": "f"}]}
    client = ScriptedClient([json.dumps(x, ensure_ascii=False)
                             for x in (r1, r2_partial, r2_retry, r3, r3_retry, chief, final)])
    s = ReviewOrchestrator(client, passes=1, lookup=False).run(calc)
    ps = s.passes[0]
    assert ps.issue_ids == ["Q1", "Q2", "Q3"]  # 統一重新編號
    assert ps.round2.responses[2].stance == S.Stance.ADMIT  # 未回覆者補記為未能抗辯
    assert ps.round3.verdicts[2].recommended_solution == 1  # 方案序號超出範圍時改為 1
    assert [i.issue_id for i in ps.chief.no_concern_items] == ["Q1"]
    assert [i.issue_id for i in ps.chief.major_doubts] == ["Q2", "Q3"]
    assert ps.chief.overall_conclusion == S.OverallConclusion.RETURNED
    f = s.final
    # 綜合裁決：無效來源剔除、風險取最嚴、遺漏的高風險項目補回、總結論不得寬於該輪
    assert [i.source_ids for i in f.items] == [["P1-Q2"], ["P1-Q3"]]
    assert all(i.risk_level == S.RiskLevel.HIGH for i in f.items)
    assert f.overall_conclusion == S.OverallConclusion.RETURNED
    notes = "\n".join(s.notes)
    assert "未就 Q2（中風險）提出解決方案" in notes and "只接受" in notes and "P9-Q1" in notes and "遺漏 P1-Q3" in notes


@pytest.mark.parametrize("engine", ["weasyprint", "reportlab"])
def test_pdf_generation(session, engine):
    if engine == "weasyprint":
        pytest.importorskip("weasyprint")
    html = render_html(session)
    assert "C1" in html and 'class="tex"' in html and "rc-member-designer" in html  # 算式以 LaTeX 排成 SVG
    pdf, used = generate_pdf(session, engine)
    assert used == engine and pdf.startswith(b"%PDF")


def test_model_filters_and_flagship_defaults():
    from agents.llm_client import filter_gemini_models, filter_openai_models

    ids = ["gpt-4.1", "gpt-4o-mini", "o3", "text-embedding-3-large", "whisper-1", "dall-e-3",
           "gpt-4o-realtime-preview", "gpt-3.5-turbo-instruct", "tts-1", "gpt-4.1"]
    assert filter_openai_models(ids) == ["o3", "gpt-4o-mini", "gpt-4.1"]
    models = [("models/gemini-2.5-pro", ["generateContent", "countTokens"]),
              ("models/text-embedding-004", ["embedContent"]),
              ("models/gemini-2.5-flash-preview-tts", ["generateContent"])]
    assert filter_gemini_models(models) == ["gemini-2.5-pro"]
    assert default_model("gemini", ["gemini-4-argon-preview-0901", "gemini-4-argon", "gemini-2.5-pro"]) == "gemini-4-argon"
    assert default_model("gemini", ["gemini-3-flash", "gemini-3-pro"]) == "gemini-3-pro"
    assert default_model("claude", ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"]) == "claude-opus-5-5"
    assert default_model("openai", ["gpt-6.1-sol-mini", "gpt-6.1-sol", "gpt-6.1"]) == "gpt-6.1-sol"
    assert default_model("openai", ["o3"]) is None


def test_claude_client_sends_pdf_document(monkeypatch):
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

    c = ClaudeClient("claude-opus-5-5", api_key="k")
    c._client = NS(messages=FakeMessages(False), beta=NS(messages=FakeMessages(True)))
    FakeMessages.next_reply = reply()
    att = [Attachment("calc.pdf", b"%PDF-1.4 x")]
    assert c._complete("sys", "user", 0.3, att) == '{"ok": 1}'
    beta, kw = calls[-1]
    assert beta and kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert "temperature" not in kw and kw["system"] == "sys"
    doc, text = kw["messages"][0]["content"]
    assert doc["type"] == "document" and doc["source"]["media_type"] == "application/pdf" and text["text"] == "user"

    c.model = "claude-haiku-4-5"  # 不支援備援的模型改走一般端點
    c._complete("sys", "user", 0.3, [])
    beta, kw = calls[-1]
    assert not beta and "fallbacks" not in kw and len(kw["messages"][0]["content"]) == 1

    FakeMessages.next_reply = reply("refusal")
    with pytest.raises(LLMError, match="拒絕"):
        c.generate("sys", "user", S.LookupRequest)


def test_openai_client_attaches_pdf_and_falls_back_to_text():
    from openai import BadRequestError

    from agents.llm_client import OpenAIClient

    sent = []

    class FakeCompletions:
        def create(self, **kw):
            sent.append(kw)
            if isinstance(kw["messages"][1]["content"], list) and len(sent) == 1:
                raise BadRequestError("file inputs are not supported for this model",
                                      response=NS(request=None, status_code=400, headers={}), body=None)
            return NS(choices=[NS(message=NS(content='{"ok": 1}'))])

    c = OpenAIClient("gpt-6.1-sol", api_key="k")
    c._client = NS(chat=NS(completions=FakeCompletions()))
    assert c._complete("sys", "user", 0.3, [Attachment("calc.pdf", b"%PDF")]) == '{"ok": 1}'
    first, second = sent
    assert first["messages"][1]["content"][0]["type"] == "file"
    assert second["messages"][1]["content"] == "user" and c.attachments_dropped
