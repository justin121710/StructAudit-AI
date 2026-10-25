"""Streamlit Web 介面：streamlit run app.py

風格沿用 rc-member-designer（冷灰底、白卡、深青強調色，見 .streamlit/config.toml）。
開始審查前版面置中；按下「開始審查」後改為左右兩欄：左側設定，右側即時顯示審查過程與結果。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import streamlit as st

from agents.llm_client import PROVIDER_LABEL, MockClient, create_client, default_model, list_models
from orchestrator import ROLES, DialogueMessage, ReviewOrchestrator, ReviewSession
from utils import rcmd
from utils.calcbook import CalcBook, read_calcbook
from utils.pdf_generator import generate_pdf
from utils.report_model import build_model, report_number
from utils.xlsx_report import generate_xlsx

EXAMPLES = Path(__file__).parent / "examples"
SAMPLE_PDF, SAMPLE_CASE = EXAMPLES / "sample_C1_calc.pdf", EXAMPLES / "sample_C1_case.json"
KEY_ENV = {"gemini": "GEMINI_API_KEY", "claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}  # 建議的 Secrets 名稱
# Secrets 名稱常見寫法（比對時忽略大小寫與底線），例如 OPEN_AI_KEY、openai_api_key 都能讀到
KEY_ALIASES = {
    "gemini": {"GEMINIAPIKEY", "GEMINIKEY", "GOOGLEAPIKEY"},
    "openai": {"OPENAIAPIKEY", "OPENAIKEY"},
    "claude": {"ANTHROPICAPIKEY", "ANTHROPICKEY", "CLAUDEAPIKEY", "CLAUDEKEY"},
}
MODE_AI, MODE_DEMO = "使用 AI 模型", "離線示範（不呼叫 API）"
SLOT_PROVIDER = ["gemini", "claude", "openai"]  # 三個模型槽位的預設服務商（第 1 輪依序擔任 A／B／C）
ROLE_SHORT = {"A": "設計", "B": "外審", "C": "主任技師"}
# 頭像用 Material 線條圖示（不用 emoji），與 rc-member-designer 的線條圖示風格一致
AVATAR = {"A": ":material/architecture:", "B": ":material/rate_review:", "C": ":material/gavel:",
          "F": ":material/balance:"}
STAGE_TITLE = {
    "round1": "Round 1｜初審質疑",
    "round2": "Round 2｜回覆抗辯",
    "round3": "Round 3｜覆核終審與解決方案",
    "chief": "彙整｜主任技師裁決",
    "final": "綜合裁決",
}
STAGE_INTRO = {
    "round1": "外部審查專家先查閱規範原文，再逐項核對計算書，提出需要釐清的疑義。",
    "round2": "原設計工程師逐項回覆：同意並提出修正、引用規範說明作法合規，或以計算證明設計足夠。",
    "round3": "外部審查專家判定每一項回覆是否成立、標示風險等級，並提出可直接在 rc-member-designer 調整的解決方案。",
    "chief": "主任檢核技師複核外審方案，把疑義分為重大疑義、待複核與無虞三類，給出本輪結論。",
    "final": "彙整各輪（不同模型擔任不同角色）的結果，以保守原則取最嚴風險，並選出改動最小且符合規範的方案。",
}

started = st.session_state.get("started", False)
st.set_page_config(page_title="結構計算書審查", page_icon=":material/fact_check:",
                   layout="wide" if started else "centered")
st.markdown("""<style>
[class*="st-key-card"]{background:#fff;box-shadow:0 1px 2px rgba(21,28,36,.07)}
.alert{border-radius:6px;padding:7px 12px;margin:4px 0;font-size:13.5px;border:1px solid}
.a-bad{background:#F8E6E3;border-color:rgba(176,58,46,.35)}
.a-warn{background:#FBF0DF;border-color:rgba(154,94,16,.32)}
.a-ok{background:#fff;border-color:#D8DEE5}
.verdict{font-size:16px;font-weight:800;padding:8px 12px;border:2px solid #060;display:inline-block;background:#fff;margin:4px 0 8px}
.verdict.bad{border-color:#c00} .verdict.warn{border-color:#9A5E10}
.stage{font-size:13.5px;font-weight:700;color:#0B4D57;border-bottom:1px solid #C3CCD6;padding-bottom:3px;margin:16px 0 2px}
.stage-intro{font-size:13px;color:#56636F;margin:0 0 6px}
.roles{font-size:13px;color:#56636F;line-height:1.7;margin:-4px 0 4px}
</style>""", unsafe_allow_html=True)


def _norm_key_name(name: str) -> str:
    return name.upper().replace("_", "").replace("-", "")


def secret_key(provider: str) -> str:
    """從 Streamlit Secrets 或環境變數取得 API Key；名稱寫法不同（如 OPEN_AI_KEY）也能對上。"""
    aliases = KEY_ALIASES[provider]
    try:
        names = list(st.secrets.keys())
    except Exception:  # 沒有 secrets.toml
        names = []
    for name in names:
        if _norm_key_name(name) in aliases and isinstance(st.secrets[name], str) and st.secrets[name].strip():
            return st.secrets[name].strip()
    for name, value in os.environ.items():
        if _norm_key_name(name) in aliases and value.strip():
            return value.strip()
    return ""


@st.cache_data(ttl=3600, show_spinner="查詢可用模型中…")
def fetch_models(provider: str, api_key: str) -> list[str]:
    return list_models(provider, api_key)


@st.cache_data(show_spinner="讀取計算書中…", max_entries=8)
def load_calcbook(data: bytes, name: str) -> CalcBook:
    return read_calcbook(data, filename=name)


def default_index(provider: str, names: list[str]) -> int:
    pick = default_model(provider, names)
    return names.index(pick) if pick in names else 0


class DialogueView:
    """把 Agent 發言依輪次與回合畫成對話；審查進行中逐則追加，完成後由 transcript 重繪。"""

    def __init__(self, boxes: dict[int, object]):
        self.boxes, self.stage = boxes, {}

    def add(self, msg: DialogueMessage) -> None:
        box = self.boxes.get(msg.pass_no) or next(iter(self.boxes.values()))
        with box:
            if self.stage.get(msg.pass_no) != msg.stage:
                self.stage[msg.pass_no] = msg.stage
                st.markdown(f'<div class="stage">{STAGE_TITLE[msg.stage]}</div>'
                            f'<div class="stage-intro">{STAGE_INTRO[msg.stage]}</div>', unsafe_allow_html=True)
            with st.chat_message(msg.agent, avatar=AVATAR[msg.agent]):
                model = f"（{msg.model}）" if msg.model else ""
                st.markdown(f"**{msg.speaker}**{model}　{msg.title}\n\n{msg.body}")


def _start() -> None:
    st.session_state["started"] = True
    st.session_state["pending"] = True
    st.session_state.pop("review", None)


# ================================================================ 左側：設定
if started:
    left, right = st.columns([0.95, 1.65], gap="large")
else:
    left, right = st.container(), None

with left:
    st.markdown("## 結構計算書審查")
    st.caption("外審質疑 → 設計回覆 → 外審終審與解決方案 → 主任技師裁決；三個模型輪流擔任角色，最後綜合裁決")

    has_key = any(secret_key(p) for p in KEY_ENV)
    slots: list[tuple[str, str]] = []  # 三個模型槽位 (provider, model)
    keys: dict[str, str] = {}
    with st.container(border=True, key="card_setup"):
        mode = st.radio("審查方式", [MODE_AI, MODE_DEMO], index=0 if has_key else 1, horizontal=True, key="mode")
        if mode == MODE_AI:
            with st.expander("API Key", expanded=not has_key):
                for prov in KEY_ENV:
                    # 部署端設定的 Key 只留在伺服器，不放進輸入框（否則會傳到使用者的瀏覽器）
                    preset = secret_key(prov)
                    typed = st.text_input(f"{PROVIDER_LABEL[prov]} API Key", type="password", key=f"key_{prov}",
                                          placeholder="已使用部署設定的 Key，輸入可改用自己的" if preset else "")
                    if typed.strip() or preset:
                        keys[prov] = typed.strip() or preset
                st.caption("至少填一家；有多家的 Key 時，三個模型可以來自不同服務商。"
                           f"部署時請在 Secrets 設定 {'／'.join(KEY_ENV.values())}。")

            models_by_prov: dict[str, list[str]] = {}
            failed = False
            for prov, key in keys.items():
                try:
                    names = fetch_models(prov, key)
                except Exception as e:
                    failed = True
                    st.warning(str(e))
                    continue
                if names:
                    models_by_prov[prov] = names
            if models_by_prov:
                st.markdown("**三個模型**（每一輪輪流擔任設計、外審、主任技師）")
                provs = list(models_by_prov)
                for n in range(3):
                    role = ROLES[n]
                    label = f"模型 {n + 1}（第 1 輪擔任{ROLE_SHORT[role]}）"
                    want = SLOT_PROVIDER[n]
                    if len(provs) == 1:  # 只有一家：直接選模型
                        prov = provs[0]
                        names = models_by_prov[prov]
                        model = st.selectbox(f"{label}｜{PROVIDER_LABEL[prov]}", names,
                                             index=default_index(prov, names), key=f"model_{n}_{prov}")
                    else:  # 多家：先選服務商，再選該服務商的模型
                        c1, c2 = st.columns([1, 2])
                        prov_default = provs.index(want) if want in provs else n % len(provs)
                        prov = c1.selectbox(label, provs, index=prov_default, format_func=PROVIDER_LABEL.get,
                                            key=f"prov_{n}")
                        names = models_by_prov[prov]
                        # key 含服務商，切換服務商時模型選單會換成新的清單與預設值
                        model = c2.selectbox("模型", names, index=default_index(prov, names), key=f"model_{n}_{prov}")
                    slots.append((prov, model))
                if st.button("重新查詢模型清單", type="tertiary"):
                    fetch_models.clear()
                    st.rerun()
            elif keys and not failed:
                st.caption("此 API Key 查不到可用的文字模型。")

        c1, c2 = st.columns([1, 1])
        n_passes = c1.radio("審查輪數", [1, 2, 3], index=2, horizontal=True, key="passes",
                            help="每一輪由三個模型輪流擔任設計、外審、主任技師，各輪為全新對話、互不共用記憶。")
        lookup = c2.toggle("每回合先查閱規範原文", value=True, key="lookup",
                           help="Agent 在每一回合前自行決定要查哪些條文；規範原文取自 rc-member-designer 的 specs/。")
        names_for = [m for _, m in slots] if slots else ["示範模型"] * 3
        rows = ["| 輪次 | A 設計 | B 外審 | C 主任技師 |", "|---|---|---|---|"]
        for p in range(1, n_passes + 1):
            idx = ReviewOrchestrator.roles_for(p)
            rows.append(f"| 第 {p} 輪 | " + " | ".join(names_for[idx[r]] for r in ROLES) + " |")
        st.markdown("\n".join(rows))
        per_pass = 7 if lookup else 4  # 查閱×3（可關閉）＋ Round 1～3 ＋ 主任技師
        st.caption(f"每輪 {per_pass} 次模型呼叫（{'3 次查閱規範＋' if lookup else ''}Round 1～3＋主任技師），"
                   f"共約 {n_passes * per_pass + 1} 次（含綜合裁決）；每次都附上計算書 PDF 原檔。")

        uploaded = st.file_uploader("結構計算書（PDF，或 Excel／CSV 摘要表）", type=["pdf", "xlsx", "xls", "xlsm", "csv"],
                                    key="calc_file")
        case_up = st.file_uploader("rc-member-designer 案件檔（.json，選填）", type=["json"], key="case_file",
                                   help="在 rc-member-designer 按「儲存案件」下載。提供後，解決方案會寫入調整後的案件檔，"
                                        "可直接在工具中「開啟案件」重算。")
        use_sample = st.checkbox("使用示範計算書（柱 C1，含兩項 NG）", value=uploaded is None, key="use_sample")

    calc: CalcBook | None = None
    case, case_name = None, ""
    try:
        if uploaded is not None and not use_sample:
            calc = load_calcbook(uploaded.getvalue(), uploaded.name)
        elif use_sample and SAMPLE_PDF.exists():
            calc = load_calcbook(SAMPLE_PDF.read_bytes(), SAMPLE_PDF.name)
    except Exception as e:
        st.error(f"計算書讀取失敗：{e}")
    try:
        if case_up is not None and not use_sample:
            case, case_name = rcmd.load_case(case_up.getvalue()), case_up.name
        elif use_sample and SAMPLE_CASE.exists():
            case, case_name = rcmd.load_case(SAMPLE_CASE.read_bytes()), SAMPLE_CASE.name
    except ValueError as e:
        st.error(str(e))

    project_info: dict[str, str] = {}
    if calc:
        with st.expander(f"專案資料（取自計算書：{calc.filename}，{calc.n_pages or '—'} 頁）"):
            if not calc.facts:
                st.caption("計算書表頭未辨識到專案資料；審查時由 Agent 從計算書內容補充。")
            for k, v in calc.facts.items():
                project_info[k] = st.text_input(k, v, key=f"fact_{calc.sha256[:10]}_{k}")
            if case:
                st.caption(f"案件檔：{case_name}（{rcmd.MODULE_LABEL[rcmd.case_module(case)]}模組）")

    need_models = mode == MODE_AI and len(slots) < 3
    st.button("開始審查", type="primary", width="stretch", disabled=calc is None or need_models, key="run",
              on_click=_start)
    if need_models:
        st.caption("請輸入 API Key 並選好三個模型，或改用離線示範。")

# ================================================================ 右側：審查輸出
session: ReviewSession | None = st.session_state.get("review")
if right is None:
    st.stop()

with right:
    st.markdown("### 審查過程與結果")
    summary_box = st.container()
    pending = st.session_state.pop("pending", False)
    if pending and calc is not None:
        status = st.status("審查進行中…", expanded=False)  # 進度放在對話上方，不會被逐漸變長的對話推到下面
    # 進行中先看第 1 輪的即時對話；完成後綜合裁決排在最前面
    n_tabs = n_passes if pending else len(session.passes) if session else 0
    labels = [f"第 {p} 輪" for p in range(1, n_tabs + 1)]
    if pending:
        tabs = st.tabs([*labels, "綜合裁決"])
        view = DialogueView({**{n: tabs[n - 1] for n in range(1, n_tabs + 1)}, 0: tabs[-1]})
    else:
        tabs = st.tabs(["綜合裁決", *labels])
        view = DialogueView({0: tabs[0], **{n: tabs[n] for n in range(1, n_tabs + 1)}})

    if pending and calc is not None:
        with status:
            try:
                if mode == MODE_DEMO:
                    factories = lambda: MockClient(delay=0.25)  # noqa: E731 ── 離線示範稍作停頓，看得出逐回合對話
                else:
                    # 每個槽位給「產生器」：每一輪、每個角色都建立全新的 client，不共用記憶
                    factories = [lambda p=p, m=m: create_client(p, model=m, api_key=keys[p]) for p, m in slots]
                orch = ReviewOrchestrator(factories, passes=n_passes, lookup=lookup,
                                          on_progress=lambda stage, text: status.update(label=text),
                                          on_message=view.add)
                session = orch.run(calc, case, case_name, project_info)
                st.session_state["review"] = session
                status.update(label="審查完成", state="complete")
            except Exception as e:
                status.update(label="審查失敗", state="error")
                st.error(f"{type(e).__name__}：{e}")
                session = None
    elif session:
        for msg in session.transcript:
            view.add(msg)

    if session and session.final:
        m = build_model(session)

        @st.cache_data(show_spinner="產生報表中…")
        def build_files(_s: ReviewSession, key: str) -> tuple[bytes, bytes]:
            return generate_pdf(_s)[0], generate_xlsx(_s)

        no = report_number(session)
        pdf_bytes, xlsx_bytes = build_files(session, f"{no}-{id(session)}")
        with summary_box:
            st.markdown(f'<div class="verdict {m.verdict_level}">{m.verdict_text}</div>', unsafe_allow_html=True)
            for it in m.items:
                lv = {"NG": "bad", "注意": "warn"}.get(it.judge, "ok")
                sol = f"　建議：{it.solution.label}" if it.solution else ""
                st.markdown(f'<div class="alert a-{lv}"><b>{it.risk}</b>｜{it.title}{sol}</div>', unsafe_allow_html=True)
            d1, d2, d3 = st.columns(3)
            d1.download_button("下載 PDF 報告", pdf_bytes, f"{no}.pdf", "application/pdf", type="primary",
                               width="stretch")
            d2.download_button("下載 Excel", xlsx_bytes, f"{no}.xlsx",
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", width="stretch")
            d3.download_button("下載 JSON", json.dumps(session.to_dict(), ensure_ascii=False, indent=2),
                               f"{no}.json", "application/json", width="stretch")
            e1, e2 = st.columns(2)
            if session.adjusted_case:
                stem = Path(session.case_name).stem or "case"
                e1.download_button("下載調整後案件檔", rcmd.case_bytes(session.adjusted_case), f"{stem}_審查調整.json",
                                   "application/json", width="stretch",
                                   help="於 rc-member-designer 按「開啟案件」載入，重算確認各檢核項目轉為 OK。")
            e2.link_button("開啟 rc-member-designer", rcmd.TOOL_URL, width="stretch")
            if session.adjusted_applied:
                st.caption("調整後案件檔已寫入：" + "；".join(session.adjusted_applied))
