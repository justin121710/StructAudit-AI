# StructAudit-AI｜結構計算書 AI 雙重詰問審查系統

以「雙 Agent 相互詰問 + 第三 Agent 彙整裁決」的方式審查結構計算書摘要數據，並產出 A4 PDF 審查報告。

```
 Excel / CSV ──► parser 前處理 ──► 規則式初篩（位移角、配筋比、D/C、載重組合）
                                        │
        ┌───────────────────────────────┘
        ▼
 Round 1  Agent B 外部審查專家   提出 3～5 項疑義（引用數據＋算式＋規範）
 Round 2  Agent A 原設計工程師   逐項：承認缺失／規範依據抗辯／計算推導抗辯
 Round 3  Agent B 外部審查專家   終審：接受／部分接受／駁回 ＋ Pass／Medium／High
 彙整     Agent C 主任檢核技師   無虞項目／重大疑義／待複核項目 ＋ 總結論
        │
        ▼
 Streamlit 介面 ──► PDF（WeasyPrint，備援 ReportLab）／Excel／HTML／JSON 下載
```

## 線上部署

要讓別人用網址直接開啟（不需安裝 Python），請依 [docs/DEPLOY.md](docs/DEPLOY.md) 部署到 Streamlit Community Cloud（免費）。
`packages.txt` 已列出 PDF 排版所需的系統套件與中文字型。

## 快速開始

```bash
pip install -r requirements.txt
streamlit run app.py
```

沒有設定 API Key 時預設「離線示範」，可直接走完整流程並產出報表。
API Key 可在介面輸入，或用環境變數 `GEMINI_API_KEY`／`OPENAI_API_KEY`、`.streamlit/secrets.toml`（參考 `secrets.toml.example`）。
模型名稱可在介面修改，或以 `GEMINI_MODEL`／`OPENAI_MODEL` 設定預設值；`OPENAI_BASE_URL` 可指向相容 OpenAI API 的服務。

## 介面與審查對話

介面只保留四個步驟：設定（LLM、摘要檔、專案資料、初篩門檻）→ 規則式初篩 → 開始審查 → 結論與下載（PDF／Excel／JSON）。
按下「開始審查」後，三個 Agent 的發言會依回合即時顯示在「審查對話」：

1. Round 1：外部審查專家（B）的初審意見與逐項質疑（含算式與依據）
2. Round 2：原設計工程師（A）逐項回覆（承認缺失／規範依據抗辯／計算推導抗辯）
3. Round 3：外部審查專家（B）逐項終審（接受／部分接受／駁回＋風險等級）
4. 彙整：主任檢核技師（C）的總結論

每則發言都以標示清楚的段落呈現（疑義說明、檢核算式、引用數據、規範依據；立場、回覆說明、修正方案；判定、風險等級、判定理由、應辦事項），
立場、判定與風險等級都附白話解釋，各回合標題下也有一句說明這一回合在做什麼。
Prompt 要求三個 Agent 用完整句子交代「檢查了什麼、數值與限值差多少、為什麼重要、要求什麼」，縮寫第一次出現時加註解釋，
讓業主、建築師等非結構專業的讀者也能一起閱讀。介面不使用 emoji，Agent 頭像為線條圖示。

對話紀錄同時保存在 JSON 的 `transcript` 欄位。

## 與 rc-member-designer 一致的風格

