# 部署到 Streamlit Community Cloud

這份說明讓 StructAudit-AI 變成一個網址，別人用瀏覽器就能打開使用，不需要安裝 Python。
Streamlit Community Cloud 是 Streamlit 官方的免費託管服務，直接從 GitHub repo 部署，之後推上 `main` 會自動更新。

> 為什麼不能用 GitHub Pages：GitHub Pages 只能放純前端網頁；本工具需要伺服器執行 Python（審查流程、呼叫 AI、產生 PDF／Excel）。

## 部署前已備妥的檔案

| 檔案 | 用途 |
|---|---|
| `requirements.txt` | Python 套件 |
| `packages.txt` | 系統套件：Pango（WeasyPrint 排版 PDF 用）與中文字型 Noto CJK、文泉驛正黑 |
| `.streamlit/config.toml` | 介面主題色 |
| `.streamlit/secrets.toml.example` | API Key 範本（實際的 `secrets.toml` 已列入 `.gitignore`，不會上傳） |

## 步驟

### 1. 登入 Streamlit Community Cloud

1. 開啟 <https://share.streamlit.io>，按 **Continue with GitHub**，用擁有本 repo 的 GitHub 帳號登入。
2. 第一次使用時會要求授權 Streamlit 讀取你的 GitHub repo。若本 repo 是私人（private）repo，授權時要允許存取私人 repo。

### 2. 建立 App

1. 按右上角 **Create app**，選擇從 GitHub 部署現有 repo。
2. 填入：

   | 欄位 | 值 |
   |---|---|
   | Repository | `justin121710/structaudit-ai` |
   | Branch | `main` |
   | Main file path | `app.py` |
   | App URL | 自訂子網域，例如 `structaudit`（網址會是 `https://structaudit.streamlit.app`） |

### 3. 進階設定（Advanced settings）

1. **Python version**：選 **3.12**（程式需要 3.10 以上）。
2. **Secrets**：貼上 API Key（TOML 格式，只需要填你要用的那一家）：

   ```toml
   GEMINI_API_KEY = "你的 Gemini API Key"
   OPENAI_API_KEY = "你的 OpenAI API Key"
   ANTHROPIC_API_KEY = "你的 Claude API Key"
   ```

   設定 Secrets 後，介面會用這把 Key 查詢可用模型，使用者直接從下拉選單挑三個 Agent 的模型，不必自己輸入 Key。
   Secrets 中的 Key 只留在伺服器，不會出現在網頁上；使用者也可以在介面輸入自己的 Key 改用自己的額度。
   不填 Secrets 也能部署：介面會預設「離線示範」，或由使用者自行在介面輸入 API Key。

### 4. 部署

按 **Deploy**。第一次建置需要幾分鐘（要安裝中文字型與 PDF 套件），完成後就會開啟網址。

### 5. 確認 PDF 正常

在網頁上用示範資料按「開始審查」，下載 PDF。若 PDF 版面為簡化版（表格較陽春、沒有頁首標題列），表示 Pango 沒裝成功、系統改用了備援的 ReportLab；請看下方「疑難排解」。

## 控制誰能使用

部署後，**任何拿到網址的人都能使用，呼叫 AI 的費用由 Secrets 中的 API Key 支付**。若只想給特定的人用：

1. 在 App 右下角 **Manage app** → **Settings** → **Sharing**。
2. 把觀看權限改為只限受邀者，並輸入要開放的 Email。

私人 App 的數量與條件以 Streamlit 官方說明為準。另外也建議在 Gemini／OpenAI／Claude 後台為 API Key 設定每月用量上限。

## 更新

推送到 `main` 後，Streamlit Cloud 會自動重新部署，不需要其他操作。
修改 Secrets：**Manage app** → **Settings** → **Secrets**，存檔後 App 會自動重啟。

## 疑難排解

| 狀況 | 處理方式 |
|---|---|
| 建置失敗 | **Manage app** 開啟記錄，看是 `requirements.txt`（Python 套件）還是 `packages.txt`（系統套件）出錯 |
| PDF 是簡化版版面 | Pango 未安裝成功，確認 `packages.txt` 內容並看建置記錄中的 apt 安裝訊息 |
| PDF 中文變成方框 | 中文字型未安裝成功，確認 `packages.txt` 有 `fonts-noto-cjk`、`fonts-wqy-zenhei` |
| 打開網址顯示 App 在休眠 | 免費方案的 App 一段時間沒人使用會休眠，按畫面上的喚醒按鈕即可，約需數十秒 |
| 某一家（例如 OpenAI）載不出模型 | Secrets 名稱建議用 `GEMINI_API_KEY`／`OPENAI_API_KEY`／`ANTHROPIC_API_KEY`（`OPEN_AI_KEY`、`CLAUDE_API_KEY` 等常見寫法也能辨識）；確認 Key 前後沒有多餘字元，且帳號已開通付費額度 |
| 顯示「沒有讀取模型清單的權限」 | OpenAI 限制權限的 Key 需在後台開啟 Models 讀取權限，或改用一般權限的 Key |
| 審查失敗並顯示 API 錯誤 | 檢查 Secrets 中的 Key 是否正確、帳號是否有額度，以及模型名稱是否可用 |

## 其他選擇：GitHub Codespaces（只給自己用）

不需要固定網址、只是偶爾自己要用時：

1. 在 GitHub repo 頁面按 **Code** → **Codespaces** → **Create codespace on main**。
2. 等環境開好後，在終端機執行：

   ```bash
   sudo apt-get update && sudo apt-get install -y $(cat packages.txt)
   pip install -r requirements.txt
   streamlit run app.py
   ```

3. 右下角會跳出預覽網址，點開即可使用。用完記得停止 Codespace，避免用掉免費時數。
