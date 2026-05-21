# 台灣興櫃轉板監測 Mail 通知

本專案會：

1. 透過櫃買中心 OpenAPI 取得最新興櫃公司名單
2. 整理上市、上櫃、創新板相關公開資訊
3. 監測符合條件之新增事件
4. 透過 Mail 發送通知
5. 支援網站訂閱功能（Google Sheet 訂閱名單）
6. 將歷史通知同步顯示於 GitHub Pages 網站

---

# 已完成的 GitHub Secrets

請確認 Repository Secrets 已建立：

* `EMAIL_SENDER`
* `EMAIL_APP_PASSWORD`
* `SUBSCRIBERS_API_URL`

---

# Mail 訂閱名單來源

本站使用 Google Apps Script + Google Sheet 管理訂閱名單。

網站使用者輸入 Email 後：

* 會自動寫入 Google Sheet
* GitHub Actions 執行時自動讀取訂閱名單
* 偵測到新事件後寄送 Mail 通知

---

# 測試 Mail 通知

GitHub Actions 可手動執行：

```text
mail_test = true
```

若測試收件人欄位留空：

* 將自動寄送給 Google Sheet 訂閱名單

若填入 Email：

* 則只寄送至指定測試信箱

---

# 網站聲明

本站係整理政府開放資料、官方 OpenAPI 與官方公開資訊，作為興櫃轉板進度追蹤工具，不販售原始資料、不主張官方資料之權利，亦不取代任何正式公告。

所有事件內容仍應以原始資料提供機關之最新公告為準。

本站與臺灣證券交易所、證券櫃檯買賣中心及公開資訊觀測站無官方合作或隸屬關係。
