from __future__ import annotations

import hashlib
import html
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

# === 官方資料來源 ===
# 櫃買中心 OpenAPI：興櫃公司基本資料
TPEX_ESB_COMPANIES_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O"

# 公開資訊觀測站（舊版）即時重大訊息
MOPS_REALTIME_URL = "https://mopsov.twse.com.tw/mops/web/t05sr01_1"

# 檔案路徑
ROOT = Path(__file__).resolve().parent
SEEN_PATH = ROOT / "data" / "seen.json"
ALERTS_PATH = ROOT / "docs" / "data" / "alerts.json"
STATUS_PATH = ROOT / "docs" / "data" / "status.json"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; TW-ESB-Listing-Alert/1.0; "
        "+https://github.com/)"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

# 關鍵字規則：
# 目標是抓「興櫃股可能進入上市、上櫃、創新板流程」的重大訊息。
KEYWORD_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("創新板", re.compile(r"創新板|創新版")),
    ("上櫃", re.compile(r"申請.*上櫃|股票.*上櫃|上櫃.*申請")),
    ("上市", re.compile(r"申請.*上市|股票.*上市|上市.*申請")),
]

PROGRESS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("董事會決議", re.compile(r"董事會.*決議")),
    ("正式申請", re.compile(r"申請")),
    ("送件", re.compile(r"送件|遞件")),
    ("受理", re.compile(r"受理")),
    ("審議通過", re.compile(r"審議.*通過|審查.*通過|通過")),
    ("核准", re.compile(r"核准|同意")),
    ("撤回", re.compile(r"撤回|撤銷")),
]

# 可酌量排除的明顯非目標情境。
# 因為我們已先限制在「興櫃公司」，以下只是降低誤報。
EXCLUDE_PATTERNS = [
    re.compile(r"私募.*上市交易"),
    re.compile(r"轉換公司債.*上市"),
]

MAX_ALERTS_TO_KEEP = 300
MAX_SEEN_TO_KEEP = 4000


