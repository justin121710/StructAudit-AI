from pathlib import Path

import pytest

streamlit_testing = pytest.importorskip("streamlit.testing.v1")
APP = Path(__file__).resolve().parent.parent / "app.py"


def test_offline_review_flow(monkeypatch):
    for key in ("GEMINI_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    at = streamlit_testing.AppTest.from_file(str(APP), default_timeout=180)
    at.run()
    assert not at.exception
    assert not at.chat_message
    at.button(key="run").click().run()
    assert not at.exception
    session = at.session_state["review"]
    # 每則 Agent 發言都顯示在對話區，且完成後提供 PDF／Excel／JSON 下載
    assert len(at.chat_message) == len(session.transcript) > 0
    assert len(at.get("download_button")) == 3


def test_pick_three_models_with_api_key(monkeypatch):
    import agents.llm_client as llm

    secret_key = "sk-test-secret-should-not-leak"
    monkeypatch.setenv("GEMINI_API_KEY", secret_key)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    seen = {}

    def fake_list(provider, api_key):
        seen["key"] = api_key
        return ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"]

    shared = llm.MockClient()

    def fake_create(provider, model=None, api_key=None):
        c = llm.MockClient()  # 共用示範狀態，只換模型名稱
        c.model, c._severity, c._source = model, shared._severity, shared._source
        return c

    monkeypatch.setattr(llm, "list_models", fake_list)
    monkeypatch.setattr(llm, "create_client", fake_create)
    at = streamlit_testing.AppTest.from_file(str(APP), default_timeout=180)
    at.run()
    assert not at.exception
    assert seen["key"] == secret_key  # 用部署設定的 Key 查詢模型
    assert all(t.value == "" for t in at.text_input if "API Key" in t.label)  # Key 不會送到瀏覽器
    picks = {s.key: s for s in at.selectbox if s.key and s.key.startswith("model_")}
    assert set(picks) == {"model_A", "model_B", "model_C"}
    assert picks["model_B"].value == ("gemini", "gemini-2.5-pro")  # 預設取偏好清單第一個
    picks["model_A"].set_value(("gemini", "gemini-2.5-flash"))
    picks["model_C"].set_value(("gemini", "gemini-2.0-flash"))
    at.run()
    at.button(key="run").click().run()
    assert not at.exception
    s = at.session_state["review"]
    assert s.agent_models == {"A": "mock / gemini-2.5-flash", "B": "mock / gemini-2.5-pro", "C": "mock / gemini-2.0-flash"}


def test_secret_name_variants_are_recognised(monkeypatch):
    """Secrets 名稱寫成 OPEN_AI_KEY（含空白）也要讀得到。"""
    import agents.llm_client as llm

    for key in ("GEMINI_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    seen = {}

    def fake_list(provider, api_key):
        seen[provider] = api_key
        return ["gpt-4.1", "gpt-4o"]

    monkeypatch.setattr(llm, "list_models", fake_list)
    at = streamlit_testing.AppTest.from_file(str(APP), default_timeout=180)
    at.secrets["OPEN_AI_KEY"] = "  sk-openai-test \n"
    at.run()
    assert not at.exception
    assert seen == {"openai": "sk-openai-test"}
    picks = [s for s in at.selectbox if s.key and s.key.startswith("model_")]
    assert len(picks) == 3 and picks[0].value == ("openai", "gpt-4.1")
