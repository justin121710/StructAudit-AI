"""A4 審查報告產生器。

版面沿用 rc-member-designer 計算書（見 utils/report_model.py）。
優先以 Jinja2 HTML 範本 + WeasyPrint 排版；若環境缺少 WeasyPrint 所需的 Pango 等系統函式庫，
改用 ReportLab 依同一份報表資料模型產生相同章節結構的 PDF。
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

from jinja2 import Environment, FileSystemLoader, select_autoescape

from orchestrator import ReviewSession
from utils.report_model import JUDGE, ReportModel, build_model, meta_rows, report_number

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
TEMPLATE_NAME = "report_template.html"

__all__ = ["render_html", "generate_pdf", "report_number"]


def render_html(session: ReviewSession) -> str:
    m = build_model(session)
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=select_autoescape(["html"]))
    return env.get_template(TEMPLATE_NAME).render(m=m, meta_rows=meta_rows(m.meta), judge=JUDGE)


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
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import (
        HRFlowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    )

    font = _register_cjk_font(pdfmetrics, UnicodeCIDFont)
    base = ParagraphStyle("base", fontName=font, fontSize=9, leading=13, wordWrap="CJK")
    note = ParagraphStyle("note", parent=base, fontSize=8, leading=12, textColor=colors.HexColor("#444444"),
                          leftIndent=10)
    eq = ParagraphStyle("eq", parent=base, leftIndent=10, textColor=colors.HexColor("#222222"))
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

    def heading2(t):
        return [B(t, h2), HRFlowable(width="100%", thickness=1.4, color=colors.HexColor("#333333"),
                                     spaceBefore=1, spaceAfter=5)]

    def grid(rows, widths, kv=False):
        t = Table(rows, colWidths=[w * width for w in widths], repeatRows=0 if kv else 1)
        style = [("GRID", (0, 0), (-1, -1), 0.5, grid_c), ("VALIGN", (0, 0), (-1, -1), "TOP")]
        style.append(("BACKGROUND", (0, 0), (0, -1), kv_c) if kv else ("BACKGROUND", (0, 0), (-1, 0), head_c))
        t.setStyle(TableStyle(style))
        return t

    story = []
    # 表頭：左標題、右案名，下方兩欄式資料表
    title = Table([[B(m.title, h1), P(f"案名：{m.project_name}" if m.project_name else "")]],
                  colWidths=[0.6 * width, 0.4 * width])
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

    # 1
    story += heading2("1. 審查依據與輸入資料")
    story.append(grid([[B(k), P(v)] for k, v in m.source_rows], [0.22, 0.78], kv=True))
    story.append(B("1.0 規則式初篩門檻之依據", h3))
    story.append(grid([[B("項目"), B("採用"), B("出處")]] + [[P(a), P(b), P(c)] for a, b, c in m.limit_rows],
                      [0.28, 0.36, 0.36]))
    story.append(P("門檻值由審查者於介面設定；初篩僅就上傳之摘要表逐列比對，作為詰問審查之客觀錨點，不取代逐項檢核。", note))

    # 2
    story += heading2("2. 規則式初篩結果")
    if m.prechecks:
        rows = [[B(h) for h in ("項目", "工作表", "說明", "判定", "依據")]]
        rows += [[P(f["item"]), P(f["sheet"]), P(f["detail"]), judge(f["judge"]), P(f["reference"])] for f in m.prechecks]
        story.append(grid(rows, [0.2, 0.13, 0.27, 0.08, 0.32]))
    else:
        story.append(P("未發現超出門檻之數值。", note))

    # 3
    story += heading2("3. 詰問審查歷程")
    story.append(P(f"Round 1 外審總體意見：{m.round1_summary}", note))
    for n, t in enumerate(m.threads, 1):
        i, r, v = t["issue"], t["response"], t["verdict"]
        head = f"3.{n} {i.issue_id}　{i.title}（{i.category}）"
        if v:
            head += f"　— 終審：{v.decision.value}／{JUDGE[v.risk_level]}"
        rows = [[B("R1 外審質疑"), [P(i.challenge), P(i.formula, eq),
                                     P(f"引用數據：{i.cited_data}｜依據：{i.code_reference}", note)]]]
        if r:
            body = [P(r.response)]
            if r.calculation:
                body.append(P(r.calculation, eq))
            if r.proposed_action:
                body.append(P(f"修正方案：{r.proposed_action}"))
            body.append(P(f"數據出處：{r.data_source}｜規範依據：{r.code_reference}", note))
            rows.append([B(f"R2 設計回覆\n（{r.stance.value}）"), body])
        if v:
            body = [P(v.rationale)]
            if v.required_action:
                body.append(P(f"應辦事項：{v.required_action}", note))
            rows.append([B(f"R3 外審終審\n（{v.decision.value}）"), body])
        story.append(KeepTogether([B(head, h3), grid(rows, [0.16, 0.84], kv=True)]))

    # 4
    story += heading2("4. 主任檢核技師裁決")
    story.append(P(m.executive_summary))
    for n, (_, label, items) in enumerate(m.buckets, 1):
        story.append(B(f"4.{n} {label}（{len(items)} 項）", h3))
        if not items:
            story.append(P("無。", note))
            continue
        rows = [[B(h) for h in ("編號", "項目", "檢核結論", "判定依據", "處理意見")]]
        rows += [[P(it.issue_id), P(it.title), P(it.finding), P(it.basis), P(it.recommendation)] for it in items]
        story.append(grid(rows, [0.08, 0.16, 0.36, 0.2, 0.2]))
    if m.observations:
        story += [B("4.4 審查過程觀察", h3), P(m.observations, note)]

    # 彙總與總判定
    story += heading2("檢核彙總與總判定")
    rows = [[B(h) for h in ("檢核項目", "分類", "外審終審", "風險等級", "判定", "主任技師分類", "依據")]]
    rows += [[P(f"{s.issue_id} {s.title}"), P(s.category), P(s.decision), P(s.risk), judge(s.judge), P(s.bucket), P(s.ref)]
             for s in m.summary_rows]
    story.append(grid(rows, [0.22, 0.1, 0.1, 0.1, 0.07, 0.12, 0.29]))
    border = {"bad": "#cc0000", "warn": "#9A5E10", "ok": "#006600"}[m.verdict_level]
    vt = Table([[B(m.verdict_text, ParagraphStyle("v", parent=base, fontSize=11, leading=15))]])
    vt.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 1.6, colors.HexColor(border)),
                            ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    vt.hAlign = "LEFT"
    story += [Spacer(1, 4), vt]
    if m.verdict_items:
        label = "重大疑義" if m.verdict_level == "bad" else "待複核項目"
        story.append(P(f"{label}：" + "；".join(f"({k}) {t}" for k, t in enumerate(m.verdict_items, 1)), note))
    story.append(P("總判定依主任檢核技師分類：有任一重大疑義為「退回補正」，僅有待複核項目為「修正後同意」，其餘為「同意備查」；"
                   "「未涵蓋」項目須另行檢核，不計入本判定。", note))

    # 附註
    story += heading2("附註")
    lines = [
        "1. 本報告由大型語言模型輔助產生，僅供結構技師審查參考，不得取代依法辦理之結構簽證或外審。",
        "2. 模型引用之條號與數值應由審查技師對照規範原文確認；初篩條號已依土木401-112 與建築物耐震設計規範及解說（113 年版）原文核對。",
        "3. 判定用語：OK＝疑義解除；注意＝須補充資料或修正後複核；NG＝不符規範或影響結構安全。",
    ] + [f"{k}. 系統紀錄：{n}" for k, n in enumerate(m.notes, 4)]
    story.append(P("\n".join(lines), note))
    story.append(P("本報告未涵蓋、須另行檢核：" + "；".join(f"({k}) {t}" for k, t in enumerate(m.uncovered, 1)) + "。", note))

    # 簽核欄（審查／校核／審核）
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
