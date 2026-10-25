"""規範條文庫：讓 Agent 自行查閱規範原文，而不是由程式預設審查門檻。

條文索引 data/spec_index.json.gz 由 tools/build_spec_index.py 從 rc-member-designer 的 specs/（規範原文 PDF）建立，
每段含規範代碼、條號、規範頁碼與全文（含解說）。審查時：
1. 計算書中引用的條號自動查出原文，先提供給 Agent；
2. 每一回合開始前，Agent 可再依條號或關鍵字查閱（Agent 只能引用查得的原文）；
3. Agent 輸出的條號逐一比對索引，查無者記入系統紀錄，提醒技師核對。
"""

from __future__ import annotations

import gzip
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

INDEX_FILE = Path(__file__).resolve().parent.parent / "data" / "spec_index.json.gz"
DEFAULT_SPEC = "01"
SCOPE_SPECS = {"bldg": ["01", "02", "03"], "bridge": ["01", "11", "12"], "water": ["01", "03", "22"]}
CLAUSE_ID = r"\d{1,2}(?:\.\d{1,3}){1,5}"
CJK = r"一-鿿"


@dataclass(frozen=True)
class Clause:
    spec: str
    id: str
    page: str
    pdf_page: int
    title: str
    text: str
    spec_short: str = ""

    @property
    def ref(self) -> str:
        """引用寫法，例如「土木401-112 §18.4.3.2」「土木401-112 表18.4.5.4」。"""
        sep = " " if self.id[0] in "表圖" else " §"
        return f"{self.spec_short}{sep}{self.id}"

    @property
    def key(self) -> str:
        return f"{self.spec}:{self.id}"

    def where(self) -> str:
        return f"規範頁 {self.page}" if self.page else f"PDF 第 {self.pdf_page} 頁"


def _norm_id(cid: str) -> str:
    """「§ 18.4.3.2」「第18.4.3.2節」「表 18.4.5.4」「式(19.2.2.1.b)」→ 索引用條號。"""
    t = re.sub(r"[\s§第節條款]", "", str(cid))
    t = t.replace("（", "(").replace("）", ")")
    m = re.match(rf"^(表|圖)?(R?{CLAUSE_ID})", t.lstrip("式("))
    if not m:
        return t
    return (m.group(1) or "") + m.group(2)


