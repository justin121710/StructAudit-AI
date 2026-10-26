"""LaTeX 算式排版：報告中的檢核算式以 LaTeX 撰寫（與 rc-member-designer 計算書的 KaTeX 算式同一寫法）。

- 介面：Streamlit 直接以 KaTeX 顯示（$$…$$）。
- PDF：WeasyPrint 不執行 JavaScript，無法跑 KaTeX，改用 matplotlib mathtext 排成 SVG（Computer Modern 字面，
  外觀接近 KaTeX）內嵌；ReportLab 備援則用 PNG。
- Excel 與無法排版時：轉成可讀的純文字（例如 \\frac{a}{b} → (a)/(b)、\\phi → φ）。

mathtext 只支援 LaTeX 的子集，sanitize() 會先把常見的 KaTeX 寫法改成 mathtext 能排的形式；
仍無法排版的算式回傳 None，由呼叫端改顯示純文字，不會讓報告產生失敗。
"""

from __future__ import annotations

import base64
import io
import re
import threading
from functools import lru_cache

_LOCK = threading.Lock()  # matplotlib 的 rcParams 為全域狀態，Streamlit 多執行緒下需序列化
CJK = re.compile(r"[\u2E80-\u9FFF\uF900-\uFAFF\uFF00-\uFFEF\u3000-\u303F]")
CJK_FONTS = ["Noto Serif CJK TC", "Noto Sans CJK TC", "Noto Sans CJK JP", "WenQuanYi Zen Hei",
             "Microsoft JhengHei", "PingFang TC", "Arial Unicode MS"]

# KaTeX → mathtext 可排版的寫法
_REPLACE = [
    (r"\displaystyle", ""), (r"\textstyle", ""), (r"\dfrac", r"\frac"), (r"\tfrac", r"\frac"),
    (r"\left.", ""), (r"\right.", ""), (r"\lvert", "|"), (r"\rvert", "|"), (r"\coloneqq", ":="),
    (r"\textbf", r"\mathbf"), (r"\textrm", r"\mathrm"), (r"\mathit", r"\mathit"), ("−", "-"), ("≤", r"\leq "),
    ("≥", r"\geq "), ("×", r"\times "), ("·", r"\cdot "), ("φ", r"\phi "), ("ϕ", r"\phi "), ("ρ", r"\rho "),
    ("Σ", r"\Sigma "), ("≠", r"\neq "), ("≈", r"\approx "), ("′", "'"),
]


def sanitize(tex: str) -> str:
    t = str(tex or "").strip()
    t = re.sub(r"^\$+|\$+$", "", t).strip()  # 模型偶爾自行加上 $…$
    t = re.sub(r"^\\\[|\\\]$", "", t).strip()
    for a, b in _REPLACE:
        t = t.replace(a, b)
    t = re.sub(r"\\(lt|gt)(?![a-zA-Z])", lambda m: "<" if m.group(1) == "lt" else ">", t)
    t = re.sub(r"\\(le|ge)(?![a-zA-Z])", lambda m: "\\" + m.group(1) + "q", t)
    t = re.sub(r"\\operatorname\{([^}]*)\}", r"\\mathrm{\1}", t)
    t = re.sub(r"\\boxed\{([^}]*)\}", r"\1", t)
    t = re.sub(r"\\begin\{(aligned|align\*?|split)\}|\\end\{(aligned|align\*?|split)\}", "", t)
    t = t.replace("&", "")
    t = re.sub(r"\\text\{([^}]*)\}", lambda m: r"\mathrm{" + m.group(1).replace(" ", r"\ ") + "}", t)
    t = re.sub(r"(?<!\\)\\\\", r"\\quad ", t)  # 呼叫端應先以 split_lines() 斷行；漏斷的改為空白
    t = re.sub(f"({CJK.pattern}+)", r"\\mathrm{\1}", t)  # 中文一律排正體，避免落到斜體字型而缺字
    t = re.sub(r"(?<![\\A-Za-z])(\d+(?:\.\d+)?)", r"{\\mathrm{\1}}", t)  # 數字排正體（與 KaTeX／LaTeX 相同）
    return t.strip()


def split_lines(tex: str) -> list[str]:
    """多行算式（\\\\ 斷行）拆成多條，各自排版。"""
    return [x.strip() for x in re.split(r"\\\\", str(tex or "")) if x.strip()]


def _cjk_font() -> str | None:
    from matplotlib import font_manager

    names = {f.name for f in font_manager.fontManager.ttflist}
    return next((n for n in CJK_FONTS if n in names), None)


