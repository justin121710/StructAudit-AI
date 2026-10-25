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
