from pathlib import Path

import pytest

streamlit_testing = pytest.importorskip("streamlit.testing.v1")
APP = Path(__file__).resolve().parent.parent / "app.py"
KEYS = ("GEMINI_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY")


def test_offline_review_flow_moves_output_to_the_right(monkeypatch):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    at = streamlit_testing.AppTest.from_file(str(APP), default_timeout=300)
    at.run()
    assert not at.exception
    assert not at.chat_message and not [c for c in at.columns if c.gap == "large"]  # 開始前只有置中的設定區
    assert {t.label for t in at.text_input} >= {"構件", "斷面", "設計依據"}  # 專案資料取自計算書
    assert [x.key for x in at.number_input] == ["fx"]  # 沒有初篩門檻，只有計費匯率
    assert not at.toggle  # 查閱規範固定執行，沒有開關
    at.radio(key="passes").set_value(2).run()
    at.button(key="run").click().run()
    assert not at.exception
    session = at.session_state["review"]
    assert len(session.passes) == 2 and session.final is not None
    assert len([c for c in at.columns if c.gap == "large"]) == 2  # 按下開始後左右兩欄
    assert any("審查過程與結果" in x.value for x in at.markdown)
    assert len(at.chat_message) == len(session.transcript) > 0
    assert len(session.usage) == 2 * 7 + 1  # 每輪 7 次（含 3 次查閱規範）＋綜合裁決
    assert any("本次審查實際花費：NT$0（離線示範不計費）" in x.value for x in at.markdown)
    assert [t.label for t in at.tabs] == ["第 1 輪", "第 2 輪", "綜合裁決"]
    labels = [b.label for b in at.get("download_button")]
    assert labels == ["下載 PDF 報告", "下載 Excel", "下載 JSON", "下載調整後案件檔"]
    assert [b.proto.label for b in at.get("link_button")] == ["開啟 RC 構件設計工具"]
    assert len(session.thoughts) == 2 * 7 + 1  # 每次呼叫一段思考摘要
    at.run()  # 重新整理（例如按下載）後仍為左右兩欄，綜合裁決排在最前
    assert not at.exception and [t.label for t in at.tabs][0] == "綜合裁決"
    assert "審查完成｜AI 思考過程（15 段）" in [e.label for e in at.expander]  # 點開可看各 Agent 的思考過程
    assert any('class="vcard bad"' in x.value for x in at.markdown)  # 總判定卡片


def test_pick_three_models_with_flagship_defaults(monkeypatch):
    import agents.llm_client as llm

    secret_key = "sk-test-secret-should-not-leak"
    monkeypatch.setenv("GEMINI_API_KEY", secret_key)
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    seen = {}

    def fake_list(provider, api_key):
        seen["key"] = api_key
        return ["gemini-4-argon", "gemini-3-pro", "gemini-3-flash"]

    monkeypatch.setattr(llm, "list_models", fake_list)
    monkeypatch.setattr(llm, "create_client", lambda provider, model=None, api_key=None: llm.MockClient(model))
    at = streamlit_testing.AppTest.from_file(str(APP), default_timeout=300)
    at.run()
    assert not at.exception
    assert seen["key"] == secret_key  # 用部署設定的 Key 查詢模型
    assert all(t.value == "" for t in at.text_input if "API Key" in t.label)  # Key 不會送到瀏覽器
    picks = {s.key: s for s in at.selectbox if s.key and s.key.startswith("model_")}
    assert set(picks) == {"model_0_gemini", "model_1_gemini", "model_2_gemini"}
    assert all(p.value == "gemini-4-argon" for p in picks.values())  # 預設旗艦模型
    prices = {x.key: x.value for x in at.number_input}
    assert prices["price_in_gemini-4-argon"] == 2.0 and prices["price_out_gemini-4-argon"] == 10.0
    picks["model_1_gemini"].set_value("gemini-3-pro")
    at.run()
    at.radio(key="passes").set_value(1).run()
    at.button(key="run").click().run()
    assert not at.exception
    s = at.session_state["review"]
    assert s.model_slots == ["mock / gemini-4-argon", "mock / gemini-3-pro", "mock / gemini-4-argon"]
    assert s.passes[0].roles["B"] == "mock / gemini-3-pro"
    assert {u["model"] for u in s.usage} == {"gemini-4-argon", "gemini-3-pro"}
    assert "gemini-3-pro" in s.prices or "gemini-3-pro" in s.cost().missing  # 未收錄單價的模型提示填寫


def test_multiple_providers_default_to_flagships(monkeypatch):
    """三家都有 Key 時，三個槽位預設 Gemini／Claude／OpenAI 的旗艦模型。"""
    import agents.llm_client as llm

    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "c-key")
    monkeypatch.setenv("OPEN_AI_KEY", "o-key")  # 名稱寫法不同也能讀到
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    catalog = {"gemini": ["gemini-4-argon", "gemini-3-pro"],
               "claude": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"],
               "openai": ["gpt-6.1-sol", "gpt-6.1-mini"]}
    monkeypatch.setattr(llm, "list_models", lambda provider, api_key: catalog[provider])
    at = streamlit_testing.AppTest.from_file(str(APP), default_timeout=300)
    at.run()
    assert not at.exception
    provs = {s.key: s.value for s in at.selectbox if s.key and s.key.startswith("prov_")}
    assert provs == {"prov_0": "gemini", "prov_1": "claude", "prov_2": "openai"}
    models = {s.key: s.value for s in at.selectbox if s.key and s.key.startswith("model_")}
    assert models == {"model_0_gemini": "gemini-4-argon", "model_1_claude": "claude-opus-5-5",
                      "model_2_openai": "gpt-6.1-sol"}


def test_pdf_without_text_layer_warns_but_allows_ai_review(monkeypatch, scanned_pdf):
    import agents.llm_client as llm

    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    at = streamlit_testing.AppTest.from_file(str(APP), default_timeout=300)
    at.run()
    at.checkbox(key="use_sample").uncheck()
    at.file_uploader(key="calc_file").set_value(("掃描計算書.pdf", scanned_pdf, "application/pdf"))
    at.run()
    assert not at.exception and not at.error  # 不再當成讀取失敗
    assert any("沒有文字層" in w.value for w in at.warning)
    assert at.button(key="run").disabled  # 離線示範需要文字層

    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setattr(llm, "list_models", lambda provider, api_key: ["gemini-4-argon"])
    at.radio(key="mode").set_value("使用 AI 模型").run()
    assert not at.button(key="run").disabled  # AI 模型直接判讀 PDF 頁面影像