@lru_cache(maxsize=2048)
def _render(tex: str, fmt: str, size: float, dpi: int) -> bytes | None:
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import mathtext
    from matplotlib.font_manager import FontProperties

    t = sanitize(tex)
    if not t:
        return None
    rc = {"mathtext.fontset": "cm", "svg.fonttype": "path", "mathtext.default": "it"}
    if CJK.search(t):  # Computer Modern 沒有中文字，含中文的算式改用中文字型排正體字
        font = _cjk_font()
        if not font:
            return None
        rc.update({"mathtext.fontset": "custom", "mathtext.rm": font, "mathtext.bf": font,
                   "mathtext.it": "DejaVu Serif:italic", "mathtext.sf": font})
    buf = io.BytesIO()
    try:
        with _LOCK, matplotlib.rc_context(rc):
            mathtext.math_to_image(f"${t}$", buf, prop=FontProperties(size=size), dpi=dpi, format=fmt)
    except Exception:  # mathtext 不支援的語法
        return None
    return buf.getvalue()


def to_svg(tex: str, size: float = 10.5) -> str | None:
    """回傳可直接嵌入 HTML 的 <img>（data URI，SVG 向量）；無法排版時回傳 None。"""
    svg = _render(str(tex), "svg", size, 72)
    if not svg:
        return None
    w = re.search(rb'width="([\d.]+)pt"', svg)
    width = f' style="width:{float(w.group(1)):.1f}pt;max-width:100%"' if w else ""
    return f'<img class="tex" alt="" src="data:image/svg+xml;base64,{base64.b64encode(svg).decode()}"{width}>'


def to_png(tex: str, size: float = 10.5, dpi: int = 220) -> bytes | None:
    return _render(str(tex), "png", size, dpi)


_SYMBOLS = {
    r"\phi": "φ", r"\varphi": "φ", r"\rho": "ρ", r"\Sigma": "Σ", r"\sum": "Σ", r"\Delta": "Δ", r"\delta": "δ",
    r"\varepsilon": "ε", r"\epsilon": "ε", r"\beta": "β", r"\alpha": "α", r"\lambda": "λ", r"\theta": "θ",
    r"\psi": "ψ", r"\gamma": "γ", r"\mu": "μ", r"\pi": "π", r"\sigma": "σ", r"\tau": "τ", r"\omega": "ω",
    r"\Omega": "Ω", r"\eta": "η", r"\xi": "ξ", r"\ell": "ℓ", r"\times": "×", r"\cdot": "·", r"\leq": "≤",
    r"\geq": "≥", r"\le": "≤", r"\ge": "≥", r"\neq": "≠", r"\approx": "≈", r"\pm": "±", r"\infty": "∞",
    r"\Rightarrow": "⇒", r"\rightarrow": "→", r"\to": "→", r"\circ": "°", r"\%": "%", r"\quad": "  ",
    r"\qquad": "    ", r"\,": " ", r"\;": " ", r"\:": " ", r"\!": "", r"\ ": " ", r"\max": "max", r"\min": "min",
    r"\left": "", r"\right": "", r"\cdots": "⋯", r"\ldots": "…", r"\prime": "′",
}


def to_text(tex: str) -> str:
    """LaTeX 轉成可讀純文字（Excel、無法排版時使用）。"""
    t = str(tex or "").strip().strip("$")
    t = re.sub(r"\\(?:begin|end)\{[^}]*\}", "", t).replace("&", "")
    t = re.sub(r"\\(?:mathrm|text|mathbf|textbf|operatorname|mathit|boxed)\{([^{}]*)\}", r"\1", t)
    t = re.sub(r"_\{([^{}]*)\}", r"_\1", t)
    t = re.sub(r"\^\{([^{}]*)\}", r"^\1", t)
    for _ in range(4):  # 由內而外展開巢狀分式與根號
        t = re.sub(r"\\[dt]?frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", t)
        t = re.sub(r"\\sqrt\{([^{}]*)\}", r"√(\1)", t)
    for k in sorted(_SYMBOLS, key=len, reverse=True):
        t = t.replace(k, _SYMBOLS[k])
    t = re.sub(r"\\displaystyle|\\textstyle", "", t)
    t = t.replace("{", "").replace("}", "").replace("\\\\", "；")
    t = re.sub(r"\\([a-zA-Z]+)", r"\1", t)
    return re.sub(r"[ \t]{2,}", " ", t).strip()
