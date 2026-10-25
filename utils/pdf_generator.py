"""A4 審查報告產生器。

優先以 Jinja2 HTML 範本 + WeasyPrint 排版（版面最完整）；
若環境缺少 WeasyPrint 所需的 Pango 等系統函式庫，則改用 ReportLab 產生簡化版 PDF
（使用內建 CID 字型 MSung-Light，不需額外安裝中文字型）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from jinja2 import Environment, FileSystemLoader, select_autoescape

from orchestrator import ReviewSession

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
TEMPLATE_NAME = "report_template.html"


def report_number(session: ReviewSession) -> str:
    stamp = datetime.strptime(session.started_at, "%Y-%m-%d %H:%M").strftime("%Y%m%d-%H%M")
    return f"SR-{stamp}"


def render_html(session: ReviewSession) -> str:
    if session.chief is None:
        raise ValueError("審查尚未完成，無法產生報告")
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=select_autoescape(["html"]))
    return env.get_template(TEMPLATE_NAME).render(
        session=session,
        chief=session.chief,
        threads=session.threads(),
        info=session.project_info,
        report_no=report_number(session),
    )


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
    return _reportlab_pdf(session), "reportlab"


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


def _reportlab_pdf(session: ReviewSession) -> bytes:
    import io

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font = _register_cjk_font(pdfmetrics, UnicodeCIDFont)
    base = ParagraphStyle("base", fontName=font, fontSize=9, leading=13, wordWrap="CJK")
    small = ParagraphStyle("small", parent=base, fontSize=8, leading=11, textColor=colors.grey)
    h1 = ParagraphStyle("h1", parent=base, fontSize=18, leading=24, alignment=TA_CENTER, spaceAfter=8)
    h2 = ParagraphStyle("h2", parent=base, fontSize=12, leading=18, spaceBefore=10, spaceAfter=4,
                        textColor=colors.HexColor("#1f3a5f"))
    risk_color = {"High": "#b91c1c", "Medium": "#b45309", "Pass": "#15803d", "Info": "#6b7280"}

    P = lambda t, st=base: Paragraph(escape(str(t or "")).replace("\n", "<br/>"), st)  # noqa: E731

    def tag(level: str) -> Paragraph:
        return Paragraph(f'<font color="{risk_color.get(level, "#000")}"><b>{level}</b></font>', base)

    def grid(rows, widths):
        t = Table(rows, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d1d5db")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f7")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        return t

    chief = session.chief
    story = [P("結構計算書審查報告", h1)]
    info_rows = [[P("報告編號"), P(report_number(session))]]
    info_rows += [[P(k), P(v)] for k, v in session.project_info.items() if v]
    info_rows += [[P("審查日期"), P(session.started_at)],
                  [P("語言模型"), P(f"{session.provider} / {session.model}")]]
    story.append(grid(info_rows, [30 * mm, 144 * mm]))

    story.append(P("一、審查結論", h2))
    color = {"退回補正": "High", "修正後同意": "Medium", "同意備查": "Pass"}[chief.overall_conclusion.value]
    story.append(Paragraph(
        f'<font color="{risk_color[color]}"><b>{chief.overall_conclusion.value}</b></font>',
        ParagraphStyle("verdict", parent=base, fontSize=15, leading=22)))
    story.append(Spacer(1, 4))
    story.append(P(chief.executive_summary))

    head = [P(h) for h in ("編號", "項目", "檢核結論", "判定依據", "處理意見")]
    widths = [16 * mm, 28 * mm, 60 * mm, 35 * mm, 35 * mm]
    for title, items in (("（一）重大疑義", chief.major_doubts),
                         ("（二）待複核項目", chief.pending_review_items),
                         ("（三）無虞項目", chief.no_concern_items)):
        story.append(P(title, ParagraphStyle("h3", parent=base, fontSize=10.5, spaceBefore=6, spaceAfter=3)))
        if not items:
            story.append(P("無。", small))
            continue
        rows = [head] + [[P(i.issue_id), P(i.title), P(i.finding), P(i.basis), P(i.recommendation)]
                         for i in items]
        story.append(grid(rows, widths))
    if chief.debate_observations:
        story += [P("（四）主任檢核技師觀察", h2), P(chief.debate_observations)]

    story.append(P("二、規則式初篩結果", h2))
    if session.findings:
        rows = [[P(h) for h in ("等級", "工作表", "項目", "說明", "參考規範")]]
        rows += [[tag(f["level"]), P(f["sheet"]), P(f["item"]), P(f["detail"]), P(f["reference"])]
                 for f in session.findings]
        story.append(grid(rows, [16 * mm, 26 * mm, 40 * mm, 50 * mm, 42 * mm]))
    else:
        story.append(P("未發現超出預設門檻之數值。", small))

    story.append(P("三、詰問審查歷程", h2))
    for t in session.threads():
        i, r, v = t["issue"], t["response"], t["verdict"]
        rows = [[P(f"{i.issue_id}｜{i.title}（{i.category}）"),
                 tag(v.risk_level.value) if v else P("")],
                [P(f"[Round 1 質疑] {i.challenge}\n算式：{i.formula}\n"
                   f"引用數據：{i.cited_data}｜依據：{i.code_reference}"), ""]]
        if r:
            text = f"[Round 2 回覆｜{r.stance.value}] {r.response}"
            if r.calculation:
                text += f"\n計算：{r.calculation}"
            if r.proposed_action:
                text += f"\n修正方案：{r.proposed_action}"
            text += f"\n數據出處：{r.data_source}｜規範依據：{r.code_reference}"
            rows.append([P(text), ""])
        if v:
            text = f"[Round 3 終審｜{v.decision.value}] {v.rationale}"
            if v.required_action:
                text += f"\n應辦事項：{v.required_action}"
            rows.append([P(text), ""])
        tbl = Table(rows, colWidths=[154 * mm, 20 * mm])
        tbl.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#d1d5db")),
            ("LINEBELOW", (0, 0), (-1, -2), 0.3, colors.HexColor("#e5e7eb")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f7")),
            *[("SPAN", (0, n), (1, n)) for n in range(1, len(rows))],
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story += [tbl, Spacer(1, 6)]

    if session.notes:
        story.append(P("四、系統紀錄", h2))
        story += [P(f"• {n}", small) for n in session.notes]

    story += [Spacer(1, 12), P("聲明：本報告由大型語言模型輔助產生，內容可能有誤，僅供結構技師審查參考，"
                               "不得取代依法辦理之結構簽證或外審。", small)]

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(font, 8)
        canvas.setFillColor(colors.grey)
        canvas.drawCentredString(A4[0] / 2, 10 * mm, f"第 {doc.page} 頁")
        canvas.restoreState()

    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                      topMargin=18 * mm, bottomMargin=18 * mm,
                      title="結構計算書審查報告").build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
