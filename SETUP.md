# 取消訂閱功能

本版沿用現有 EMAIL_SENDER 與 EMAIL_APP_PASSWORD，不變更寄件地址。

每封通知逐人寄送並附上簽章取消訂閱連結。開啟連結只顯示確認頁，確認後將 Sheet 的 status 設成 unsubscribed，後續名單不再包含該地址。

Apps Script 使用 apps-script/Code.gs。設定指令碼屬性 SPREADSHEET_ID、SHEET_NAME、SUBSCRIBERS_API_KEY 與 UNSUBSCRIBE_SECRET，執行 setup() 後更新既有部署。兩組密鑰至少 32 字元，分別同步至 GitHub Actions 同名 Secrets。

SUBSCRIBERS_API_URL 沿用現有 /exec 網址（可保留舊版 ?action=list），程式會以驗證過的 POST 讀取有效名單。公開 GET 不再提供完整名單。

測試模式只寄給明確指定地址。取消訂閱連結是管理憑證，請勿公開轉貼。已傳送中的通知仍可能送達。

驗證：python -m unittest discover -s tests -v；node --test tests/apps-script.test.cjs。
