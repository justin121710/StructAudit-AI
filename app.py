"""Streamlit Web 介面：streamlit run app.py

介面沿用 rc-member-designer 的版面語言：頂列（品名＋設定摘要＋審查戳記）、狀態列判定膠囊、
冷灰底白卡、左側輸入欄＋右側分頁工作區、A4 報告預覽。配色見 .streamlit/config.toml。
"""

from __future__ import annotations

import base64
import html
import json
import os
from datetime import datetime
from pathlib import Path

import streamlit as st

from agents.llm_client import DEFAULT_MODELS, create_client
from orchestrator import ReviewOrchestrator, ReviewSession
from utils.parser import DEFAULT_LIMITS, StructuralSummary, parse_structural_file
from utils.pdf_generator import generate_pdf, render_html
from utils.report_model import JUDGE, build_model, report_number
from utils.xlsx_report import generate_xlsx
from version import __version__

SAMPLE_FILE = Path(__file__).parent / "examples" / "sample_structure.xlsx"
PROVIDERS = {"離線示範（不呼叫 API）": "mock", "Google Gemini": "gemini", "OpenAI": "openai"}
KEY_ENV = {"gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY"}
LEVEL_OF_RISK = {"Pass": "ok", "Medium": "warn", "High": "bad"}

st.set_page_config(page_title="結構計算書審查", page_icon="🏗️", layout="wide",
                   initial_sidebar_state="collapsed")

LOGO = """<svg class="sa-logo" viewBox="0 0 32 32" aria-hidden="true">
<rect x="4.2" y="4.2" width="23.6" height="23.6" fill="none" stroke="currentColor" stroke-width="2"/>
<circle cx="9" cy="9" r="1.75" fill="currentColor"/><circle cx="23" cy="9" r="1.75" fill="currentColor"/>
<circle cx="9" cy="23" r="1.75" fill="currentColor"/>
<path d="M13.5 17.2l3 3 6-7" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>"""

