# 取消訂閱與私人信箱保護：設定步驟

本次已修改網站、寄信程式及 Actions，並補上原壓縮檔中缺少的 Google Apps Script 後端。
程式碼尚未上傳到你的 GitHub 或 Google 帳號。完成以下設定後才會在網站生效。

## 1. 準備獨立的寄信帳號

建議建立網站專用 Gmail，例如 `your-service-alerts@gmail.com`（僅示意，請使用自己建立的帳號）。
設定兩步驟驗證，建立此帳號的應用程式密碼。

收件人將看見「興櫃轉板監測通知 <你的服務信箱>」。
寄件人、回覆地址、SMTP 登入及 envelope sender 都使用同一個服務信箱。
程式不再讀取舊版 `EMAIL_SENDER`、`EMAIL_APP_PASSWORD`，以免沿用你的私人 Gmail。

只改顯示名稱或使用私人 Gmail 的 `+alerts` 地址，都無法隱藏原信箱。
私人帳號加上另一個寄件別名，也可能被郵件服務顯示為「代表某帳號寄送」。
電子郵件仍需有有效寄件地址；本版以獨立服務帳號保護你的私人地址。
為避免其他 Google 介面顯示私人帳號，也建議使用服務帳號部署下方 Apps Script，並將原試算表分享給服務帳號編輯。

## 2. 設定訂閱後端

先備份目前的訂閱 Google Sheet，確認訂閱資料所在的工作表分頁名稱。

1. 開啟既有 Apps Script 專案，將原本處理訂閱的 `doGet` / `doPost` 程式換成 `apps-script/Code.gs` 全文。不要保留舊版公開輸出完整名單的處理函式，也不要有重複的 `doGet` / `doPost`。
2. 到「專案設定」→「指令碼屬性」，加入下表資料。
3. 在編輯器選擇 `setup`，執行一次並完成 Google 授權。
4. 到「部署」→「管理部署作業」→ 編輯既有網頁應用程式 → 選擇新版本。執行身分選擇自己，誰可以存取選擇「所有人」。不要選成必須登入 Google 才能存取。
5. 複製正式網址 `https://script.google.com/macros/s/你的部署ID/exec`。使用 `/exec`，不要使用 `/dev`，也不要加 key 或其他查詢參數。
6. 在 `docs/index.html` 搜尋 `MAIL_SUBSCRIBE_URL`，將值換成這個正式網址。若沿用同一部署且網址相同，保留原值即可。

| Apps Script 指令碼屬性 | 設定值 |
| --- | --- |
| `SPREADSHEET_ID` | 訂閱試算表網址 `/d/` 與 `/edit` 之間的 ID |
| `SHEET_NAME` | 現有訂閱資料分頁名稱，須完全相符，例如 `工作表1` |
| `SUBSCRIBERS_API_KEY` | 自行產生的隨機字串，至少 32 字元；供程式讀取名單 |
| `UNSUBSCRIBE_SECRET` | 另一組獨立隨機字串，至少 32 字元；供取消訂閱連結簽章 |

可在自己的電腦執行以下命令兩次，分別產生上述兩組密鑰。不要將密鑰填入 HTML、README 或提交 Git。

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

`setup` 會保留舊欄位、資料與順序，新增 `status`、`created_at`、`updated_at`、`unsubscribed_at`、`source` 等缺少的管理欄位。
第一列必須是欄位名稱，且只能有一個 Email 欄位。支援 `email`、`e-mail`、`email address`、`電子郵件`、`電子郵件地址`、`信箱`、`郵件`，英文不分大小寫。
沒有 Email 表頭或表頭重複時會停止，不會猜測哪一欄是信箱。空白工作表會建立標準表頭。

原有空白 `status` 會標成 `active`。若舊系統另有停用欄位，請先將停用者在 `status` 欄設成 `unsubscribed`，再上線。
這份壓縮檔未包含原後端程式或訂閱表，因此無法自動確認你的舊表格欄位及既有停用紀錄。
試算表不必公開或「發布到網路」。若之前曾公開訂閱名單，請取消該公開設定；舊 Apps Script 公開名單部署也應關閉，避免繼續從舊網址讀取。

## 3. 設定 GitHub Actions Secrets

到儲存庫 Settings → Secrets and variables → Actions → Secrets，新增或修改：

