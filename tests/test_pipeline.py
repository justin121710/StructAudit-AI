import json
import os
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
    # 每輪 3 次查閱＋Round 1～3＋主任技師，3 輪加綜合裁決共 22 次；每次都記錄用量
    assert len(session.usage) == 22
    assert [u["task"] for u in session.usage if u["pass"] == 1] == ["lookup", "round1", "lookup", "round2", "lookup",
                                                                    "round3", "chief"]
    assert session.usage[-1]["role"] == "F" and session.to_dict()["cost"]["calls"] == 22
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
    lookup = {"clauses": [{"spec": "01", "clause": "18.4.3"}], "keywords": []}
    client = ScriptedClient([json.dumps(x, ensure_ascii=False) for x in
                             (lookup, r1, lookup, r2_partial, r2_retry, lookup, r3, r3_retry, chief, final)])
    s = ReviewOrchestrator(client, passes=1).run(calc)
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
    if os.getenv("SPEC_PDF_DIR"):  # 附錄條文為原文版面裁切圖，不是零散的擷取文字
        assert 'class="rp-orig"' in html and "無法取得規範原文" not in html
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


class FakeStream:
    """模擬 SDK 的串流 context manager：依序吐出事件，最後回傳完整回覆。"""

    def __init__(self, events, final):
        self.events, self.final = events, final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self.events)

    def get_final_message(self):
        return self.final

    get_final_response = get_final_message


def claude_reply(stop="end_turn", text='{"ok": 1}', usage=None, model="claude-opus-5-5"):
    return NS(stop_reason=stop, stop_details=NS(category="cyber") if stop == "refusal" else None, usage=usage,
              model=model, content=[NS(type="thinking", thinking="摘要"), NS(type="text", text=text)])


def test_claude_client_streams_thinking_and_sends_pdf():
    from agents.llm_client import ClaudeClient

    calls = []
    thinking = [NS(type="content_block_delta", delta=NS(type="thinking_delta", thinking="先核對第 15 頁")),
                NS(type="content_block_delta", delta=NS(type="thinking_delta", thinking="的強柱弱梁。")),
                NS(type="content_block_delta", delta=NS(type="text_delta", text="{"))]

    class FakeMessages:
        reply = claude_reply()

        def __init__(self, beta):
            self.beta = beta

        def stream(self, **kw):
            calls.append((self.beta, kw))
            return FakeStream(thinking, FakeMessages.reply)

    c = ClaudeClient("claude-opus-5-5", api_key="k")
    c._client = NS(messages=FakeMessages(False), beta=NS(messages=FakeMessages(True)))
    seen = []
    c.on_thinking = seen.append
    assert c._complete("sys", "user", 0.3, [Attachment("calc.pdf", b"%PDF-1.4 x")]) == '{"ok": 1}'
    assert "".join(seen) == "先核對第 15 頁的強柱弱梁。"  # 思考摘要即時回報
    beta, kw = calls[-1]
    assert beta and kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert "temperature" not in kw and kw["system"] == "sys"
    doc, text = kw["messages"][0]["content"]
    assert doc["type"] == "document" and doc["source"]["media_type"] == "application/pdf" and text["text"] == "user"

    c.model = "claude-haiku-4-5"  # 不支援備援的模型改走一般端點
    c._complete("sys", "user", 0.3, [])
    beta, kw = calls[-1]
    assert not beta and "fallbacks" not in kw and len(kw["messages"][0]["content"]) == 1

    FakeMessages.reply = claude_reply("refusal")
    with pytest.raises(LLMError, match="拒絕"):
        c.generate("sys", "user", S.LookupRequest)


