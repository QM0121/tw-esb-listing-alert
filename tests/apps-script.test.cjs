const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');

const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'apps-script/Code.gs'), 'utf8');
const secret = 'test-unsubscribe-secret-' + 'x'.repeat(32);
const apiKey = 'test-api-key-' + 'y'.repeat(32);

class FakeSheet {
  constructor(rows) { this.rows = rows.map(row => row.slice()); }
  getLastRow() { return this.rows.length; }
  getLastColumn() { return Math.max(0, ...this.rows.map(row => row.length)); }
  appendRow(row) { this.rows.push(Array.from(row)); }
  getDataRange() { return this.getRange(1, 1, Math.max(1, this.getLastRow()), Math.max(1, this.getLastColumn())); }
  getRange(row, column, height = 1, width = 1) {
    const sheet = this;
    return {
      getValues() {
        return Array.from({length: height}, (_, y) => Array.from({length: width}, (_, x) => sheet.rows[row - 1 + y]?.[column - 1 + x] ?? ''));
      },
      setValues(values) {
        values.forEach((line, y) => line.forEach((value, x) => {
          sheet.rows[row - 1 + y] ??= [];
          sheet.rows[row - 1 + y][column - 1 + x] = value;
        }));
        return this;
      },
      setValue(value) { return this.setValues([[value]]); }
    };
  }
}

function environment(rows = [['timestamp', 'Email', 'note'], ['old-date', 'Alice@Example.com', 'keep me']]) {
  const sheet = new FakeSheet(rows);
  let locks = 0;
  let releases = 0;
  const properties = {SPREADSHEET_ID: 'private-sheet-id', SHEET_NAME: 'Subscribers', SUBSCRIBERS_API_KEY: apiKey, UNSUBSCRIBE_SECRET: secret};
  const output = value => ({value, setTitle() { return this; }, setMimeType() { return this; }});
  const ctx = vm.createContext({
    PropertiesService: {getScriptProperties: () => ({getProperty: name => properties[name]})},
    SpreadsheetApp: {openById: () => ({getSheetByName: () => sheet}), flush() {}},
    LockService: {getScriptLock: () => ({waitLock() { locks++; }, releaseLock() { releases++; }})},
    Utilities: {
      Charset: {UTF_8: 'UTF-8'},
      computeHmacSha256Signature: (value, key) => Array.from(crypto.createHmac('sha256', key).update(value, 'utf8').digest(), b => b > 127 ? b - 256 : b),
      base64DecodeWebSafe: value => Buffer.from(value, 'base64url'),
      newBlob: bytes => ({getDataAsString: () => Buffer.from(bytes).toString('utf8')})
    },
    HtmlService: {createHtmlOutput: output},
    ContentService: {createTextOutput: output, MimeType: {JSON: 'application/json'}},
    ScriptApp: {getService: () => ({getUrl: () => 'https://script.google.com/macros/s/test-deployment/exec'})}
  });
  vm.runInContext(source, ctx);
  function post(data) { return ctx.doPost({postData: {type: 'application/json', contents: JSON.stringify(data)}}).value; }
  return {ctx, sheet, properties, post, counts: () => ({locks, releases}), list: () => JSON.parse(post({action: 'list', api_key: apiKey}))};
}

function token(email) {
  const encoded = Buffer.from(email).toString('base64url');
  return `v1.${encoded}.${crypto.createHmac('sha256', secret).update(`unsubscribe:v1:${encoded}`).digest('hex')}`;
}

test('legacy sheet migration preserves rows, column order and additional fields', () => {
  const env = environment();
  env.ctx.setup();
  env.ctx.setup();
  assert.deepEqual(env.sheet.rows[0], ['timestamp', 'Email', 'note', 'status', 'created_at', 'updated_at', 'unsubscribed_at', 'source']);
  assert.deepEqual(env.sheet.rows[1].slice(0, 4), ['old-date', 'Alice@Example.com', 'keep me', 'active']);
  assert.deepEqual(env.list().emails, ['alice@example.com']);
  assert.equal(env.counts().locks, env.counts().releases);
});

test('empty sheet initializes, invalid and duplicate email headers do not mutate data', () => {
  const env = environment([]);
  env.ctx.setup();
  assert.equal(env.sheet.rows[0][0], 'email');
  assert.deepEqual(env.list().emails, []);
  for (const headers of [['name'], ['email', 'Email']]) {
    const invalid = environment([headers]);
    const before = JSON.stringify(invalid.sheet.rows);
    assert.throws(() => invalid.ctx.setup());
    assert.equal(JSON.stringify(invalid.sheet.rows), before);
    assert.equal(invalid.counts().locks, invalid.counts().releases);
  }
});

test('no public GET or unauthenticated POST reveals subscriber list', () => {
  const env = environment();
  env.ctx.setup();
  const before = JSON.stringify(env.sheet.rows);
  const html = env.ctx.doGet({parameter: {action: 'list', api_key: apiKey}}).value;
  assert.ok(!html.includes('alice@example.com'));
  assert.ok(!html.includes('Alice@Example.com'));
  const response = JSON.parse(env.post({action: 'list', api_key: 'bad'}));
  assert.equal(response.ok, false);
  assert.equal(response.error, 'unauthorized');
  assert.ok(!('emails' in response));
  assert.equal(JSON.stringify(env.sheet.rows), before);
});

