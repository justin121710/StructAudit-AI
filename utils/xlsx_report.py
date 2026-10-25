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
CHECK_COLS = [("檢核項目", 32), ("編號", 9), ("判定", 8), ("風險等級", 10),
              ("內容／算式", 48), ("判定標準／處理意見", 44), ("規範依據（條文出處）", 40)]


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

    def item(values, judge=""):
        nonlocal r
        r += 1
        height = 16.0
        for j, v in enumerate(values, 1):
            c = _put(ws.cell(r, j), v)
            ng = j == 3 and judge == "NG"
            c.font = Font(name=FONT, size=10, bold=ng, color=K["ng"] if ng else None)
            c.border = _box()
            c.alignment = Alignment(horizontal="center" if j in (2, 3, 4) else "left", vertical="top", wrap_text=True)
            height = max(height, _height(v, CHECK_COLS[j - 1][1]))
        ws.row_dimensions[r].height = height

    section("一、規則式初篩")
    for f in m.prechecks:
        item([f"{f['item']}（{f['sheet']}）", "—", f["judge"], f["level"], f["detail"],
              m.criteria.get(f.get("check", ""), "依初篩門檻"), f["reference"]], f["judge"])
    if not m.prechecks:
        item(["未發現超出門檻之數值", "—", "OK", "Pass", "", "", ""])

    section("二、詰問審查項目")
    advice = {it.issue_id: it.recommendation for _, _, items in m.buckets for it in items}
    for t in m.threads:
        i, rsp, v = t["issue"], t["response"], t["verdict"]
        judge = JUDGE[v.risk_level] if v else "—"
        content = f"{i.challenge}\n算式：{i.formula}"
        if rsp:
            content += f"\n設計回覆（{rsp.stance.value}）：{rsp.response}"
        crit = (f"終審：{v.decision.value}｜{v.rationale}\n" if v else "") + f"處理意見：{advice.get(i.issue_id, '—')}"
        item([f"{i.title}（{i.category}）", i.issue_id, judge, v.risk_level.value if v else "—", content, crit,
              i.code_reference], judge)

    section("三、檢核彙總")
    r += 1
    for j, h in enumerate(["檢核項目", "編號", "判定", "風險等級", "外審終審", "主任技師分類", "依據"], 1):
        c = ws.cell(r, j, h)
        c.font, c.fill, c.border = Font(name=FONT, size=10, bold=True), _fill(K["hdr"]), _box()
        c.alignment = Alignment(horizontal="center")
    for s in m.summary_rows:
        item([s.title, s.issue_id, s.judge, s.risk, s.decision, s.bucket, s.ref], s.judge)
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
    a.title(m.title, "AI 雙重詰問審查｜判定：OK／注意／NG")
    meta = {k: v for k, v, _ in m.meta}
    a.input("工程名稱", m.project_name)
    for key in ("報告編號", "設計單位", "建築地點", "結構系統", "樓層數", "語言模型", "程式版本／雜湊", "產生時間"):
        if key in meta:
            a.input(key, meta[key])
    a.input("審查者／檢核者", "")

    a.chap("一、審查依據")
    a.para("1.1", "建築物混凝土結構設計規範（土木401-112），內政部；強度、配筋與耐震細則條號依 112 年版。")
    a.para("1.2", "建築物耐震設計規範及解說（113 年版）；層間相對側向位移角（§2.16.1）、意外扭矩（§2.14）。")
    a.para("1.3", "建築技術規則建築構造編。")
    a.para("1.4", meta.get("審查方式", ""))

    a.chap("二、輸入資料與初篩門檻")
    a.table(["項目", "內容", None, None, None], [[k, v, None, None, None] for k, v in m.source_rows], spans=[(1, 4)])
    a.sub("2.1 規則式初篩門檻之依據")
    a.table(["項目", "採用", None, "出處", None], [[i, u, None, ref, None] for i, u, ref in m.limit_rows],
            spans=[(1, 2), (3, 4)])
    a.sub("2.2 規則式初篩結果")
    if m.prechecks:
        a.table(["項目", "說明", "工作表", "判定", "依據"],
                [[f["item"], f["detail"], f["sheet"], f["judge"], f["reference"]] for f in m.prechecks], judge_col=3)
    else:
        a.para("", "未發現超出門檻之數值。")

    a.chap("三、詰問審查歷程")
    a.para("", f"Round 1 外審總體意見：{m.round1_summary}")
    for n, t in enumerate(m.threads, 1):
        i, r, v = t["issue"], t["response"], t["verdict"]
        a.sub(f"3.{n} {i.issue_id} {i.title}（{i.category}）" + (f"— 終審：{v.decision.value}／{JUDGE[v.risk_level]}" if v else ""))
        a.para("R1 外審質疑", f"{i.challenge}\n算式：{i.formula}\n引用數據：{i.cited_data}｜依據：{i.code_reference}")
        if r:
            text = r.response + (f"\n計算：{r.calculation}" if r.calculation else "")
            text += f"\n修正方案：{r.proposed_action}" if r.proposed_action else ""
            text += f"\n數據出處：{r.data_source}｜規範依據：{r.code_reference}"
            a.para(f"R2 設計回覆\n（{r.stance.value}）", text)
        if v:
            a.para(f"R3 外審終審\n（{v.decision.value}）", v.rationale + (f"\n應辦事項：{v.required_action}" if v.required_action else ""))

    a.chap("四、主任檢核技師裁決")
    a.para("4.0", m.executive_summary)
    for n, (_, label, items) in enumerate(m.buckets, 1):
        a.sub(f"4.{n} {label}（{len(items)} 項）")
        if items:
            a.table(["編號／項目", "檢核結論", "判定依據", None, "處理意見"],
                    [[f"{it.issue_id} {it.title}", it.finding, it.basis, None, it.recommendation] for it in items],
                    spans=[(2, 3)])
        else:
            a.para("", "無。")

    a.chap("五、檢核彙總")
    a.table(["檢核項目", "外審終審／主任技師分類", "風險等級", "判定", "依據"],
            [[f"{s.issue_id} {s.title}", f"{s.decision}／{s.bucket}", s.risk, s.judge, s.ref] for s in m.summary_rows],
            judge_col=3)
    a.total("整體審查判定", m.verdict_text.replace("總判定：", ""))

    a.chap("六、結論")
    a.para("6.1", m.executive_summary)
    if m.verdict_items:
        label = "重大疑義" if m.verdict_level == "bad" else "待複核項目"
        a.para("6.2", f"{label}：" + "；".join(f"({k}) {t}" for k, t in enumerate(m.verdict_items, 1)))
    a.para("6.3", {"bad": "本案有重大疑義，應退回設計單位補正後重新送審。",
                   "warn": "本案無重大疑義，待複核項目經設計單位修正並送複核後同意。",
                   "ok": "本案各審查項目疑義均已解除，同意備查。"}[m.verdict_level])

    a.chap("七、附註")
    a.para("7.1", "本報告由大型語言模型輔助產生，僅供結構技師審查參考，不得取代依法辦理之結構簽證或外審。")
    a.para("7.2", "模型引用之條號與數值應由審查技師對照規範原文確認。")
    a.para("7.3", "本報告未涵蓋、須另行檢核：" + "；".join(f"({k}) {t}" for k, t in enumerate(m.uncovered, 1)) + "。")
    for k, note in enumerate(m.notes, 4):
        a.para(f"7.{k}", f"系統紀錄：{note}")
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
