/**
 * 興櫃轉板監測訂閱服務。完整設定方式見 SETUP.md。
 * 不使用 MailApp/GmailApp，避免從 Apps Script 擁有者的私人帳號寄信。
 * 公開 GET 不輸出名單、不變更訂閱狀態；名單僅供帶 API key 的 POST 讀取。
 */
const REQUIRED_COLUMNS = ['email', 'status', 'created_at', 'updated_at', 'unsubscribed_at', 'source'];
const EMAIL_HEADERS = ['email', 'e-mail', 'email address', '電子郵件', '電子郵件地址', '信箱', '郵件'];
const SITE_URL = 'https://qm0121.github.io/tw-esb-listing-alert/';

function config_() {
  const p = PropertiesService.getScriptProperties();
  const config = {
    sheetId: p.getProperty('SPREADSHEET_ID'),
    sheetName: p.getProperty('SHEET_NAME'),
    apiKey: p.getProperty('SUBSCRIBERS_API_KEY') || '',
    secret: p.getProperty('UNSUBSCRIBE_SECRET') || ''
  };
  if (!config.sheetId || !config.sheetName || config.apiKey.length < 32 || config.secret.length < 32) {
    throw new Error('Missing subscription service configuration.');
  }
  return config;
}

function sheet_(config) {
  const sheet = SpreadsheetApp.openById(config.sheetId).getSheetByName(config.sheetName);
  if (!sheet) throw new Error('Subscription sheet does not exist.');
  return sheet;
}

function locked_(callback) {
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    return callback();
  } finally {
    try { SpreadsheetApp.flush(); } finally { lock.releaseLock(); }
  }
}

/** 設定屬性後手動執行一次。保留現有資料和欄位，只補齊缺少的管理欄位。 */
function setup() {
  const config = config_();
  locked_(function () {
    const sheet = sheet_(config);
    if (!sheet.getLastRow()) {
      sheet.appendRow(REQUIRED_COLUMNS);
      return;
    }
    const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0]
      .map(function (v) { return String(v).trim().toLowerCase(); });
    const emailIndices = headers.map(function (v, i) { return EMAIL_HEADERS.indexOf(v) >= 0 ? i : -1; })
      .filter(function (i) { return i >= 0; });
    if (emailIndices.length !== 1) throw new Error('Expected exactly one Email header in row 1.');
    REQUIRED_COLUMNS.forEach(function (name) {
      const existing = name === 'email' ? emailIndices : headers.filter(function (h) { return h === name; });
      if (existing.length > 1) throw new Error('Duplicate management column: ' + name);
    });
    REQUIRED_COLUMNS.filter(function (name) { return name !== 'email' && headers.indexOf(name) < 0; })
      .forEach(function (name) {
        sheet.getRange(1, headers.length + 1).setValue(name);
        headers.push(name);
      });
    // 舊列沒有 status 時視為原有訂閱；既有取消紀錄不會清空。
    const statusIndex = headers.indexOf('status');
    if (sheet.getLastRow() > 1) {
      const range = sheet.getRange(2, statusIndex + 1, sheet.getLastRow() - 1, 1);
      range.setValues(range.getValues().map(function (row) { return [String(row[0]).trim() ? row[0] : 'active']; }));
    }
  });
}

function table_(config) {
  const sheet = sheet_(config);
  const rows = sheet.getDataRange().getValues();
  const headers = rows[0].map(function (v) { return String(v).trim().toLowerCase(); });
  const indices = {};
  REQUIRED_COLUMNS.forEach(function (name) {
    const matches = headers.map(function (value, index) {
      return (name === 'email' ? EMAIL_HEADERS.indexOf(value) >= 0 : value === name) ? index : -1;
    }).filter(function (i) { return i >= 0; });
    if (matches.length !== 1) throw new Error('Run setup() first; invalid column: ' + name);
    indices[name] = matches[0];
  });
  return { sheet: sheet, rows: rows.slice(1), indices: indices, width: headers.length };
}

function normalizeEmail_(value) { return String(value || '').trim().toLowerCase(); }