test('unsubscribe GET is safe for email scanners; only confirmed POST changes status', () => {
  const env = environment();
  env.ctx.setup();
  const value = token('alice@example.com');
  const before = JSON.stringify(env.sheet.rows);
  const html = env.ctx.doGet({parameter: {action: 'unsubscribe', token: value}}).value;
  assert.ok(html.includes('確認取消訂閱'));
  assert.ok(html.includes('method="post"'));
  assert.equal(JSON.stringify(env.sheet.rows), before);
  assert.ok(env.post({action: 'unsubscribe', token: value}).includes('已取消訂閱'));
  assert.deepEqual(env.list().emails, []);
  assert.ok(env.post({action: 'unsubscribe', token: value}).includes('已取消訂閱'));
  assert.equal(env.sheet.rows.length, 2);
  assert.equal(env.sheet.rows[1][2], 'keep me');
});

test('invalid, forged and missing tokens cannot unsubscribe any email', () => {
  const env = environment();
  env.ctx.setup();
  const before = JSON.stringify(env.sheet.rows);
  const valid = token('alice@example.com');
  for (const value of ['', 'invalid', valid.slice(0, -1) + (valid.endsWith('a') ? 'b' : 'a'), token('bob@example.com').replace('Ym9i', 'YWxpY2U')]) {
    assert.ok(env.post({action: 'unsubscribe', email: 'alice@example.com', token: value}).includes('連結無效'));
  }
  assert.equal(JSON.stringify(env.sheet.rows), before);
});

test('public subscribe is deduplicated and cannot reactivate unsubscribed users', () => {
  const env = environment();
  env.ctx.setup();
  assert.ok(env.post({action: 'subscribe', email: ' Alice@example.com '}).includes('訂閱成功'));
  assert.equal(env.sheet.rows.length, 2);
  env.post({action: 'unsubscribe', token: token('alice@example.com')});
  assert.ok(env.post({action: 'subscribe', email: 'alice@example.com'}).includes('此信箱已取消訂閱'));
  assert.deepEqual(env.list().emails, []);
  assert.ok(env.post({action: 'subscribe', token: token('alice@example.com')}).includes('訂閱成功'));
  assert.deepEqual(env.list().emails, ['alice@example.com']);
  assert.ok(env.post({email: 'new@example.com'}).includes('訂閱成功'));
  assert.deepEqual(env.list().emails, ['alice@example.com', 'new@example.com']);
});

test('native HTML form body is supported and malicious email input is rejected', () => {
  const env = environment();
  env.ctx.setup();
  assert.ok(env.ctx.doPost({parameter: {action: 'subscribe', email: 'native@example.com'}}).value.includes('訂閱成功'));
  const before = JSON.stringify(env.sheet.rows);
  for (const email of ['=IMPORTXML("https://evil.example")', 'a@example.com\nBcc: x@example.com', '<script>@evil.com']) {
    assert.ok(env.post({email}).includes('信箱格式錯誤'));
  }
  assert.equal(JSON.stringify(env.sheet.rows), before);
});

test('suppression wins over duplicate active rows; confirming updates all matching rows', () => {
  const env = environment([['email', 'status'], ['alice@example.com', 'active'], ['ALICE@example.com', 'unsubscribed'], ['bob@example.com', 'active']]);
  env.ctx.setup();
  assert.deepEqual(env.list().emails, ['bob@example.com']);
  env.post({action: 'subscribe', token: token('alice@example.com')});
  assert.deepEqual(env.list().emails, ['alice@example.com', 'bob@example.com']);
  env.post({action: 'unsubscribe', token: token('alice@example.com')});
  assert.equal(env.sheet.rows[1][1], 'unsubscribed');
  assert.equal(env.sheet.rows[2][1], 'unsubscribed');
  assert.deepEqual(env.list().emails, ['bob@example.com']);
});

test('backend failure never reports success or exposes private configuration', () => {
  const env = environment();
  env.ctx.setup();
  env.sheet.appendRow = () => { throw new Error('private-sheet-id alice@example.com ' + secret); };
  const html = env.post({email: 'new@example.com'});
  assert.ok(html.includes('處理未完成'));
  assert.ok(!html.includes('訂閱成功'));
  assert.ok(!html.includes(secret));
  assert.ok(!html.includes('private-sheet-id'));
  assert.equal(env.counts().locks, env.counts().releases);
});

test('Python-generated link verifies in Apps Script and works through complete unsubscribe flow', () => {
  const url = execFileSync(process.env.PYTHON || 'python', ['-c',
    'import monitor; print(monitor.make_unsubscribe_url("Alice@Example.com"))'], {
      cwd: root, encoding: 'utf8', env: {...process.env, EMAIL_SUBSCRIBERS_URL: 'https://script.google.com/macros/s/test-deployment/exec', EMAIL_UNSUBSCRIBE_SECRET: secret}
    }).trim();
  const value = new URL(url).searchParams.get('token');
  const env = environment();
  env.ctx.setup();
  assert.equal(env.ctx.verifyToken_(value, secret), 'alice@example.com');
  assert.equal(env.ctx.verifyToken_(value, 'another-secret'), null);
  assert.ok(env.ctx.doGet({parameter: {action: 'unsubscribe', token: value}}).value.includes('確認取消訂閱'));
  env.post({action: 'unsubscribe', token: value});
  const data = env.list();
  assert.deepEqual(data.emails, []);
  assert.equal(data.signing_key_check, crypto.createHmac('sha256', secret).update('unsubscribe-config:v1').digest('hex'));
  // 將 Apps Script 回傳值交回 Python 解析，檢查兩邊協定一致。
  const result = execFileSync(process.env.PYTHON || 'python', ['-c',
    'import sys,monitor; print(monitor.parse_subscriber_emails_from_payload(sys.stdin.read()))'], {
      cwd: root, encoding: 'utf8', input: JSON.stringify(data), env: {...process.env, EMAIL_UNSUBSCRIBE_SECRET: secret}
    }).trim();
  assert.equal(result, '[]');
});
