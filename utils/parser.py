"""Excel / CSV 結構摘要前處理。

讀入結構分析軟體匯出的摘要表（ETABS / SAP2000 / MIDAS 匯出或自行整理），完成三件事：
1. 欄位辨識：以中英文別名對應到標準欄位（樓層、位移角、配筋比、應力比…）。
2. 規則式初篩：依常用規範門檻先標出異常值，作為 Agent 討論的客觀錨點。
3. 產生給 LLM 的精簡文字摘要（控制長度，避免把整張表塞進 prompt）。
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

import pandas as pd

# 標準欄位 -> 可辨識的欄名別名（比對前會轉小寫並去除空白與括號單位）
COLUMN_ALIASES: dict[str, list[str]] = {
    "story": ["樓層", "層", "story", "floor", "level"],
    "member": ["構件", "構件編號", "編號", "member", "label", "element", "id"],
    "member_type": ["構件類型", "類型", "type", "membertype"],
    "drift_ratio": ["層間位移角", "位移角", "driftratio", "drift", "storydrift"],
    "rebar_ratio": ["配筋比", "鋼筋比", "主筋比", "rho", "ρ", "rebarratio", "reinforcementratio"],
    "dcr": ["應力比", "強度比", "dcr", "d/c", "demandcapacityratio", "ratio"],
    "load_combo": ["載重組合", "組合", "loadcombination", "combo", "combination"],
    "axial_ratio": ["軸力比", "axialratio", "pu/agfc"],
    "shear_force": ["樓層剪力", "storyshear", "shear", "vx", "vy"],
    "fc": ["fc", "f'c", "fc'", "混凝土強度"],
    "fy": ["fy", "鋼筋強度"],
}

# 規則式初篩門檻（預設值；實際案件可在 UI 調整）
DEFAULT_LIMITS = {
    # 建築物耐震設計規範及解說 §2.16.1 容許層間相對側向位移角
    "drift_max": 0.005,
    # 土木401-112 §18.3.3.1 特殊抗彎矩構架梁 ρ ≤ 0.025
    "beam_rho_max": 0.025,
    # 土木401-112 §18.4.4.1 特殊抗彎矩構架柱 0.01Ag ≤ Ast ≤ 0.06Ag（一般柱 §10.6.1.1 為 1%～8%）
    "col_rho_min": 0.01,
    "col_rho_max": 0.06,
    # 強度需求／容量比
    "dcr_max": 1.0,
    "dcr_warn": 0.95,
}

# 初篩項目引用之條號（已對照 rc-member-designer specs/ 規範原文）
REF = {
    "drift": "建築物耐震設計規範及解說 §2.16.1",
    "beam_rho": "土木401-112 §18.3.3.1",
    "col_rho": "土木401-112 §18.4.4.1（一般柱 §10.6.1.1）",
    "dcr": "土木401-112 §9.5.1.1、§10.5.1.1（φS_n ≥ U）",
    "combo": "土木401-112 §5.3.1（表 5.3.1）",
}

# 常見應有的載重組合（土木401-112 §5.3.1 表 5.3.1），以正規式檢查是否遺漏
EXPECTED_COMBOS = {
    "1.4D": r"1\.4\s*d(?![a-z])",
    "1.2D+1.6L": r"1\.2\s*d\s*\+\s*1\.6\s*l",
    "地震組合 (E)": r"(?<![a-z])(e|eq|ex|ey)(?![a-z])|地震",
    "風力組合 (W)": r"(?<![a-z])(w|wx|wy)(?![a-z])|風",
    "0.9D±E 抗傾覆": r"0\.9\s*d",
}

MAX_ROWS_IN_SUMMARY = 25


@dataclass
class Finding:
    """規則式初篩結果。"""

    level: str  # "High" / "Medium" / "Info"
    sheet: str
    item: str
    detail: str
    reference: str
    check: str = ""  # drift / beam_rho / col_rho / dcr / combo

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class SheetSummary:
    name: str
    n_rows: int
    columns: list[str]
    mapped: dict[str, str]  # 標準欄位 -> 原始欄名
    preview_markdown: str
    stats: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass
class StructuralSummary:
    source_name: str
    sheets: list[SheetSummary]
    findings: list[Finding]
    frames: dict[str, pd.DataFrame] = field(default_factory=dict, repr=False)
    sha256: str = ""  # 輸入檔雜湊，列於報告表頭供追溯
    limits: dict = field(default_factory=dict)  # 本次初篩採用之門檻

    def to_prompt_text(self, project_info: dict | None = None) -> str:
        """組成給 LLM 的結構數據摘要。"""
        lines: list[str] = []
        if project_info:
            lines.append("## 專案基本資料")
            for k, v in project_info.items():
                if v:
                    lines.append(f"- {k}：{v}")
            lines.append("")
        lines.append(f"## 輸入檔案：{self.source_name}")
        for s in self.sheets:
            lines.append(f"\n### 工作表「{s.name}」（{s.n_rows} 列）")
            if s.mapped:
                mapping = "、".join(f"{v}→{k}" for k, v in s.mapped.items())
                lines.append(f"辨識欄位：{mapping}")
            for col, st in s.stats.items():
                lines.append(
                    f"- {col}：最小 {st['min']:.4g}、最大 {st['max']:.4g}、平均 {st['mean']:.4g}"
                )
            lines.append("")
            lines.append(s.preview_markdown)
        lines.append("\n## 規則式初篩結果（系統自動計算，供參考）")
        if self.findings:
            for f in self.findings:
                lines.append(f"- [{f.level}] {f.sheet}｜{f.item}：{f.detail}（{f.reference}）")
        else:
            lines.append("- 未發現超出預設門檻之數值。")
        return "\n".join(lines)


def _norm(name: str) -> str:
    s = str(name).strip().lower()
    s = re.sub(r"[\(（\[].*?[\)）\]]", "", s)  # 去除括號內單位
    return re.sub(r"[\s_\-]", "", s)


def map_columns(columns: list[str]) -> dict[str, str]:
    """回傳 {標準欄位: 原始欄名}。先完全比對，再做包含比對，每個原始欄只對應一次。"""
    normed = {c: _norm(c) for c in columns}
    mapped: dict[str, str] = {}
    used: set[str] = set()
    for exact in (True, False):
        for key, aliases in COLUMN_ALIASES.items():
            if key in mapped:
                continue
            for col, n in normed.items():
                if col in used:
                    continue
                hit = any(n == _norm(a) for a in aliases) if exact else any(
                    len(_norm(a)) >= 2 and _norm(a) in n for a in aliases
                )
                if hit:
                    mapped[key] = col
                    used.add(col)
                    break
    return mapped


def _to_ratio(series: pd.Series) -> pd.Series:
    """數值化；若看起來是百分比（例如 1.8 代表 1.8%）則換算為小數。"""
    s = pd.to_numeric(series.astype(str).str.replace("%", "", regex=False), errors="coerce")
    if s.dropna().empty:
        return s
    if series.astype(str).str.contains("%").any() or s.dropna().max() > 0.2:
        s = s / 100.0
    return s


def _drift_to_ratio(series: pd.Series) -> pd.Series:
    """位移角可能以 0.0042、0.42%、或 1/238 表示。"""
    def conv(v):
        t = str(v).strip()
        if "/" in t:
            a, _, b = t.partition("/")
            try:
                return float(a) / float(b)
            except ValueError:
                return float("nan")
        if t.endswith("%"):
            try:
                return float(t[:-1]) / 100
            except ValueError:
                return float("nan")
        try:
            return float(t)
        except ValueError:
            return float("nan")

    return series.map(conv)


def _label(row: pd.Series, mapped: dict[str, str]) -> str:
    parts = [str(row[mapped[k]]) for k in ("story", "member") if k in mapped]
    return " ".join(parts) or f"第 {row.name + 1} 列"


def _member_kind(row: pd.Series, mapped: dict[str, str], sheet_name: str) -> str:
    text = " ".join(
        str(row[mapped[k]]) for k in ("member_type", "member") if k in mapped
    ) + " " + sheet_name
    t = text.lower()
    if any(k in t for k in ("柱", "column", "col")) or re.search(r"\bc\d", t):
        return "column"
    if any(k in t for k in ("梁", "樑", "beam")) or re.search(r"\b[bg]\d", t):
        return "beam"
    return "unknown"


def run_prechecks(name: str, df: pd.DataFrame, mapped: dict[str, str], limits: dict) -> list[Finding]:
    findings: list[Finding] = []

    if "drift_ratio" in mapped:
        drift = _drift_to_ratio(df[mapped["drift_ratio"]])
        for idx, v in drift.items():
            if pd.notna(v) and v > limits["drift_max"]:
                findings.append(Finding(
                    "High", name, f"層間位移角 {_label(df.loc[idx], mapped)}",
                    f"{v:.4f} > 限值 {limits['drift_max']}",
                    REF["drift"], check="drift",
                ))

    if "rebar_ratio" in mapped:
        rho = _to_ratio(df[mapped["rebar_ratio"]])
        for idx, v in rho.items():
            if pd.isna(v):
                continue
            kind = _member_kind(df.loc[idx], mapped, name)
            label = _label(df.loc[idx], mapped)
            if kind == "beam" and v > limits["beam_rho_max"]:
                findings.append(Finding(
                    "High", name, f"梁配筋比 {label}",
                    f"ρ = {v:.2%} > {limits['beam_rho_max']:.1%}", REF["beam_rho"], check="beam_rho",
                ))
            elif kind == "column" and not (limits["col_rho_min"] <= v <= limits["col_rho_max"]):
                findings.append(Finding(
                    "High", name, f"柱主筋比 {label}",
                    f"ρg = {v:.2%} 不在 {limits['col_rho_min']:.0%}～{limits['col_rho_max']:.0%}",
                    REF["col_rho"], check="col_rho",
                ))

    if "dcr" in mapped:
        dcr = pd.to_numeric(df[mapped["dcr"]], errors="coerce")
        for idx, v in dcr.items():
            if pd.isna(v):
                continue
            label = _label(df.loc[idx], mapped)
            if v > limits["dcr_max"]:
                findings.append(Finding(
                    "High", name, f"應力比 {label}", f"D/C = {v:.2f} > {limits['dcr_max']}",
                    REF["dcr"], check="dcr",
                ))
            elif v > limits["dcr_warn"]:
                findings.append(Finding(
                    "Medium", name, f"應力比 {label}", f"D/C = {v:.2f}，餘裕不足 5%",
                    REF["dcr"], check="dcr",
                ))

    if "load_combo" in mapped:
        combos = " | ".join(df[mapped["load_combo"]].astype(str)).lower()
        for label, pattern in EXPECTED_COMBOS.items():
            if not re.search(pattern, combos):
                findings.append(Finding(
                    "Medium", name, "載重組合", f"未見「{label}」相關組合",
                    REF["combo"], check="combo",
                ))
    return findings


def _sheet_stats(df: pd.DataFrame, mapped: dict[str, str]) -> dict[str, dict[str, float]]:
    stats = {}
    for key in ("drift_ratio", "rebar_ratio", "dcr", "axial_ratio", "shear_force"):
        if key not in mapped:
            continue
        col = mapped[key]
        if key == "drift_ratio":
            s = _drift_to_ratio(df[col])
        elif key == "rebar_ratio":
            s = _to_ratio(df[col])
        else:
            s = pd.to_numeric(df[col], errors="coerce")
        s = s.dropna()
        if not s.empty:
            stats[col] = {"min": float(s.min()), "max": float(s.max()), "mean": float(s.mean())}
    return stats


def _read_frames(data: bytes, filename: str) -> dict[str, pd.DataFrame]:
    suffix = Path(filename).suffix.lower()
    if suffix in (".xlsx", ".xlsm", ".xls"):
        sheets = pd.read_excel(io.BytesIO(data), sheet_name=None)
    elif suffix in (".csv", ".txt"):
        for enc in ("utf-8-sig", "cp950", "big5", "utf-8"):
            try:
                sheets = {Path(filename).stem: pd.read_csv(io.BytesIO(data), encoding=enc)}
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("無法辨識 CSV 編碼，請另存為 UTF-8")
    else:
        raise ValueError(f"不支援的檔案格式：{suffix}（請使用 .xlsx / .xls / .csv）")
    return {
        str(k): v.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
        for k, v in sheets.items()
    }


def parse_structural_file(
    file: str | Path | BinaryIO | bytes,
    filename: str | None = None,
    limits: dict | None = None,
) -> StructuralSummary:
    """讀取 Excel／CSV 並產生結構摘要。file 可為路徑、bytes 或檔案物件（Streamlit UploadedFile）。"""
    if isinstance(file, (str, Path)):
        filename = filename or Path(file).name
        data = Path(file).read_bytes()
    elif isinstance(file, bytes):
        data = file
    else:
        filename = filename or getattr(file, "name", "upload.xlsx")
        data = file.read()
    if not filename:
        raise ValueError("bytes 輸入需提供 filename 以判斷格式")

    lim = {**DEFAULT_LIMITS, **(limits or {})}
    frames = _read_frames(data, filename)
    sheets, findings = [], []
    for name, df in frames.items():
        if df.empty:
            continue
        df.columns = [str(c).strip() for c in df.columns]
        mapped = map_columns(list(df.columns))
        preview = df.head(MAX_ROWS_IN_SUMMARY).to_markdown(index=False) if _has_tabulate() else \
            df.head(MAX_ROWS_IN_SUMMARY).to_csv(index=False)
        if len(df) > MAX_ROWS_IN_SUMMARY:
            preview += f"\n…（其餘 {len(df) - MAX_ROWS_IN_SUMMARY} 列省略，統計值已涵蓋全部資料）"
        sheets.append(SheetSummary(
            name=name, n_rows=len(df), columns=list(df.columns), mapped=mapped,
            preview_markdown=preview, stats=_sheet_stats(df, mapped),
        ))
        findings.extend(run_prechecks(name, df, mapped, lim))

    if not sheets:
        raise ValueError("檔案中沒有可讀取的資料")
    order = {"High": 0, "Medium": 1, "Info": 2}
    findings.sort(key=lambda f: order.get(f.level, 9))
    return StructuralSummary(
        source_name=filename, sheets=sheets, findings=findings, frames=frames,
        sha256=hashlib.sha256(data).hexdigest(), limits=lim,
    )


def _has_tabulate() -> bool:
    try:
        import tabulate  # noqa: F401
        return True
    except ImportError:
        return False