# rc-member-designer 的設計語彙：--acc 深青、--ok/--warn/--bad 語意色與其淡底、卡片白底細框
st.markdown("""<style>
:root{--bg:#EBEEF2;--panel:#fff;--line:#D8DEE5;--line2:#C3CCD6;--txt:#151C24;--dim:#56636F;--faint:#7D8994;
  --acc:#0F5F6B;--acc-h:#0B4D57;--acc-soft:#E2EDEF;--ok:#1E7A44;--warn:#9A5E10;--bad:#B03A2E;
  --ok-soft:#E5F2EA;--warn-soft:#FBF0DF;--bad-soft:#F8E6E3;--sub:#F5F7F9;--chrome:#F8FAFB}
header[data-testid="stHeader"]{background:transparent;height:0}
.block-container{padding-top:.9rem;padding-bottom:2rem;max-width:1560px}
.sa-top{display:flex;flex-wrap:wrap;align-items:center;gap:10px 14px;padding:9px 16px;background:var(--chrome);
  border:1px solid var(--line);border-radius:8px}
.sa-brand{display:flex;align-items:center;gap:9px;font-size:16px;font-weight:700;letter-spacing:.02em;white-space:nowrap}
.sa-logo{width:26px;height:26px;color:var(--acc);flex-shrink:0}
.sa-setup{display:inline-flex;align-items:center;gap:8px;padding:4px 10px;border:1px solid var(--line2);border-radius:8px;
  font-size:13px;font-weight:600;background:var(--panel)}
.sa-setup .ed{font-size:11.5px;font-weight:600;color:var(--acc-h)}
.sa-stamp{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:var(--faint)}
.sa-grow{flex-grow:1}
.sa-sb{display:flex;flex-wrap:wrap;gap:6px 8px;padding:8px 2px 4px}
.sa-chip{display:inline-flex;align-items:center;gap:7px;background:var(--panel);border:1px solid var(--line);border-radius:999px;
  padding:3px 11px 3px 4px;font-size:13px;line-height:1.5;white-space:nowrap}
.sa-chip .i{width:19px;height:19px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;
  color:#fff;font-size:11px;font-weight:700;flex-shrink:0}
.sa-chip.ok .i{background:var(--ok)} .sa-chip.warn .i{background:var(--warn)} .sa-chip.bad .i{background:var(--bad)}
.sa-chip.info .i{background:var(--faint)}
.sa-chip.warn{background:var(--warn-soft);border-color:rgba(154,94,16,.45)}
.sa-chip.bad{background:var(--bad-soft);border-color:rgba(176,58,46,.40)}
.sa-chip.info{border-style:dashed}
.sa-chip .d{color:var(--dim)}
.sa-div{width:1px;align-self:stretch;background:var(--line2);margin:2px 4px}
[class*="st-key-card"]{background:var(--panel);box-shadow:0 1px 2px rgba(21,28,36,.07)}
.sa-h2{font-size:14.5px;font-weight:700;margin:0 0 4px}
.sa-cap{font-size:12px;color:var(--dim);margin:-2px 0 6px}
.alert{border-radius:6px;padding:8px 12px;margin:6px 0;font-size:13px;line-height:1.6;border:1px solid}
.a-bad{background:var(--bad-soft);border-color:rgba(176,58,46,.35)}
.a-warn{background:var(--warn-soft);border-color:rgba(154,94,16,.32)}
.a-ok{background:var(--panel);border-color:var(--line)}
.a-ok::before{content:"✓";color:var(--ok);font-weight:700;margin-right:8px}
.a-info{background:var(--sub);border-color:var(--line)}
.alert .ref{font-size:12px;color:var(--dim)}
.verdict{font-size:15px;font-weight:800;padding:8px 12px;margin:2px 0 10px;border:2px solid #060;display:inline-block;
  background:var(--panel);border-radius:4px}
.verdict.bad{border-color:#c00} .verdict.warn{border-color:var(--warn)}
.rp-bar{display:flex;align-items:center;gap:10px;padding:9px 14px;margin:0 0 10px;border:1px solid rgba(154,94,16,.32);
  background:var(--warn-soft);border-radius:8px;font-size:13px}
.thread{border:1px solid var(--line);border-radius:6px;background:var(--panel);margin:4px 0 10px}
.thread .row{display:grid;grid-template-columns:118px minmax(0,1fr);border-top:1px solid var(--line)}
.thread .row:first-child{border-top:0}
.thread .who{background:var(--sub);padding:8px 10px;font-size:12.5px;font-weight:700;color:var(--txt)}
.thread .who span{display:block;font-weight:500;color:var(--dim)}
.thread .body{padding:8px 12px;font-size:13px;line-height:1.6}
.thread .eq{font-family:"Times New Roman",serif;background:var(--sub);border-radius:4px;padding:3px 8px;margin:4px 0;
  white-space:pre-wrap;font-size:13.5px}
.thread .src{font-size:12px;color:var(--dim)}
button[data-baseweb="tab"] p{font-size:13.5px}
button[data-baseweb="tab"][aria-selected="true"] p{color:var(--acc-h);font-weight:700}
</style>""", unsafe_allow_html=True)


def secret(name: str) -> str:
    try:
        return st.secrets.get(name, "") or os.getenv(name, "")
    except Exception:  # 沒有 secrets.toml
        return os.getenv(name, "")


def esc(t) -> str:
    return html.escape(str(t or ""))


def chip(level: str, label: str, value: str = "", detail: str = "") -> str:
    icon = {"ok": "✓", "warn": "!", "bad": "✕", "info": "?"}[level]
    tail = f' <span class="d">{esc(detail)}</span>' if detail else ""
    return f'<span class="sa-chip {level}"><span class="i">{icon}</span>{esc(label)} <b>{esc(value)}</b>{tail}</span>'


def column_max(summary: StructuralSummary, key: str) -> float | None:
    vals = [sh.stats[sh.mapped[key]]["max"] for sh in summary.sheets
            if key in sh.mapped and sh.mapped[key] in sh.stats]
    return max(vals) if vals else None