def now_taipei_string() -> str:
    # GitHub Actions 主機通常是 UTC，這裡以 +08:00 顯示。
    taipei_tz = timezone.utc.__class__(timezone.utc.utcoffset(None))  # placeholder, not used
    dt = datetime.now(timezone.utc).astimezone(timezone.utc)
    # 不依賴 zoneinfo，直接手動 +8 小時
    dt = dt.replace(tzinfo=timezone.utc)
    local = dt.timestamp() + 8 * 3600
    return datetime.fromtimestamp(local, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + " Asia/Taipei"


def now_taipei_iso() -> str:
    ts = datetime.now(timezone.utc).timestamp() + 8 * 3600
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def fetch_json(url: str) -> Any:
    response = requests.get(url, headers=REQUEST_HEADERS, timeout=30)
    response.raise_for_status()
    return response.json()


def fetch_html(url: str) -> str:
    response = requests.get(url, headers=REQUEST_HEADERS, timeout=30)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def find_value(row: dict[str, Any], candidate_keys: list[str]) -> str:
    for candidate in candidate_keys:
        for key, value in row.items():
            if candidate in str(key):
                return normalize_text(str(value))
    return ""


def fetch_esb_companies() -> dict[str, str]:
    payload = fetch_json(TPEX_ESB_COMPANIES_URL)
    if not isinstance(payload, list):
        raise RuntimeError("櫃買中心興櫃公司 OpenAPI 回傳格式異常。")

    companies: dict[str, str] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue

        code = find_value(
            row,
            ["公司代號", "證券代號", "股票代號", "SecuritiesCompanyCode", "Code"],
        )
        name = find_value(
            row,
            ["公司名稱", "公司簡稱", "證券名稱", "CompanyName", "Name"],
        )

        # 股票代號通常是 4 碼或 6 碼；先保留數字型代號。
        code = re.sub(r"\D", "", code)
        if code:
            companies[code] = name or code

    if not companies:
        raise RuntimeError("未能從櫃買中心 OpenAPI 解析出興櫃公司名單。")

    return companies


def parse_realtime_rows(page_html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(page_html, "html.parser")
    parsed: list[dict[str, str]] = []

    for tr in soup.find_all("tr"):
        cells = [normalize_text(td.get_text(" ", strip=True)) for td in tr.find_all("td")]
        if len(cells) < 5:
            continue

        code = re.sub(r"\D", "", cells[0])
        if not code:
            continue

        item = {
            "code": code,
            "name": cells[1],
            "date": cells[2],
            "time": cells[3],
            "title": cells[4],
            "source_url": MOPS_REALTIME_URL,
        }
        parsed.append(item)

    return parsed


def classify_title(title: str) -> tuple[str | None, str | None]:
    if any(pattern.search(title) for pattern in EXCLUDE_PATTERNS):
        return None, None

    event_type: str | None = None
    for label, pattern in KEYWORD_PATTERNS:
        if pattern.search(title):
            event_type = label
            break

    if not event_type:
        return None, None

    stage = "相關公告"
    for label, pattern in PROGRESS_PATTERNS:
        if pattern.search(title):
            stage = label
            break

    return event_type, stage


def build_unique_id(item: dict[str, str]) -> str:
    raw = "|".join(
        [
            item.get("code", ""),
            item.get("date", ""),
            item.get("time", ""),
            item.get("title", ""),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def format_telegram_message(alert: dict[str, str]) -> str:
    text = (
        "【興櫃轉板監測通知】\n\n"
        f"公司：{alert['name']}（{alert['code']}）\n"
        f"類型：{alert['event_type']}｜{alert['stage']}\n"
        f"發言時間：{alert['date']} {alert['time']}\n\n"
        f"主旨：{alert['title']}\n\n"
        f"來源：公開資訊觀測站 即時重大訊息\n"
        f"{alert['source_url']}"
    )
    return text


def send_telegram_message(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID。")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": False,
    }
    response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram 發送失敗：{body}")


def main() -> None:
    status = {
        "last_checked_at": now_taipei_iso(),
        "esb_company_count": 0,
        "rows_scanned": 0,
        "matched_count": 0,
        "new_alert_count": 0,
        "last_run_ok": False,
        "message": "",
    }

    try:
        esb_companies = fetch_esb_companies()
        status["esb_company_count"] = len(esb_companies)

        realtime_html = fetch_html(MOPS_REALTIME_URL)
        rows = parse_realtime_rows(realtime_html)
        status["rows_scanned"] = len(rows)

        seen_payload = read_json(SEEN_PATH, {"seen": []})
        seen_list = seen_payload.get("seen", [])
        if not isinstance(seen_list, list):
            seen_list = []
        seen_set = set(str(x) for x in seen_list)

        alerts = read_json(ALERTS_PATH, [])
        if not isinstance(alerts, list):
            alerts = []

        matched_count = 0
        new_alerts: list[dict[str, str]] = []

        for item in rows:
            code = item["code"]
            if code not in esb_companies:
                continue

            # 以官方興櫃名單校正公司名稱
            item["name"] = esb_companies.get(code) or item["name"] or code

            event_type, stage = classify_title(item["title"])
            if not event_type:
                continue

            matched_count += 1
            unique_id = build_unique_id(item)
            if unique_id in seen_set:
                continue

            alert = {
                "id": unique_id,
                "code": item["code"],
                "name": item["name"],
                "date": item["date"],
                "time": item["time"],
                "title": item["title"],
                "event_type": event_type,
                "stage": stage or "相關公告",
                "source_url": item["source_url"],
                "detected_at": now_taipei_iso(),
            }
            new_alerts.append(alert)
            seen_set.add(unique_id)

        # 先發送 Telegram，再持久化。
        # 若 Telegram 發送失敗，該筆不會被寫入 alerts，下一輪可重試。
        successfully_sent: list[dict[str, str]] = []
        for alert in new_alerts:
            send_telegram_message(format_telegram_message(alert))
            successfully_sent.append(alert)

        if successfully_sent:
            alerts = successfully_sent + alerts
            alerts = alerts[:MAX_ALERTS_TO_KEEP]

        status["matched_count"] = matched_count
        status["new_alert_count"] = len(successfully_sent)
        status["last_run_ok"] = True
        status["message"] = (
            f"完成。掃描 {len(rows)} 筆即時重大訊息，"
            f"興櫃轉板關鍵公告命中 {matched_count} 筆，"
            f"新增通知 {len(successfully_sent)} 筆。"
        )

        # 保留近期 seen，避免 JSON 無限制膨脹
        ordered_seen = list(seen_set)
        ordered_seen = ordered_seen[-MAX_SEEN_TO_KEEP:]

        write_json(SEEN_PATH, {"seen": ordered_seen})
        write_json(ALERTS_PATH, alerts)
        write_json(STATUS_PATH, status)
        print(status["message"])

    except Exception as exc:
        status["message"] = f"執行失敗：{exc}"
        write_json(STATUS_PATH, status)
        print(status["message"])
        raise


if __name__ == "__main__":
    main()
