"""Excel 審查報表：格式沿用 rc-member-designer 的 Excel 計算書（calc-xlsx.js）。

- 工作表：「檢核表」（七欄、凍結表頭、A3 橫向）＋「審查報告(A4)」（章節式、A4 直向）
- 色碼（v2 標準）：表頭 D9E1F2、分節 F2F2F2、總判定淺琥珀 FFF2CC、格線 808080；
  輸入資料（工程名稱等）淺藍底＋藍框＋藍字，與 rc-member-designer 一致。
- 字型 Arial；頁尾「日期｜第 n 頁 / 共 N 頁」。
內容來自 utils/report_model.py，與 PDF 報告一致。
"""

from __future__ import annotations

import io
import math

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from orchestrator import ReviewSession
from utils.latex import split_lines, to_text
from utils.report_model import JUDGE, ReportModel, build_model

K = {
    "inFill": "DDEBF7", "inBorder": "2E75B6", "inFont": "0000FF",
    "grid": "808080", "hdr": "D9E1F2", "sec": "F2F2F2", "amber": "FFF2CC",
    "ng": "C00000",
}
FONT = "Arial"
FOOTER_PAGE = "第 &P 頁 / 共 &N 頁"


def _fill(rgb: str) -> PatternFill:
    return PatternFill("solid", fgColor=rgb)


def _box(rgb: str = K["grid"]) -> Border:
    s = Side(style="thin", color=rgb)
    return Border(left=s, right=s, top=s, bottom=s)


def _put(cell, value):
    """寫入文字；以「=」開頭的模型輸出（例如算式）一律存成文字，避免被 Excel 當成公式執行。"""
    cell.value = value
    if isinstance(value, str) and value.startswith("="):
        cell.data_type = "s"
    return cell


def tex_text(formulas: list[str]) -> list[str]:
    """Excel 無法排版 LaTeX，算式轉為可讀純文字。"""
    return [to_text(x) for f in formulas for x in split_lines(f)]


def _disp_width(text: str) -> int:
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def _height(text: str, width_chars: float, line_pt: float = 12.5, min_lines: int = 1) -> float:
    usable = max(width_chars * 0.9 - 1, 1)  # 欄寬單位約等於一個半形字，留餘裕避免最後一行被切掉
    lines = sum(max(1, math.ceil(_disp_width(part) / usable)) for part in str(text).split("\n"))
    return max(lines, min_lines) * line_pt + 4


def _footer(ws):
    ws.oddFooter.left.text = "&D"
    ws.oddFooter.right.text = FOOTER_PAGE


# ----------------------------------------------------------------------
# 檢核表（七欄，與 rc-member-designer 檢核表同欄位結構）
# ----------------------------------------------------------------------
CHECK_COLS = [("檢核項目", 32), ("來源／編號", 14), ("判定", 8), ("風險等級", 10),
              ("內容／算式", 52), ("建議方案／處理意見", 48), ("規範依據（條文出處）", 36)]


