# 專案規則

**Git：每次修改完成後直接 commit 並 push 到 main，不另開分支、不需再詢問**（與 rc-member-designer、midas-civil-book、midas-curve-gen 相同）。
推送前先跑測試：`python3 -m pytest -q`（在儲存庫根目錄執行；需 `pip install -r requirements.txt`）。

回覆使用者一律用**台灣繁體中文與台灣用語**，專有名詞與程式碼除外。

## 與 rc-member-designer 的耦合

- 輸入欄位清單 `data/rcmd_params.json` 由 `tools/build_rcmd_params.js` 從 rc-member-designer 的 `index.html`（PIN_META）產生；工具新增或改名欄位時重產。
- 規範條文索引 `data/spec_index.json.gz` 由 `tools/build_spec_index.py` 從 rc-member-designer 的 `specs/` 產生。
- 計算書表頭欄位（`utils/calcbook.py` 的 `RCMD_HEADER_KEYS`）對應 rc-member-designer 計算書的 `rpHead`。
- 「內力來源」（工具整合 M4）：`utils/calcbook.py` 擷取計算書的「內力來源」段落，`utils/rcmd.py` 讀案件檔 `loadsrc`；
  `loads_key()`（指紋 v2）必須與 rc-member-designer `core/midas-io.js` 的 `loadsKey` 結果相同（判斷匯入後是否手動修改）：
  `tests/data/loads_key_vectors.json` 與該工具 `tests/fixtures/loads_key_vectors.json` 必須是同一份，規則改了兩邊一起改。
- 配對核對（`utils/pairing.py`）：`case_input_hash` 必須與 rc-member-designer `inputHash` 相同（去掉 saved、members、conf、review 後的 JSON 雜湊）；
  不符時 `orchestrator._apply_case` 不寫出調整後案件檔。寫回只改 `values`，並附 `review` 區塊（契約見 rc-member-designer `docs/INTEGRATION.md` §3.3～3.5）。
  rc-member-designer 改了該段文字或 `loadsrc` 欄位時，同步修改這裡並以 `examples/make_sample_midas.js` 重產示範檔。
- 下載檔名（多人共用，rc-member-designer `docs/TEAM_FOLDER.md`、INTEGRATION §3.6）：`utils/report_model.py` 的 `file_stem`
  （`{構件}_審查報告_{雜湊8}_{報告編號}`）與 `case_file_name`（`{構件}_案件_{雜湊8}_審查調整.json`）必須與 rc-member-designer `fileStem`
  （`{構件}_案件_{雜湊8}.json`）同一規則；雜湊8＝計算書表頭「輸入 SHA-256」前 8 碼。
- 介面配色依書稿（`.streamlit/config.toml` 與 `app.py` 的 CSS：teal `#1E7F86`、navy `#1F3A5F`），與 rc-member-designer、midas-curve-gen 一致。
- 示範檔腳本開啟 rc-member-designer 時要先關任務入口（`#taskSkip`）。
- 四個工具的串接規格見 rc-member-designer `docs/INTEGRATION.md`。

## 一般計算書模式與埋錯測試集（2026-11-17）

- 沒有案件檔、且計算書不是 rc-member-designer 產生（`CalcBook.from_rcmd` 為假）時進入一般計算書模式：`agents/prompts.py` 的 `GENERAL_MODE` 接在各角色 System Prompt 後、
  解決方案不寫工具欄位（`param_id` 清空）、報告標籤改為「修正項目總結」（`ReportModel.general`）。測試 `tests/test_general.py`。
- 「內力來源」辨識 Midas Civil 與 SAP2000（rc-member-designer `loadsrc.from = 'sap'`，欄位 P／M3／M2）。
- `bench/`：埋錯測試集（`seeds.py` 9 個 Seed，針對 `examples/sample_C1_calc.pdf`）與執行器 `python3 -m bench.run_bench`；
  rc-member-designer 計算書版面改了導致 Seed 不適用時，`tests/test_bench.py` 會報錯，依新版文字修正樣式。
  改審查預設輪數或模型組合前，先用它比較抓錯率、誤報候選與費用。

## 示範檔

- `examples/sample_C1_*`：建築柱 C1（`make_sample_pdf.js`）。
- `examples/sample_P1_midas_*`：從 Midas 匯入的橋墩 P1（`make_sample_midas.js`，需 rc-member-designer 與 Playwright）。

## UI／UX 原則（四個專案共用）
避免不必要的次要標籤與文字說明，讓版面自己說話：移除多餘小字、輔助標籤與冗長說明（`st.caption`、`help=`）；以排版、留白、圖示與視覺層級傳達功能；極簡、不擁擠。
