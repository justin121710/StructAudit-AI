"""計算書讀取、規範條文庫、LaTeX 排版、rc-member-designer 案件檔。"""

import json
from pathlib import Path

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


# ---------- 工具整合 M4：rc-member-designer 從 Midas 匯入之內力來源 ----------
MIDAS_PDF = Path(__file__).resolve().parent.parent / "examples" / "sample_P1_midas_calc.pdf"
MIDAS_CASE = MIDAS_PDF.with_name("sample_P1_midas_case.json")


def test_load_source_from_calc_book():
    c = read_calcbook(MIDAS_PDF)
    assert c.facts["構件"] == "P1_COL"
    assert c.facts["內力來源"] == "Midas Civil 結果表格匯入；結構群組 P1_COL；元素 101to103；模型 midas_pier.mct"
    assert "cLCB2 @E102-J" in c.load_source and "5.1" not in c.load_source
    # 沒有內力來源的計算書不加此欄
    assert "內力來源" not in read_calcbook(SAMPLE_PDF).facts


def test_load_source_summary_flags_edits_and_plates():
    from utils.calcbook import load_source_summary, load_source_text
    pages = ["5. 控制組合檢核\n內力來源\n本表載重組合由 Midas Civil 結果表格匯入：構件結構群組 S1_DECK、元素 301to302。"
             "版內力（Plate Force）取 x 向 Wood-Armer 設計彎矩。匯入後載重表已修改，以表內數值為準。\n6. 剪力與扭矩設計\n其他"]
    t = load_source_text(pages)
    assert "其他" not in t
    s = load_source_summary(t)
    assert "Wood-Armer" in s and "S1_DECK" in s and "已手動修改" in s
    assert load_source_text(["內力來源\n無關文字"]) == ""


def test_case_load_source_and_edit_detection():
    case = rcmd.load_case(open(MIDAS_CASE, "rb").read())
    text = rcmd.catalog_text("pylon", case)
    assert "載重來源（案件檔 loadsrc）" in text and "結構群組 P1_COL" in text and "載重表與匯入結果一致" in text
    assert "篩選依據 P_u 最大：cLCB2（E102-J，830）" in text and "極值篩選" in text and "載重列 cLCB2 @E102-J ← E102-J" in text
    case["loads"][0]["Pu"] += 1
    assert "匯入後載重表已手動修改" in rcmd.load_source_text(case)
    assert rcmd.load_source_text(rcmd.load_case(open(SAMPLE_CASE, "rb").read())) == ""
    # 審查後寫回之案件檔保留載重來源
    new, applied, _ = rcmd.apply_changes(case, [("tieSize", "#5")])
    assert new["loadsrc"]["pylon"]["group"] == "P1_COL" and applied


def test_reviewer_prompt_mentions_load_source():
    from agents import prompts as P
    assert "內力來源" in P.AGENT_B_SYSTEM and "包絡組合" in P.AGENT_B_SYSTEM


# ---------- 四專案流程改善：交換一致性（指紋 v2、寫回驗證、配對核對） ----------
def test_loads_key_v2_matches_shared_vectors():
    V = json.load(open(Path(__file__).parent / "data" / "loads_key_vectors.json", encoding="utf-8"))
    for c in V["cases"]:
        assert rcmd.loads_key(c["loads"]) == c["key"], c["note"]
    # 捨入邊界：v1（toFixed 對 Python 格式）會不一致，v2 一致
    assert rcmd._key_num(1.0625) == "1063" and rcmd._key_num(-1.0625) == "-1062" and rcmd._key_num(-0.0) == "0"
    assert rcmd._key_num(float("nan")) == "NaN" and rcmd._key_num(1e20) == "OV"
    # 舊案件的 v1 鍵仍以 v1 比對
    a = [{"name": "A", "Pu": 1, "Mux": 2}]
    assert not rcmd.loads_edited(a, rcmd.loads_key_v1(a)) and not rcmd.loads_edited(a, rcmd.loads_key(a))
    assert rcmd.loads_edited([{"name": "A", "Pu": 1, "Mux": 2.1}], rcmd.loads_key(a))


def test_write_back_rejects_invalid_and_keeps_loads():
    case = rcmd.load_case(open(MIDAS_CASE, "rb").read())
    assert "不是有限" in rcmd.validate_change("pylon", "nB", "nan", case)
    assert "不是有限" in rcmd.validate_change("pylon", "nB", "inf", case)
    assert "必須大於 0" in rcmd.validate_change("pylon", "fc", "-280", case)
    assert "不是柱斷面模組的欄位" in rcmd.validate_change("pylon", "bw", "60", case)
    assert rcmd.validate_change("pylon", "fc", "350", case) == ""
    new, applied, rejected = rcmd.apply_changes(case, [("fc", "350"), ("tieSize", "#5"), ("bw", "60"), ("nB", "nan")],
                                                source="P1.pdf", pairing="已核對")
    assert len(applied) == 2 and len(rejected) == 2
    assert new["loads"] == case["loads"] and new["loadsrc"] == case["loadsrc"]      # 載重與來源不被改寫
    rv = new["review"]
    assert rv["status"] == "pending-recalc" and rv["pairing"] == "已核對" and rv["source"] == "P1.pdf"
    assert [c["id"] for c in rv["changes"]] == ["fc", "tieSize"]
    assert "回 Midas 重分析" in rv["analysis"]          # f'c 屬「材料」群組：分析可能過期


def test_pairing_pdf_and_case():
    from utils import pairing
    calc = read_calcbook(MIDAS_PDF)
    case = rcmd.load_case(open(MIDAS_CASE, "rb").read())
    p = pairing.check(calc, case)
    assert p.status == pairing.MATCH and [c[1] for c in p.checks] == [pairing.MATCH] * 4
    # review 區塊與存檔時間不影響輸入雜湊（與 rc-member-designer inputHash 同規則）
    case2 = dict(case, review={"by": "x", "changes": []}, saved="2030-01-01")
    assert pairing.check(calc, case2).status == pairing.MATCH
    # 輸入改了一個值 → 不是同一版
    case3 = json.loads(json.dumps(case)); case3["values"]["tieSize"] = "#5"
    assert pairing.check(calc, case3).status == pairing.MISMATCH
    # 不同構件的計算書
    p4 = pairing.check(read_calcbook(SAMPLE_PDF), case)
    assert p4.status == pairing.MISMATCH and "構件名稱" in p4.summary
    assert pairing.check(calc, None).status == pairing.UNKNOWN
