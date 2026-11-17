"""一般計算書模式：不是 rc-member-designer 產生、也沒有案件檔的計算書（手算＋Midas／SAP2000）。"""
import io

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from agents import prompts as P
from agents.llm_client import MockClient
from orchestrator import ReviewOrchestrator
from utils.calcbook import read_calcbook
from utils.pdf_generator import generate_pdf, render_html
from utils.report_model import build_model
from utils.xlsx_report import generate_xlsx

LINES = ["P1 橋墩柱設計計算書（手算）", "一、設計依據：公路橋梁設計規範、公路橋梁耐震設計規範",
         "二、材料：f'c = 280 kgf/cm2，fy = 4200 kgf/cm2", "三、斷面：180 cm × 240 cm，主筋 D32 共 60 根",
         "四、內力：SAP2000 Element Forces - Frames，P = 850 tf，M3 = 520 tf-m",
         "五、剪力檢核：Vu = 95 tf，箍筋 D16@15 cm"]


def make_pdf() -> bytes:
    pdfmetrics.registerFont(UnicodeCIDFont("MSung-Light"))
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("MSung-Light", 12)
    for k, t in enumerate(LINES):
        c.drawString(60, 780 - 22 * k, t)
    c.showPage()
    c.save()
    return buf.getvalue()


class Spy(MockClient):
    systems: list[str] = []

    def generate(self, system, user, schema, **kw):
        Spy.systems.append(system)
        return super().generate(system, user, schema, **kw)


@pytest.fixture(scope="module")
def general():
    calc = read_calcbook(make_pdf(), filename="P1_hand_calc.pdf")
    Spy.systems = []
    return calc, ReviewOrchestrator(Spy, passes=1).run(calc, None, "")


def test_general_mode_detected(general):
    calc, s = general
    assert not calc.from_rcmd
    assert s.calc["mode"] == "general"
    assert s.project_info["審查模式"] == "一般計算書"


def test_general_prompts(general):
    _, s = general
    assert Spy.systems and all("本案為一般計算書" in t for t in Spy.systems)
    assert P.system_prompt("B", True).endswith(P.GENERAL_MODE)
    assert "本案為一般計算書" not in P.system_prompt("B", False)


def test_general_solutions_have_no_tool_fields(general):
    _, s = general
    for ps in s.passes:
        for v in ps.round3.verdicts:
            for sol in v.solutions:
                assert all(c.param_id == "" for c in sol.changes)
    assert s.adjusted_case is None if hasattr(s, "adjusted_case") else True


def test_general_report_labels(general):
    _, s = general
    m = build_model(s)
    assert m.general and m.fix_title == "修正項目總結" and m.field_head == "修改項目" and m.tool_url == ""
    assert "原計算方式" in m.uncovered[-1]
    html = render_html(s)
    assert "修正項目總結" in html and "rc-member-designer 調整總結" not in html
    assert generate_pdf(s)[0][:4] == b"%PDF"
    assert generate_xlsx(s)[:2] == b"PK"


def test_rcmd_mode_unchanged():
    calc = read_calcbook("examples/sample_C1_calc.pdf")
    assert calc.from_rcmd
    s = ReviewOrchestrator(MockClient, passes=1).run(calc, None, "")
    assert s.calc["mode"] == "rcmd"
    m = build_model(s)
    assert not m.general and m.fix_title == "rc-member-designer 調整總結"
