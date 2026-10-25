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
    assert [t.label for t in at.tabs] == ["前處理與初篩"]
    at.button(key="run").click().run()
    assert not at.exception
    assert [t.label for t in at.tabs] == ["結果摘要", "詰問歷程", "預覽報告", "前處理與初篩", "原始 JSON"]
