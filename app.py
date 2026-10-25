"""Streamlit Web 介面：streamlit run app.py

風格沿用 rc-member-designer（冷灰底、白卡、深青強調色，見 .streamlit/config.toml），版面維持簡單：
設定 → 初篩 → 開始審查（即時顯示三個 Agent 的對話）→ 結論與下載。
"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path

import streamlit as st

from agents.llm_client import DEFAULT_MODELS, create_client
from orchestrator import AGENT_NAMES, DialogueMessage, ReviewOrchestrator, ReviewSession
from utils.parser import DEFAULT_LIMITS, StructuralSummary, parse_structural_file
from utils.pdf_generator import generate_pdf
from utils.report_model import build_model, report_number
from utils.xlsx_report import generate_xlsx

SAMPLE_FILE = Path(__file__).parent / "examples" / "sample_structure.xlsx"
PROVIDERS = {"離線示範（不呼叫 API）": "mock", "Google Gemini": "gemini", "OpenAI": "openai"}
KEY_ENV = {"gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY"}
# 頭像用 Material 線條圖示（不用 emoji），與 rc-member-designer 的線條圖示風格一致
AVATAR = {"A": ":material/architecture:", "B": ":material/rate_review:", "C": ":material/gavel:"}
ROLE_INTRO = {
    "B": "外部審查專家：負責挑出計算書中不符規範或不夠保守之處",
    "A": "原設計工程師：負責說明設計依據，必要時承認缺失並提出修正",
    "C": "主任檢核技師：不參與辯論，綜合整個過程做出最後裁決",
}
STAGE_INTRO = {
    "round1": "外部審查專家閱讀上傳的結構數據與初篩結果，挑出最需要釐清的 3～5 項疑義。",
    "round2": "原設計工程師逐項回覆：同意並提出修正、引用規範說明作法合規，或以計算證明設計足夠。",
    "round3": "外部審查專家檢視每一項回覆，判定是否接受，並標示風險等級。",
    "chief": "主任檢核技師把所有疑義分為重大疑義、待複核與無虞三類，並給出本案的總結論。",
}
STAGE_TITLE = {
    "round1": "Round 1｜初審質疑",
    "round2": "Round 2｜回覆抗辯",
    "round3": "Round 3｜覆核終審",
    "chief": "彙整｜主任技師裁決",
}

st.set_page_config(page_title="結構計算書審查", page_icon=":material/fact_check:", layout="centered")
st.markdown("""<style>
[class*="st-key-card"]{background:#fff;box-shadow:0 1px 2px rgba(21,28,36,.07)}
.alert{border-radius:6px;padding:7px 12px;margin:4px 0;font-size:13.5px;border:1px solid}
.a-bad{background:#F8E6E3;border-color:rgba(176,58,46,.35)}
.a-warn{background:#FBF0DF;border-color:rgba(154,94,16,.32)}
.a-ok{background:#fff;border-color:#D8DEE5}
.alert small{color:#56636F}
.verdict{font-size:16px;font-weight:800;padding:8px 12px;border:2px solid #060;display:inline-block;background:#fff;margin:4px 0 8px}
.verdict.bad{border-color:#c00} .verdict.warn{border-color:#9A5E10}
.stage{font-size:13.5px;font-weight:700;color:#0B4D57;border-bottom:1px solid #C3CCD6;padding-bottom:3px;margin:16px 0 2px}
.stage-intro{font-size:13px;color:#56636F;margin:0 0 6px}
.roles{font-size:13px;color:#56636F;line-height:1.7;margin:-4px 0 4px}
</style>""", unsafe_allow_html=True)


def secret(name: str) -> str:
    try:
        return st.secrets.get(name, "") or os.getenv(name, "")
    except Exception:  # 沒有 secrets.toml
        return os.getenv(name, "")


def dialogue_header() -> None:
    st.markdown("### 審查對話")
    st.markdown('<div class="roles">' + "<br>".join(f"Agent {k}｜{v}" for k, v in ROLE_INTRO.items()) + "</div>",
                unsafe_allow_html=True)


class DialogueView:
    """把 Agent 發言依回合畫成對話；審查進行中逐則追加，完成後由 transcript 重繪。"""

    def __init__(self, box):
        self.box, self.stage = box, None

    def add(self, msg: DialogueMessage) -> None:
        with self.box:
            if msg.stage != self.stage:
                self.stage = msg.stage
                st.markdown(f'<div class="stage">{STAGE_TITLE[msg.stage]}</div>'
                            f'<div class="stage-intro">{STAGE_INTRO[msg.stage]}</div>', unsafe_allow_html=True)
            with st.chat_message(msg.agent, avatar=AVATAR[msg.agent]):
                st.markdown(f"**{msg.speaker}**　{msg.title}\n\n{msg.body}")


st.markdown("## 結構計算書審查")
st.caption(f"{AGENT_NAMES['B']} 質疑 → {AGENT_NAMES['A']} 回覆 → {AGENT_NAMES['B']} 終審 → {AGENT_NAMES['C']} 裁決")

# ---------------------------------------------------------------- 設定
has_key = any(secret(k) for k in KEY_ENV.values())
with st.container(border=True, key="card_setup"):
    provider = PROVIDERS[st.selectbox("LLM 服務", list(PROVIDERS), index=1 if has_key else 0)]
    model = api_key = None
    if provider != "mock":
        c1, c2 = st.columns(2)
        model = c1.text_input("模型名稱", DEFAULT_MODELS[provider])
        api_key = c2.text_input("API Key", value=secret(KEY_ENV[provider]), type="password")

    uploaded = st.file_uploader("結構摘要檔（Excel／CSV）", type=["xlsx", "xls", "xlsm", "csv"])
    use_sample = st.checkbox("使用示範資料", value=uploaded is None)

    with st.expander("專案資料（顯示於報告表頭）"):
        c1, c2 = st.columns(2)
        project_info = {
            "案名": c1.text_input("案名", "示範大樓新建工程"),
            "設計單位": c2.text_input("設計單位", ""),
            "建築地點": c1.text_input("建築地點", ""),
            "結構系統": c2.text_input("結構系統", "RC 特殊抗彎矩構架"),
        }
    with st.expander("初篩門檻"):
        c1, c2 = st.columns(2)
        limits = {
            "drift_max": c1.number_input("層間位移角上限", value=DEFAULT_LIMITS["drift_max"], format="%.4f", step=0.0005),
            "beam_rho_max": c2.number_input("梁鋼筋比上限", value=DEFAULT_LIMITS["beam_rho_max"], format="%.3f", step=0.001),
            "col_rho_min": c1.number_input("柱主筋比下限", value=DEFAULT_LIMITS["col_rho_min"], format="%.3f", step=0.001),
            "col_rho_max": c2.number_input("柱主筋比上限", value=DEFAULT_LIMITS["col_rho_max"], format="%.3f", step=0.005),
            "dcr_max": c1.number_input("D/C 上限", value=DEFAULT_LIMITS["dcr_max"], step=0.05),
            "dcr_warn": c2.number_input("D/C 警示值", value=DEFAULT_LIMITS["dcr_warn"], step=0.05),
        }

summary: StructuralSummary | None = None
try:
    if uploaded is not None and not use_sample:
        summary = parse_structural_file(uploaded.getvalue(), filename=uploaded.name, limits=limits)
    elif use_sample and SAMPLE_FILE.exists():
        summary = parse_structural_file(SAMPLE_FILE, limits=limits)
except Exception as e:
    st.error(f"檔案解析失敗：{e}")

# ---------------------------------------------------------------- 初篩
if summary:
    with st.expander(f"規則式初篩：{summary.source_name}（{len(summary.findings)} 項）"):
        for f in summary.findings:
            lv = {"High": "bad", "Medium": "warn"}.get(f.level, "ok")
            item, detail, ref = (html.escape(x) for x in (f.item, f.detail, f.reference))  # 內容來自上傳檔，須跳脫
            st.markdown(f'<div class="alert a-{lv}"><b>{item}</b>：{detail}　<small>{ref}</small></div>',
                        unsafe_allow_html=True)
        if not summary.findings:
            st.markdown('<div class="alert a-ok">未發現超出門檻之數值。</div>', unsafe_allow_html=True)

need_key = provider != "mock" and not api_key
run = st.button("開始審查", type="primary", width="stretch", disabled=summary is None or need_key, key="run")
if need_key:
    st.caption("請輸入 API Key，或改用離線示範。")

# ---------------------------------------------------------------- 審查對話
if run and summary is not None:
    st.session_state.pop("review", None)
    dialogue_header()
    status = st.status("審查進行中…", expanded=False)  # 進度放在對話上方，不會被逐漸變長的對話推到下面
    view = DialogueView(st.container())
    with status:
        try:
            client = create_client(provider, model=model, api_key=api_key)
            if provider == "mock":
                client.delay = 0.6  # 離線示範瞬間完成，稍作停頓才看得出逐回合的對話
            orch = ReviewOrchestrator(
                client,
                on_progress=lambda stage, text: status.update(label=text),
                on_message=view.add,
            )
            st.session_state["review"] = orch.run(summary, project_info)
            status.update(label="審查完成", state="complete")
        except Exception as e:
            status.update(label="審查失敗", state="error")
            st.error(f"{type(e).__name__}：{e}")
    session: ReviewSession | None = st.session_state.get("review")
else:
    session = st.session_state.get("review")
    if session:
        dialogue_header()
        view = DialogueView(st.container())
        for msg in session.transcript:
            view.add(msg)

if not (session and session.chief):
    st.stop()

# ---------------------------------------------------------------- 結論與下載
m = build_model(session)
st.markdown("### 審查結論")
st.markdown(f'<div class="verdict {m.verdict_level}">{m.verdict_text}</div>', unsafe_allow_html=True)


@st.cache_data(show_spinner="產生報表中…")
def build_files(_s: ReviewSession, key: str) -> tuple[bytes, bytes]:
    return generate_pdf(_s)[0], generate_xlsx(_s)


no = report_number(session)
pdf_bytes, xlsx_bytes = build_files(session, f"{no}-{id(session)}")
d1, d2, d3 = st.columns(3)
d1.download_button("下載 PDF 報告", pdf_bytes, f"{no}.pdf", "application/pdf", type="primary", width="stretch")
d2.download_button("下載 Excel", xlsx_bytes, f"{no}.xlsx",
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", width="stretch")
d3.download_button("下載 JSON", json.dumps(session.to_dict(), ensure_ascii=False, indent=2),
                   f"{no}.json", "application/json", width="stretch")
