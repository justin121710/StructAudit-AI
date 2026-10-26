"""Streamlit Web 介面：streamlit run app.py

風格沿用 rc-member-designer（冷灰底、白卡、深青強調色，見 .streamlit/config.toml）。
開始審查前版面置中；按下「開始審查」後改為左右兩欄：左側設定，右側即時顯示審查過程與結果。
"""

from __future__ import annotations

import html
import json
import os
import re
import time
from pathlib import Path

import streamlit as st

from agents.llm_client import PROVIDER_LABEL, MockClient, create_client, default_model, list_models
from orchestrator import ROLES, DialogueMessage, ReviewOrchestrator, ReviewSession
from utils import rcmd
from utils.calcbook import CalcBook, read_calcbook
from utils.pdf_generator import generate_pdf
from utils.pricing import USD_TWD, Price, default_price, summarize, twd
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
PDF_WARN_MB = 15  # 計算書 PDF 超過此大小時提醒（附件以 base64 傳送，約增大 1/3；Gemini 單次請求上限約 20 MB）
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
TASK_LABEL = {"lookup": "查閱規範原文", "round1": "Round 1 初審質疑", "round2": "Round 2 回覆抗辯",
              "round3": "Round 3 覆核終審", "chief": "主任技師裁決", "final": "綜合裁決"}
