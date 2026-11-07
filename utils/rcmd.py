"""rc-member-designer 串接：Round 3 外審提出的修正方案，直接對應到 rc-member-designer 的輸入欄位。

- 參數清單 data/rcmd_params.json 由 tools/build_rcmd_params.js 從 rc-member-designer 的 index.html（PIN_META）擷取，
  含欄位 id、名稱、符號、單位與下拉選單的可選值。
- 若一併上傳 rc-member-designer 的案件檔（.json，「儲存案件」下載），Agent 可看到每個欄位的目前值，
  系統再把採用方案的參數變更寫回案件檔，使用者在工具中按「開啟案件」即可載入、重算確認。
"""

from __future__ import annotations

import json
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


def loads_key(loads: list) -> str:
    """與 rc-member-designer core/midas-io.js loadsKey 相同：判斷匯入後載重表是否被修改。"""
    def f3(v):
        x = float(v)
        return f"{0.0 if x == 0 else x:.3f}"
    rows = []
    for r in loads or []:
        rows.append("|".join([str(r.get("name", "")), str(r.get("pos") or "")]
                             + ["" if r.get(k) is None else f3(r[k]) for k in LOADS_KEY_FIELDS]))
    return ";".join(rows)


def load_source_text(case: dict | None, module: str | None = None) -> str:
    """案件檔的載重來源（loadsrc，rc-member-designer 從 Midas 匯入時記錄）：給 Agent 核對控制組合的依據。"""
    if not case:
        return ""
    mod = module or case_module(case)
    src = (case.get("loadsrc") or {}).get(mod)
    if not isinstance(src, dict):
        return ""
    loads = case.get("bloads" if mod == "beam" else "loads") or []
    edited = loads_key(loads) != src.get("key", "")
    kind = "版內力（Plate Force），" + f"{src.get('dir', 'x')} 向 Wood-Armer" if src.get("kind") == "plate" else "Beam Force"
    lines = [f"載重來源（案件檔 loadsrc）：Midas Civil {kind}；內力表 {src.get('table') or '貼上'}（單位 {src.get('units', '')}）"
             + (f"；模型 {src['mct']}" if src.get("mct") else "")
             + (f"；結構群組 {src['group']}" if src.get("group") else "") + f"；元素 {src.get('elems', '')}"
             + (f"；{src['tb']}" if src.get("tb") else "")
             + ("；列出全部勾選組合" if src.get("mode") == "all" else "；只列控制組合")
             + (f"；P_D 取自載重 {src['pd']}" if src.get("pd") else "")
             + ("；x、y 已對調" if src.get("swap") else "")
             + ("" if src.get("sagPos", True) else "；Midas 正彎矩視為上緣受拉")
             + ("。匯入後載重表已手動修改，以案件檔載重為準。" if edited else "。載重表與匯入結果一致。")]
    for c in src.get("ctrl") or []:
        lines.append(f"- 控制條件 {c.get('why', '')}：{c.get('load', '')}（{c.get('tag', '')}，{c.get('v', '')}）")
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


def validate_change(module: str | None, pid: str, proposed: str, case: dict | None = None) -> str:
    """回傳錯誤原因；可套用時回傳空字串。"""
    p = param(module, pid)
    values = (case or {}).get("values", {})
    if not p and pid not in values:
        return f"rc-member-designer 沒有輸入欄位「{pid}」"
    opts = [v for v, _ in (p or {}).get("options", [])]
    if opts and str(proposed) not in opts:
        return f"「{pid}」只接受 {('、'.join(opts))}"
    cur = values.get(pid)
    if not opts and cur is not None and _is_number(cur) and not _is_number(proposed):
        return f"「{pid}」應為數值"
    return ""


def apply_changes(case: dict, changes: list[tuple[str, str]]) -> tuple[dict, list[str], list[str]]:
    """把 [(param_id, 建議值)] 寫入案件檔副本。回傳（新案件檔, 已套用說明, 未套用說明）。"""
    mod = case_module(case)
    new = deepcopy(case)
    applied, rejected = [], []
    for pid, value in changes:
        why = validate_change(mod, pid, value, case)
        if why:
            rejected.append(f"{pid} → {value}：{why}")
            continue
        old = new["values"].get(pid, "")
        new["values"][pid] = str(value).replace(",", "") if _is_number(value) else str(value)
        p = param(mod, pid)
        applied.append(f"{(p or {}).get('label', pid)}（{pid}）：{old} → {value}{(p or {}).get('unit', '')}")
    new["saved"] = datetime.now(timezone.utc).isoformat()
    new["conf"] = {"pylon": [], "beam": []}  # 輸入已變更，「需確認」項目須在工具中重新勾選
    return new, applied, rejected


def case_bytes(case: dict) -> bytes:
    return json.dumps(case, ensure_ascii=False, indent=1).encode("utf-8")
