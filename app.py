"""Streamlit Web 介面：streamlit run app.py"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st

from agents.llm_client import DEFAULT_MODELS, create_client
from orchestrator import AGENT_NAMES, ReviewOrchestrator, ReviewSession
from utils.parser import DEFAULT_LIMITS, StructuralSummary, parse_structural_file
from utils.pdf_generator import generate_pdf, render_html, report_number

SAMPLE_FILE = Path(__file__).parent / "examples" / "sample_structure.xlsx"
PROVIDERS = {"Google Gemini": "gemini", "OpenAI": "openai", "離線示範（不呼叫 API）": "mock"}
KEY_ENV = {"gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY"}
RISK_ICON = {"High": "🔴", "Medium": "🟠", "Pass": "🟢", "Info": "⚪"}

st.set_page_config(page_title="結構計算書 AI 審查", page_icon="🏗️", layout="wide")


def secret(name: str) -> str:
    try:
        return st.secrets.get(name, "") or os.getenv(name, "")
    except Exception:  # 沒有 secrets.toml
        return os.getenv(name, "")


# ---------------------------------------------------------------- 側邊欄
with st.sidebar:
    st.header("⚙️ 模型設定")
    # 未設定任何 API Key 時預設離線示範，打開即可試跑
    has_key = any(secret(k) for k in KEY_ENV.values())
    provider = PROVIDERS[st.selectbox("LLM 服務", list(PROVIDERS), index=0 if has_key else 2)]
    model = api_key = None
    if provider != "mock":
        model = st.text_input("模型名稱", DEFAULT_MODELS[provider])
        api_key = st.text_input(
            "API Key", value=secret(KEY_ENV[provider]), type="password",
            help=f"也可設定環境變數 {KEY_ENV[provider]} 或 .streamlit/secrets.toml",
        )
    else:
        st.caption("離線模式依規則式初篩結果產生示範對話，用於測試流程與報告版面。")

    st.header("📏 初篩門檻")
    limits = {
        "drift_max": st.number_input("層間位移角上限", value=DEFAULT_LIMITS["drift_max"],
                                     format="%.4f", step=0.0005),
        "beam_rho_max": st.number_input("梁配筋比上限", value=DEFAULT_LIMITS["beam_rho_max"],
                                        format="%.3f", step=0.001),
        "col_rho_min": st.number_input("柱主筋比下限", value=DEFAULT_LIMITS["col_rho_min"],
                                       format="%.3f", step=0.001),
        "col_rho_max": st.number_input("柱主筋比上限", value=DEFAULT_LIMITS["col_rho_max"],
                                       format="%.3f", step=0.005),
        "dcr_max": st.number_input("應力比 D/C 上限", value=DEFAULT_LIMITS["dcr_max"], step=0.05),
        "dcr_warn": st.number_input("應力比警示值", value=DEFAULT_LIMITS["dcr_warn"], step=0.05),
    }

# ---------------------------------------------------------------- 主畫面
st.title("🏗️ 結構計算書 AI 雙重詰問審查")
st.caption(
    f"{AGENT_NAMES['B']} 質疑 → {AGENT_NAMES['A']} 抗辯 → {AGENT_NAMES['B']} 終審 → "
    f"{AGENT_NAMES['C']} 彙整裁決"
)

st.subheader("1. 專案資料與結構摘要檔")
c1, c2, c3 = st.columns(3)
project_info = {
    "案名": c1.text_input("案名", "示範大樓新建工程"),
    "設計單位": c2.text_input("設計單位", ""),
    "建築地點": c3.text_input("建築地點", ""),
    "結構系統": c1.text_input("結構系統", "RC 特殊抗彎矩構架"),
    "樓層數": c2.text_input("樓層數", "地上 8 層／地下 1 層"),
    "備註": c3.text_input("備註（審查重點等）", ""),
}

uploaded = st.file_uploader("上傳 Excel／CSV（ETABS、MIDAS 匯出摘要或自行整理之檢核表）",
                            type=["xlsx", "xls", "xlsm", "csv"])
use_sample = st.checkbox("使用示範資料 examples/sample_structure.xlsx", value=uploaded is None)

summary: StructuralSummary | None = None
try:
    if uploaded is not None and not use_sample:
        summary = parse_structural_file(uploaded.getvalue(), filename=uploaded.name, limits=limits)
    elif use_sample and SAMPLE_FILE.exists():
        summary = parse_structural_file(SAMPLE_FILE, limits=limits)
except Exception as e:
    st.error(f"檔案解析失敗：{e}")

if summary:
    with st.expander(f"📄 前處理結果：{summary.source_name}（{len(summary.sheets)} 張工作表）",
                     expanded=False):
        for sheet in summary.sheets:
            st.markdown(f"**{sheet.name}**（{sheet.n_rows} 列）　辨識欄位："
                        + ("、".join(f"`{v}`→{k}" for k, v in sheet.mapped.items()) or "無"))
            frame = summary.frames[sheet.name]
            # 混合型別欄位（例如設計參數表）轉字串，避免 Arrow 序列化警告
            frame = frame.astype({c: str for c in frame.columns[frame.dtypes == object]})
            st.dataframe(frame, width="stretch", hide_index=True)
        st.markdown("**送給 Agent 的文字摘要**")
        st.code(summary.to_prompt_text(project_info), language="markdown")

    st.markdown("**規則式初篩**")
    if summary.findings:
        df = pd.DataFrame([f.as_dict() for f in summary.findings])
        df.insert(0, "", df.pop("level").map(lambda x: f"{RISK_ICON.get(x, '')} {x}"))
        df.columns = ["等級", "工作表", "項目", "說明", "參考規範"]
        st.dataframe(df, width="stretch", hide_index=True)
    else:
        st.success("未發現超出門檻之數值。")

# ---------------------------------------------------------------- 執行審查
st.subheader("2. 執行審查")
ready = summary is not None and (provider == "mock" or bool(api_key))
if provider != "mock" and not api_key:
    st.info("請於左側輸入 API Key，或改用離線示範模式。")

if st.button("▶️ 開始三回合詰問審查", type="primary", disabled=not ready):
    with st.status("審查進行中…", expanded=True) as status:
        try:
            client = create_client(provider, model=model, api_key=api_key)
            orchestrator = ReviewOrchestrator(
                client, on_progress=lambda stage, msg: status.write(msg)
            )
            st.session_state["review"] = orchestrator.run(summary, project_info)
            status.update(label="審查完成", state="complete", expanded=False)
        except Exception as e:
            status.update(label="審查失敗", state="error")
            st.error(f"{type(e).__name__}：{e}")

session: ReviewSession | None = st.session_state.get("review")
if not session or not session.chief:
    st.stop()

# ---------------------------------------------------------------- 結果
st.subheader("3. 審查結果")
chief = session.chief
verdict_color = {"退回補正": "red", "修正後同意": "orange", "同意備查": "green"}
st.markdown(f"### 總結論：:{verdict_color[chief.overall_conclusion.value]}"
            f"[{chief.overall_conclusion.value}]")
st.write(chief.executive_summary)
m1, m2, m3 = st.columns(3)
m1.metric("🔴 重大疑義", len(chief.major_doubts))
m2.metric("🟠 待複核項目", len(chief.pending_review_items))
m3.metric("🟢 無虞項目", len(chief.no_concern_items))

tab_report, tab_debate, tab_json = st.tabs(["📋 裁決彙整", "💬 詰問歷程", "🧾 原始 JSON"])

with tab_report:
    for title, items in (("重大疑義", chief.major_doubts), ("待複核項目", chief.pending_review_items),
                         ("無虞項目", chief.no_concern_items)):
        st.markdown(f"#### {title}（{len(items)}）")
        if not items:
            st.caption("無")
        for it in items:
            with st.container(border=True):
                st.markdown(f"**{RISK_ICON[it.risk_level.value]} {it.issue_id}｜{it.title}**")
                st.write(it.finding)
                st.caption(f"判定依據：{it.basis}")
                st.markdown(f"**處理意見：** {it.recommendation}")
    if chief.debate_observations:
        st.markdown("#### 主任檢核技師觀察")
        st.write(chief.debate_observations)
    if session.notes:
        with st.expander("系統一致性檢查紀錄"):
            for n in session.notes:
                st.write("• " + n)

with tab_debate:
    st.caption(f"Round 1 總體意見：{session.round1.summary}")
    for t in session.threads():
        i, r, v = t["issue"], t["response"], t["verdict"]
        badge = f"　{RISK_ICON[v.risk_level.value]} {v.decision.value}／{v.risk_level.value}" if v else ""
        with st.expander(f"{i.issue_id}｜{i.title}（{i.category}）{badge}", expanded=False):
            with st.chat_message("B", avatar="🔍"):
                st.markdown(f"**Round 1｜外部審查專家 質疑**\n\n{i.challenge}")
                st.code(i.formula, language=None)
                st.caption(f"引用數據：{i.cited_data}｜依據：{i.code_reference}")
            if r:
                with st.chat_message("A", avatar="📐"):
                    st.markdown(f"**Round 2｜原設計工程師 回覆（{r.stance.value}）**\n\n{r.response}")
                    if r.calculation:
                        st.code(r.calculation, language=None)
                    if r.proposed_action:
                        st.markdown(f"修正方案：{r.proposed_action}")
                    st.caption(f"數據出處：{r.data_source}｜規範依據：{r.code_reference}")
            if v:
                with st.chat_message("B", avatar="⚖️"):
                    st.markdown(f"**Round 3｜外部審查專家 終審（{v.decision.value}）**\n\n{v.rationale}")
                    if v.required_action:
                        st.caption(f"應辦事項：{v.required_action}")

with tab_json:
    st.json(session.to_dict())

# ---------------------------------------------------------------- 下載
st.subheader("4. 下載報告")
report_no = report_number(session)


@st.cache_data(show_spinner="產生 PDF 中…")
def build_pdf(_session: ReviewSession, key: str) -> tuple[bytes, str]:
    return generate_pdf(_session)


pdf_bytes, engine = build_pdf(session, f"{report_no}-{id(session)}")
d1, d2, d3 = st.columns(3)
d1.download_button("⬇️ PDF 審查報告", pdf_bytes, f"{report_no}.pdf", "application/pdf",
                   type="primary", width="stretch")
d2.download_button("⬇️ HTML 報告", render_html(session), f"{report_no}.html", "text/html",
                   width="stretch")
d3.download_button("⬇️ JSON 審查紀錄",
                   json.dumps(session.to_dict(), ensure_ascii=False, indent=2),
                   f"{report_no}.json", "application/json", width="stretch")
st.caption(f"PDF 排版引擎：{engine}" + ("（WeasyPrint 不可用，已改用簡化版面）" if engine == "reportlab" else ""))
