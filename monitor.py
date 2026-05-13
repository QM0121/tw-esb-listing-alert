from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
SEEN_PATH = ROOT / "data" / "seen.json"
ALERTS_PATH = ROOT / "docs" / "data" / "alerts.json"
STATUS_PATH = ROOT / "docs" / "data" / "status.json"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; TW-ESB-Listing-Alert/3.0; "
        "+https://github.com/)"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

# 官方來源
TPEX_ESB_COMPANIES_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O"
MOPS_REALTIME_URL = "https://mopsov.twse.com.tw/mops/web/t05sr01_1"
TWSE_APPLY_LISTING_URL = "https://www.twse.com.tw/rwd/zh/company/applylisting?response=html"
TPEX_APPLY_OTC_URL = "https://www.tpex.org.tw/zh-tw/mainboard/applying/status/company.html"
TIB_NEWS_URL = "https://www.twse.com.tw/TIB/zh/news.html"

SCHEMA_VERSION = 3
MAX_EVENTS_TO_KEEP = 5000
MAX_SEEN_TO_KEEP = 30000

SOURCE_LABELS = {
    "mops": "MOPS 即時重大訊息",
    "twse_apply": "TWSE 申請上市公司",
    "tpex_apply": "TPEx 申請上櫃公司",
    "tib_news": "TWSE 臺灣創新板新聞稿",
}

MOPS_RULES: list[tuple[str, str, re.Pattern[str]]] = [
    ("申請上市", "董事會決議申請", re.compile(r"董事會.*決議.*申請.*上市|決議.*申請.*股票.*上市")),
    ("申請上櫃", "董事會決議申請", re.compile(r"董事會.*決議.*申請.*上櫃|決議.*申請.*股票.*上櫃")),
    ("申請創新板上市", "董事會決議申請", re.compile(r"董事會.*決議.*申請.*創新板|決議.*申請.*股票.*創新板")),
    ("申請上市", "撤回申請", re.compile(r"撤回.*上市.*申請|撤銷.*上市.*申請")),
    ("申請上櫃", "撤回申請", re.compile(r"撤回.*上櫃.*申請|撤銷.*上櫃.*申請")),
    ("申請創新板上市", "撤回申請", re.compile(r"撤回.*創新板.*申請|撤銷.*創新板.*申請")),
    ("轉板進度重大更新", "審議通過", re.compile(r"審議.*通過|審查.*通過")),
    ("轉板進度重大更新", "受理", re.compile(r"受理.*上市|受理.*上櫃|受理.*創新板")),
]

TIB_RULES: list[tuple[str, str, re.Pattern[str]]] = [
    ("申請創新板上市", "送件", re.compile(r"送件申請.*創新板.*上市")),
    ("申請創新板上市", "撤回申請", re.compile(r"撤回.*創新板.*上市申請")),
    ("申請創新板上市", "審議通過", re.compile(r"審議.*通過.*創新板.*上市案")),
    ("轉板進度重大更新", "轉板進度重大更新", re.compile(r"改列上市|開始改列上市買賣")),
]

def taipei_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))

def taipei_now_iso() -> str:
    return taipei_now().isoformat(timespec="seconds")

def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()