def precheck_chips(summary: StructuralSummary, lim: dict) -> list[str]:
    """初篩判定膠囊：有資料才判定該類，沒資料的類別以虛線「未提供」提醒。"""
    by: dict[str, list] = {}
    for f in summary.findings:
        by.setdefault(f.check, []).append(f)

    def has(key: str) -> bool:
        return any(key in sh.mapped for sh in summary.sheets)

    out = []
    if has("drift_ratio"):
        v = column_max(summary, "drift_ratio")
        bad = bool(by.get("drift"))
        out.append(chip("bad" if bad else "ok", "位移角", f"θ {v:.4f}" if v is not None else "—",
                        f"{'>' if bad else '≤'} {lim['drift_max']:.4f}"))
    else:
        out.append(chip("info", "位移角", "未提供"))
    if has("rebar_ratio"):
        n = len(by.get("beam_rho", [])) + len(by.get("col_rho", []))
        out.append(chip("bad" if n else "ok", "配筋比", f"{n} 處超限" if n else "通過", "梁 ρ／柱 ρg"))
    else:
        out.append(chip("info", "配筋比", "未提供"))
    if has("dcr"):
        v = column_max(summary, "dcr")
        lv = "bad" if v is not None and v > lim["dcr_max"] else "warn" if v is not None and v > lim["dcr_warn"] else "ok"
        out.append(chip(lv, "應力比", f"D/C {v:.3f}" if v is not None else "—", "最大值"))
    else:
        out.append(chip("info", "應力比", "未提供"))
    if has("load_combo"):
        n = len(by.get("combo", []))
        out.append(chip("warn" if n else "ok", "載重組合", f"缺 {n} 類" if n else "完整"))
    else:
        out.append(chip("info", "載重組合", "未提供"))
    n_mapped = sum(1 for sh in summary.sheets if sh.mapped)
    out.append(chip("info" if n_mapped < len(summary.sheets) else "ok", "工作表",
                    f"{len(summary.sheets)} 張", f"辨識 {n_mapped} 張"))
    return out


def review_chips(session: ReviewSession) -> list[str]:
    m = build_model(session)
    c = session.chief
    out = [chip(m.verdict_level, "總判定", c.overall_conclusion.value),
           chip("bad" if c.major_doubts else "ok", "重大疑義", f"{len(c.major_doubts)} 項"),
           chip("warn" if c.pending_review_items else "ok", "待複核", f"{len(c.pending_review_items)} 項"),
           chip("ok", "無虞", f"{len(c.no_concern_items)} 項")]
    if session.notes:
        out.append(chip("info", "系統補正", f"{len(session.notes)} 項", "見報告附註"))
    return out


ss = st.session_state
ss.setdefault("runs", 0)
has_key = any(secret(k) for k in KEY_ENV.values())

top = st.empty()  # 頂列與狀態列：等輸入與審查結果都確定後再填
left, right = st.columns([5, 7], gap="medium")

