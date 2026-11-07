"""rc-member-designer 串接：Round 3 外審提出的修正方案，直接對應到 rc-member-designer 的輸入欄位。

- 參數清單 data/rcmd_params.json 由 tools/build_rcmd_params.js 從 rc-member-designer 的 index.html（PIN_META）擷取，
  含欄位 id、名稱、符號、單位與下拉選單的可選值。
- 若一併上傳 rc-member-designer 的案件檔（.json，「儲存案件」下載），Agent 可看到每個欄位的目前值，
  系統再把採用方案的參數變更寫回案件檔，使用者在工具中按「開啟案件」即可載入、重算確認。
"""

from __future__ import annotations

import json
import math
from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

TOOL_URL = "https://justin121710.github.io/rc-member-designer/"
TOOL_REPO = "https://github.com/justin121710/rc-member-designer"
CASE_APP = "rc-section-designer"
PARAMS_FILE = Path(__file__).resolve().parent.parent / "data" / "rcmd_params.json"
MODULE_LABEL = {"pylon": "柱斷面", "beam": "梁／版斷面"}


@lru_cache(maxsize=1)
def _catalog() -> dict:
    return json.loads(PARAMS_FILE.read_text(encoding="utf-8"))


def params(module: str) -> list[dict]:
    return _catalog()["modules"].get(module, [])


def param(module: str | None, pid: str) -> dict | None:
    for mod in ([module] if module else []) + ["pylon", "beam"]:
        for p in params(mod):
            if p["id"] == pid:
                return p
    return None


def load_case(data: bytes) -> dict:
    """讀取 rc-member-designer 案件檔；格式不符時拋出 ValueError。"""
    try:
        case = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"案件檔不是有效的 JSON：{e}") from e
    if not isinstance(case, dict) or case.get("app") != CASE_APP or not isinstance(case.get("values"), dict):
        raise ValueError("這不是 rc-member-designer 的案件檔（缺少 app／values 欄位）")
    return case


def case_module(case: dict) -> str:
    return "beam" if case.get("tab") == "beam" else "pylon"


def catalog_text(module: str | None, case: dict | None = None) -> str:
    """給 Agent 的輸入欄位清單：id｜名稱（符號）｜單位｜目前值｜可選值。"""
    mod = module or (case_module(case) if case else "pylon")
    values = (case or {}).get("values", {})
    lines = [f"rc-member-designer「{MODULE_LABEL.get(mod, mod)}」模組輸入欄位（param_id｜名稱｜單位｜"
             + ("目前值｜" if case else "") + "可選值）："]
    for p in params(mod):
        opts = "、".join(v for v, _ in p.get("options", []))
        cur = values.get(p["id"], "")
        sym = f"（{p['symbol']}）" if p["symbol"] not in ("", "—") else ""
        lines.append("｜".join(x for x in [p["id"], p["label"] + sym, p["unit"] or "—",
                                           (str(cur) if case else None), opts or "數值"] if x is not None))
    if case:
        loads = case.get("bloads" if mod == "beam" else "loads") or []
        if loads:
            lines.append("載重組合（案件檔，已乘載重因數；單位 tf、tf·m）：")
            lines += [f"- {json.dumps(row, ensure_ascii=False)}" for row in loads]
        src = load_source_text(case, mod)
        if src:
            lines.append(src)
    return "\n".join(lines)


LOADS_KEY_FIELDS = ["Pu", "Mux", "Muy", "Vux", "Vuy", "Tu", "Mpos", "Mneg", "Vu"]