class SpecLibrary:
    def __init__(self, data: dict):
        self.source = data.get("source", "")
        self.specs = {s["code"]: s for s in data["specs"]}
        self.clauses: dict[str, Clause] = {}
        for c in data["clauses"]:
            spec = self.specs[c["spec"]]
            cl = Clause(c["spec"], c["id"], c.get("page", ""), c.get("pdf_page", 0), c.get("title", ""), c["text"],
                        spec["short"])
            self.clauses[cl.key] = cl
        # 別名比對（長者優先，避免「耐震設計規範」吃掉「公路橋梁耐震設計規範」）
        alias = [(a, code) for code, s in self.specs.items() for a in [s["short"], *s.get("aliases", [])]]
        self._alias = sorted(alias, key=lambda x: -len(x[0]))
        pat = "|".join(re.escape(a) for a, _ in self._alias)
        self._ref_re = re.compile(
            rf"(?:(?P<spec>{pat})[^§表式\n]{{0,12}})?(?:§\s*|第\s*|(?P<kind>表|式)\s*\(?\s*)(?P<id>R?{CLAUSE_ID}(?:\.[a-z])?)")

    # ------------------------------------------------------------------
    def spec_code(self, text: str) -> str | None:
        for a, code in self._alias:
            if a in str(text):
                return code
        t = str(text).strip()
        return t if t in self.specs else None

    def get(self, spec: str, cid: str) -> Clause | None:
        code = self.spec_code(spec) or spec or DEFAULT_SPEC
        cid = _norm_id(cid)
        hit = self.clauses.get(f"{code}:{cid}")
        if hit or cid[:1] in "表圖":
            return hit
        # 式 (19.2.2.1.b) 之類的編號 → 所在條文
        parts = cid.split(".")
        while len(parts) > 2 and not hit:
            parts = parts[:-1]
            hit = self.clauses.get(f"{code}:{'.'.join(parts)}")
        return hit

    def exists(self, spec: str, cid: str) -> bool:
        code = self.spec_code(spec) or spec or DEFAULT_SPEC
        return f"{code}:{_norm_id(cid)}" in self.clauses

    def lookup(self, spec: str, cid: str, max_children: int = 12) -> list[Clause]:
        """取條文本身；若為節（如 18.4.3）則附上其下各條（如 18.4.3.1～18.4.3.3），以及同號之表。"""
        base = self.get(spec, cid)
        if not base:
            return []
        out = [base]
        prefix = base.id + "."
        kids = [c for c in self.clauses.values() if c.spec == base.spec and c.id.startswith(prefix)]
        out += sorted(kids, key=lambda c: [int(x) if x.isdigit() else 0 for x in c.id.split(".")])[:max_children]
        table = self.clauses.get(f"{base.spec}:表{base.id}")
        if table and table not in out:
            out.append(table)
        return out

    def search(self, query: str, specs: list[str] | None = None, k: int = 4) -> list[Clause]:
        """關鍵字查詢（中文以雙字詞比對，標題命中加權）。"""
        terms = [t for t in re.split(r"[\s、，,;；/]+", str(query)) if t]
        if not terms:
            return []
        grams = {t[i:i + 2] for t in terms for i in range(len(t) - 1)} | {t for t in terms if len(t) < 2}
        scored = []
        for c in self.clauses.values():
            if specs and c.spec not in specs:
                continue
            title_hits = sum(g in c.title for g in grams)
            body = sum(min(c.text.count(g), 4) for g in grams)
            body += sum(10 for t in terms if t in c.text or t in c.title)
            score = title_hits * 4 + body
            if score:
                scored.append((score, -len(c.text) // 400, c))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [c for *_, c in scored[:k]]

    def find_refs(self, text: str, default_spec: str = DEFAULT_SPEC) -> list[tuple[str, str]]:
        """從文字中找出條文引用，回傳 [(規範代碼, 條號)]（依出現順序、去重）。"""
        seen, out = set(), []
        for m in self._ref_re.finditer(str(text)):
            code = self.spec_code(m.group("spec")) if m.group("spec") else default_spec
            cid = m.group("id")
            if m.group("kind") == "表":
                cid = "表" + cid
            key = (code, _norm_id(cid))
            if key not in seen:
                seen.add(key)
                out.append(key)
        return out

    def check_refs(self, text: str, default_spec: str = DEFAULT_SPEC) -> list[str]:
        """回傳文字中「查無原文」的引用（供系統紀錄提醒技師核對）。"""
        missing = []
        for code, cid in self.find_refs(text, default_spec):
            if not self.get(code, cid):
                missing.append(f"{self.specs[code]['short']} {'' if cid[0] in '表圖' else '§'}{cid}")
        return missing

    def toc(self, specs: list[str], depth: int = 2) -> str:
        """章節目錄（條號至第 depth 層），讓 Agent 知道可以查哪些條文。"""
        lines = []
        for code in specs:
            s = self.specs[code]
            heads = [c for c in self.clauses.values()
                     if c.spec == code and c.id[0].isdigit() and c.id.count(".") < depth]
            heads.sort(key=lambda c: [int(x) for x in c.id.split(".")])
            lines.append(f"### [{code}] {s['name']}（引用簡稱：{s['short']}）")
            lines.append("；".join(f"{c.id} {c.title[:18]}" for c in heads))
        return "\n".join(lines)

    def catalog(self, specs: list[str]) -> str:
        return "\n".join(f"- [{c}] {self.specs[c]['name']}（引用簡稱：{self.specs[c]['short']}）" for c in specs)


@lru_cache(maxsize=1)
def load_library(path: str | None = None) -> SpecLibrary:
    with gzip.open(path or INDEX_FILE, "rt", encoding="utf-8") as f:
        return SpecLibrary(json.load(f))


def format_clauses(clauses: list[Clause], max_chars: int = 1800) -> str:
    """條文原文段落（提供給 Agent）。"""
    blocks = []
    for c in clauses:
        text = c.text if len(c.text) <= max_chars else c.text[:max_chars] + "…（節錄）"
        blocks.append(f"#### {c.ref}（{c.where()}）\n{text}")
    return "\n\n".join(blocks)


def scope_of(basis: str | None) -> list[str]:
    return SCOPE_SPECS.get(basis or "bldg", SCOPE_SPECS["bldg"])