def sha_id(*parts: str) -> str:
    raw = "|".join(normalize_text(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default

def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

def get_text(url: str) -> str:
    response = requests.get(url, headers=HEADERS, timeout=40)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"
    return response.text

def get_json(url: str) -> Any:
    response = requests.get(url, headers=HEADERS, timeout=40)
    response.raise_for_status()
    return response.json()

def find_value(row: dict[str, Any], keywords: list[str]) -> str:
    for keyword in keywords:
        for key, value in row.items():
            if keyword in str(key):
                return normalize_text(value)
    return ""

def clean_stock_code(value: str) -> str:
    return re.sub(r"\D", "", value or "")

def fetch_esb_companies() -> dict[str, str]:
    payload = get_json(TPEX_ESB_COMPANIES_URL)
    if not isinstance(payload, list):
        raise RuntimeError("櫃買中心興櫃公司名單回傳格式異常。")

    companies: dict[str, str] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        code = clean_stock_code(find_value(
            row, ["公司代號", "證券代號", "股票代號", "CompanyCode", "Code"]
        ))
        name = find_value(
            row, ["公司名稱", "公司簡稱", "證券名稱", "CompanyName", "Name"]
        )
        if code:
            companies[code] = name or code

    if not companies:
        raise RuntimeError("無法解析興櫃公司名單。")
    return companies

def make_event(
    *,
    source: str,
    event_type: str,
    stage: str,
    event_date: str,
    company_code: str = "",
    company_name: str = "",
    title: str,
    url: str,
    detail: str = "",
) -> dict[str, str]:
    event_id = sha_id(
        source, event_type, stage, event_date, company_code, company_name, title, detail
    )
    return {
        "id": event_id,
        "source": source,
        "source_label": SOURCE_LABELS.get(source, source),
        "event_type": event_type,
        "stage": stage,
        "event_date": normalize_text(event_date),
        "company_code": normalize_text(company_code),
        "company_name": normalize_text(company_name),
        "title": normalize_text(title),
        "detail": normalize_text(detail),
        "url": url,
        "first_seen_at": taipei_now_iso(),
    }

# --------------------------
# 1) MOPS 即時重大訊息
# --------------------------
def parse_mops_realtime(html: str, esb_companies: dict[str, str]) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    events: list[dict[str, str]] = []

    for tr in soup.find_all("tr"):
        cells = [normalize_text(td.get_text(" ", strip=True)) for td in tr.find_all("td", recursive=False)]
        if len(cells) < 5:
            # 部分 MOPS 列結構不是 direct child，保留 fallback
            cells = [normalize_text(td.get_text(" ", strip=True)) for td in tr.find_all("td")]
        if len(cells) < 5:
            continue

        code = clean_stock_code(cells[0])
        if not code or code not in esb_companies:
            continue

        company_name = esb_companies.get(code) or cells[1]
        date = cells[2]
        time = cells[3]
        title = cells[4]
        full_date = normalize_text(f"{date} {time}")

        for event_type, stage, pattern in MOPS_RULES:
            if pattern.search(title):
                events.append(make_event(
                    source="mops",
                    event_type=event_type,
                    stage=stage,
                    event_date=full_date,
                    company_code=code,
                    company_name=company_name,
                    title=title,
                    url=MOPS_REALTIME_URL,
                    detail="由 MOPS 即時重大訊息關鍵字判斷",
                ))
                break

    return dedupe_events(events)

# ----------------------------------------
# 通用：找出指定官方表格
# ----------------------------------------
def find_table_by_headers(soup: BeautifulSoup, required_headers: list[str]) -> Any:
    for table in soup.find_all("table"):
        header_text = " ".join(
            normalize_text(th.get_text(" ", strip=True))
            for th in table.find_all("th")
        )
        if all(header in header_text for header in required_headers):
            return table
    return None

def direct_row_cells(tr: Any) -> list[str]:
    cells = [normalize_text(td.get_text(" ", strip=True)) for td in tr.find_all("td", recursive=False)]
    if cells:
        return cells
    return [normalize_text(td.get_text(" ", strip=True)) for td in tr.find_all("td")]

def table_rows(table: Any) -> list[list[str]]:
    if table is None:
        return []
    body_rows = table.select("tbody > tr")
    if not body_rows:
        body_rows = table.find_all("tr")
    rows: list[list[str]] = []
    for tr in body_rows:
        cells = direct_row_cells(tr)
        if cells:
            rows.append(cells)
    return rows

def ensure_reasonable_row_count(source: str, rows: list[list[str]], max_rows: int = 500) -> None:
    # 官方申請上市 / 上櫃公司表格目前遠低於此數；
    # 若超過，通常代表 HTML 結構誤抓，寧可失敗也不要寫入大量錯誤資料。
    if len(rows) > max_rows:
        raise RuntimeError(f"{source} 解析列數異常：{len(rows)}，停止寫入避免污染資料。")

# ----------------------------------------
# 2) TWSE 申請上市公司
# 欄位順序：
# 0索引 1公司代號 2公司簡稱 3申請日期 4董事長 5股本
# 6上市審議日期 7董事會通過 8契約備查/核准 9上市買賣日期 10承銷商 11承銷價 12備註
# ----------------------------------------
def parse_twse_apply(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    table = find_table_by_headers(soup, ["公司代號", "公司簡稱", "申請日期"])
    rows = table_rows(table)
    ensure_reasonable_row_count("TWSE 申請上市公司", rows)

    events: list[dict[str, str]] = []
    for cells in rows:
        if len(cells) < 10:
            continue

        code = clean_stock_code(cells[1] if len(cells) > 1 else "")
        name = cells[2] if len(cells) > 2 else ""
        app_date = cells[3] if len(cells) > 3 else ""
        review_date = cells[6] if len(cells) > 6 else ""
        board_date = cells[7] if len(cells) > 7 else ""
        contract_date = cells[8] if len(cells) > 8 else ""
        listing_date = cells[9] if len(cells) > 9 else ""
        remarks = cells[12] if len(cells) > 12 else ""
        row_text = " ".join(cells)
        is_tib = "創新板" in row_text or "創" in name
        event_type = "申請創新板上市" if is_tib else "申請上市"
        url = TWSE_APPLY_LISTING_URL

        if not code or not name or not app_date:
            continue

        events.append(make_event(
            source="twse_apply",
            event_type=event_type,
            stage="送件",
            event_date=app_date,
            company_code=code,
            company_name=name,
            title=f"{name}（{code}）列入證交所申請上市公司名單",
            url=url,
            detail="官方申請上市公司表格顯示申請日期",
        ))
        events.append(make_event(
            source="twse_apply",
            event_type=event_type,
            stage="受理",
            event_date=app_date,
            company_code=code,
            company_name=name,
            title=f"{name}（{code}）出現在證交所申請上市公司名單",
            url=url,
            detail="以新列入官方申請公司清單作為受理追蹤訊號",
        ))

        if review_date:
            events.append(make_event(
                source="twse_apply",
                event_type="轉板進度重大更新",
                stage="審議通過",
                event_date=review_date,
                company_code=code,
                company_name=name,
                title=f"{name}（{code}）上市審議委員會審議日期已更新",
                url=url,
                detail="官方申請上市公司表格已出現上市審議委員會審議日期",
            ))

        updates = [
            (board_date, "交易所董事會通過上市日期已更新"),
            (contract_date, "上市契約備查／主管機關核准日期已更新"),
            (listing_date, "股票上市買賣日期已更新"),
        ]
        for date_value, detail in updates:
            if date_value:
                events.append(make_event(
                    source="twse_apply",
                    event_type="轉板進度重大更新",
                    stage="轉板進度重大更新",
                    event_date=date_value,
                    company_code=code,
                    company_name=name,
                    title=f"{name}（{code}）：{detail}",
                    url=url,
                    detail=detail,
                ))

        if "撤件" in remarks or "撤回" in remarks or "撤銷" in remarks:
            events.append(make_event(
                source="twse_apply",
                event_type=event_type,
                stage="撤回申請",
                event_date=app_date,
                company_code=code,
                company_name=name,
                title=f"{name}（{code}）申請案備註顯示撤件／撤回",
                url=url,
                detail=remarks,
            ))

    return dedupe_events(events)

# ----------------------------------------
# 3) TPEx 申請上櫃公司
# 欄位順序：
# 0索引 1股票代號 2公司名稱 3申請日期 4董事長 5股本
# 6上櫃審議日期 7董事會通過 8同意/核准契約 9上櫃買賣日期 10承銷商 11承銷價 12備註
# ----------------------------------------
def parse_tpex_apply(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    table = find_table_by_headers(soup, ["股票代號", "公司名稱", "申請日期"])
    rows = table_rows(table)
    ensure_reasonable_row_count("TPEx 申請上櫃公司", rows)

    events: list[dict[str, str]] = []
    for cells in rows:
        if len(cells) < 10:
            continue

        code = clean_stock_code(cells[1] if len(cells) > 1 else "")
        name = cells[2] if len(cells) > 2 else ""
        app_date = cells[3] if len(cells) > 3 else ""
        review_date = cells[6] if len(cells) > 6 else ""
        board_date = cells[7] if len(cells) > 7 else ""
        contract_date = cells[8] if len(cells) > 8 else ""
        trading_date = cells[9] if len(cells) > 9 else ""
        remarks = cells[12] if len(cells) > 12 else ""
        url = TPEX_APPLY_OTC_URL

        if not code or not name or not app_date:
            continue

        events.append(make_event(
            source="tpex_apply",
            event_type="申請上櫃",
            stage="送件",
            event_date=app_date,
            company_code=code,
            company_name=name,
            title=f"{name}（{code}）列入櫃買中心申請上櫃公司名單",
            url=url,
            detail="官方申請上櫃公司表格顯示申請日期",
        ))
        events.append(make_event(
            source="tpex_apply",
            event_type="申請上櫃",
            stage="受理",
            event_date=app_date,
            company_code=code,
            company_name=name,
            title=f"{name}（{code}）出現在櫃買中心申請上櫃公司名單",
            url=url,
            detail="以新列入官方申請公司清單作為受理追蹤訊號",
        ))

        if review_date:
            events.append(make_event(
                source="tpex_apply",
                event_type="轉板進度重大更新",
                stage="審議通過",
                event_date=review_date,
                company_code=code,
                company_name=name,
                title=f"{name}（{code}）上櫃審議委員會審議日期已更新",
                url=url,
                detail="官方申請上櫃公司表格已出現上櫃審議委員會審議日期",
            ))

        updates = [
            (board_date, "櫃買董事會通過上櫃日期已更新"),
            (contract_date, "上櫃契約同意／核准日期已更新"),
            (trading_date, "股票上櫃買賣日期已更新"),
        ]
        for date_value, detail in updates:
            if date_value:
                events.append(make_event(
                    source="tpex_apply",
                    event_type="轉板進度重大更新",
                    stage="轉板進度重大更新",
                    event_date=date_value,
                    company_code=code,
                    company_name=name,
                    title=f"{name}（{code}）：{detail}",
                    url=url,
                    detail=detail,
                ))

        if "撤件" in remarks or "撤回" in remarks or "撤銷" in remarks:
            events.append(make_event(
                source="tpex_apply",
                event_type="申請上櫃",
                stage="撤回申請",
                event_date=app_date,
                company_code=code,
                company_name=name,
                title=f"{name}（{code}）申請案備註顯示撤件／撤回",
                url=url,
                detail=remarks,
            ))

    return dedupe_events(events)

# ----------------------------------------
# 4) 臺灣創新板新聞稿
# ----------------------------------------
def extract_news_date(text: str) -> str:
    match = re.match(r"(\d{3}年\d{1,2}月\d{1,2}日)", text)
    return match.group(1) if match else ""

def extract_company_code(text: str) -> str:
    match = re.search(r"(?:代號|證券代號)[:：]?\s*(\d{4,6})", text)
    return match.group(1) if match else ""

def extract_company_name_from_news(text: str) -> str:
    cleaned = re.sub(r"^\d{3}年\d{1,2}月\d{1,2}日\s*", "", text)
    for marker in ("於", "送件", "股票", "接獲", "訂於", "審議", "撤回"):
        if marker in cleaned:
            candidate = cleaned.split(marker)[0]
            if candidate:
                return normalize_text(candidate)
    return ""

def parse_tib_news(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    events: list[dict[str, str]] = []
    seen_text: set[str] = set()

    for link in soup.find_all("a"):
        title = normalize_text(link.get_text(" ", strip=True))
        if not title or title in seen_text:
            continue
        seen_text.add(title)

        date = extract_news_date(title)
        code = extract_company_code(title)
        name = extract_company_name_from_news(title)
        href = link.get("href") or ""
        if href.startswith("http"):
            url = href
        elif href.startswith("/"):
            url = "https://www.twse.com.tw" + href
        else:
            url = TIB_NEWS_URL

        for event_type, stage, pattern in TIB_RULES:
            if pattern.search(title):
                events.append(make_event(
                    source="tib_news",
                    event_type=event_type,
                    stage=stage,
                    event_date=date,
                    company_code=code,
                    company_name=name,
                    title=title,
                    url=url,
                    detail="臺灣創新板官方新聞稿",
                ))
                break

    return dedupe_events(events)

def dedupe_events(events: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for event in events:
        event_id = event.get("id", "")
        if event_id and event_id not in seen:
            seen.add(event_id)
            out.append(event)
    return out

def parse_roc_or_iso_date(value: str) -> datetime:
    text = normalize_text(value)
    # 115/04/29
    m = re.search(r"(\d{2,3})[/-](\d{1,2})[/-](\d{1,2})", text)
    if m:
        year = int(m.group(1)) + 1911
        month = int(m.group(2))
        day = int(m.group(3))
        try:
            return datetime(year, month, day, tzinfo=timezone(timedelta(hours=8)))
        except ValueError:
            pass
    # 2026-05-14
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone(timedelta(hours=8)))
        except ValueError:
            pass
    return datetime(1970, 1, 1, tzinfo=timezone(timedelta(hours=8)))

def event_sort_key(event: dict[str, str]) -> tuple[datetime, str, str]:
    return (
        parse_roc_or_iso_date(event.get("event_date", "")),
        event.get("company_code", ""),
        event.get("stage", ""),
    )

def format_telegram(event: dict[str, str]) -> str:
    company = event.get("company_name") or "未解析公司名稱"
    code = event.get("company_code")
    company_line = f"{company}（{code}）" if code else company
    return (
        "【興櫃轉板網站監測通知】\n\n"
        f"事件：{event.get('event_type', '')}｜{event.get('stage', '')}\n"
        f"公司：{company_line}\n"
        f"日期：{event.get('event_date', '')}\n"
        f"來源：{event.get('source_label', '')}\n\n"
        f"{event.get('title', '')}\n\n"
        f"{event.get('url', '')}"
    )

def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID。")
    endpoint = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    response = requests.post(
        endpoint,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": False},
        timeout=40,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API 回傳失敗：{payload}")

def summarize(events: list[dict[str, str]]) -> dict[str, Any]:
    stage_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    for event in events:
        stage = event.get("stage", "未分類")
        event_type = event.get("event_type", "未分類")
        source = event.get("source_label", event.get("source", "未分類"))
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
        type_counts[event_type] = type_counts.get(event_type, 0) + 1
        source_counts[source] = source_counts.get(source, 0) + 1
    return {
        "stage_counts": stage_counts,
        "type_counts": type_counts,
        "source_counts": source_counts,
    }

def main() -> None:
    checked_at = taipei_now_iso()
    status: dict[str, Any] = {
        "last_checked_at": checked_at,
        "last_run_ok": False,
        "schema_version": SCHEMA_VERSION,
        "message": "",
        "sources": {},
        "new_event_count": 0,
        "telegram_sent_count": 0,
        "total_event_count": 0,
    }

    try:
        seen_payload = read_json(SEEN_PATH, {"seen": []})
        if not isinstance(seen_payload, dict):
            seen_payload = {"seen": []}

        seen_ids = set(str(item) for item in seen_payload.get("seen", []))
        initialized_sources = set(seen_payload.get("initialized_sources", []))
        prior_schema_version = int(seen_payload.get("schema_version", 0) or 0)
        is_v3_migration = prior_schema_version < SCHEMA_VERSION

        existing_events = read_json(ALERTS_PATH, [])
        if not isinstance(existing_events, list):
            existing_events = []

        # MOPS 是即時重大訊息，保留歷史累積；
        # TWSE / TPEx / TIB 官方頁面每次重建，避免舊版錯誤資料殘留。
        preserved_mops_events = [
            event for event in existing_events
            if isinstance(event, dict) and event.get("source") == "mops"
        ]

        esb_companies = fetch_esb_companies()

        source_events: dict[str, list[dict[str, str]]] = {
            "mops": parse_mops_realtime(get_text(MOPS_REALTIME_URL), esb_companies),
            "twse_apply": parse_twse_apply(get_text(TWSE_APPLY_LISTING_URL)),
            "tpex_apply": parse_tpex_apply(get_text(TPEX_APPLY_OTC_URL)),
            "tib_news": parse_tib_news(get_text(TIB_NEWS_URL)),
        }

        for source, events in source_events.items():
            status["sources"][source] = {
                "label": SOURCE_LABELS.get(source, source),
                "fetched": len(events),
            }

        # 網站資料：官方清單類來源每次重建，MOPS 保留累積紀錄
        website_events = (
            source_events["twse_apply"]
            + source_events["tpex_apply"]
            + source_events["tib_news"]
            + preserved_mops_events
            + source_events["mops"]
        )
        website_events = dedupe_events(website_events)
        website_events.sort(key=event_sort_key, reverse=True)
        website_events = website_events[:MAX_EVENTS_TO_KEEP]

        new_events: list[dict[str, str]] = []
        sendable_events: list[dict[str, str]] = []

        for source, events in source_events.items():
            source_new = [event for event in events if event["id"] not in seen_ids]
            new_events.extend(source_new)
            seen_ids.update(event["id"] for event in source_new)

            # v3 首次修復執行時，官方清單來源先做靜默基線，
            # 避免舊資料重新整理後一次洗 Telegram。
            if is_v3_migration and source in {"twse_apply", "tpex_apply", "tib_news"}:
                initialized_sources.add(source)
                continue

            if source in initialized_sources:
                sendable_events.extend(source_new)
            else:
                initialized_sources.add(source)

        telegram_sent_count = 0
        for event in sendable_events:
            send_telegram(format_telegram(event))
            telegram_sent_count += 1

        status["new_event_count"] = len(new_events)
        status["telegram_sent_count"] = telegram_sent_count
        status["total_event_count"] = len(website_events)
        status["summary"] = summarize(website_events)
        status["last_run_ok"] = True
        status["message"] = (
            f"完成：網站重建 {len(website_events)} 筆事件，"
            f"本輪新增事件 {len(new_events)} 筆，Telegram 推播 {telegram_sent_count} 筆。"
        )

        write_json(SEEN_PATH, {
            "seen": list(seen_ids)[-MAX_SEEN_TO_KEEP:],
            "initialized_sources": sorted(initialized_sources),
            "schema_version": SCHEMA_VERSION,
        })
        write_json(ALERTS_PATH, website_events)
        write_json(STATUS_PATH, status)
        print(status["message"])

    except Exception as exc:
        status["message"] = f"執行失敗：{exc}"
        write_json(STATUS_PATH, status)
        print(status["message"])
        raise

if __name__ == "__main__":
    main()
