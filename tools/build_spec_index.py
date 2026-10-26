"""建立規範條文索引 data/spec_index.json.gz（開發時執行，產物提交進 repo）。

規範原文取自 rc-member-designer 的 specs/（政府機關公開之規範 PDF）：
    python tools/build_spec_index.py /path/to/rc-member-designer
未指定路徑時，從 GitHub 下載 specs/ 內的 PDF。

規範 PDF 的字型對照表不完整，pypdf 會讀成亂碼，因此這裡改用 PyMuPDF（僅建索引時需要，執行期不需要）：
    pip install pymupdf

條文以「條號開頭的行」切段（例如「18.4.4.1 柱之縱向鋼筋……」、「表 18.4.5.4 ……」），
每段保留條號、規範頁碼（如 18-61）、PDF 頁次、全文（含其後之解說），以及條文本文在 PDF 上的範圍
（報告附錄據此裁切原文版面；規範的公式是方程式物件，文字擷取會拆成零散字元，只有原文版面能正確呈現）。
"""

from __future__ import annotations

import gzip
import json
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "spec_index.json.gz"
REPO_RAW = "https://raw.githubusercontent.com/justin121710/rc-member-designer/main/"

# 代碼、檔案（相對 rc-member-designer）、全名、簡稱（引用時使用）、別名（辨識引用文字用）、適用類別
SPECS = [
    ("01", "specs/建築/01_建築物混凝土結構設計規範_112年版.pdf", "建築物混凝土結構設計規範（112 年版）", "土木401-112",
     ["土木401", "401-112", "建築物混凝土結構設計規範", "混凝土結構設計規範", "混凝土規範"], ["bldg", "bridge", "water"]),
    ("02", "specs/建築/02_建築物耐震設計規範及解說_113年3月.pdf", "建築物耐震設計規範及解說（113 年 3 月）", "建築物耐震設計規範",
     ["建築物耐震設計規範", "耐震設計規範及解說", "耐震設計規範", "耐震規範"], ["bldg"]),
    ("03", "specs/建築/03_建築物基礎構造設計規範_112年版.pdf", "建築物基礎構造設計規範（112 年版）", "建築物基礎構造設計規範",
     ["建築物基礎構造設計規範", "基礎構造設計規範", "基礎規範"], ["bldg", "water"]),
    ("11", "specs/橋梁/11_公路橋梁設計規範_98年12月.pdf", "公路橋梁設計規範（98 年 12 月）", "公路橋梁設計規範",
     ["公路橋梁設計規範", "橋梁設計規範"], ["bridge"]),
    ("12", "specs/橋梁/12_公路橋梁耐震設計規範_107年12月.pdf", "公路橋梁耐震設計規範（107 年 12 月）", "公路橋梁耐震設計規範",
     ["公路橋梁耐震設計規範", "橋梁耐震設計規範", "橋梁耐震規範"], ["bridge"]),
    ("22", "specs/下水道/22_下水道設施耐震設計及解說_109年11月.pdf", "下水道設施耐震設計及解說（109 年 11 月）", "下水道設施耐震設計及解說",
     ["下水道設施耐震設計及解說", "下水道設施耐震設計"], ["water"]),
]
# 下水道工程設施標準（21）為影像檔、沒有文字層，無法建立條文索引

CJK = "\u4e00-\u9fff"
CLAUSE = re.compile(rf"^\s*(R?)(\d{{1,2}}(?:\.\d{{1,3}}){{1,5}})\s+(?=[(（]?[a-zA-Z0-9]?[)）]?\s*[{CJK}])(.*)$")
TABLE = re.compile(r"^\s*(表|圖)\s*(R?\d{1,2}(?:\.\d{1,3}){1,5}(?:\.?[a-z0-9]+)?)\s*(.*)$")
PAGE_LABEL = re.compile(r"^\s*(\d{1,2})-(\d{1,3})\s*$")
MAX_CHARS = 4000

# 規範 PDF 的數學符號多用 Symbol 字型，PyMuPDF 讀成私用區 U+F0xx；依 Adobe Symbol 編碼轉回 Unicode
_SYMBOL_GREEK = dict(zip("abgdezhqiklmnxoprstufcywjvJ", "αβγδεζηθικλμνξοπρστυφχψωϕϖϑ"))
_SYMBOL_GREEK.update(zip("ABGDEZHQIKLMNXOPRSTUFCYW", "ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ"))
_SYMBOL_MISC = {0xA2: "′", 0xA3: "≤", 0xA5: "∞", 0xB0: "°", 0xB1: "±", 0xB2: "″", 0xB3: "≥", 0xB4: "×", 0xB5: "∝",
                0xB6: "∂", 0xB7: "•", 0xB8: "÷", 0xB9: "≠", 0xBA: "≡", 0xBB: "≈", 0xBC: "…", 0xAE: "→", 0xAC: "←",
                0xC5: "⊕", 0xC6: "∅", 0xD6: "√", 0xD7: "⋅", 0xD8: "¬", 0xDB: "⇔", 0xDE: "⇒", 0xE5: "∑", 0xF2: "∫",
                0x2D: "−", 0x22: "∀", 0x24: "∃", 0x27: "∋"}