function validEmail_(email) {
  return email.length <= 254 && /^[a-zA-Z0-9][a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]*@[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?\.[a-zA-Z]{2,}$/.test(email);
}

function active_(status) {
  return ['', 'active', 'true', '1', 'subscribed', '已訂閱', '啟用'].indexOf(String(status).trim().toLowerCase()) >= 0;
}

function equal_(a, b) {
  if (a.length !== b.length) return false;
  let difference = 0;
  for (let i = 0; i < a.length; i++) difference |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return difference === 0;
}

function hmacHex_(value, secret) {
  return Utilities.computeHmacSha256Signature(value, secret, Utilities.Charset.UTF_8)
    .map(function (b) { return ('0' + ((b + 256) % 256).toString(16)).slice(-2); }).join('');
}

function verifyToken_(token, secret) {
  const parts = String(token || '').split('.');
  if (parts.length !== 3 || parts[0] !== 'v1' || !/^[A-Za-z0-9_-]{1,340}$/.test(parts[1]) || !/^[a-f0-9]{64}$/.test(parts[2])) return null;
  const signature = hmacHex_('unsubscribe:v1:' + parts[1], secret);
  if (!equal_(signature, parts[2])) return null;
  try {
    const padded = parts[1] + '='.repeat((4 - parts[1].length % 4) % 4);
    const email = Utilities.newBlob(Utilities.base64DecodeWebSafe(padded)).getDataAsString('UTF-8');
    return validEmail_(email) && normalizeEmail_(email) === email ? email : null;
  } catch (error) { return null; }
}

function listEmails_(config) {
  return locked_(function () {
    const table = table_(config);
    const states = new Map();
    table.rows.forEach(function (row) {
      const email = normalizeEmail_(row[table.indices.email]);
      if (!validEmail_(email)) return;
      // 舊表格若有重複列，只要任一列已停用，就不寄送。
      states.set(email, states.get(email) !== false && active_(row[table.indices.status]));
    });
    return Array.from(states.keys()).filter(function (email) { return states.get(email); });
  });
}

function changeSubscription_(config, email, action, authorized) {
  return locked_(function () {
    const table = table_(config);
    const matching = [];
    table.rows.forEach(function (row, index) {
      if (normalizeEmail_(row[table.indices.email]) === email) matching.push(index);
    });
    if (action === 'subscribe' && !authorized && matching.some(function (i) { return !active_(table.rows[i][table.indices.status]); })) {
      return '此信箱已取消訂閱。若要重新訂閱，請從原通知信的取消訂閱連結開啟管理頁面。';
    }
    const now = new Date().toISOString();
    const status = action === 'unsubscribe' ? 'unsubscribed' : 'active';
    if (!matching.length) {
      const row = new Array(table.width).fill('');
      row[table.indices.email] = email;
      row[table.indices.status] = status;
      row[table.indices.created_at] = now;
      row[table.indices.updated_at] = now;
      row[table.indices.unsubscribed_at] = status === 'unsubscribed' ? now : '';
      row[table.indices.source] = 'tw-esb-listing-alert';
      table.sheet.appendRow(row);
    } else {
      matching.forEach(function (index) {
        // 只更新管理欄位，保留舊資料與公式。
        table.sheet.getRange(index + 2, table.indices.status + 1).setValue(status);
        table.sheet.getRange(index + 2, table.indices.updated_at + 1).setValue(now);
        table.sheet.getRange(index + 2, table.indices.unsubscribed_at + 1).setValue(status === 'unsubscribed' ? now : '');
      });
    }
    return action === 'unsubscribe'
      ? '已取消訂閱。後續通知將不再寄送；已在傳送中的信件可能仍會送達。'
      : '訂閱成功！之後有新事件時將寄送通知，不會重複寄送歷史資料。';
  });
}

function escape_(value) {
  return String(value).replace(/[&<>"']/g, function (c) { return ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'})[c]; });
}

function form_(token, action, label) {
  // _blank 同時支援獨立及試算表綁定專案，避免 iframe 的頂層導覽限制。
  return '<form method="post" action="' + escape_(ScriptApp.getService().getUrl()) + '" target="_blank" rel="noopener noreferrer">' +
    '<input type="hidden" name="action" value="' + escape_(action) + '">' +
    '<input type="hidden" name="token" value="' + escape_(token) + '">' +
    '<button type="submit">' + escape_(label) + '</button></form>';
}

function page_(title, message, controls) {
  return HtmlService.createHtmlOutput('<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer">' +
    '<base target="_blank"><title>' + escape_(title) + '</title><style>' +
    'body{font-family:system-ui,sans-serif;background:#f5f7fb;color:#19334e;margin:0;padding:32px 18px}' +
    'main{max-width:520px;margin:8vh auto;padding:28px;background:white;border-radius:18px;box-shadow:0 12px 36px #19334e12}' +
    'h1{font-size:23px}p{line-height:1.8}button{background:#19334e;color:white;border:0;padding:13px 20px;border-radius:10px;font:inherit;cursor:pointer}' +
    'a{color:#305b8c}form{margin:20px 0}</style></head><body><main><h1>' + escape_(title) + '</h1><p>' +
    escape_(message) + '</p>' + (controls || '') + '<p><a href="' + SITE_URL + '">返回興櫃轉板監測站</a></p></main></body></html>')
    .setTitle(title);
}

function json_(payload) {
  return ContentService.createTextOutput(JSON.stringify(payload)).setMimeType(ContentService.MimeType.JSON);
}

function params_(e) {
  if (e && e.postData && /application\/json/i.test(e.postData.type || '')) return JSON.parse(e.postData.contents);
  return e && e.parameter || {};
}

function doGet(e) {
  try {
    const config = config_();
    const p = e && e.parameter || {};
    if (p.action === 'unsubscribe') {
      const email = verifyToken_(p.token, config.secret);
      if (!email) return page_('連結無效', '請使用最近一封通知信中的取消訂閱連結。');
      // 不在 GET 修改任何狀態，避免信箱的安全掃描器誤退訂。
      return page_('管理通知訂閱', '按下確認後將停止收到興櫃轉板通知，處理結果會另開頁面顯示。若要恢復通知，也可以選擇重新訂閱。',
        form_(p.token, 'unsubscribe', '確認取消訂閱') + form_(p.token, 'subscribe', '重新訂閱'));
    }
    return page_('通知訂閱服務', '若要取消訂閱，請點選任一封新通知信底部的「取消訂閱」連結。');
  } catch (error) {
    return page_('服務暫時無法使用', '請稍後再試。');
  }
}

function doPost(e) {
  let action = '';
  try {
    const p = params_(e);
    action = String(p.action || 'subscribe');
    const config = config_();
    if (action === 'list') {
      if (!equal_(String(p.api_key || ''), config.apiKey)) return json_({ok: false, error: 'unauthorized'});
      return json_({ok: true, schema_version: 1, emails: listEmails_(config),
        signing_key_check: hmacHex_('unsubscribe-config:v1', config.secret)});
    }
    if (action !== 'subscribe' && action !== 'unsubscribe') return page_('操作無效', '請從網站或通知信重新開啟服務。');
    const tokenEmail = p.token ? verifyToken_(p.token, config.secret) : null;
    if ((p.token && !tokenEmail) || (action === 'unsubscribe' && !tokenEmail)) {
      return page_('連結無效', '請使用最近一封通知信中的取消訂閱連結。');
    }
    const email = tokenEmail || normalizeEmail_(p.email);
    if (!validEmail_(email)) return page_('信箱格式錯誤', '請輸入正確的 Email 地址。');
    const message = changeSubscription_(config, email, action, Boolean(tokenEmail));
    return page_('訂閱處理結果', message,
      tokenEmail && action === 'unsubscribe' ? form_(p.token, 'subscribe', '重新訂閱') : '');
  } catch (error) {
    // 不回傳堆疊、帳號、token 或 Sheet ID。
    return action === 'list' ? json_({ok: false, error: 'service_unavailable'})
      : page_('處理未完成', '目前無法儲存訂閱設定，請稍後再試。');
  }
}