| GitHub Secret | 設定值 |
| --- | --- |
| `EMAIL_SERVICE_SENDER` | 第 1 步建立的獨立服務 Gmail |
| `EMAIL_SERVICE_APP_PASSWORD` | 該服務 Gmail 的應用程式密碼 |
| `SUBSCRIBERS_API_URL` | 第 2 步的 Apps Script `/exec` 正式網址，不含查詢參數 |
| `SUBSCRIBERS_API_KEY` | 與 Apps Script 的 `SUBSCRIBERS_API_KEY` **完全相同** |
| `UNSUBSCRIBE_SECRET` | 與 Apps Script 的 `UNSUBSCRIBE_SECRET` **完全相同** |

Variables 可選填 `EMAIL_FROM_NAME`，預設「興櫃轉板監測通知」。
使用 Gmail 時，`EMAIL_SMTP_HOST`、`EMAIL_SMTP_PORT` 不必填，預設 `smtp.gmail.com:465`。
本版使用 SMTP over SSL；其他郵件服務也必須支援此連線方式。

舊版 `EMAIL_SENDER`、`EMAIL_APP_PASSWORD` 不再使用，可刪除；`EMAIL_RECIPIENTS` 也不再混入正式名單，以免繞過取消訂閱狀態。
新設定未完成時，程式會停止寄信，不會自動改回私人帳號。
若訂閱 API 無法讀取、未授權、格式舊版，或兩邊取消訂閱密鑰不一致，也會停止寄信。

## 4. 上傳檔案並檢查

將本專案檔案上傳到原儲存庫，包含 `.github/workflows/monitor.yml`。
`data/seen.json`、`docs/data/alerts.json`、`docs/data/status.json` 已保留原內容，不需要清除或重建。

1. 在更新後的網站，用你自己的測試信箱訂閱。會另開結果頁；以結果頁顯示為準。
2. 確認 Sheet 的測試信箱 `status` 是 `active`。
3. GitHub Actions → Monitor ESB Listing Alerts → Run workflow，`mail_test` 選 `true`，`mail_test_recipient` 填自己的測試信箱。測試信不會寄給其他訂閱者。
4. 查看測試信的寄件人、回覆地址與原始郵件標頭，應只有服務寄件信箱；`To` 只有你指定的單一收件人。
5. 點信底部的取消訂閱連結。只開啟頁面時，Sheet 狀態應不變；按「確認取消訂閱」後應變為 `unsubscribed`。
6. 重複點擊並確認不會出錯。從一般網站表單再填同一信箱，不會自動恢復已取消的訂閱。若要恢復，使用原通知信連結中的「重新訂閱」。
7. **手動測試模式會刻意寄給指定信箱，即使它已退訂；請以正式模式名單驗證退訂是否生效。** 執行一般模式（`mail_test=false`）時，只會寄給 `active` 名單，且須有新事件才寄信。

測試取消後，也可用下列唯讀程式檢查 API 不再包含該地址。密鑰會在本機以隱藏輸入讀取，不會出現在指令文字或網址中：

```python
import getpass
import requests

endpoint = input("Apps Script /exec URL: ").strip()
key = getpass.getpass("SUBSCRIBERS_API_KEY: ")
email = input("要確認已退訂的測試信箱: ").strip().lower()
response = requests.post(endpoint, json={"action": "list", "api_key": key}, timeout=40)
response.raise_for_status()
payload = response.json()
assert payload.get("ok") is True, "API 未授權或設定錯誤"
assert email not in payload["emails"], "此信箱仍在正式寄送名單"
print("確認：該地址已不在正式寄送名單。")
```

取消訂閱連結使用簽章，無法只輸入別人的 Email 就取消對方的訂閱。
連結中的 token 是管理憑證，持有者可取消或恢復該信箱的通知，請不要公開轉貼。
token 內的地址只是 Base64 編碼，不是加密；API key 和簽章密鑰不會放入通知信。
如需輪替 `UNSUBSCRIBE_SECRET`，請同步更新兩邊；所有舊信的管理連結會失效。
本版不提供郵件供應商的 RFC 8058 一鍵退訂；信內連結與 `List-Unsubscribe` 標頭會開啟確認頁。
退訂只停止後續通知，已在寄送中的信件仍可能送達。

## 本機檢查

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
node --test tests/apps-script.test.cjs
```

測試使用模擬 SMTP、HTTP 和 Google Sheet，不會寄出真實郵件，也不會修改線上名單。

參考：[Apps Script 網頁應用程式](https://developers.google.com/apps-script/guides/web)、[Gmail 寄件別名與原地址顯示說明](https://support.google.com/mail/answer/22370?hl=zh-Hant)。