# ================================================================ 左欄：輸入
with left:
    with st.container(border=True, key="card_setup"):
        st.markdown('<div class="sa-h2">審查設定</div>', unsafe_allow_html=True)
        provider = PROVIDERS[st.selectbox("LLM 服務", list(PROVIDERS), index=1 if has_key else 0,
                                          help="未設定 API Key 時預設離線示範，可直接試跑流程與報表")]
        model = api_key = None
        if provider != "mock":
            c1, c2 = st.columns(2)
            model = c1.text_input("模型名稱", DEFAULT_MODELS[provider])
            api_key = c2.text_input("API Key", value=secret(KEY_ENV[provider]), type="password",
                                    help=f"也可設定環境變數 {KEY_ENV[provider]} 或 .streamlit/secrets.toml")

    with st.container(border=True, key="card_project"):
        st.markdown('<div class="sa-h2">專案資料</div><div class="sa-cap">顯示於報告表頭與 Excel 封面，不影響審查</div>',
                    unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        project_info = {
            "案名": c1.text_input("案名", "示範大樓新建工程"),
            "設計單位": c2.text_input("設計單位", ""),
            "建築地點": c1.text_input("建築地點", ""),
            "結構系統": c2.text_input("結構系統", "RC 特殊抗彎矩構架"),
            "樓層數": c1.text_input("樓層數", "地上 8 層／地下 1 層"),
            "備註": c2.text_input("備註（審查重點等）", ""),
        }

    with st.container(border=True, key="card_input"):
        st.markdown('<div class="sa-h2">結構摘要檔</div>'
                    '<div class="sa-cap">ETABS、MIDAS 匯出摘要或自行整理之檢核表（Excel／CSV）</div>',
                    unsafe_allow_html=True)
        uploaded = st.file_uploader("上傳檔案", type=["xlsx", "xls", "xlsm", "csv"], label_visibility="collapsed")
        use_sample = st.checkbox("使用示範資料 examples/sample_structure.xlsx", value=uploaded is None)
        with st.expander("初篩門檻（規範係數）"):
            g1, g2 = st.columns(2)
            limits = {
                "drift_max": g1.number_input("層間位移角上限 θ", value=DEFAULT_LIMITS["drift_max"],
                                             format="%.4f", step=0.0005, help="建築物耐震設計規範及解說 §2.16.1"),
                "beam_rho_max": g2.number_input("梁拉力鋼筋比上限 ρ", value=DEFAULT_LIMITS["beam_rho_max"],
                                                format="%.3f", step=0.001, help="土木401-112 §18.3.3.1"),
                "col_rho_min": g1.number_input("柱主筋比下限 ρg", value=DEFAULT_LIMITS["col_rho_min"],
                                               format="%.3f", step=0.001, help="土木401-112 §18.4.4.1"),
                "col_rho_max": g2.number_input("柱主筋比上限 ρg", value=DEFAULT_LIMITS["col_rho_max"],
                                               format="%.3f", step=0.005,
                                               help="特殊抗彎矩構架柱 0.06（§18.4.4.1）；一般柱 0.08（§10.6.1.1）"),
                "dcr_max": g1.number_input("應力比 D/C 上限", value=DEFAULT_LIMITS["dcr_max"], step=0.05,
                                           help="土木401-112 §9.5.1.1、§10.5.1.1"),
                "dcr_warn": g2.number_input("應力比警示值", value=DEFAULT_LIMITS["dcr_warn"], step=0.05),
            }

    summary: StructuralSummary | None = None
    parse_error = ""
    try:
        if uploaded is not None and not use_sample:
            summary = parse_structural_file(uploaded.getvalue(), filename=uploaded.name, limits=limits)
        elif use_sample and SAMPLE_FILE.exists():
            summary = parse_structural_file(SAMPLE_FILE, limits=limits)
    except Exception as e:
        parse_error = f"檔案解析失敗：{e}"

    need_key = provider != "mock" and not api_key
    run = st.button("▶ 開始三回合詰問審查", type="primary", width="stretch",
                    disabled=summary is None or need_key, key="run")
    if need_key:
        st.caption("請輸入 API Key，或改用離線示範。")
    if parse_error:
        st.error(parse_error)

# ================================================================ 執行審查
run_error = ""
if run and summary is not None:
    with right, st.status("審查進行中…", expanded=True) as status:
        try:
            client = create_client(provider, model=model, api_key=api_key)
            orch = ReviewOrchestrator(client, on_progress=lambda stage, msg: status.write(msg))
            ss["review"] = orch.run(summary, project_info)
            ss["runs"] += 1
            ss["stamp"] = datetime.now().strftime("%H:%M:%S")
            status.update(label="審查完成", state="complete", expanded=False)
        except Exception as e:
            status.update(label="審查失敗", state="error")
            run_error = f"{type(e).__name__}：{e}"

session: ReviewSession | None = ss.get("review")
done = bool(session and session.chief)

# ================================================================ 頂列＋狀態列
setup = {"mock": "離線示範", "gemini": "Gemini", "openai": "OpenAI"}[provider] + (f"｜{model}" if model else "")
stamp = f"審查 #{ss['runs']} · {ss['stamp']}" if ss.get("stamp") else "尚未審查"
chips = precheck_chips(summary, limits) if summary else []
if done:  # 初篩膠囊與審查結論膠囊之間以分隔線區隔
    chips += ['<span class="sa-div"></span>'] + review_chips(session)
top.markdown(
    f'<div class="sa-top"><div class="sa-brand">{LOGO}結構計算書審查</div>'
    f'<span class="sa-setup">{esc(setup)}<span class="ed">AI 雙重詰問</span></span>'
    f'<span class="sa-stamp">{esc(stamp)}</span><span class="sa-grow"></span>'
    f'<span class="sa-stamp">v{__version__}</span></div>'
    f'<div class="sa-sb">{"".join(chips)}</div>',
    unsafe_allow_html=True,
)


@st.cache_data(show_spinner="產生報表中…")
def build_files(_s: ReviewSession, key: str) -> tuple[bytes, str, bytes, str]:
    pdf, engine = generate_pdf(_s)
    return pdf, engine, generate_xlsx(_s), render_html(_s)


# ================================================================ 右欄：工作區
with right:
    if run_error:
        st.error(run_error)

    if done:
        no = report_number(session)
        pdf_bytes, engine, xlsx_bytes, report_html = build_files(session, f"{no}-{id(session)}")
        d1, d2, d3, d4 = st.columns(4)
        d1.download_button("⬇ 列印 / 存成 PDF", pdf_bytes, f"{no}.pdf", "application/pdf",
                           type="primary", width="stretch")
        d2.download_button("⬇ 匯出 Excel", xlsx_bytes, f"{no}.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", width="stretch")
        d3.download_button("⬇ HTML 報告", report_html, f"{no}.html", "text/html", width="stretch")
        d4.download_button("⬇ JSON 紀錄", json.dumps(session.to_dict(), ensure_ascii=False, indent=2),
                           f"{no}.json", "application/json", width="stretch")
        tab_names = ["結果摘要", "詰問歷程", "預覽報告", "前處理與初篩", "原始 JSON"]
    else:
        tab_names = ["前處理與初篩"]
    tabs = dict(zip(tab_names, st.tabs(tab_names)))

    with tabs["前處理與初篩"]:
        if summary is None:
            st.info("請上傳結構摘要檔或勾選示範資料。")
        else:
            st.markdown(f'<div class="sa-h2">規則式初篩</div><div class="sa-cap">{esc(summary.source_name)}　'
                        f'SHA-256 {summary.sha256[:12]}</div>', unsafe_allow_html=True)
            for f in summary.findings:
                lv = {"High": "bad", "Medium": "warn"}.get(f.level, "info")
                st.markdown(f'<div class="alert a-{lv}"><b>{esc(f.item)}</b>（{esc(f.sheet)}）：{esc(f.detail)}'
                            f'　<span class="ref">{esc(f.reference)}</span></div>', unsafe_allow_html=True)
            if not summary.findings:
                st.markdown('<div class="alert a-ok">未發現超出門檻之數值。</div>', unsafe_allow_html=True)
            st.markdown('<div class="sa-h2" style="margin-top:12px">工作表與欄位辨識</div>', unsafe_allow_html=True)
            for sheet in summary.sheets:
                mapped = "、".join(f"`{v}`→{k}" for k, v in sheet.mapped.items()) or "未辨識（全表提供參考）"
                with st.expander(f"{sheet.name}（{sheet.n_rows} 列）"):
                    st.markdown(f"辨識欄位：{mapped}")
                    frame = summary.frames[sheet.name]
                    # 混合型別欄位（例如設計參數表）轉字串，避免 Arrow 序列化警告
                    frame = frame.astype({c: str for c in frame.columns[frame.dtypes == object]})
                    st.dataframe(frame, width="stretch", hide_index=True)
            with st.expander("送給 Agent 的文字摘要"):
                st.code(summary.to_prompt_text(project_info), language="markdown")

    if not done:
        st.stop()
    m = build_model(session)

    with tabs["結果摘要"]:
        st.markdown(f'<div class="verdict {m.verdict_level}">{esc(m.verdict_text)}</div>', unsafe_allow_html=True)
        st.write(m.executive_summary)
        for _, label, items in m.buckets:
            st.markdown(f'<div class="sa-h2" style="margin-top:10px">{label}（{len(items)}）</div>',
                        unsafe_allow_html=True)
            if not items:
                st.markdown('<div class="sa-cap">無</div>', unsafe_allow_html=True)
            for it in items:
                st.markdown(
                    f'<div class="alert a-{LEVEL_OF_RISK[it.risk_level.value]}"><b>{esc(it.issue_id)}｜{esc(it.title)}</b>'
                    f'　{JUDGE[it.risk_level]}<br>{esc(it.finding)}<br><b>處理意見：</b>{esc(it.recommendation)}'
                    f'<br><span class="ref">判定依據：{esc(it.basis)}</span></div>', unsafe_allow_html=True)
        if m.observations:
            st.markdown('<div class="sa-h2" style="margin-top:10px">審查過程觀察</div>', unsafe_allow_html=True)
            st.markdown(f'<div class="alert a-info">{esc(m.observations)}</div>', unsafe_allow_html=True)
        if m.notes:
            with st.expander(f"系統一致性補正（{len(m.notes)} 項）"):
                for n in m.notes:
                    st.write("• " + n)

    with tabs["詰問歷程"]:
        st.markdown(f'<div class="sa-cap">Round 1 外審總體意見：{esc(m.round1_summary)}</div>', unsafe_allow_html=True)
        for t in m.threads:
            i, r, v = t["issue"], t["response"], t["verdict"]
            badge = f"　{JUDGE[v.risk_level]}｜{v.decision.value}" if v else ""
            with st.expander(f"{i.issue_id}｜{i.title}（{i.category}）{badge}"):
                rows = [("R1 外審質疑", "", f'{esc(i.challenge)}<div class="eq">{esc(i.formula)}</div>'
                         f'<div class="src">引用數據：{esc(i.cited_data)}｜依據：{esc(i.code_reference)}</div>')]
                if r:
                    body = esc(r.response)
                    body += f'<div class="eq">{esc(r.calculation)}</div>' if r.calculation else ""
                    body += f"<div>修正方案：{esc(r.proposed_action)}</div>" if r.proposed_action else ""
                    body += f'<div class="src">數據出處：{esc(r.data_source)}｜規範依據：{esc(r.code_reference)}</div>'
                    rows.append(("R2 設計回覆", r.stance.value, body))
                if v:
                    body = esc(v.rationale)
                    body += f'<div class="src">應辦事項：{esc(v.required_action)}</div>' if v.required_action else ""
                    rows.append(("R3 外審終審", f"{v.decision.value}／{v.risk_level.value}", body))
                st.markdown('<div class="thread">' + "".join(
                    f'<div class="row"><div class="who">{who}<span>{esc(sub)}</span></div>'
                    f'<div class="body">{body}</div></div>' for who, sub, body in rows) + "</div>",
                    unsafe_allow_html=True)

    with tabs["預覽報告"]:  # 與 PDF 同一份 HTML 範本
        st.markdown('<div class="rp-bar"><b>報告預覽</b>　內容隨審查結果產生；要修改請調整輸入後重新審查'
                    f'<span class="sa-grow"></span><span class="sa-stamp">PDF 排版：{engine}</span></div>',
                    unsafe_allow_html=True)
        # 報告含模型輸出（範本已跳脫）；以 data: URL 載入使 iframe 為獨立來源，不與本 app 同源
        st.iframe("data:text/html;base64," + base64.b64encode(report_html.encode()).decode(), height=1100)

    with tabs["原始 JSON"]:
        st.json(session.to_dict())