def test_openai_client_streams_reasoning_and_falls_back_to_text():
    from openai import BadRequestError

    from agents.llm_client import OpenAIClient

    sent = []
    usage = NS(input_tokens=40000, output_tokens=9000, input_tokens_details=NS(cached_tokens=10000))
    events = [NS(type="response.reasoning_summary_text.delta", delta="比對 §18.4.3.2"),
              NS(type="response.output_text.delta", delta="{")]

    class FakeResponses:
        def stream(self, **kw):
            sent.append(kw)
            if kw["input"][0]["content"][0]["type"] == "input_file":
                raise BadRequestError("file inputs are not supported for this model",
                                      response=NS(request=None, status_code=400, headers={}), body=None)
            return FakeStream(events, NS(output_text='{"ok": 1}', usage=usage, model="gpt-6.1-sol"))

    c = OpenAIClient("gpt-6.1-sol", api_key="k")
    c._client = NS(responses=FakeResponses())
    seen = []
    c.on_thinking = seen.append
    assert c._complete("sys", "user", 0.3, [Attachment("calc.pdf", b"%PDF")]) == '{"ok": 1}'
    first, second = sent
    assert first["store"] is False and first["reasoning"] == {"summary": "auto"}  # 伺服器端不保留對話
    assert first["text"] == {"format": {"type": "json_object"}} and first["instructions"] == "sys"
    assert second["input"][0]["content"] == [{"type": "input_text", "text": "user"}] and c.attachments_dropped
    assert seen == ["比對 §18.4.3.2"]
    assert (c.usage[-1].input, c.usage[-1].cached, c.usage[-1].output) == (30000, 10000, 9000)


def test_usage_and_thoughts_from_claude_and_gemini():
    from agents.llm_client import ClaudeClient, GeminiClient

    seen = []
    c = ClaudeClient("claude-opus-5-5", api_key="k")
    usage = NS(input_tokens=1000, cache_read_input_tokens=30000, cache_creation_input_tokens=0, output_tokens=5000,
               iterations=None)
    c._client = NS(beta=NS(messages=NS(stream=lambda **kw: FakeStream([], claude_reply(usage=usage)))))
    c.on_usage = seen.append
    c._task = "round1"
    c._complete("s", "u", 0.3, [])
    assert (seen[-1].input, seen[-1].cached, seen[-1].output, seen[-1].task) == (1000, 30000, 5000, "round1")

    g = GeminiClient("gemini-4-argon", api_key="k")
    meta = NS(prompt_token_count=20000, cached_content_token_count=0, candidates_token_count=3000,
              thoughts_token_count=4000)
    part = lambda text, thought=False: NS(text=text, thought=thought)  # noqa: E731
    chunks = [NS(candidates=[NS(content=NS(parts=[part("先讀表頭", True)]))], usage_metadata=None),
              NS(candidates=[NS(content=NS(parts=[part('{"ok"'), part(": 1}")]))], usage_metadata=meta)]
    g._client = NS(models=NS(generate_content_stream=lambda **kw: iter(chunks)))
    thoughts = []
    g.on_thinking = thoughts.append
    assert g._complete("s", "u", 0.3, []) == '{"ok": 1}'
    assert thoughts == ["先讀表頭"]
    assert (g.usage[-1].input, g.usage[-1].output) == (20000, 7000)  # 思考 token 計入輸出


def test_thinking_entries_recorded_per_call(calc):
    s = ReviewOrchestrator(MockClient, passes=1).run(calc)
    assert len(s.thoughts) == 8  # 每次呼叫一段：查閱×3＋Round 1～3＋主任技師＋綜合裁決
    first = s.thoughts[0]
    assert first["role"] == "B" and first["task"] == "lookup" and "Round 1" in first["stage"] and first["text"]
    assert s.to_dict()["thoughts"][3]["task"] == "round2"


def test_cost_uses_model_prices_and_fx():
    from datetime import date

    from utils.pricing import Price, default_price, summarize

    usage = [{"model": "claude-opus-5-5", "input": 1_000_000, "cached": 1_000_000, "cache_write": 0, "output": 100_000},
             {"model": "gpt-6.1-sol", "input": 500_000, "cached": 0, "cache_write": 0, "output": 50_000},
             {"model": "mystery-model", "input": 10, "output": 10}]
    c = summarize(usage, fx=32.5, on=date(2026, 10, 25))
    by = {r.model: r for r in c.rows}
    assert by["claude-opus-5-5"].usd == pytest.approx(4 + 0.2 + 2)  # 輸入 4、快取讀取 0.2、輸出 20×0.1
    assert by["gpt-6.1-sol"].usd == pytest.approx(1 + 0.5)
    assert by["mystery-model"].usd is None and c.missing == ["mystery-model"]
    assert c.twd == pytest.approx((6.2 + 1.5) * 32.5)
    assert default_price("gemini-4-argon", date(2026, 11, 30)).input == 2.0
    assert default_price("gemini-4-argon", date(2026, 12, 1)).input == 4.0  # 優惠期後標準價
    custom = summarize(usage[:1], {"claude-opus-5-5": Price(1, 1, 1, 1)}, fx=30)
    assert custom.usd == pytest.approx(2.1)  # 使用者自訂單價優先
