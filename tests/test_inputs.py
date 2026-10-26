"""計算書讀取、規範條文庫、LaTeX 排版、rc-member-designer 案件檔。"""

import json

import pytest

from utils import latex, rcmd
from utils.calcbook import header_facts, read_calcbook
from utils.specs import load_library

SAMPLE_PDF, SAMPLE_CASE = "examples/sample_C1_calc.pdf", "examples/sample_C1_case.json"


@pytest.fixture(scope="module")
def calc():
    return read_calcbook(SAMPLE_PDF)


# ---------------- 計算書 ----------------
def test_project_info_comes_from_calc_book_header(calc):
    assert calc.kind == "pdf" and calc.n_pages == 16 and calc.pdf.startswith(b"%PDF")
    assert calc.title == "RC 柱斷面設計檢核計算書" and calc.from_rcmd
    assert calc.facts["構件"] == "C1"
    assert calc.facts["斷面"].startswith("實心矩形 B × H = 60 × 60")
    assert "土木401" in calc.facts["設計依據"]
    assert calc.facts["程式版本／雜湊"].startswith("v2026")
    assert calc.module == "pylon" and calc.basis == "bldg"


def test_calc_text_has_page_markers_and_no_cjk_gaps(calc):
    text = calc.text()
    assert "<<第 15 頁>>" in text and "不合格項目" in text
    assert "柱 斷 面" not in text  # pypdf 在中文字間插入的空白已移除


def test_pdf_without_text_layer_is_still_reviewable(scanned_pdf):
    cb = read_calcbook(scanned_pdf, filename="掃描計算書.pdf")
    assert cb.kind == "pdf" and cb.n_pages == 3 and not cb.has_text and cb.pdf == scanned_pdf
    assert cb.facts == {} and "沒有文字層" in cb.prompt_text() and "頁面影像" in cb.text()


def test_pdfium_fallback_when_pypdf_finds_no_text(monkeypatch):
    import utils.calcbook as C

    monkeypatch.setattr(C, "_pypdf_pages", lambda data: ["", ""])  # 模擬 pypdf 讀不出文字
    pages = C.extract_pages(open(SAMPLE_PDF, "rb").read())
    assert len(pages) == 16 and "不合格項目" in "".join(pages)


def test_generic_header_keys():
    title, facts = header_facts("第 1 頁\n某某大樓結構計算書\n工程名稱：示範大樓\n設計單位：某事務所\n1. 設計概要")
    assert title == "某某大樓結構計算書" and facts == {"工程名稱": "示範大樓", "設計單位": "某事務所"}


def test_excel_input_still_supported():
    cb = read_calcbook("examples/sample_structure.xlsx")
    assert cb.kind == "table" and cb.pdf is None
    assert cb.facts["結構系統"] == "RC 特殊抗彎矩構架" and "U1" not in cb.facts
    assert "層間位移角" in cb.text()


# ---------------- 規範條文庫 ----------------
def test_spec_library_lookup_and_refs():
    lib = load_library()
    c = lib.get("土木401-112", "§18.4.3.2")
    assert c and c.ref == "土木401-112 §18.4.3.2" and "Σ Mnc ≥ (6/5) ΣMnb" in c.text  # Symbol 字型已轉回 ≥
    assert lib.get("01", "表18.4.5.4").title.startswith("特殊抗彎矩構架柱之橫向鋼筋")
    assert lib.get("01", "式(19.2.2.1.b)").id == "19.2.2.1"  # 式號對應所在條文
    assert [x.id for x in lib.lookup("01", "18.4.3")] == ["18.4.3", "18.4.3.1", "18.4.3.2", "18.4.3.3"]
    assert lib.get("01", "6.2.5.1").title.startswith("滿足下列(a)或(b)")  # 條號與內文分行排版者也能辨識
    assert c.box and c.box[0][0] == c.pdf_page  # 條文在原文 PDF 上的範圍
    refs = lib.find_refs("土木401-112 §18.4.3.2；表 25.5.2.1；建築物耐震設計規範 §2.16.1；公路橋梁耐震設計規範 §5.3.3")
    assert refs == [("01", "18.4.3.2"), ("01", "表25.5.2.1"), ("02", "2.16.1"), ("12", "5.3.3")]
    assert lib.check_refs("§18.4.3.2、§99.9.9") == ["土木401-112 §99.9.9"]


def test_spec_search_by_keyword():
    lib = load_library()
    hits = lib.search("梁柱接頭 剪力強度", ["01"], k=5)
    assert hits and all(h.spec == "01" for h in hits)


# ---------------- LaTeX ----------------
def test_latex_renders_katex_style_formulas():
    for tex in [r"\beta_1=\max\!\left(0.65,\min\!\left(0.85,\;0.85-0.05\frac{f'_c-280}{70}\right)\right)",
                r"s \le \min\left(\frac{b}{4},\,6d_b,\,s_o\right)", r"\dfrac{A_{st}}{A_g}=1.126\%",
                r"s = 7.5\ \text{cm}（不符）"]:
        assert latex.to_svg(tex), tex
        assert latex.to_png(tex)[:4] == b"\x89PNG"
    assert latex.to_svg(r"\unknowncommand{x}") is None  # 無法排版時由呼叫端改顯示純文字
    assert latex.split_lines(r"a=b \\ c=d") == ["a=b", "c=d"]
    assert latex.to_text(r"\phi V_n = \frac{A_{sh}}{s\,b_c} \geq 0.3") == "φ V_n = (A_sh)/(s b_c) ≥ 0.3"


# ---------------- rc-member-designer ----------------
def test_rcmd_catalog_and_case_changes():
    case = rcmd.load_case(open(SAMPLE_CASE, "rb").read())
    assert rcmd.case_module(case) == "pylon"
    text = rcmd.catalog_text("pylon", case)
    assert "tieSize｜箍筋 / 繫筋號數｜—｜#3｜#3、#4、#5、#6、#7" in text and "載重組合" in text
    assert rcmd.validate_change("pylon", "tieSize", "#4", case) == ""
    assert "只接受" in rcmd.validate_change("pylon", "tieSize", "#9", case)
    assert "沒有輸入欄位" in rcmd.validate_change("pylon", "nope", "1", case)
    assert "應為數值" in rcmd.validate_change("pylon", "nB", "四", case)
    new, applied, rejected = rcmd.apply_changes(case, [("tieSize", "#4"), ("nB", "4"), ("tieSize2", "#5")])
    assert new["values"]["tieSize"] == "#4" and new["values"]["nB"] == "4" and case["values"]["tieSize"] == "#3"
    assert len(applied) == 2 and len(rejected) == 1
    assert json.loads(rcmd.case_bytes(new))["app"] == "rc-section-designer"
    with pytest.raises(ValueError):
        rcmd.load_case(b'{"app": "other"}')


def test_clause_image_from_original_spec_pdf():
    import os

    from utils.specs import clause_png

    if not os.getenv("SPEC_PDF_DIR"):
        pytest.skip("需要本機的 rc-member-designer（規範原文 PDF）")
    png = clause_png("01", "6.2.5.1")
    assert png and png[:4] == b"\x89PNG"
    assert clause_png("01", "99.9.9") is None