def _check_sheet(ws, m: ReportModel):
    for j, (_, w) in enumerate(CHECK_COLS, 1):
        ws.column_dimensions[get_column_letter(j)].width = w
    ws.page_setup.paperSize = ws.PAPERSIZE_A3
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = "1:2"
    ws.freeze_panes = "A3"
    _footer(ws)

    r = 1
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=7)
    c = ws.cell(r, 1, f"{m.title}－檢核表" + (f"（{m.project_name}）" if m.project_name else ""))
    c.font, c.alignment = Font(name=FONT, size=14, bold=True), Alignment(vertical="center")
    ws.row_dimensions[r].height = 24
    r = 2
    for j, (h, _) in enumerate(CHECK_COLS, 1):
        c = ws.cell(r, j, h)
        c.font, c.fill, c.border = Font(name=FONT, size=10, bold=True), _fill(K["hdr"]), _box()
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    def section(text):
        nonlocal r
        r += 2
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=7)
        c = ws.cell(r, 1, text)
        c.font, c.fill, c.border = Font(name=FONT, size=10, bold=True), _fill(K["sec"]), _box()

    def item(values, judge="", head=False):
        nonlocal r
        r += 1
        height = 16.0
        for j, v in enumerate(values, 1):
            c = _put(ws.cell(r, j), v)
            ng = j == 3 and judge == "NG"
            c.font = Font(name=FONT, size=10, bold=ng or head, color=K["ng"] if ng else None)
            c.border = _box()
            if head:
                c.fill = _fill(K["hdr"])
            c.alignment = Alignment(horizontal="center" if head or j in (2, 3, 4) else "left", vertical="top",
                                    wrap_text=True)
            height = max(height, _height(v, CHECK_COLS[j - 1][1]))
        ws.row_dimensions[r].height = height

    section("一、綜合審查項目（各輪合併，風險取最嚴者）")
    for it in m.items:
        sol = it.solution
        plan = (f"{sol.label}：{sol.approach}\n改動幅度：{sol.change_extent}\n驗算：" + "；".join(tex_text(sol.verification))
                if sol else "—") + (f"\n應辦事項：{it.required_action}" if it.required_action else "")
        item([it.title, "、".join(it.sources), it.judge, it.risk, it.finding, plan, "、".join(it.clauses) or "—"], it.judge)
    if not m.items:
        item(["無疑義項目", "—", "OK", "低風險", "", "", ""])

    section("二、rc-member-designer 調整參數（" + m.tool_url + "）")
    item(["參數", "欄位 id", "", "", "目前值 → 建議值", "所屬項目／方案", "單位"], head=True)
    for it in m.items:
        for ch in (it.solution.changes if it.solution else []):
            item([ch.label, ch.param_id or "—", "", "", f"{ch.current} → {ch.proposed}", f"{it.title}｜{it.solution.label}",
                  ch.unit or "—"])
    for x in m.adjusted_applied:
        item(["已寫入調整後案件檔", "—", "", "", x, m.case_name, ""])

    section("三、各輪審查疑義")
    for p in m.passes:
        for t in p.threads:
            i, v, c = t["issue"], t["verdict"], t["chief"]
            risk = c.risk_level if c else (v.risk_level if v else None)
            judge = JUDGE[risk] if risk else "—"
            content = f"{i.challenge}\n算式：" + "；".join(tex_text(i.formulas))
            if t["response"]:
                content += f"\n設計回覆（{t['response'].stance.value}）：{t['response'].response}"
            crit = (f"終審：{v.decision.value}｜{v.rationale}\n" if v else "") + (
                f"主任技師：{c.recommendation}" if c else "") + (f"\n採用：{t['adopted'].label}" if t["adopted"] else "")
            item([f"{i.title}（{i.category}）", f"P{p.no}-{i.issue_id}", judge, risk.value if risk else "—", content, crit,
                  "、".join(i.code_clauses)], judge)

    section("四、檢核彙總")
    item(["檢核項目", "來源", "判定", "風險等級", "建議方案", "", "依據"], head=True)
    for srow in m.summary_rows:
        item([srow.title, srow.sources, srow.judge, srow.risk, srow.solution, "", srow.ref], srow.judge)
    r += 1
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=2)
    ws.merge_cells(start_row=r, start_column=3, end_row=r, end_column=7)
    ws.cell(r, 1, "整體審查判定")
    ws.cell(r, 3, m.verdict_text.replace("總判定：", ""))
    for j in range(1, 8):
        c = ws.cell(r, j)
        c.fill, c.border, c.font = _fill(K["amber"]), _box(), Font(name=FONT, size=11, bold=True)
        c.alignment = Alignment(vertical="center")
    ws.row_dimensions[r].height = 20


# ----------------------------------------------------------------------
# 審查報告(A4)：章節式，欄寬比照 rc-member-designer a4Base
# ----------------------------------------------------------------------
A4_COLS = [3.2, 18, 30, 11, 12, 30]  # A（留白）B～F