def _key_num(v) -> str:
    """指紋 v2 的數值：×1000 後四捨五入（半數往 +∞），非有限值 NaN、過大 OV。
    與 rc-member-designer core/midas-io.js keyNum 同一規則（tests/data/loads_key_vectors.json 驗證）。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "NaN"
    if not math.isfinite(x):
        return "NaN"
    t = x * 1000.0
    if abs(t) >= 9e15:
        return "OV"
    r = math.floor(t)
    return str(int(r + 1 if t - r >= 0.5 else r))


def loads_key(loads: list) -> str:
    """載重表指紋 v2（與 rc-member-designer loadsKey 相同）：判斷匯入後載重表是否被修改。"""
    rows = []
    for r in loads or []:
        name = r.get("name")
        rows.append("|".join(["" if name is None else str(name), str(r.get("pos") or "")]
                             + ["" if r.get(k) is None else _key_num(r[k]) for k in LOADS_KEY_FIELDS]))
    return "k2:" + ";".join(rows)


def loads_key_v1(loads: list) -> str:
    """舊版指紋（toFixed(3)）：只用來比對 v1 時期存的案件檔；捨入邊界與 JS 不一致，新案件不用。"""
    def f3(v):
        x = float(v)
        return f"{0.0 if x == 0 else x:.3f}"
    return ";".join("|".join([str(r.get("name", "")), str(r.get("pos") or "")]
                             + ["" if r.get(k) is None else f3(r[k]) for k in LOADS_KEY_FIELDS]) for r in loads or [])


def loads_edited(loads: list, key: str) -> bool:
    key = str(key or "")
    return (loads_key(loads) if key.startswith("k2:") else loads_key_v1(loads)) != key


def load_source_text(case: dict | None, module: str | None = None) -> str:
    """案件檔的載重來源（loadsrc，rc-member-designer 從 Midas 匯入時記錄）：給 Agent 核對控制組合的依據。"""
    if not case:
        return ""
    mod = module or case_module(case)
    src = (case.get("loadsrc") or {}).get(mod)
    if not isinstance(src, dict):
        return ""
    loads = case.get("bloads" if mod == "beam" else "loads") or []
    edited = loads_edited(loads, src.get("key", ""))
    kind = "版內力（Plate Force），" + f"{src.get('dir', 'x')} 向 Wood-Armer" if src.get("kind") == "plate" else "Beam Force"
    lines = [f"載重來源（案件檔 loadsrc）：Midas Civil {kind}；內力表 {src.get('table') or '貼上'}（單位 {src.get('units', '')}）"
             + (f"；模型 {src['mct']}" if src.get("mct") else "")
             + (f"；結構群組 {src['group']}" if src.get("group") else "") + f"；元素 {src.get('elems', '')}"
             + (f"；{src['tb']}" if src.get("tb") else "")
             + ("；全部組合（逐組列出，工具逐列驗算）" if src.get("mode") == "all"
                else "；極值篩選（只列各分量最大／最小所在的組合，不等於逐組驗算，未列入的組合仍可能控制）")
             + ("；梁／版每列為群組設計包絡（同一組合在群組內各分量的最大值，M⁺、M⁻、V、T 可能來自不同位置，不是同時內力）"
                if mod == "beam" else "；柱每列為同一組合、同一端點的同時內力")
             + (f"；P_D 取自載重 {src['pd']}" if src.get("pd") else "")
             + ("；x、y 已對調" if src.get("swap") else "")
             + ("" if src.get("sagPos", True) else "；Midas 正彎矩視為上緣受拉")
             + ("。匯入後載重表已手動修改，以案件檔載重為準。" if edited else "。載重表與匯入結果一致。")]
    for c in src.get("ctrl") or []:
        lines.append(f"- 篩選依據 {c.get('why', '')}：{c.get('load', '')}（{c.get('tag', '')}，{c.get('v', '')}）")
    for r in src.get("rows") or []:
        pos = {"top": "頂", "bot": "底"}.get(r.get("pos", ""), "")
        lines.append(f"- 載重列 {r.get('name', '')}{('（' + pos + '）') if pos else ''} ← {r.get('tag', '')}")
    for w in src.get("warn") or []:
        lines.append(f"- 匯入時警示：{w}")
    return "\n".join(lines)


def _is_number(v) -> bool:
    try:
        float(str(v).replace(",", ""))
        return True
    except ValueError:
        return False


# 這些單位的欄位是尺寸、強度或根數：必須為正
POSITIVE_UNITS = {"cm", "mm", "m", "kgf/cm²", "根", "kg/m³"}
# 這些群組的欄位會改變分析用的斷面勁度、自重或材料：改了可能要回 Midas 重分析
ANALYSIS_GROUPS = {"斷面幾何", "材料"}


def param_in(module: str, pid: str) -> dict | None:
    """只在指定模組的欄位清單中找（不退回另一個模組）。"""
    return next((p for p in params(module) if p["id"] == pid), None)


def validate_change(module: str | None, pid: str, proposed: str, case: dict | None = None) -> str:
    """回傳錯誤原因；可套用時回傳空字串。
    欄位必須屬於案件（或計算書）所屬的模組；數值必須是有限值，尺寸、強度、根數不得為零或負。"""
    mod = module or (case_module(case) if case else None)
    p = param_in(mod, pid) if mod else param(None, pid)
    values = (case or {}).get("values", {})
    if not p:
        other = param(None, pid)
        if other and mod:
            return f"「{pid}」不是{MODULE_LABEL.get(mod, mod)}模組的欄位，不能寫入此案件"
        if pid not in values:
            return f"rc-member-designer 沒有輸入欄位「{pid}」"
    opts = [v for v, _ in (p or {}).get("options", [])]
    if opts and str(proposed) not in opts:
        return f"「{pid}」只接受 {('、'.join(opts))}"
    cur = values.get(pid)
    numeric = not opts and ((cur is not None and _is_number(cur)) or (p or {}).get("unit") in POSITIVE_UNITS)
    if numeric:
        if not _is_number(proposed):
            return f"「{pid}」應為數值"
        x = float(str(proposed).replace(",", ""))
        if not math.isfinite(x):
            return f"「{pid}」= {proposed} 不是有限的數值"
        if (p or {}).get("unit") in POSITIVE_UNITS and x <= 0:
            return f"「{pid}」= {proposed} 必須大於 0"
    return ""


def apply_changes(case: dict, changes: list[tuple[str, str]], source: str = "",
                  pairing: str = "") -> tuple[dict, list[str], list[str]]:
    """把 [(param_id, 建議值)] 寫入案件檔副本。回傳（新案件檔, 已套用說明, 未套用說明）。
    只改 values；載重（loads／bloads）與內力來源（loadsrc）一律不動。另寫入 review 區塊：
    變更清單、狀態「待重算」（rc-member-designer 開啟後重算、工程師確認才算通過）與可能過期的分析。"""
    mod = case_module(case)
    new = deepcopy(case)
    applied, rejected, log = [], [], []
    for pid, value in changes:
        why = validate_change(mod, pid, value, case)
        if why:
            rejected.append(f"{pid} → {value}：{why}")
            continue
        old = new["values"].get(pid, "")
        new["values"][pid] = str(value).replace(",", "") if _is_number(value) else str(value)
        p = param_in(mod, pid) or {}
        applied.append(f"{p.get('label', pid)}（{pid}）：{old} → {value}{p.get('unit', '')}")
        log.append({"id": pid, "label": p.get("label", pid), "unit": p.get("unit", ""), "group": p.get("group", ""),
                    "old": str(old), "new": new["values"][pid]})
    for k in ("loads", "bloads", "loadsrc"):              # 防呆：審查只改輸入欄位，不改載重與來源
        if k in case:
            new[k] = deepcopy(case[k])
    new["saved"] = datetime.now(timezone.utc).isoformat()
    new["conf"] = {"pylon": [], "beam": []}  # 輸入已變更，「需確認」項目須在工具中重新勾選
    if log:
        stale = [c["label"] for c in log if c["group"] in ANALYSIS_GROUPS]
        new["review"] = {"by": "StructAudit-AI", "at": new["saved"], "source": source, "pairing": pairing,
                         "status": "pending-recalc", "changes": log,
                         "analysis": ("斷面或材料已變更（" + "、".join(stale) + "）：若影響分析勁度、自重或材料，"
                                      "須回 Midas 重分析並重新匯入內力") if stale else ""}
    return new, applied, rejected


def case_bytes(case: dict) -> bytes:
    return json.dumps(case, ensure_ascii=False, indent=1).encode("utf-8")
