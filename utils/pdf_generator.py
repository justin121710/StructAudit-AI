"""A4 審查報告產生器。

版面沿用 rc-member-designer 計算書（見 utils/report_model.py），算式以 LaTeX 排版（utils/latex.py）。
附錄的規範條文直接裁切規範原文 PDF 的版面（utils/specs.py clause_png），公式與表格與原文一致。
優先以 Jinja2 HTML 範本 + WeasyPrint 排版（算式為向量 SVG）；若環境缺少 WeasyPrint 所需的 Pango 等系統函式庫，
改用 ReportLab 依同一份報表資料模型產生相同章節結構的 PDF（算式為高解析 PNG）。
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from xml.sax.saxutils import escape

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from orchestrator import ReviewSession
from utils import latex
from utils.report_model import ReportModel, build_model, meta_rows, report_number
from utils.specs import Clause, clause_png

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
TEMPLATE_NAME = "report_template.html"

__all__ = ["render_html", "generate_pdf", "report_number"]


def tex_html(formulas) -> Markup:
    """LaTeX 算式 → HTML（SVG 圖）；無法排版者以純文字顯示。"""
    if isinstance(formulas, str):
        formulas = [formulas]
    out = []
    for f in formulas or []:
        for line in latex.split_lines(f):
            img = latex.to_svg(line)
            out.append(f'<div class="rp-eq">{img}</div>' if img
                       else f'<div class="rp-eq"><code>{escape(latex.to_text(line))}</code></div>')
    return Markup("".join(out))


CLAUSE_DPI = 150


def clause_images(clauses: list[Clause]) -> dict[str, bytes]:
    """附錄條文的原文版面（PNG）；取不到者不列入，由呼叫端改顯示擷取文字。"""
    out = {}
    for c in clauses:
        png = clause_png(c.spec, c.id, CLAUSE_DPI)
        if png:
            out[c.key] = png
    return out


def _img_tag(png: bytes) -> Markup:
    from PIL import Image

    w = Image.open(io.BytesIO(png)).width * 72 / CLAUSE_DPI
    return Markup(f'<img alt="" src="data:image/png;base64,{base64.b64encode(png).decode()}" '
                  f'style="width:{w:.0f}pt;max-width:100%">')


def render_html(session: ReviewSession) -> str:
    m = build_model(session)
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=select_autoescape(["html"]))
    env.filters["tex"] = tex_html
    images = {k: _img_tag(v) for k, v in clause_images(m.clauses).items()}
    return env.get_template(TEMPLATE_NAME).render(m=m, meta_rows=meta_rows(m.meta), images=images)


def generate_pdf(session: ReviewSession, engine: str = "auto") -> tuple[bytes, str]:
    """回傳 (PDF bytes, 實際使用的引擎名稱)。engine: auto / weasyprint / reportlab。"""
    if engine in ("auto", "weasyprint"):
        try:
            from weasyprint import HTML

            html = render_html(session)
            return HTML(string=html, base_url=str(TEMPLATE_DIR)).write_pdf(), "weasyprint"
        except (ImportError, OSError) as e:
            if engine == "weasyprint":
                raise RuntimeError(f"WeasyPrint 無法使用：{e}") from e
    return _reportlab_pdf(build_model(session)), "reportlab"


# ----------------------------------------------------------------------
# ReportLab 備援
# ----------------------------------------------------------------------
# 可嵌入 PDF 的中文字型（依序尋找）；都找不到時改用 Adobe CID 字型（不嵌入，需閱讀器支援）
CJK_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "C:/Windows/Fonts/msjh.ttc",
    "C:/Windows/Fonts/mingliu.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
]
PNG_DPI = 220
APPENDIX_NOTE = ("以下條文直接裁切自規範原文 PDF（rc-member-designer specs/），公式與表格均為原文版面；"
                 "僅列條文本文，解說請對照原文頁次。")


def _register_cjk_font(pdfmetrics, UnicodeCIDFont) -> str:
    from reportlab.pdfbase.ttfonts import TTFont

    for path in CJK_FONT_CANDIDATES:
        if Path(path).exists():
            try:
                pdfmetrics.registerFont(TTFont("CJK", path, subfontIndex=0))
                return "CJK"
            except Exception:  # 部分 .ttc 為 CFF 外框，ReportLab 不支援
                continue
    pdfmetrics.registerFont(UnicodeCIDFont("MSung-Light"))
    return "MSung-Light"


def _reportlab_pdf(m: ReportModel) -> bytes:
    import io

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import (
        HRFlowable, Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    )

    font = _register_cjk_font(pdfmetrics, UnicodeCIDFont)
    base = ParagraphStyle("base", fontName=font, fontSize=9, leading=13, wordWrap="CJK")
    note = ParagraphStyle("note", parent=base, fontSize=8, leading=12, textColor=colors.HexColor("#444444"),
                          leftIndent=10)
    eq = ParagraphStyle("eq", parent=base, leftIndent=12, textColor=colors.HexColor("#222222"))
    h1 = ParagraphStyle("h1", parent=base, fontSize=14, leading=20)
    h2 = ParagraphStyle("h2", parent=base, fontSize=10.5, leading=15, spaceBefore=10)
    h3 = ParagraphStyle("h3", parent=base, fontSize=9.5, leading=14, spaceBefore=6, spaceAfter=3)
    width = A4[0] - 24 * mm
    grid_c, head_c, kv_c = colors.HexColor("#999999"), colors.HexColor("#eeeeee"), colors.HexColor("#f2f2f2")

    def P(t, st=base):
        return Paragraph(escape(str(t or "")).replace("\n", "<br/>"), st)

    def B(t, st=base):
        return Paragraph("<b>" + escape(str(t or "")).replace("\n", "<br/>") + "</b>", st)

    def judge(t):
        return B(t) if t == "NG" else P(t)

    def tex(formulas, max_w=width - 14 * mm):
        out = []
        for f in formulas or []:
            for line in latex.split_lines(f):
                png = latex.to_png(line, dpi=PNG_DPI)
                if not png:
                    out.append(P(latex.to_text(line), eq))
                    continue
                w, h = ImageReader(io.BytesIO(png)).getSize()
                w, h = w * 72 / PNG_DPI, h * 72 / PNG_DPI
                k = min(1.0, max_w / w)
                img = Image(io.BytesIO(png), width=w * k, height=h * k)
                img.hAlign = "LEFT"
                out.append(img)
        return out

    def heading2(t):
        return [B(t, h2), HRFlowable(width="100%", thickness=1.4, color=colors.HexColor("#333333"),
                                     spaceBefore=1, spaceAfter=5)]

    def grid(rows, widths, kv=False):
        t = Table(rows, colWidths=[w * width for w in widths], repeatRows=0 if kv else 1)
        style = [("GRID", (0, 0), (-1, -1), 0.5, grid_c), ("VALIGN", (0, 0), (-1, -1), "TOP")]
        style.append(("BACKGROUND", (0, 0), (0, -1), kv_c) if kv else ("BACKGROUND", (0, 0), (-1, 0), head_c))
        t.setStyle(TableStyle(style))
        return t

    def solution(sol, rec=False):
        body = [B(("建議採用｜" if rec else "") + sol.label), P(f"做法：{sol.approach}"), P(f"改動幅度：{sol.change_extent}")]
        if sol.changes:
            rows = [[B(h) for h in ("rc-member-designer 欄位", "參數", "目前值", "建議值", "單位")]]
            rows += [[P(c.param_id or "—"), P(c.label), P(c.current), B(c.proposed), P(c.unit)] for c in sol.changes]
            body.append(grid(rows, [0.2, 0.3, 0.15, 0.15, 0.12]))
        body.append(P("修正後驗算："))
        body += tex(sol.verification)
        if sol.tool_steps:
            body.append(P(f"工具操作：{sol.tool_steps}", note))
        if sol.other_method:
            body.append(P(f"其他作法：{sol.other_method}", note))
        return body

    story = []
    title = Table([[B(m.title, h1), P(m.project_name)]], colWidths=[0.6 * width, 0.4 * width])
    title.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, 0), 1.4, colors.HexColor("#333333")),
                               ("ALIGN", (1, 0), (1, 0), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "BOTTOM")]))
    story.append(title)
    meta, spans = [], []
    for n, row in enumerate(meta_rows(m.meta)):
        if len(row) == 1:
            meta.append([B(row[0][0]), P(row[0][1]), "", ""])
            spans.append(("SPAN", (1, n), (3, n)))
        else:
            cells = [x for k, v in row for x in (B(k), P(v))]
            meta.append(cells + [""] * (4 - len(cells)))
    mt = Table(meta, colWidths=[w * width for w in (0.16, 0.34, 0.16, 0.34)])
    mt.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#dddddd")),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"), *spans]))
    story += [mt, Spacer(1, 6)]

    story += heading2("1. 審查依據與輸入資料")
    story.append(grid([[B(k), P(v)] for k, v in m.source_rows], [0.24, 0.76], kv=True))
    story.append(P("本審查不預設審查門檻：各項限值、係數均由 Agent 自行查閱規範原文（附錄 A）後判斷；計算書 PDF 原檔直接提供給模型判讀。", note))

    story += heading2("2. 角色配置與輪替")
    story.append(grid([[B(h) for h in ("輪次", "Agent A｜原設計工程師", "Agent B｜外部審查專家", "Agent C｜主任檢核技師")]]
                      + [[P(x) for x in r] for r in m.role_rows], [0.12, 0.29, 0.29, 0.30]))
    story.append(P(f"每一輪均為全新對話，提示中不含其他輪的內容，同一模型在不同輪次不共用記憶；綜合裁決：{m.final_model}（全新對話）。", note))

    story += heading2("3. 綜合審查結論")
    story.append(P(m.executive_summary))
    if m.cross_pass_notes:
        story.append(P(f"各輪一致性：{m.cross_pass_notes}", note))
    if m.items:
        rows = [[B(h) for h in ("編號", "項目", "風險等級", "判定", "來源", "綜合結論")]]
        rows += [[P(i.no), P(i.title), P(i.risk), judge(i.judge), P("、".join(i.sources)), P(i.finding)] for i in m.items]
        story.append(grid(rows, [0.06, 0.18, 0.1, 0.07, 0.14, 0.45]))

    story += heading2("4. 解決方案（改動最小且符合規範）")
    for n, it in enumerate([i for i in m.items if i.solution or i.required_action], 1):
        block = [B(f"4.{n} {it.title}（{it.risk}）", h3)]
        if it.clauses:
            block.append(P("依據：" + "、".join(it.clauses), note))
        if it.solution:
            block += solution(it.solution, True)
        if it.why_minimal:
            block.append(P(f"為何改動最小：{it.why_minimal}", note))
        if it.required_action:
            block.append(P(f"應辦事項：{it.required_action}", note))
        story += block
    story.append(B("rc-member-designer 調整總結", h3))
    story.append(P(f"工具：{m.tool_url}" + (f"\n{m.tool_summary}" if m.tool_summary else ""), note))
    if m.adjusted_applied:
        story.append(P("已寫入調整後案件檔：\n" + "\n".join(f"・{x}" for x in m.adjusted_applied), note))
    if m.adjusted_rejected:
        story.append(P("未能自動套用：\n" + "\n".join(f"・{x}" for x in m.adjusted_rejected), note))

    story += heading2("5. 各輪審查歷程")
    for k, p in enumerate(m.passes, 1):
        story.append(B(f"5.{k} 第 {p.no} 輪（本輪結論：{p.conclusion}）", h3))
        story.append(P("；".join(f"{r}＝{mm_}" for r, mm_ in p.roles) + "\n規範查閱：" + "；".join(p.lookups)
                       + f"\nRound 1 外審總體意見：{p.round1_summary}", note))
        for t in p.threads:
            i, r, v, c = t["issue"], t["response"], t["verdict"], t["chief"]
            head = f"{i.issue_id}　{i.title}（{i.category}）" + (f"　— 終審：{v.decision.value}／{v.risk_level.value}" if v else "")
            rows = [[B(f"R1 外審質疑\n初判 {i.preliminary_risk.value}"),
                     [P(i.challenge), *tex(i.formulas),
                      P(f"引用數據：{i.cited_data}（{i.calc_ref}）\n依據：{'、'.join(i.code_clauses)}——{i.code_quote}", note)]]]
            if r:
                body = [P(r.response), *tex(r.calculations)]
                if r.proposed_action:
                    body.append(P(f"修正方案：{r.proposed_action}"))
                body.append(P(f"數據出處：{r.data_source}｜規範依據：{'、'.join(r.code_clauses) or '—'}", note))
                rows.append([B(f"R2 設計回覆\n（{r.stance.value}）"), body])
            if v:
                body = [P(v.rationale), *tex(v.check_formulas)]
                if v.required_action:
                    body.append(P(f"應辦事項：{v.required_action}", note))
                body += [P(f"方案 {n}{'（外審建議）' if n == v.recommended_solution else ''}：{s.label}｜{s.change_extent}", note)
                         for n, s in enumerate(v.solutions, 1)]
                rows.append([B(f"R3 外審終審\n（{v.decision.value}）"), body])
            if c:
                body = [P(c.finding)]
                if c.solution_check:
                    body.append(P(f"方案複核：{c.solution_check}"))
                body += tex(c.check_formulas)
                if t["adopted"]:
                    body.append(P(f"採用：{t['adopted'].label}", note))
                rows.append([B(f"主任技師\n（{c.risk_level.value}）"), body])
            story.append(B(head, h3))
            story.append(grid(rows, [0.16, 0.84], kv=True))
        story.append(P(f"主任技師總結：{p.executive_summary}" + (f"\n審查過程觀察：{p.observations}" if p.observations else ""), note))

    story += heading2("檢核彙總與總判定")
    rows = [[B(h) for h in ("編號", "檢核項目", "來源", "風險等級", "判定", "建議方案", "依據")]]
    rows += [[P(s.no), P(s.title), P(s.sources), P(s.risk), judge(s.judge), P(s.solution), P(s.ref)] for s in m.summary_rows]
    story.append(grid(rows, [0.06, 0.2, 0.12, 0.1, 0.07, 0.22, 0.23]))
    border = {"bad": "#cc0000", "warn": "#9A5E10", "ok": "#006600"}[m.verdict_level]
    vt = Table([[B(m.verdict_text, ParagraphStyle("v", parent=base, fontSize=11, leading=15))]])
    vt.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 1.6, colors.HexColor(border)),
                            ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    vt.hAlign = "LEFT"
    story += [Spacer(1, 4), vt]
    if m.verdict_items:
        label = "高風險項目" if m.verdict_level == "bad" else "中風險項目"
        story.append(P(f"{label}：" + "；".join(f"({k}) {t}" for k, t in enumerate(m.verdict_items, 1)), note))
    story.append(P("總判定依綜合裁決：有任一高風險項目為「退回補正」，僅有中風險項目為「修正後同意」，其餘為「同意備查」；"
                   "且不寬於任一輪之結論。", note))

    story += heading2("附註")
    lines = [
        "1. 本報告由大型語言模型輔助產生，僅供結構技師審查參考，不得取代依法辦理之結構簽證或外審。",
        "2. Agent 引用之條文已逐條比對規範原文索引，查無者列於系統紀錄；條文要點與數值仍應由審查技師對照原文確認。",
        "3. 判定用語：OK＝低風險（疑義解除）；注意＝中風險（須補充資料或修正後複核）；NG＝高風險（不符規範或影響結構安全）。",
        "4. 建議方案之參數須於 rc-member-designer 重新計算確認各檢核項目轉為 OK 後，始得作為修正依據。",
    ] + [f"{k}. 系統紀錄：{n}" for k, n in enumerate(m.notes, 5)]
    story.append(P("\n".join(lines), note))
    story.append(P("本報告未涵蓋、須另行檢核：" + "；".join(f"({k}) {t}" for k, t in enumerate(m.uncovered, 1)) + "。", note))

    if m.clauses:
        story += heading2("附錄 A　引用規範條文（原文版面）")
        story.append(P(APPENDIX_NOTE, note))
        images = clause_images(m.clauses)
        for c in m.clauses:
            block = [B(f"{c.ref}（{c.where()}）", h3)]
            png = images.get(c.key)
            if png:
                w, h = ImageReader(io.BytesIO(png)).getSize()
                w, h = w * 72 / CLAUSE_DPI, h * 72 / CLAUSE_DPI
                k = min(1.0, width / w)
                img = Image(io.BytesIO(png), width=w * k, height=h * k)
                img.hAlign = "LEFT"
                block.append(img)
            else:
                block.append(P("（無法取得規範原文 PDF，以下為擷取文字，公式可能不完整）\n"
                               + c.text[:700] + ("…" if len(c.text) > 700 else ""), note))
            story.append(KeepTogether(block))

    sign = Table([[B(r) for r in m.sign_roles], [""] * 3, [P("日期：")] * 3],
                 colWidths=[width / 3] * 3, rowHeights=[None, 16 * mm, None])
    sign.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, grid_c), ("BACKGROUND", (0, 0), (-1, 0), head_c),
                              ("ALIGN", (0, 0), (-1, 0), "CENTER")]))
    story += [Spacer(1, 14), KeepTogether([sign])]

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(font, 8)
        canvas.setFillColor(colors.HexColor("#444444"))
        canvas.drawRightString(A4[0] - 12 * mm, 9 * mm, f"第 {doc.page} 頁")
        canvas.drawString(12 * mm, 9 * mm, m.report_no)
        canvas.restoreState()

    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=14 * mm,
                      bottomMargin=16 * mm, title=m.title).build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