報表與介面配色沿用 [rc-member-designer](https://github.com/justin121710/rc-member-designer)（RC 構件設計工具）：

| 項目 | 對齊內容 |
|---|---|
| A4 報告（PDF） | 標題列＋兩欄式表頭、「程式版本／雜湊」、粗底線編號章節、「檢核彙總與總判定」表＋總判定框、附註、簽核欄（設計／校核／審核）、頁尾頁碼 |
| 判定用語 | OK／注意／NG（對應風險等級 Pass／Medium／High） |
| Excel 報表 | 「檢核表」＋「審查報告(A4)」；表頭 `D9E1F2`、分節 `F2F2F2`、整體判定淺琥珀 `FFF2CC`；Arial |
| 介面 | 冷灰底白卡、深青強調色 `#0F5F6B`（`.streamlit/config.toml`） |
| 規範條號 | 初篩條號已對照 rc-member-designer `specs/` 內的規範原文 |

PDF 與 Excel 共用 `utils/report_model.py` 的同一份報表資料，內容一致。

## 專案結構

```
app.py                     Streamlit 介面
version.py                 程式版本（列於報告表頭）
.streamlit/config.toml     介面主題色
orchestrator.py            三回合辯論排程與一致性檢查
agents/
  prompts.py               A／B／C 的 System Prompt 與各回合 User Prompt
  schemas.py               各回合 JSON 輸出的 Pydantic 模型
  llm_client.py            OpenAI／Gemini／離線 Mock 封裝、JSON 驗證與重試
utils/
  parser.py                Excel／CSV 前處理、欄位辨識、規則式初篩
  report_model.py          報表資料模型（PDF／HTML／Excel 共用）
  pdf_generator.py         HTML 範本 → PDF（WeasyPrint，備援 ReportLab）
  xlsx_report.py           Excel 審查報表（檢核表＋審查報告(A4)）
templates/report_template.html
examples/                  示範資料與產生腳本
tests/                     pytest
```

## 輸入資料格式

一個 Excel 可含多張工作表（或單一 CSV）。系統以欄名辨識數據，欄名可含單位括號，例如：

| 標準欄位 | 可辨識的欄名（節錄） | 數值格式 |
|---|---|---|
| 樓層 | 樓層、Story、Floor | |
| 構件 | 構件編號、Member、Label | B1／G3 視為梁，C1 視為柱；或另設「構件類型」欄 |
| 層間位移角 | 層間位移角、Drift | `0.0042`、`0.42%`、`1/238` |
| 配筋比 | 配筋比、鋼筋比、主筋比、ρ | `1.8`（%）或 `0.018` |
| 應力比 | 應力比、D/C、DCR | `0.95` |
| 載重組合 | 載重組合、Load Combination | `1.2D+1.0L+1.0EX` |

無法辨識的工作表（如設計參數表）仍會以表格形式提供給 Agent 參考。`python examples/make_sample.py` 可重新產生示範檔。

### 規則式初篩預設門檻（介面可調）

| 項目 | 門檻 | 參考 |
|---|---|---|
| 層間位移角 | ≤ 0.005 | 建築物耐震設計規範及解說 §2.16.1 |
| 梁拉力鋼筋比 | ≤ 2.5% | 土木401-112 §18.3.3.1 |
| 柱主筋比 | 1%～6% | 土木401-112 §18.4.4.1（一般柱 §10.6.1.1 為 1%～8%） |
| 應力比 D/C | > 1.0 為 NG，> 0.95 為注意 | 土木401-112 §9.5.1.1、§10.5.1.1 |
| 載重組合 | 檢查 1.4D、1.2D+1.6L、E、W、0.9D 是否存在 | 土木401-112 §5.3.1（表 5.3.1） |

初篩結果會一併提供給三個 Agent，作為辯論的客觀錨點，降低模型憑空推論。

## 設計重點

- **固定回合數**：Orchestrator 依序呼叫四次（缺漏時最多補問一次），不存在開放式迴圈。
- **結構化輸出**：每回合皆要求 JSON 並以 Pydantic 驗證；格式錯誤時把錯誤訊息回饋給模型重試。
- **一致性保護**：疑義統一編號 Q1～Q5；A 未回覆者記為「未能抗辯」；B 未判定者列待複核；
  C 的分類不重不漏（重複者取較嚴重分類、遺漏者依 B 終審風險補入），總結論依分類結果校正。
  所有修正都記錄在報告「系統紀錄」中。
- **分角色模型**：`ReviewOrchestrator({"A": client_a, "B": client_b, "C": client_c})` 可讓三個角色使用不同模型（例如 C 用較強的模型）。
- **PDF**：WeasyPrint 依 `templates/report_template.html` 排版；若系統缺少 Pango，自動改用 ReportLab
  依同一份報表資料產生相同章節的 PDF（會自動尋找系統中文字型嵌入）。WeasyPrint 需要中文字型，Linux 可安裝 `fonts-noto-cjk`。
- **Excel**：模型輸出以「=」開頭時一律存成文字，不會被 Excel 當成公式執行。

## 測試

```bash
python -m pytest -q
```

## 限制與聲明

- 審查品質取決於輸入摘要的完整度；系統只看得到上傳的表格，看不到模型檔與圖說。
- LLM 可能引用錯誤條號，Prompt 已要求不確定時標記「條號待查」，仍須由技師核對。
- 本系統為審查輔助工具，產出報告不得取代依法辦理之結構簽證或外審。
