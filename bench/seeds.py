"""埋錯測試集：在計算書擷取文字中植入已知錯誤，量測各種審查設定的抓錯率。

每個 Seed：
- edits：依序套用的 (正規式, 取代字串)；每一條都必須在計算書中至少命中一次，否則該 Seed 不適用於這份計算書。
  同一個錯誤在計算書裡出現的地方（輸入表、代入式、結果）一起改，讓錯誤「自洽」，模型不能只靠前後數字不一致就抓到。
  （S07 例外：它就是要測「前後數字不一致」能不能被發現。）
- hit：判定「抓到」的關鍵字，list[list[str]]——每一組至少命中一個（組內 OR、組間 AND），比對綜合裁決各項目的
  標題、發現、應辦事項與解決方案文字。這是自動初判，報告另列每項的原文供人工確認。
- clause：正確作法的規範依據（說明用，不參與判定）。

目前的 Seed 針對 examples/sample_C1_calc.pdf（rc-member-designer 建築柱 C1）；其他計算書不適用的 Seed 會自動略過。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from utils.calcbook import CalcBook


@dataclass(frozen=True)
class Seed:
    id: str
    title: str
    edits: tuple[tuple[str, str], ...]
    hit: tuple[tuple[str, ...], ...]
    clause: str = ""
    kind: str = "規範"  # 規範（係數、限值用錯）／一致性（前後數字不符）／細則（配筋細則）


SEEDS: list[Seed] = [
    Seed("S01", "剪力強度折減因數用 0.85（建築柱應為 0.75）",
         ((r"剪力 ϕ ϕ = 0\.75", "剪力 ϕ ϕ = 0.85"),
          (r"−0\.75\n57\.5 42\.8 = 33\.9", "−0.85\n57.5 42.8 = 24.9")),
         (("剪力",), ("0.85", "0.75", "折減因數", "ϕ", "φ")), "土木401-112 表 21.2.1"),
    Seed("S02", "最大軸力上限 0.80P_o 寫成 0.85P_o",
         ((r"0\.65 × 0\.80P", "0.65 × 0.85P"),
          (r"0\.65 × 0\.80 × 1, 017 = 529", "0.65 × 0.85 × 1, 017 = 562")),
         (("最大軸力", "P_{n,max}", "Pn,max", "P_n,max", "軸力上限", "純壓"), ("0.80", "0.85", "22.4.2")),
         "土木401-112 §22.4.2.1"),
    Seed("S03", "柱外保護層 4.0 cm 改為 2.0 cm",
         ((r"外保護層 4\.0 cm", "外保護層 2.0 cm"),),
         (("保護層",),), "土木401-112 表 20.5.1.3.1", "細則"),
    Seed("S04", "容量設計剪力 V_e = 2M_pr/l_u 少乘 2",
         ((r"V = 2M /l", "V = M /l"),
          (r"2 × 92 × 100 57\.5 tf", "92 × 100 28.8 tf")),
         (("容量設計", "V_e", "Ve", "M_{pr}", "Mpr", "機率彎矩"), ("2M", "兩端", "2 ×", "18.4.6")),
         "土木401-112 §18.4.6.1"),
    Seed("S05", "圍束筋 A_sh 第一式係數 0.3 寫成 0.2",
         ((r"max 0\.3 × 52\.0 × \(1\.3314 − 1\)", "max 0.2 × 52.0 × (1.3314 − 1)"),
          (r"max\{0\.3446,  0\.3120\} = 0\.3446", "max{0.2297,  0.3120} = 0.3120")),
         (("圍束", "A_{sh}", "Ash", "A_sh"), ("0.3", "0.2", "18.4.5")), "土木401-112 表 18.4.5.4"),
    Seed("S06", "加密區採用箍筋間距 15 cm（需求 ≤ 6.21 cm）",
         ((r"採用 #3 @ 7\.5 cm", "採用 #3 @ 15 cm"),),
         (("間距", "箍筋", "加密區"), ("15", "6.21", "不足", "大於")), "土木401-112 §18.4.5.3", "細則"),
    Seed("S07", "判定段 D/C 與檢核表不一致（表 0.788、判定寫 0.688）",
         ((r"D/C = 0\.788 ≤ 1\.0", "D/C = 0.688 ≤ 1.0"),),
         (("0.688",), ("0.788", "不一致", "不符", "矛盾")), "", "一致性"),
    Seed("S08", "主筋比低於 1%（8 根改 4 根，ρ_g = 0.563%）",
         ((r"總根數 8 根 A 40\.5", "總根數 4 根 A 20.3"),
          (r"0\.01126 = 1\.126%", "0.00563 = 0.563%")),
         (("鋼筋比", "ρ", "主筋量", "配筋率"), ("1%", "0.01", "0.563", "下限", "18.7.4", "10.6.1")),
         "土木401-112 §10.6.1.1、§18.7.4.1"),
    Seed("S09", "混凝土彈性模數係數 12,000 寫成 15,000",
         ((r"E  =c 12000  =f\nc′ 12000  =280 200, 798", "E  =c 15000  =f\nc′ 15000  =280 250, 998"),),
         (("彈性模數", "E_c", "Ec", "E_{c}"), ("15000", "15,000", "12000", "12,000", "19.2.2")),
         "土木401-112 §19.2.2.1"),
]


def _pat(p: str) -> str:
    """樣式裡的空白一律當成 \\s+：PDF 擷取文字常有不換行空白（U+00A0）與多個空白。"""
    return re.sub(r" +", r"\\s+", p)


def applicable(seed: Seed, text: str) -> bool:
    return all(re.search(_pat(p), text) for p, _ in seed.edits)


def seed_text(seed: Seed, text: str) -> str:
    out = text
    for p, r in seed.edits:
        out, n = re.subn(_pat(p), r, out)
        if not n:
            raise ValueError(f"{seed.id}：計算書中找不到「{p}」")
    return out


def seeded_calc(calc: CalcBook, seed: Seed) -> CalcBook:
    """植入錯誤後的計算書：逐頁取代文字；不附 PDF 原檔（模型只讀文字，原檔裡沒有這個錯誤）。"""
    pages = seed_text(seed, "\x00".join(calc.pages)).split("\x00")
    return replace(calc, pages=pages, pdf=None, sha256=f"seed-{seed.id}-{calc.sha256[:12]}",
                   filename=f"{seed.id}_{calc.filename}")


def norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s or "")).replace("φ", "ϕ")


def hit(seed: Seed, texts: list[str]) -> str | None:
    """回傳第一個同時滿足所有關鍵字組的文字；都不滿足時 None。"""
    for t in texts:
        n = norm(t)
        if all(any(norm(k) in n for k in group) for group in seed.hit):
            return t
    return None


@dataclass
class Finding:
    risk: str
    text: str


def session_findings(session) -> list[Finding]:
    """綜合裁決的每一項（標題、發現、應辦事項、解決方案）合成一段文字。"""
    out = []
    f = session.final
    if f is None:
        return out
    for it in f.items:
        sol = it.final_solution
        parts = [it.title, it.finding, it.required_action, " ".join(it.code_clauses)]
        if sol:
            parts += [sol.label, sol.approach, " ".join(sol.verification),
                      " ".join(f"{c.label} {c.current} {c.proposed}" for c in sol.changes), sol.other_method]
        out.append(Finding(it.risk_level.value, "｜".join(x for x in parts if x)))
    return out


@dataclass
class SeedResult:
    seed: str
    detected: bool
    risk: str = ""
    evidence: str = ""
    n_items: int = 0
    cost_twd: float = 0.0
    seconds: float = 0.0
    extra: dict = field(default_factory=dict)


def score(seed: Seed, session) -> SeedResult:
    fs = session_findings(session)
    got = hit(seed, [x.text for x in fs])
    risk = next((x.risk for x in fs if x.text == got), "") if got else ""
    return SeedResult(seed.id, bool(got), risk, (got or "")[:300], len(fs))
