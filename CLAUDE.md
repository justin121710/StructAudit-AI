# 專案規則

**Git：每次修改完成後直接 commit 並 push 到 main，不另開分支、不需再詢問**（與 rc-member-designer、midas-civil-book、midas-curve-gen 相同）。
推送前先跑測試：`python3 -m pytest -q`（在儲存庫根目錄執行；需 `pip install -r requirements.txt`）。

回覆使用者一律用**台灣繁體中文與台灣用語**，專有名詞與程式碼除外。

## 與 rc-member-designer 的耦合

- 輸入欄位清單 `data/rcmd_params.json` 由 `tools/build_rcmd_params.js` 從 rc-member-designer 的 `index.html`（PIN_META）產生；工具新增或改名欄位時重產。
- 規範條文索引 `data/spec_index.json.gz` 由 `tools/build_spec_index.py` 從 rc-member-designer 的 `specs/` 產生。
- 計算書表頭欄位（`utils/calcbook.py` 的 `RCMD_HEADER_KEYS`）對應 rc-member-designer 計算書的 `rpHead`。
- 「內力來源」（工具整合 M4）：`utils/calcbook.py` 擷取計算書的「內力來源」段落，`utils/rcmd.py` 讀案件檔 `loadsrc`；
  `loads_key()` 必須與 rc-member-designer `core/midas-io.js` 的 `loadsKey` 結果相同（判斷匯入後是否手動修改）。
  rc-member-designer 改了該段文字或 `loadsrc` 欄位時，同步修改這裡並以 `examples/make_sample_midas.js` 重產示範檔。
- 四個工具的串接規格見 rc-member-designer `docs/INTEGRATION.md`。

## 示範檔

- `examples/sample_C1_*`：建築柱 C1（`make_sample_pdf.js`）。
- `examples/sample_P1_midas_*`：從 Midas 匯入的橋墩 P1（`make_sample_midas.js`，需 rc-member-designer 與 Playwright）。
