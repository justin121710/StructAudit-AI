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
 Streamlit 介面 ──► PDF（WeasyPrint，備援 ReportLab）／HTML／JSON 下載
```

## 快速開始

```bash
pip install -r requirements.txt
streamlit run app.py
```

左側選擇 LLM 服務並輸入 API Key；沒有 Key 時可選「離線示範」走完整流程與報告版面。
API Key 也可用環境變數 `GEMINI_API_KEY`／`OPENAI_API_KEY`，或 `.streamlit/secrets.toml`（參考 `secrets.toml.example`）。
模型名稱可在介面修改，或以 `GEMINI_MODEL`／`OPENAI_MODEL` 設定預設值；`OPENAI_BASE_URL` 可指向相容 OpenAI API 的服務。

## 專案結構

```
app.py                     Streamlit 介面
orchestrator.py            三回合辯論排程與一致性檢查
agents/
  prompts.py               A／B／C 的 System Prompt 與各回合 User Prompt
  schemas.py               各回合 JSON 輸出的 Pydantic 模型
  llm_client.py            OpenAI／Gemini／離線 Mock 封裝、JSON 驗證與重試
utils/
  parser.py                Excel／CSV 前處理、欄位辨識、規則式初篩
  pdf_generator.py         HTML 範本 → PDF
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
| 層間位移角 | ≤ 0.005 | 建築物耐震設計規範 3.13 |
| 梁拉力筋比 | ≤ 2.5% | 土木401 15.3.2 |
| 柱主筋比 | 1%～6% | 土木401 10.9.1／15.4.3 |
| 應力比 D/C | > 1.0 為 High，> 0.95 為 Medium | 土木401 9.1 |
| 載重組合 | 檢查 1.4D、1.2D+1.6L、E、W、0.9D 是否存在 | 耐震規範 2.17／土木401 9.2 |

初篩結果會一併提供給三個 Agent，作為辯論的客觀錨點，降低模型憑空推論。

## 設計重點

- **固定回合數**：Orchestrator 依序呼叫四次（缺漏時最多補問一次），不存在開放式迴圈。
- **結構化輸出**：每回合皆要求 JSON 並以 Pydantic 驗證；格式錯誤時把錯誤訊息回饋給模型重試。
- **一致性保護**：疑義統一編號 Q1～Q5；A 未回覆者記為「未能抗辯」；B 未判定者列待複核；
  C 的分類不重不漏（重複者取較嚴重分類、遺漏者依 B 終審風險補入），總結論依分類結果校正。
  所有修正都記錄在報告「系統紀錄」中。
- **分角色模型**：`ReviewOrchestrator({"A": client_a, "B": client_b, "C": client_c})` 可讓三個角色使用不同模型（例如 C 用較強的模型）。
- **PDF**：WeasyPrint 依 `templates/report_template.html` 排版（頁首頁尾、頁碼、簽章欄）；
  若系統缺少 Pango，自動改用 ReportLab 產生簡化版（會自動尋找系統中文字型嵌入）。
  WeasyPrint 需要中文字型，Linux 可安裝 `fonts-noto-cjk`。

## 測試

```bash
python -m pytest -q
```

## 限制與聲明

- 審查品質取決於輸入摘要的完整度；系統只看得到上傳的表格，看不到模型檔與圖說。
- LLM 可能引用錯誤條號，Prompt 已要求不確定時標記「條號待查」，仍須由技師核對。
- 本系統為審查輔助工具，產出報告不得取代依法辦理之結構簽證或外審。
