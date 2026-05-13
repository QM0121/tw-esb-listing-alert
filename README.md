# 台灣興櫃股轉板監測 Telegram 通知

本專案會：

1. 透過櫃買中心 OpenAPI 取得最新興櫃公司名單
2. 讀取公開資訊觀測站「即時重大訊息」
3. 篩選興櫃公司中與「上市、上櫃、創新板」相關的公告
4. 命中後立即用 Telegram Bot 發送通知
5. 將歷史通知同步顯示在 GitHub Pages 網站

---

## 已完成的 GitHub Secrets

請確認 Repository Secrets 已建立：

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

---

## 專案檔案

```text
tw-esb-listing-alert/
├─ .github/workflows/monitor.yml
├─ data/seen.json
├─ docs/
│  ├─ index.html
│  └─ data/
│     ├─ alerts.json
│     └─ status.json
├─ monitor.py
├─ requirements.txt
└─ README.md
```

---

## 第一次啟動

### 1. 上傳本專案檔案到你的 GitHub Repository

可以直接把壓縮檔解壓縮後的檔案上傳，或使用 Git 指令推送。

### 2. 到 GitHub 的 Actions 頁籤

找到 workflow：

```text
Monitor ESB Listing Alerts
```

點：

```text
Run workflow
```

這會先手動執行一次監測，也會驗證 Telegram Secrets 是否設定成功。

### 3. 等待 Telegram 測試結果

如果目前 MOPS 即時重大訊息中剛好有符合條件的公告，你會收到通知。
若沒有，Workflow 仍會成功，但不會有 Telegram 訊息。

---

## 啟用 GitHub Pages 網站

到 Repository：

```text
Settings → Pages
```

設定：

```text
Source: Deploy from a branch
Branch: main
Folder: /docs
```

網站首頁使用：

```text
docs/index.html
```

---

## 排程頻率

GitHub Actions 已設定：

```yaml
cron: "*/5 * * * *"
```

代表約每 5 分鐘執行一次。

---

## 通知條件

第一版會偵測標題中出現：

- 申請上市
- 申請上櫃
- 創新板
- 股票上市 / 股票上櫃
- 上市申請 / 上櫃申請

並輔助判斷進度：

- 董事會決議
- 送件
- 受理
- 審議通過
- 核准
- 撤回

---

## 注意事項

- 本工具是公告監測，不是投資建議。
- 公告內容請以公開資訊觀測站原始揭露為準。
- GitHub Actions 排程不保證精準在每個整 5 分鐘立刻啟動，可能會有些微延遲。
