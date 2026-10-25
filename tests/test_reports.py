import io

import openpyxl
import pytest

from agents import schemas as S
from agents.llm_client import MockClient
from orchestrator import ReviewOrchestrator
from utils.calcbook import read_calcbook
from utils.rcmd import load_case
from utils.report_model import build_model, meta_rows
from utils.xlsx_report import generate_xlsx


@pytest.fixture(scope="module")
def session():
    calc = read_calcbook("examples/sample_C1_calc.pdf")
    case = load_case(open("examples/sample_C1_case.json", "rb").read())
    return ReviewOrchestrator(MockClient, passes=2).run(calc, case, "sample_C1_case.json", {"工程名稱": "測試案"})


def test_header_comes_from_calc_book(session):
    m = build_model(session)
    meta = {k: v for k, v, _ in m.meta}
    assert m.project_name == "測試案"
    assert meta["構件"] == "C1" and meta["斷面"].startswith("實心矩形")
    assert meta["程式版本／雜湊"].startswith("v") and session.calc["sha256"][:16] in meta["程式版本／雜湊"]
    assert dict(m.source_rows)["計算書程式版本"].startswith("v2026")
    for row in meta_rows(m.meta):  # 兩欄式表頭：跨欄項目自成一列，其餘每列兩項
        assert 1 <= len(row) <= 2
    assert m.sign_roles == ("設計", "校核", "審核")
    assert [r[0] for r in m.role_rows] == ["第 1 輪", "第 2 輪"]


def test_judgement_and_solutions(session):
    m = build_model(session)
    for row in m.summary_rows:
        assert row.judge == {"低風險": "OK", "中風險": "注意", "高風險": "NG"}[row.risk]
    assert session.final.overall_conclusion == S.OverallConclusion.RETURNED
    assert m.verdict_level == "bad" and "NG" in m.verdict_text
    assert m.items[0].solution.changes[0].param_id == "tieSize"
    assert any("tieSize" in x for x in m.adjusted_applied)
    assert any(c.ref == "土木401-112 §18.4.3.2" for c in m.clauses)  # 附錄：引用條文原文


def test_xlsx_layout_and_formula_guard(session):
    session.passes[0].round1.issues[0].challenge = "=HYPERLINK(\"http://x\")"  # 模型輸出以 = 開頭時不得變成公式
    wb = openpyxl.load_workbook(io.BytesIO(generate_xlsx(session)))
    assert wb.sheetnames == ["檢核表", "審查報告(A4)"]
    for ws in wb:
        assert not [c.coordinate for row in ws.iter_rows() for c in row if c.data_type == "f"]
    ws = wb["檢核表"]
    assert ws.freeze_panes == "A3"
    total = [r for r in ws.iter_rows() if r[0].value == "整體審查判定"]
    assert total and total[0][0].fill.fgColor.rgb.endswith("FFF2CC")
    texts = [c.value for row in ws.iter_rows() for c in row if isinstance(c.value, str)]
    assert any(t.startswith("二、rc-member-designer 調整參數") for t in texts) and "#3 → #4" in texts
    a4 = wb["審查報告(A4)"]
    texts = [c.value for row in a4.iter_rows() for c in row if isinstance(c.value, str)]
    assert "四、解決方案（改動最小且符合規範）" in texts and "六、檢核彙總" in texts and "設計" in texts
    assert any("≥" in t or "≤" in t for t in texts)  # LaTeX 轉為可讀文字
