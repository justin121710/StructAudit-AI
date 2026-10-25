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


def test_drift_formats_are_normalised():
    df = pd.DataFrame({"樓層": ["3F", "2F", "1F"], "位移角": ["1/250", "0.45%", "0.0052"]})
    s = parse_structural_file(_csv(df), filename="drift.csv")
    hits = [f for f in s.findings if "位移角" in f.item]
    assert [f.item for f in hits] == ["層間位移角 1F"]


def test_rebar_ratio_by_member_kind():
    df = pd.DataFrame({
        "構件編號": ["B1", "C1", "C2"],
        "構件類型": ["梁", "柱", "柱"],
        "配筋比(%)": [2.8, 0.8, 2.0],
    })
    s = parse_structural_file(_csv(df), filename="members.csv")
    items = {f.item for f in s.findings}
    assert "梁配筋比 B1" in items
    assert "柱主筋比 C1" in items
    assert not any("C2" in i for i in items)


def test_missing_load_combinations_detected():
    df = pd.DataFrame({"載重組合": ["1.4D", "1.2D+1.6L", "1.2D+1.0L+1.0EX", "0.9D+1.0EY"]})
    s = parse_structural_file(_csv(df), filename="combo.csv")
    details = [f.detail for f in s.findings]
    assert any("風力" in d for d in details)
    assert not any("地震" in d for d in details)
    assert not any("0.9D" in d for d in details)


def test_dcr_levels_and_custom_limits():
    df = pd.DataFrame({"構件": ["G1", "G2", "G3"], "D/C": [1.02, 0.97, 0.5]})
    s = parse_structural_file(_csv(df), filename="dcr.csv")
    assert [(f.level, f.item) for f in s.findings] == [("High", "應力比 G1"), ("Medium", "應力比 G2")]
    s2 = parse_structural_file(_csv(df), filename="dcr.csv", limits={"dcr_max": 1.05, "dcr_warn": 1.0})
    assert [f.item for f in s2.findings] == ["應力比 G1"]


def test_sample_workbook_and_prompt_text():
    s = parse_structural_file("examples/sample_structure.xlsx")
    assert len(s.sheets) == 5
    text = s.to_prompt_text({"案名": "測試案"})
    assert "測試案" in text and "規則式初篩" in text


def test_unsupported_format():
    with pytest.raises(ValueError):
        parse_structural_file(b"abc", filename="model.e2k")
