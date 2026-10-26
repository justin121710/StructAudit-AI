"""結構計算書讀取：PDF（rc-member-designer 等工具輸出的完整計算書）或 Excel／CSV 摘要表。

PDF 會做兩件事：
1. 原檔保留，審查時直接附給模型（Gemini／OpenAI／Claude 皆可原生讀 PDF，含算式、圖與表格版面）；
2. 以 pypdf 擷取文字（加上頁碼標記），供條文引用比對、離線示範與不支援 PDF 的模型使用。

報告表頭的專案資料直接取自計算書表頭（rc-member-designer 計算書為「構件、構材類別、斷面、設計依據…」），
不再由使用者另行填寫；Agent 於 Round 1 另會補充擷取計算書中的其他專案資訊。
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from utils.parser import StructuralSummary, parse_structural_file

CJK = r"[⺀-鿿豈-﫿＀-￯　-〿]"
# rc-member-designer 計算書表頭欄位（rpHead）；長者在前，避免「斷面」吃掉「斷面區段」
RCMD_HEADER_KEYS = ["程式版本／雜湊", "構材類別", "斷面區段", "檢核斷面", "設計依據", "產生時間", "構件", "斷面", "環境", "單位"]
GENERIC_KEYS = ["工程名稱", "案名", "計畫名稱", "工程地點", "建築地點", "基地位置", "設計單位", "設計者", "設計人",
                "結構系統", "構件編號", "構件名稱", "樓層數", "用途"]
MAX_PROMPT_CHARS = 80_000


@dataclass
class CalcBook:
    filename: str
    sha256: str
    kind: str  # pdf／table
    title: str = ""
    pages: list[str] = field(default_factory=list)  # 每頁文字
    facts: dict[str, str] = field(default_factory=dict)  # 表頭專案資料（項目 → 內容）
    pdf: bytes | None = field(default=None, repr=False)
    table: StructuralSummary | None = field(default=None, repr=False)

    @property
    def n_pages(self) -> int:
        return len(self.pages)

    @property
    def module(self) -> str | None:
        """對應 rc-member-designer 的模組：pylon（柱／基樁）或 beam（梁／版）。"""
        head = self.title + self.facts.get("構材類別", "")
        if re.search(r"梁|版", head):
            return "beam"
        if re.search(r"柱|樁|墩|塔", head):
            return "pylon"
        return None

    @property
    def basis(self) -> str:
        """設計依據：bldg（建築物）／bridge（橋梁）／water（水工）。"""
        text = " ".join(self.facts.values()) + self.title
        if "水工" in text:
            return "water"
        if "橋梁" in text or "公路橋梁" in text:
            return "bridge"
        return "bldg"

    @property
    def from_rcmd(self) -> bool:
        return "程式版本／雜湊" in self.facts and "計算書" in self.title

    @property
    def has_text(self) -> bool:
        """PDF 是否有文字層；沒有時（掃描影像等）審查改由模型直接判讀附件 PDF 的頁面影像。"""
        return self.kind != "pdf" or any(p.strip() for p in self.pages)

    def text(self) -> str:
        if self.kind == "table" and self.table is not None:
            return self.table.to_prompt_text()
        if not self.has_text:
            return (f"（此 PDF 共 {self.n_pages} 頁，沒有文字層，可能是掃描檔或以影像方式列印。"
                    "請直接判讀附件 PDF 的頁面影像，並從第 1 頁表頭擷取專案資料。）")
        return "\n".join(f"<<第 {n} 頁>>\n{t}" for n, t in enumerate(self.pages, 1))

    def prompt_text(self, max_chars: int = MAX_PROMPT_CHARS) -> str:
        t = self.text()
        if len(t) > max_chars:
            t = t[:max_chars] + f"\n…（文字過長，其餘 {len(t) - max_chars} 字省略；請以附件 PDF 原檔為準）"
        return t

    def facts_text(self) -> str:
        return "\n".join(f"- {k}：{v}" for k, v in self.facts.items())


def clean_text(t: str) -> str:
    """pypdf 會在中文字之間插入空白（KaTeX 與字距排版造成），這裡移除，並去掉無法編碼的字元。"""
    t = t.encode("utf-8", "replace").decode("utf-8")
    t = re.sub(rf"(?<={CJK})[  ]+(?={CJK})", "", t)
    t = re.sub(rf"(?<={CJK})[  ]{{2,}}(?=\S)|(?<=\S)[  ]{{2,}}(?={CJK})", " ", t)
    return re.sub(r"[ \t]+\n", "\n", t)


def _pypdf_pages(data: bytes) -> list[str]:
    import pypdf

    logging.getLogger("pypdf").setLevel(logging.ERROR)  # 部分 PDF 的字型表會產生大量無害警告
    reader = pypdf.PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        reader.decrypt("")  # 只設權限密碼（可開啟、限制列印／複製）的 PDF
    return [p.extract_text() or "" for p in reader.pages]


def _pdfium_pages(data: bytes) -> list[str]:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(data)
    try:
        return [doc[i].get_textpage().get_text_range() for i in range(len(doc))]
    finally:
        doc.close()


def extract_pages(data: bytes) -> list[str]:
    """擷取每頁文字：先用 pypdf，字數過少或失敗時改用 pdfium，取擷取到較多文字者。
    兩者都沒有文字時（掃描影像、以影像方式列印），回傳空字串清單，由模型直接判讀 PDF 頁面影像。"""
    best: list[str] = []
    for reader in (_pypdf_pages, _pdfium_pages):
        try:
            pages = reader(data)
        except Exception:  # 損毀、加密等：交給下一個擷取器
            continue
        if sum(len(p.strip()) for p in pages) > sum(len(p.strip()) for p in best) or not best:
            best = pages
        if sum(len(p.strip()) for p in best) >= 50 * max(len(best), 1):
            break
    if not best:
        raise ValueError("無法開啟此 PDF（檔案損毀或需要開啟密碼）")
    return [clean_text(p) for p in best]


def header_facts(first_page: str) -> tuple[str, dict[str, str]]:
    """從第一頁表頭擷取（標題, {項目: 內容}）。"""
    lines = [x.strip() for x in first_page.splitlines() if x.strip()]
    lines = [x for x in lines if not re.match(r"^第\s*\d+\s*頁", x)]
    title, facts = "", {}
    head: list[str] = []
    for x in lines:
        if re.match(r"^1\s*[.．、]\s*\S", x):  # 第 1 章開始即結束表頭
            break
        head.append(x)
    for x in head:
        if not title and "計算書" in x:
            title = re.split(r"構件[：:]", x)[0].strip()
            m = re.search(r"構件[：:]\s*(\S+)", x)
            if m:
                facts["構件"] = m.group(1)
            continue
        keys = [k for k in RCMD_HEADER_KEYS if k != "構件"]
        pat = "|".join(re.escape(k) for k in keys)
        spots = [(m.start(), m.group()) for m in re.finditer(pat, x)]
        # 同一列可有兩組「項目 內容」（兩欄式表頭）；只在列首或前一組之後找項目名稱
        picked = []
        for pos, key in spots:
            if pos == 0 or (picked and pos > picked[-1][0] + len(picked[-1][1])):
                if pos == 0 or not picked or x[pos - 1] in "）) 0123456789cm":
                    picked.append((pos, key))
        for n, (pos, key) in enumerate(picked):
            end = picked[n + 1][0] if n + 1 < len(picked) else len(x)
            val = x[pos + len(key):end].strip(" ：:")
            if val and key not in facts:
                facts[key] = val
    for x in lines[:60]:
        m = re.match(rf"^({'|'.join(GENERIC_KEYS)})\s*[：:]\s*(.+)$", x)
        if m and m.group(1) not in facts:
            facts[m.group(1)] = m.group(2).strip()
    return title, facts


def read_calcbook(file: str | Path | bytes, filename: str | None = None) -> CalcBook:
    """讀取計算書：PDF 或 Excel／CSV。file 可為路徑或 bytes（bytes 需提供 filename）。"""
    if isinstance(file, (str, Path)):
        filename = filename or Path(file).name
        data = Path(file).read_bytes()
    else:
        data = bytes(file)
    if not filename:
        raise ValueError("bytes 輸入需提供 filename 以判斷格式")
    sha = hashlib.sha256(data).hexdigest()
    if Path(filename).suffix.lower() == ".pdf":
        pages = extract_pages(data)  # 沒有文字層時為空字串清單：仍可審查，模型直接判讀 PDF 頁面影像
        title, facts = header_facts(pages[0] if pages else "")
        return CalcBook(filename, sha, "pdf", title=title, pages=pages, facts=facts, pdf=data)
    summary = parse_structural_file(data, filename=filename)
    facts = {}
    for name, df in summary.frames.items():  # 「項目／內容」兩欄的參數表
        cols = [str(c).strip() for c in df.columns]
        if len(cols) == 2 and len(df) <= 40 and cols[0] in ("項目", "參數", "名稱", "設計參數", "item", "Item"):
            for k, v in df.itertuples(index=False):
                if isinstance(k, str) and str(v).strip() and str(v) != "nan":
                    facts.setdefault(k.strip(), str(v).strip())
    return CalcBook(filename, sha, "table", title="結構計算摘要表", facts=facts, table=summary)
