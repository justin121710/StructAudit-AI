import io

import numpy as np
import pandas as pd
import pytest

from utils.parser import map_columns, parse_structural_file


def _csv(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


def test_map_columns_with_units_and_aliases():
    m = map_columns(["樓層", "層間位移角 X", "配筋比 (%)", "應力比 D/C", "Load Combination"])
    assert m["story"] == "樓層"
    assert m["drift_ratio"] == "層間位移角 X"
    assert m["rebar_ratio"] == "配筋比 (%)"
    assert m["dcr"] == "應力比 D/C"
    assert m["load_combo"] == "Load Combination"


def test_no_preset_thresholds():
    """摘要表只做欄位辨識與統計，不做任何門檻判定（由 Agent 查閱規範後判斷）。"""
    df = pd.DataFrame({"構件": ["G1", "G2"], "D/C": [1.30, 0.5]})
    s = parse_structural_file(_csv(df), filename="dcr.csv")
    assert not hasattr(s, "findings")
    text = s.to_prompt_text()
    assert "初篩" not in text and "最大 1.3" in text


def test_sample_workbook_and_prompt_text():
    s = parse_structural_file("examples/sample_structure.xlsx")
    assert len(s.sheets) == 5
    text = s.to_prompt_text({"案名": "測試案"})
    assert "測試案" in text and "層間位移角" in text


def test_unsupported_format():
    with pytest.raises(ValueError):
        parse_structural_file(b"abc", filename="model.e2k")


def test_blank_cells_in_text_columns():
    """空白格不得造成解析失敗（pandas 3 的 astype(str) 會保留 NaN）。"""
    df = pd.DataFrame({
        "樓層": [np.nan, "2F", "3F"],
        "構件": ["B1", np.nan, "C1"],
        "配筋比 (%)": [2.8, np.nan, "0.8%"],
        "D/C": [np.nan, 1.05, 0.5],
        "載重組合": [np.nan, "1.4D", "1.2D+1.6L"],
    })
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    s = parse_structural_file(buf.getvalue(), filename="blank.xlsx")
    assert s.sheets[0].mapped["dcr"] == "D/C"
    assert "nan" not in s.to_prompt_text().split("|")[0]