class _A4:
    def __init__(self, ws):
        self.ws, self.n = ws, 0
        for j, w in enumerate(A4_COLS, 1):
            ws.column_dimensions[get_column_letter(j)].width = w
        ws.page_setup.paperSize = ws.PAPERSIZE_A4
        ws.page_setup.orientation = "portrait"
        ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.print_options.horizontalCentered = True
        ws.page_margins.left, ws.page_margins.right = 42 / 72, 28 / 72
        ws.page_margins.top = ws.page_margins.bottom = 42 / 72
        ws.page_margins.header = ws.page_margins.footer = 20 / 72
        _footer(ws)

    def row(self):
        self.n += 1
        return self.n

    def _merge(self, n, c1=2, c2=6):
        self.ws.merge_cells(start_row=n, start_column=c1, end_row=n, end_column=c2)

    def title(self, text, sub):
        n = self.row()
        self._merge(n)
        c = self.ws.cell(n, 2, text)
        c.font, c.alignment = Font(name=FONT, size=14, bold=True), Alignment(horizontal="center", vertical="center")
        self.ws.row_dimensions[n].height = 26
        n2 = self.row()
        self._merge(n2)
        c = self.ws.cell(n2, 2, sub)
        c.font, c.alignment = Font(name=FONT, size=9), Alignment(horizontal="center")
        self.ws.print_title_rows = f"{n}:{n2}"

    def input(self, label, value):
        n = self.row()
        c = self.ws.cell(n, 2, label)
        c.font = Font(name=FONT, size=9, bold=True)
        c.alignment = Alignment(vertical="top")
        self._merge(n, 3, 6)
        for j in range(3, 7):
            cc = self.ws.cell(n, j)
            cc.fill, cc.border = _fill(K["inFill"]), _box(K["inBorder"])
        c = _put(self.ws.cell(n, 3), value)
        c.font = Font(name=FONT, size=9, bold=True, color=K["inFont"])
        c.alignment = Alignment(wrap_text=True, vertical="top")
        self.ws.row_dimensions[n].height = _height(value, sum(A4_COLS[2:6]), 12)

    def chap(self, text):
        self.row()
        n = self.row()
        self._merge(n)
        c = self.ws.cell(n, 2, text)
        c.font, c.fill, c.alignment = Font(name=FONT, size=11, bold=True), _fill(K["hdr"]), Alignment(vertical="center")
        self.ws.row_dimensions[n].height = 22

    def sub(self, text):
        n = self.row()
        self._merge(n)
        c = _put(self.ws.cell(n, 2), text)
        c.font, c.fill = Font(name=FONT, size=9, bold=True), _fill(K["sec"])
        c.alignment = Alignment(vertical="center", wrap_text=True)
        self.ws.row_dimensions[n].height = max(18, _height(text, sum(A4_COLS[1:6]), 12))

    def para(self, no, text, bold=False):
        n = self.row()
        c = self.ws.cell(n, 2, no)
        c.font, c.alignment = Font(name=FONT, size=9, bold=bold), Alignment(vertical="top", wrap_text=True)
        self._merge(n, 3, 6)
        c = _put(self.ws.cell(n, 3), text)
        c.font, c.alignment = Font(name=FONT, size=9, bold=bold), Alignment(wrap_text=True, vertical="top")
        self.ws.row_dimensions[n].height = max(_height(text, sum(A4_COLS[2:6]), 12.5),
                                               _height(no, A4_COLS[1], 12.5))

    def table(self, heads, rows, spans=None, judge_col=None):
        """heads／rows 為 5 欄（B～F）；spans 指定合併（如 [(3, 4)] 表 D:E 合併）。"""
        spans = spans or []
        widths = A4_COLS[1:6]

        def write(n, values, head):
            for a, b in spans:
                self.ws.merge_cells(start_row=n, start_column=2 + a, end_row=n, end_column=2 + b)
            height = 15.0
            covered = {j for a, b in spans for j in range(a + 1, b + 1)}
            for j, v in enumerate(values):
                if j in covered:  # 合併範圍內的格子不可寫值
                    continue
                c = _put(self.ws.cell(n, 2 + j), v)
                ng = judge_col == j and v == "NG" and not head
                c.font = Font(name=FONT, size=9, bold=head or ng, color=K["ng"] if ng else None)
                c.border = _box()
                if head:
                    c.fill = _fill(K["hdr"])
                c.alignment = Alignment(horizontal="center" if head or judge_col == j else "left",
                                        vertical="center" if head else "top", wrap_text=True)
                w = widths[j] + sum(sum(widths[a + 1:b + 1]) for a, b in spans if a == j)
                height = max(height, _height(v or "", w, 12))
            for a, b in spans:  # 合併範圍內其他格也要框線
                for j in range(a + 1, b + 1):
                    self.ws.cell(n, 2 + j).border = _box()
            self.ws.row_dimensions[n].height = height

        write(self.row(), heads, True)
        for values in rows:
            write(self.row(), values, False)

    def total(self, label, value):
        n = self.row()
        self._merge(n, 2, 5)
        self.ws.cell(n, 2, label)
        self.ws.cell(n, 6, value)
        for j in range(2, 7):
            c = self.ws.cell(n, j)
            c.fill, c.border, c.font = _fill(K["amber"]), _box(), Font(name=FONT, size=10, bold=True)
            c.alignment = Alignment(horizontal="center" if j == 6 else "left", vertical="center", wrap_text=True)
        self.ws.row_dimensions[n].height = _height(value, A4_COLS[5], 13)

    def sign(self, roles):
        self.row()
        n = self.row()
        # 三格：B:C、D:E、F
        groups = [(2, 3), (4, 5), (6, 6)]
        for (a, b), role in zip(groups, roles):
            for k, (text, h) in enumerate([(role, 18), ("", 46), ("日期：", 18)]):
                rr = n + k
                if a != b:
                    self.ws.merge_cells(start_row=rr, start_column=a, end_row=rr, end_column=b)
                c = self.ws.cell(rr, a, text)
                c.font = Font(name=FONT, size=9, bold=k == 0)
                c.alignment = Alignment(horizontal="center" if k == 0 else "left", vertical="center")
                for j in range(a, b + 1):
                    cc = self.ws.cell(rr, j)
                    cc.border = _box()
                    if k == 0:
                        cc.fill = _fill(K["hdr"])
                self.ws.row_dimensions[rr].height = h
        self.n = n + 2