STAGE_INTRO = {
    "round1": "外部審查專家先查閱規範原文，再逐項核對計算書，提出需要釐清的疑義。",
    "round2": "原設計工程師逐項回覆：同意並提出修正、引用規範說明作法合規，或以計算證明設計足夠。",
    "round3": "外部審查專家判定每一項回覆是否成立、標示風險等級，並提出可直接在 RC 構件設計工具調整的解決方案。",
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
.vcard{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;background:#fff;
  border:1px solid #D8DEE5;border-left:5px solid #1E7A44;border-radius:6px;padding:10px 16px;margin:2px 0 8px}
.vcard.bad{border-left-color:#B03A2E} .vcard.warn{border-left-color:#9A5E10}
.vcard .vk{font-size:12px;color:#56636F;margin-bottom:1px}
.vcard .vv{font-size:19px;font-weight:700;color:#151C24;display:flex;align-items:center;gap:8px}
.vchip{font-size:12px;font-weight:700;border-radius:10px;padding:1px 9px;border:1px solid}
.vchip.bad{color:#B03A2E;background:#F8E6E3;border-color:rgba(176,58,46,.35)}
.vchip.warn{color:#9A5E10;background:#FBF0DF;border-color:rgba(154,94,16,.32)}
.vchip.ok{color:#1E7A44;background:#E6F2EA;border-color:rgba(30,122,68,.3)}
.vstats{display:flex;gap:18px;font-size:13px;color:#56636F}
.vstats b{display:block;font-size:16px;color:#151C24}
.stage-now{font-size:13px;color:#0B4D57;margin:0 0 4px}
.stage-now:before{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;background:#0F5F6B;
  margin-right:8px;vertical-align:1px;animation:pulse 1.2s ease-in-out infinite}
@keyframes pulse{50%{opacity:.25}}
.thought-h{font-size:12.5px;font-weight:700;color:#0B4D57;margin:10px 0 2px}
.thought{font-size:12.5px;color:#56636F;line-height:1.7;border-left:2px solid #D8DEE5;padding-left:10px}
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


def cost_line(c, done: bool = False) -> str:
    """一行花費摘要（實際 token 數 × 單價）。"""
    head = "本次審查實際花費" if done else "目前已花費"
    tokens = f"{c.calls} 次呼叫｜輸入 {sum(r.total_input for r in c.rows):,} token、輸出 {sum(r.output for r in c.rows):,} token"
    if c.estimated:
        return f"**{head}：NT$0（離線示範不計費）**　{tokens}（依字數估算）"
    miss = f"；{'、'.join(c.missing)} 未設定單價，未計入" if c.missing else ""
    return f"**{head}：{twd(c.usd, c.fx)}**（US${c.usd:,.2f}，匯率 {c.fx:g}）　{tokens}{miss}"


def cost_table(c) -> str:
    rows = ["| 模型 | 呼叫 | 輸入 | 快取命中 | 快取寫入 | 輸出（含思考） | 美元 | 新台幣 |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in c.rows:
        usd = "未設定單價" if r.usd is None else f"{r.usd:,.3f}"
        ntd = "—" if r.usd is None else twd(r.usd, c.fx)
        rows.append(f"| {r.model} | {r.calls} | {r.input:,} | {r.cached:,} | {r.cache_write:,} | {r.output:,} | {usd} | {ntd} |")
    rows.append(f"| **合計** | {c.calls} | | | | | **{c.usd:,.3f}** | **{twd(c.usd, c.fx)}** |")
    return "\n".join(rows)


class ThinkingView:
    """把各 Agent 串流中的思考摘要即時寫進進度列（st.status）；每一次 API 呼叫一段，畫面更新有節流。"""

    INTERVAL = 0.25

    def __init__(self):
        self.boxes: dict[int, object] = {}
        self.last = 0.0
        self.pending: dict | None = None

    @staticmethod
    def head(entry: dict) -> str:
        return html.escape(f"{entry['stage'] or TASK_LABEL.get(entry['task'], entry['task'])}（{entry['model']}）")

    @staticmethod
    def body(text: str) -> str:
        # 段落改用 <br>：HTML 區塊中的空行會讓 Markdown 把後半段當成一般段落，造成大片空白
        return f'<div class="thought">{"<br>".join(html.escape(x) for x in re.split(r"\n+", text.strip()))}</div>'

    @staticmethod
    def render(entry: dict) -> None:
        st.markdown(f'<div class="thought-h">{ThinkingView.head(entry)}</div>{ThinkingView.body(entry["text"])}',
                    unsafe_allow_html=True)

    def add(self, entry: dict, delta: str) -> None:
        key = id(entry)
        if key not in self.boxes:
            self.flush()
            st.markdown(f'<div class="thought-h">{self.head(entry)}</div>', unsafe_allow_html=True)
            self.boxes[key] = st.empty()
        self.pending = entry
        if time.monotonic() - self.last >= self.INTERVAL:
            self.flush()

    def flush(self) -> None:
        e = self.pending
        if e is not None:
            self.boxes[id(e)].markdown(self.body(e["text"]), unsafe_allow_html=True)
            self.last, self.pending = time.monotonic(), None


def verdict_card(session: ReviewSession, m) -> str:
    """總判定卡片：與下方風險列表同樣的圓角卡片樣式，左側色條表示判定。"""
    word = session.final.overall_conclusion.value
    judge = {"bad": "NG", "warn": "注意", "ok": "OK"}[m.verdict_level]
    count = {r: sum(1 for i in m.items if i.risk == r) for r in ("高風險", "中風險", "低風險")}
    cost = session.cost()
    stats = [(f"{count['高風險']} 項", "高風險"), (f"{count['中風險']} 項", "中風險"),
             (f"{len(session.passes)} 輪", "角色輪替"),
             ("NT$0" if cost.estimated else twd(cost.usd, cost.fx), "實際花費")]
    return (f'<div class="vcard {m.verdict_level}"><div><div class="vk">總判定</div>'
            f'<div class="vv">{html.escape(word)}<span class="vchip {m.verdict_level}">{judge}</span></div></div>'
            '<div class="vstats">' + "".join(f"<div><b>{html.escape(v)}</b>{k}</div>" for v, k in stats) + "</div></div>")


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

        n_passes = st.radio("審查輪數", [1, 2, 3], index=2, horizontal=True, key="passes",
                            format_func=lambda n: f"{n} 輪",
                            help="每一輪由三個模型輪流擔任設計、外審、主任技師，各輪為全新對話、互不共用記憶。")
        names_for = [m for _, m in slots] if slots else ["示範模型"] * 3
        rows = ["| 輪次 | A 設計 | B 外審 | C 主任技師 |", "|---|---|---|---|"]
        for p in range(1, n_passes + 1):
            idx = ReviewOrchestrator.roles_for(p)
            rows.append(f"| 第 {p} 輪 | " + " | ".join(names_for[idx[r]] for r in ROLES) + " |")
        st.markdown("\n".join(rows))
        st.caption(f"每輪 7 次模型呼叫（Round 1～3 前各查閱一次規範原文＋Round 1～3＋主任技師；查閱規範固定執行），"
                   f"共 {n_passes * 7 + 1} 次（含綜合裁決）；每次都附上計算書 PDF 原檔。")

        prices: dict[str, Price] = {}
        with st.expander("計費單價與匯率（費用依實際 token 數計算）"):
            fx = st.number_input("匯率（新台幣／美元）", value=USD_TWD, min_value=1.0, step=0.1, format="%.2f", key="fx")
            for model in dict.fromkeys(m for _, m in slots):
                base = default_price(model)
                st.markdown(f"**{model}**（美元／百萬 token）" + (f"　<small>{base.source}</small>" if base else ""),
                            unsafe_allow_html=True)
                if base is None:
                    st.caption("未收錄此模型的單價，請依服務商價目表填寫；未填寫時只顯示 token 數。")
                c1, c2, c3 = st.columns(3)
                p_in = c1.number_input("輸入", value=base.input if base else 0.0, min_value=0.0, step=0.1,
                                       format="%.3f", key=f"price_in_{model}")
                p_out = c2.number_input("輸出（含思考）", value=base.output if base else 0.0, min_value=0.0, step=0.1,
                                        format="%.3f", key=f"price_out_{model}")
                p_cache = c3.number_input("快取讀取", value=base.cache_read if base else 0.0, min_value=0.0,
                                          step=0.01, format="%.3f", key=f"price_cache_{model}")
                if base or p_in or p_out:
                    write = p_in * 1.25 if model.startswith("claude") else 0.0  # Claude 快取寫入為輸入單價 1.25 倍
                    prices[model] = Price(p_in, p_out, p_cache, write, "使用者設定" if base is None else base.source)
            if not slots:
                st.caption("離線示範不呼叫 API、不計費；token 數為依字數估算。")

        uploaded = st.file_uploader("結構計算書（PDF，或 Excel／CSV 摘要表）", type=["pdf", "xlsx", "xls", "xlsm", "csv"],
                                    key="calc_file")
        case_up = st.file_uploader("RC 構件設計工具案件檔（.json，選填）", type=["json"], key="case_file",
                                   help="在 RC 構件設計工具按「儲存案件」下載。提供後，解決方案會寫入調整後的案件檔，"
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

    if calc is not None and not calc.has_text:
        st.warning(
            f"「{calc.filename}」沒有文字層（{calc.n_pages} 頁皆為影像，可能是掃描檔或以影像方式列印）。"
            "AI 模型仍會直接讀取 PDF 頁面影像進行審查，表頭專案資料改由 Agent 從頁面判讀；"
            "但計算書引用的條號無法自動查出原文（改由 Agent 自行查閱），離線示範也無法使用。\n\n"
            "若是 RC 構件設計工具的計算書，建議重新輸出：按「列印 / 存成 PDF」，目的地選「另存為 PDF」"
            "（不要選「Microsoft Print to PDF」或以圖片列印），可保留文字層，審查較準確，檔案也較小。",
            icon=":material/image:")
    if calc is not None and calc.pdf and len(calc.pdf) > PDF_WARN_MB * 1e6:
        st.warning(f"計算書 PDF 為 {len(calc.pdf) / 1e6:.1f} MB。每次模型呼叫都會附上原檔，"
                   f"超過約 {PDF_WARN_MB} MB 時部分服務商可能拒收；建議重新輸出較小的 PDF 或分冊審查。",
                   icon=":material/warning:")

    project_info: dict[str, str] = {}
    if calc:
        with st.expander(f"專案資料（取自計算書：{calc.filename}，{calc.n_pages or '—'} 頁）"):
            if not calc.facts:
                st.caption("計算書表頭未辨識到專案資料；審查時由 Agent 從計算書頁面判讀後補充。")
            for k, v in calc.facts.items():
                project_info[k] = st.text_input(k, v, key=f"fact_{calc.sha256[:10]}_{k}")
            if case:
                st.caption(f"案件檔：{case_name}（{rcmd.MODULE_LABEL[rcmd.case_module(case)]}模組）")

    need_models = mode == MODE_AI and len(slots) < 3
    demo_no_text = mode == MODE_DEMO and calc is not None and not calc.has_text
    st.button("開始審查", type="primary", width="stretch", disabled=calc is None or need_models or demo_no_text,
              key="run", on_click=_start)
    if need_models:
        st.caption("請輸入 API Key 並選好三個模型，或改用離線示範。")
    if demo_no_text:
        st.caption("離線示範需要 PDF 的文字層；此計算書請改用 AI 模型審查。")

# ================================================================ 右側：審查輸出
session: ReviewSession | None = st.session_state.get("review")
if right is None:
    st.stop()

with right:
    st.markdown("### 審查過程與結果")
    summary_box = st.container()
    pending = st.session_state.pop("pending", False)
    if pending and calc is not None:
        cost_box = st.empty()  # 即時花費：每完成一次 API 呼叫就依實際 token 數更新
        stage_box = st.empty()  # 目前階段（不放在進度列標題：標題一更新，使用者點開的進度列就會被收起）
    thinking_slot = st.container()  # 進行中為進度列（點開看 AI 正在思考什麼）；完成後為思考過程紀錄
    if pending and calc is not None:
        with thinking_slot:  # 進度放在對話上方，不會被逐漸變長的對話推到下面
            status = st.status("審查進行中｜點開可看 AI 正在思考什麼", expanded=False)
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
                live: list[dict] = []

                def show_cost(rec: dict) -> None:
                    live.append(rec)
                    cost_box.markdown(cost_line(summarize(live, prices, fx)))

                thinking = ThinkingView()
                orch = ReviewOrchestrator(factories, passes=n_passes,
                                          on_progress=lambda stage, text: stage_box.markdown(
                                              f'<div class="stage-now">{html.escape(text)}</div>', unsafe_allow_html=True),
                                          on_message=view.add, on_usage=show_cost, on_thinking=thinking.add)
                session = orch.run(calc, case, case_name, project_info, prices=prices, fx=fx)
                thinking.flush()
                cost_box.empty()
                stage_box.empty()
                st.session_state["review"] = session
                status.update(label=f"審查完成｜AI 思考過程（{len(session.thoughts)} 段）", state="complete")
            except Exception as e:
                stage_box.empty()
                status.update(label="審查失敗", state="error")
                st.error(f"{type(e).__name__}：{e}")
                session = None
    elif session:
        with thinking_slot.expander(f"審查完成｜AI 思考過程（{len(session.thoughts)} 段）"):
            if not session.thoughts:
                st.caption("所選模型沒有回傳思考摘要。")
            for entry in session.thoughts:
                ThinkingView.render(entry)
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
            st.markdown(verdict_card(session, m), unsafe_allow_html=True)
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
                                   help="於 RC 構件設計工具按「開啟案件」載入，重算確認各檢核項目轉為 OK。")
            e2.link_button("開啟 RC 構件設計工具", rcmd.TOOL_URL, width="stretch")
            if session.adjusted_applied:
                st.caption("調整後案件檔已寫入：" + "；".join(session.adjusted_applied))
            cost = session.cost()
            st.markdown(cost_line(cost, done=True))
            with st.expander("各模型用量與費用"):
                st.markdown(cost_table(cost))