def normalize(text: str) -> str:
    out = []
    for ch in text:
        o = ord(ch)
        if 0xF000 <= o <= 0xF0FF:
            c = o - 0xF000
            if chr(c) in _SYMBOL_GREEK:
                out.append(_SYMBOL_GREEK[chr(c)])
            elif c in _SYMBOL_MISC:
                out.append(_SYMBOL_MISC[c])
            elif 0x20 <= c < 0x7F:
                out.append(chr(c))
            # 其餘（括號、大括號拼接用的組件與填充字元）不保留
        elif 0xF900 <= o <= 0xFAFF:  # CJK 相容表意字轉為一般字，便於搜尋
            out.append(unicodedata.normalize("NFKC", ch))
        else:
            out.append(ch)
    return "".join(out)


def load_pdf(spec_file: str, repo: Path | None) -> bytes:
    if repo:
        return (repo / spec_file).read_bytes()
    url = REPO_RAW + urllib.parse.quote(spec_file)
    with urllib.request.urlopen(url, timeout=120) as r:  # noqa: S310
        return r.read()


def page_lines(page) -> list[tuple[str, tuple[float, float, float, float]]]:
    """PyMuPDF 頁面 → [(文字, (x0, y0, x1, y1))]，與 get_text() 相同的行序。"""
    out = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            text = normalize("".join(s["text"] for s in ln["spans"]).encode("utf-8", "replace").decode("utf-8"))
            out.append((text, tuple(round(v, 1) for v in ln["bbox"])))
    # 同一基線、左右相鄰的行合併（例如「6.2.5.1」與其後的條文、表格同列各欄），避免條號與內文被拆開
    merged: list[tuple[str, tuple]] = []
    for text, bb in out:
        if merged:
            ptext, pb = merged[-1]
            same_row = abs((bb[1] + bb[3]) / 2 - (pb[1] + pb[3]) / 2) < 2.5 and bb[0] >= pb[2] - 1
            if same_row and text.strip():
                merged[-1] = (ptext.rstrip() + " " + text.strip(), (pb[0], min(pb[1], bb[1]), bb[2], max(pb[3], bb[3])))
                continue
        merged.append((text, bb))
    return merged


def chunk_pages(pages: list[tuple[list, float]]) -> list[dict]:
    """pages：[(page_lines, 頁高)]。每段除全文外，另記條文本文（不含解說）在 PDF 上的範圍 box：
    [[PDF 頁次, x0, y0, x1, y1], …]（最多兩頁），供報告附錄直接裁切原文版面（公式、表格不會被拆散）。"""
    chunks: list[dict] = []
    cur: dict | None = None
    label = ""
    for pno, (lines, height) in enumerate(pages, 1):
        for line, (x0, y0, x1, y1) in lines:
            m = PAGE_LABEL.match(line)
            if m:
                label = f"{m.group(1)}-{m.group(2)}"
                continue
            m = CLAUSE.match(line)
            t = TABLE.match(line) if not m else None
            if m or t:
                cid = (m.group(1) + m.group(2)) if m else t.group(1) + t.group(2)
                title = (m.group(3) if m else t.group(3)).strip()
                cur = {"id": cid, "page": label, "pdf_page": pno, "title": re.split(r"[。：:]", title)[0][:40],
                       "lines": [line.strip()], "box": {}, "body": True}
                chunks.append(cur)
            elif cur is not None and line.strip():
                cur["lines"].append(line.strip())
                if line.strip().startswith("解說"):
                    cur["body"] = False
            else:
                continue
            if not cur["body"] or y1 < height * 0.06 or y0 > height * 0.94:  # 解說、頁首頁尾不納入裁切範圍
                continue
            b = cur["box"].get(pno)
            cur["box"][pno] = [min(b[0], x0), min(b[1], y0), max(b[2], x1), max(b[3], y1)] if b else [x0, y0, x1, y1]
    out: dict[str, dict] = {}
    for c in chunks:
        text = "\n".join(c.pop("lines"))
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS] + "…（以下省略，請對照原文）"
        c["text"] = text
        c["box"] = [[pg, *bb] for pg, bb in sorted(c["box"].items())][:2]
        c.pop("body")
        prev = out.get(c["id"])
        if prev is None or len(text) > len(prev["text"]):  # 目錄頁的同條號段落較短，保留正文
            out[c["id"]] = c
    return list(out.values())


def main() -> None:
    import fitz  # PyMuPDF

    repo = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    specs, clauses = [], []
    for code, file, name, short, aliases, scope in SPECS:
        doc = fitz.open(stream=load_pdf(file, repo), filetype="pdf")
        pages = [(page_lines(p), p.rect.height) for p in doc]
        items = chunk_pages(pages)
        specs.append({"code": code, "file": file, "name": name, "short": short, "aliases": aliases,
                      "scope": scope, "pages": len(pages), "clauses": len(items)})
        clauses += [{"spec": code, **c} for c in items]
        print(f"{code} {short}: {len(pages)} 頁、{len(items)} 段")
    data = {"source": "https://github.com/justin121710/rc-member-designer/tree/main/specs", "specs": specs,
            "clauses": clauses}
    OUT.parent.mkdir(exist_ok=True)
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with open(OUT, "wb") as fh, gzip.GzipFile(fileobj=fh, mode="wb", compresslevel=9, mtime=0) as f:  # mtime=0：重建結果可重現
        f.write(raw)
    print(f"已寫入 {OUT}（{OUT.stat().st_size / 1e6:.1f} MB）")


if __name__ == "__main__":
    main()
