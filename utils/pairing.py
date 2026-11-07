"""計算書 PDF 與 rc-member-designer 案件檔的配對核對（四專案整合：交接一致性）。

rc-member-designer 計算書表頭的「輸入 SHA-256」是案件快照（扣除 saved、members、conf）的
JSON.stringify 雜湊（index.html inputHash）。案件檔是同一個快照，所以可以在此重算並比對：
相同 → PDF 與案件檔為同一版輸入；不同 → 不是同一版，審查建議不寫回案件檔。
另比對構件名稱、模組與內力來源群組，作為輔助。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from utils import rcmd

MATCH, MISMATCH, UNKNOWN = "已核對", "不符", "未驗證"


def case_input_hash(case: dict) -> str:
    """與 rc-member-designer inputHash 相同：去掉 saved、members、conf（以及審查附加的 review）後的 JSON 雜湊。"""
    c = {k: v for k, v in case.items() if k not in ("saved", "members", "conf", "review")}
    s = json.dumps(c, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


@dataclass
class Pairing:
    status: str = UNKNOWN
    checks: list[tuple[str, str | None, str]] = field(default_factory=list)   # (項目, 已核對／不符／None, 說明)

    @property
    def summary(self) -> str:
        bad = [c for c in self.checks if c[1] == MISMATCH]
        if self.status == MATCH:
            return "PDF 與案件檔為同一版輸入（輸入 SHA-256 相符）"
        if self.status == MISMATCH:
            return "PDF 與案件檔不是同一版：" + "；".join(f"{c[0]}{c[2]}" for c in bad)
        return "未驗證配對：" + "；".join(c[2] for c in self.checks if c[1] is None) if self.checks else "未上傳案件檔"


def check(calc, case: dict | None) -> Pairing:
    """calc 為 utils.calcbook.CalcBook。沒有案件檔時回傳 UNKNOWN。"""
    if not case:
        return Pairing(UNKNOWN, [])
    out = Pairing()
    trace = calc.facts.get("程式版本／雜湊", "") if calc else ""
    m = re.search(r"輸入\s*SHA-256\s*([0-9a-f]{8,64})", trace)
    if m:
        h = case_input_hash(case)
        ok = h.startswith(m.group(1))
        out.checks.append(("輸入雜湊", MATCH if ok else MISMATCH,
                           f"計算書 {m.group(1)}、案件檔 {h[:len(m.group(1))]}"))
    else:
        out.checks.append(("輸入雜湊", None, "計算書沒有「輸入 SHA-256」（非本工具計算書或版本過舊），無法核對是否同一版"))
    mod = rcmd.case_module(case)
    if calc and calc.module:
        out.checks.append(("模組", MATCH if calc.module == mod else MISMATCH,
                           f"計算書 {rcmd.MODULE_LABEL.get(calc.module, calc.module)}、案件檔 {rcmd.MODULE_LABEL[mod]}"))
    name = str(case.get("values", {}).get("bMemName" if mod == "beam" else "memName", "")).strip()
    pdf_name = (calc.facts.get("構件", "") if calc else "").strip()
    if name and pdf_name:
        out.checks.append(("構件名稱", MATCH if name == pdf_name else MISMATCH, f"計算書「{pdf_name}」、案件檔「{name}」"))
    src = (case.get("loadsrc") or {}).get(mod) or {}
    g = re.search(r"結構群組\s*([^\s、，,。]+)", re.sub(r"\s*\n\s*", "", getattr(calc, "load_source", "") or ""))
    if src.get("group") and g:
        out.checks.append(("內力來源群組", MATCH if g.group(1) == src["group"] else MISMATCH,
                           f"計算書 {g.group(1)}、案件檔 {src['group']}"))
    if any(c[1] == MISMATCH for c in out.checks):
        out.status = MISMATCH
    elif out.checks and out.checks[0][0] == "輸入雜湊" and out.checks[0][1] == MATCH:
        out.status = MATCH
    return out