def _a4_sheet(ws, m: ReportModel):
    a = _A4(ws)
    a.title(m.title, "AI 詰問審查（角色輪替）｜判定：OK／注意／NG｜風險等級：低／中／高風險")
    meta = {k: v for k, v, _ in m.meta}
    a.input("工程名稱", m.project_name)
    for key, value in meta.items():
        if key not in ("審查方式",):
            a.input(key, value)
    a.input("審查者／檢核者", "")

    a.chap("一、審查依據與輸入資料")
    a.table(["項目", "內容", None, None, None], [[k, v, None, None, None] for k, v in m.source_rows], spans=[(1, 4)])
    a.para("1.1", "不預設審查門檻：各項限值、係數均由 Agent 自行查閱規範原文（rc-member-designer specs/）後判斷；"
                  "計算書 PDF 原檔直接提供給模型判讀。")
    a.para("1.2", meta.get("審查方式", ""))

    a.chap("二、角色配置與輪替")
    a.table(["輪次", "A 原設計工程師", "B 外部審查專家", None, "C 主任檢核技師"],
            [[r[0], r[1], r[2], None, r[3]] for r in m.role_rows], spans=[(2, 3)])
    a.para("", f"每一輪均為全新對話，同一模型在不同輪次不共用記憶；綜合裁決：{m.final_model}。")

    a.chap("三、綜合審查結論")
    a.para("3.0", m.executive_summary)
    if m.cross_pass_notes:
        a.para("3.1", f"各輪一致性：{m.cross_pass_notes}")
    if m.items:
        a.table(["項目", "來源", "風險等級", "判定", "綜合結論"],
                [[i.title, "、".join(i.sources), i.risk, i.judge, i.finding] for i in m.items], judge_col=3)

    a.chap("四、解決方案（改動最小且符合規範）")
    for n, it in enumerate([i for i in m.items if i.solution or i.required_action], 1):
        a.sub(f"4.{n} {it.title}（{it.risk}）")
        sol = it.solution
        if sol:
            a.para("建議方案", f"{sol.label}\n做法：{sol.approach}\n改動幅度：{sol.change_extent}")
            if sol.changes:
                a.table(["rc-member-designer 欄位", "參數", "目前值", "建議值", "單位"],
                        [[c.param_id or "—", c.label, c.current, c.proposed, c.unit] for c in sol.changes])
            a.para("修正後驗算", "\n".join(tex_text(sol.verification)))
            if sol.tool_steps:
                a.para("工具操作", sol.tool_steps)
            if sol.other_method:
                a.para("其他作法", sol.other_method)
        if it.why_minimal:
            a.para("改動最小", it.why_minimal)
        if it.required_action:
            a.para("應辦事項", it.required_action)
    a.sub("rc-member-designer 調整總結")
    a.para("", f"工具：{m.tool_url}" + (f"\n{m.tool_summary}" if m.tool_summary else ""))
    if m.adjusted_applied:
        a.para("案件檔", "已寫入調整後案件檔：\n" + "\n".join(m.adjusted_applied))

    a.chap("五、各輪審查歷程")
    for p in m.passes:
        a.sub(f"第 {p.no} 輪（本輪結論：{p.conclusion}）｜" + "；".join(f"{r}＝{mm}" for r, mm in p.roles))
        a.para("", f"Round 1 外審總體意見：{p.round1_summary}")
        for t in p.threads:
            i, r, v, c = t["issue"], t["response"], t["verdict"], t["chief"]
            a.para(f"{i.issue_id} R1 質疑", f"{i.title}：{i.challenge}\n算式：" + "；".join(tex_text(i.formulas))
                   + f"\n依據：{'、'.join(i.code_clauses)}")
            if r:
                a.para(f"R2 回覆\n（{r.stance.value}）", r.response + (
                    "\n計算：" + "；".join(tex_text(r.calculations)) if r.calculations else ""))
            if v:
                a.para(f"R3 終審\n（{v.decision.value}）", f"{v.risk_level.value}｜{v.rationale}" + "".join(
                    f"\n方案 {n}：{s.label}｜{s.change_extent}" for n, s in enumerate(v.solutions, 1)))
            if c:
                a.para("主任技師", c.finding + (f"\n採用：{t['adopted'].label}" if t["adopted"] else ""))
        a.para("", f"主任技師總結：{p.executive_summary}")

    a.chap("六、檢核彙總")
    a.table(["檢核項目", "來源", "風險等級", "判定", "建議方案"],
            [[s.title, s.sources, s.risk, s.judge, s.solution] for s in m.summary_rows], judge_col=3)
    a.total("整體審查判定", m.verdict_text.replace("總判定：", ""))

    a.chap("七、附註")
    a.para("7.1", "本報告由大型語言模型輔助產生，僅供結構技師審查參考，不得取代依法辦理之結構簽證或外審。")
    a.para("7.2", "Agent 引用之條文已逐條比對規範原文索引；條文要點與數值仍應由審查技師對照原文確認。")
    a.para("7.3", "建議方案之參數須於 rc-member-designer 重新計算確認各檢核項目轉為 OK 後，始得作為修正依據。")
    a.para("7.4", "本報告未涵蓋、須另行檢核：" + "；".join(f"({k}) {t}" for k, t in enumerate(m.uncovered, 1)) + "。")
    for k, note in enumerate(m.notes, 5):
        a.para(f"7.{k}", f"系統紀錄：{note}")
    if m.clauses:
        a.chap("附錄 A　引用規範條文（原文節錄）")
        for cl in m.clauses:
            a.para(cl.ref, cl.text[:500] + ("…" if len(cl.text) > 500 else ""))
    a.sign(m.sign_roles)


def generate_xlsx(session: ReviewSession) -> bytes:
    m = build_model(session)
    wb = Workbook()
    ws = wb.active
    ws.title = "檢核表"
    _check_sheet(ws, m)
    _a4_sheet(wb.create_sheet("審查報告(A4)"), m)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
