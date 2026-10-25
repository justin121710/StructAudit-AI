# StructAudit-AI｜結構計算書 AI 詰問審查系統

讀取完整的結構計算書 PDF（例如 [rc-member-designer](https://justin121710.github.io/rc-member-designer/) 輸出的計算書），
由三個模型輪流擔任「原設計工程師／外部審查專家／主任檢核技師」進行多輪詰問審查，
並對每項疑義提出可直接在 rc-member-designer 調整的解決方案，產出含 LaTeX 算式的 A4 審查報告。

```
 計算書 PDF ─┬─► 原檔直接附給模型判讀（文字、算式、圖、表格版面）
            ├─► 擷取文字 → 表頭專案資料（構件、斷面、設計依據…）、計算書引用的條號
            └─► rc-member-designer 案件檔（選填）→ 各輸入欄位的目前值
 規範原文（rc-member-designer specs/）→ 條文索引 data/spec_index.json.gz

 第 p 輪（p = 1～3，三個模型輪流擔任角色，每輪全新對話、不共用記憶）
   Round 1  Agent B 外部審查專家   查閱規範原文 → 提出疑義（計算書出處＋LaTeX 算式＋條文原文）
   Round 2  Agent A 原設計工程師   查閱規範原文 → 承認缺失／規範依據抗辯／計算推導抗辯
   Round 3  Agent B 外部審查專家   查閱規範原文 → 終審（接受／部分接受／駁回＋低／中／高風險）
                                  ＋ 1～3 個解決方案（rc-member-designer 欄位、建議值、修正後驗算、改動幅度）
   彙整     Agent C 主任檢核技師   複核方案、採用改動最小且符合規範者，分類並給本輪結論
 綜合裁決  第 1 輪主任技師的模型（全新對話）合併各輪：風險取最嚴、選最小改動方案、寫回調整後案件檔
        │
        ▼
 Streamlit 介面（右側即時顯示）──► PDF（LaTeX 算式）／Excel／JSON／調整後案件檔
```

## 快速開始

```bash
pip install -r requirements.txt
streamlit run app.py
```

選「使用 AI 模型」並提供 API Key（介面輸入，或環境變數 `GEMINI_API_KEY`／`ANTHROPIC_API_KEY`／`OPENAI_API_KEY`、
`.streamlit/secrets.toml`），系統會用這把 Key 向各服務商查詢可用模型。沒有 Key 時可選「離線示範」走完整流程。
線上部署（不需安裝 Python）見 [docs/DEPLOY.md](docs/DEPLOY.md)。

## 審查原則：不預設立場

- **沒有初篩門檻**：程式不預設任何限值。位移角、配筋比、D/C、圍束筋、強柱弱梁……是否符合規範，
  一律由 Agent 查閱規範原文後自行判斷。
- **規範原文來自 rc-member-designer**：`tools/build_spec_index.py` 從 rc-member-designer 的 `specs/`
  （土木401-112、建築物耐震設計規範、建築物基礎構造設計規範、公路橋梁設計規範、公路橋梁耐震設計規範、下水道設施耐震設計及解說）
  建立條文索引，每段含條號、規範頁碼與全文（含解說）。
- **Agent 自行查閱**：計算書中引用的條號自動查出原文；每一回合開始前，該 Agent 再依條號或關鍵字查閱需要的條文。
  Agent 只能引用查得的原文；輸出的條號逐一比對索引，查無者記入報告「系統紀錄」提醒技師核對。
- **專案資料取自計算書**：報告表頭（構件、構材類別、斷面、設計依據、計算書程式版本…）直接讀自計算書表頭，
  介面可修正；Agent 於 Round 1 另會補充擷取計算書中的其他專案資訊。

## 讀取 PDF 計算書

- 計算書 PDF 原檔以附件傳給模型（Gemini、Claude、OpenAI 皆能原生讀 PDF），模型看得到算式、配筋圖與表格版面；
  若某模型不接受 PDF 附件，自動改用擷取文字並記入系統紀錄。
- 同時以 pypdf 擷取文字（含頁碼標記），用於條號比對、表頭擷取與離線示範。
- 仍支援 Excel／CSV 摘要表（只做欄位辨識與統計，不做門檻判定）。

## 角色輪替與記憶隔離

| 輪次 | Agent A 設計 | Agent B 外審 | Agent C 主任技師 |
|---|---|---|---|
| 第 1 輪 | 模型 1 | 模型 2 | 模型 3 |
| 第 2 輪 | 模型 2 | 模型 3 | 模型 1 |
| 第 3 輪 | 模型 3 | 模型 1 | 模型 2 |

- 三輪下來每個模型都擔任過每個角色一次；輪數可選 1～3（預設 3）。
- 每一輪、每一個角色都建立全新的 client，提示中不含其他輪的任何內容；每次呼叫都是單次請求（不帶歷史對話、
  不使用伺服器端記憶），所以同一個模型在不同輪次之間不會共用記憶（`tests/test_pipeline.py::test_passes_do_not_share_memory`）。
- 綜合裁決的保守校正：同一問題的風險取各輪最嚴者；任一輪的中、高風險項目都不得遺漏（遺漏者由系統補回）；
  總結論不得比任一輪寬鬆。所有校正都記錄在報告「系統紀錄」中。

### 預設模型（以旗艦模型為主）

| 服務商 | 預設 | 比對方式 |
|---|---|---|
| Gemini | Gemini 4 Argon | 依序比對 `gemini-4-argon`、名稱含 `argon`、`gemini-*-pro` |
| Claude | Claude Opus 5.5 | `claude-opus-5-5`，其次其他 Opus |
| OpenAI | GPT 6.1-sol | `gpt-6.1-sol`，其次其他 `-sol` 模型 |

預設值是從 API Key 查得的模型清單中比對（同一規則有多個時優先正式版、不取 preview／日期版），各服務商實際的模型 ID 寫法略有差異也能對上；
清單中都沒有時取清單第一個，介面上可自行改選。三家都有 Key 時，模型 1／2／3 預設為 Gemini／Claude／OpenAI。
Claude 的 Opus 5.5、Opus 5、Sonnet 5.5、Fable 5.1 會開啟伺服器端拒答備援。

## Round 3 的解決方案與 rc-member-designer

- 外審必須對中、高風險項目提出 1～3 個「確切的解決方案」：以 rc-member-designer 的輸入欄位表達
  （欄位清單 `data/rcmd_params.json` 由 `tools/build_rcmd_params.js` 從工具的 `index.html` 擷取，含下拉選單可選值），
  寫出目前值、建議值、以建議值重新驗算的 LaTeX 算式與改動幅度，並指出改動最小者；工具無法調整的問題另寫其他作法。
- 主任技師複核方案並採用改動最小且符合規範者；綜合裁決再比較各輪方案，選出最終方案。
- 建議值會檢查是否為該欄位的合法值（例如箍筋號數只能是 #3～#7），不合法者記入系統紀錄。
- **上傳 rc-member-designer 案件檔（.json，「儲存案件」下載）時**，Agent 會看到每個欄位的目前值與載重組合，
  系統把最終方案的參數變更寫入「調整後案件檔」；在工具中按「開啟案件」載入、重算，即可確認各檢核項目轉為 OK。
  以示範計算書為例：原案件有 2 項 NG（圍束筋間距無解、強柱弱梁），調整後案件檔（箍筋 #3→#4、主筋 #8→#9）在工具中重算後 NG 為 0。

## 輸出

| 輸出 | 內容 |
|---|---|
| PDF 報告 | 表頭（取自計算書）、審查依據、角色輪替表、綜合結論、解決方案（含 rc-member-designer 參數表與 LaTeX 驗算）、各輪審查歷程、檢核彙總與總判定、附註、附錄 A 引用規範條文原文、簽核欄 |
| Excel | 「檢核表」＋「審查報告(A4)」，色碼與 rc-member-designer 一致；算式轉為可讀文字 |
| JSON | 各輪完整輸出、查閱的條文、對話紀錄、系統紀錄 |
| 調整後案件檔 | 可直接在 rc-member-designer「開啟案件」 |

- **LaTeX 算式**：所有算式欄位都是 LaTeX（KaTeX 語法，與 rc-member-designer 計算書相同寫法）。
  介面以 KaTeX 顯示；PDF 以 matplotlib mathtext 排成向量 SVG（Computer Modern 字面，WeasyPrint 不執行 JavaScript，無法直接用 KaTeX），
  ReportLab 備援用高解析 PNG；無法排版的算式改顯示可讀文字，不會讓報告失敗。
- **風險等級用中文**：低風險／中風險／高風險；判定用語沿用 rc-member-designer 的 OK／注意／NG。

## 介面

開始審查前設定區置中；按下「開始審查」後改為左右兩欄：左側為設定，右側即時顯示各輪對話（分頁：第 1～3 輪、綜合裁決），
完成後右側最上方顯示總判定、各項風險與建議方案，以及 PDF／Excel／JSON／調整後案件檔下載與「開啟 rc-member-designer」。

## 專案結構

```
app.py                     Streamlit 介面
orchestrator.py            角色輪替的多輪審查、規範查閱、一致性與保守校正、綜合裁決
agents/
  prompts.py               各角色 System Prompt 與各回合 User Prompt（不預設立場）
  schemas.py               各回合 JSON 輸出的 Pydantic 模型（中文風險等級、LaTeX 算式、解決方案）
  llm_client.py            Gemini／Claude／OpenAI／離線 Mock；PDF 附件、預設旗艦模型、JSON 驗證與重試
utils/
  calcbook.py              計算書讀取（PDF 原檔＋文字擷取、表頭專案資料；Excel／CSV）
  specs.py                 規範條文庫（條號查詢、關鍵字查詢、引用比對）
  rcmd.py                  rc-member-designer 欄位清單、案件檔驗證與套用
  latex.py                 LaTeX 算式排版（SVG／PNG／純文字）
  parser.py                Excel／CSV 欄位辨識
  report_model.py          報表資料模型（PDF／HTML／Excel 共用）
  pdf_generator.py         HTML 範本 → PDF（WeasyPrint，備援 ReportLab）
  xlsx_report.py           Excel 審查報表
templates/report_template.html
data/spec_index.json.gz    規範條文索引（由 tools/build_spec_index.py 產生）
data/rcmd_params.json      rc-member-designer 輸入欄位清單（由 tools/build_rcmd_params.js 產生）
tools/                     上述兩個資料檔的產生程式
examples/                  示範計算書（柱 C1，含兩項 NG）、案件檔、Excel 摘要表與產生腳本
tests/                     pytest
```

### 更新規範索引與欄位清單

rc-member-designer 更新規範或輸入欄位後：

```bash
pip install pymupdf                       # 規範 PDF 的字型對照表不完整，需用 PyMuPDF 擷取（僅建索引時需要）
python tools/build_spec_index.py /path/to/rc-member-designer
node tools/build_rcmd_params.js /path/to/rc-member-designer/index.html
```

## 測試

```bash
python -m pytest -q
```

## 限制與聲明

- 審查依據為送審計算書與規範原文索引；系統看不到結構分析模型檔與圖說。
- 規範索引以條號切段，少數表格與公式的版面在文字擷取後可能不完整；報告附錄列出條文原文節錄，仍須由技師對照原文。
- 建議方案的數值須在 rc-member-designer 重新計算確認各檢核項目轉為 OK 後，才能作為修正依據。
- 預設 3 輪審查約 25 次模型呼叫，每次都附上計算書 PDF，請留意 API 用量；可改為 1～2 輪或關閉「每回合先查閱規範原文」。
- 本系統為審查輔助工具，產出報告不得取代依法辦理之結構簽證或外審。
