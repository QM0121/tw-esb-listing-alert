from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import smtplib
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
SEEN_PATH = ROOT / "data" / "seen.json"
ALERTS_PATH = ROOT / "docs" / "data" / "alerts.json"
STATUS_PATH = ROOT / "docs" / "data" / "status.json"

EMAIL_SMTP_HOST = os.getenv("EMAIL_SMTP_HOST", "smtp.gmail.com").strip() or "smtp.gmail.com"
EMAIL_SMTP_PORT = int(os.getenv("EMAIL_SMTP_PORT", "465").strip() or "465")
EMAIL_SENDER = os.getenv("EMAIL_SENDER", "").strip()
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD", "").replace(" ", "").strip()
EMAIL_RECIPIENTS_RAW = os.getenv("EMAIL_RECIPIENTS", "").strip()
EMAIL_SUBSCRIBERS_URL = os.getenv("EMAIL_SUBSCRIBERS_URL", "").strip()
MAIL_TEST_MODE = os.getenv("MAIL_TEST_MODE", "").strip().lower() in {"1", "true", "yes", "y"}
MAIL_TEST_RECIPIENTS_RAW = os.getenv("MAIL_TEST_RECIPIENTS", "").strip()

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

# 公開版合規強化：
# - 申請上市公司：改用政府資料開放平臺所連結之 TWSE CSV
# - 證交所新聞：改用政府資料開放平臺所連結之 TWSE CSV，再以創新板關鍵字過濾
# - MOPS 即時重大訊息：公開版暫停自動抓取，保留來源列並顯示「停用」
TWSE_APPLY_LISTING_CSV_URL = "https://www.twse.com.tw/company/applylistingCsvAndHtml?selectType=Local&type=open_data"
TWSE_APPLY_LISTING_PUBLIC_URL = "https://www.twse.com.tw/zh/listed/listed/apply-listing.html"
TWSE_NEWS_CSV_URL = "https://www.twse.com.tw/news/newsList?response=open_data"
TIB_NEWS_PUBLIC_URL = "https://www.twse.com.tw/TIB/zh/news.html"

# TPEx 申請上櫃資料本輪先維持原本 CSV 路徑；
# 待確認可完全等價替代的官方 API 後，再進一步改寫。
TPEX_APPLY_OTC_URL = "https://www.tpex.org.tw/zh-tw/mainboard/applying/status/company.html"
TPEX_APPLY_OTC_CSV_URL_TEMPLATE = "https://www.tpex.org.tw/web/regular_emerging/apply_schedule/applicant/applicant_companies_download_UTF-8.php?l=zh-tw&y={year}"

SCHEMA_VERSION = 15
MAX_EVENTS_TO_KEEP = 5000
MAX_SEEN_TO_KEEP = 30000

# 網站與通知僅保留「近三年」資料，並於每次執行時自動汰除超出期間的事件。
DATA_RETENTION_YEARS = 3


def subtract_years_safe(dt: datetime, years: int) -> datetime:
    """將日期往前推指定年數；遇到 2/29 時以 2/28 處理。"""
    try:
        return dt.replace(year=dt.year - years)
    except ValueError:
        return dt.replace(year=dt.year - years, month=2, day=28)


def current_data_window() -> tuple[datetime, datetime]:
    end_dt = taipei_now()
    start_dt = subtract_years_safe(end_dt, DATA_RETENTION_YEARS)
    return start_dt, end_dt


def roc_year(dt: datetime) -> int:
    return dt.year - 1911


def tpex_candidate_roc_years(window_start: datetime, window_end: datetime) -> list[int]:
    # 近三年是滾動區間，可能跨 4 個民國年度，例如 2023/05～2026/05 = 112、113、114、115。
    start_year = roc_year(window_start)
    end_year = roc_year(window_end)
    return list(range(end_year, start_year - 1, -1))


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


def get_text(url: str, retries: int = 3) -> str:
    """
    抓取文字型來源，遇到官方網站暫時 5xx 或 timeout 時自動重試。
    """
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, headers=HEADERS, timeout=40)
            response.raise_for_status()
            response.encoding = response.apparent_encoding or "utf-8"
            return response.text
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(min(2 * attempt, 6))
                continue
            break
    raise RuntimeError(f"抓取來源失敗：{url}；{last_exc}")


def get_json(url: str, retries: int = 3) -> Any:
    """
    抓取 JSON 型來源，遇到官方網站暫時 5xx、timeout 或 JSON 解析異常時自動重試。
    """
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, headers=HEADERS, timeout=40)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(min(2 * attempt, 6))
                continue
            break
    raise RuntimeError(f"抓取 JSON 來源失敗：{url}；{last_exc}")


def normalize_api_rows(payload: Any) -> list[dict[str, Any]]:
    """
    官方 OpenAPI 回傳格式可能是：
    - 直接 list[dict]
    - {"data": list[dict]}
    - {"Data": list[dict]}
    這裡統一轉成 list[dict]，讓後續 parser 更耐格式差異。
    """
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]

    if isinstance(payload, dict):
        for key in ("data", "Data", "result", "Result"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]

    return []


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
        time_value = cells[3]
        title = cells[4]
        full_date = normalize_text(f"{date} {time_value}")

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


def ensure_reasonable_row_count(source: str, rows: list[list[str]], max_rows: int = 5000) -> None:
    # 官方申請上市 / 上櫃公司表格目前遠低於此數；
    # 若超過，通常代表 HTML 結構誤抓，寧可失敗也不要寫入大量錯誤資料。
    if len(rows) > max_rows:
        raise RuntimeError(f"{source} 解析列數異常：{len(rows)}，停止寫入避免污染資料。")


# ----------------------------------------
# 2) TWSE 申請上市公司（官方 OpenAPI）
# OpenAPI：/opendata/t187ap04_L
# ----------------------------------------
def parse_twse_apply_csv(csv_text: str) -> list[dict[str, str]]:
    """
    解析政府資料開放平臺所連結之 TWSE「申請上市之本國公司」CSV。
    來源：
    https://www.twse.com.tw/company/applylistingCsvAndHtml?selectType=Local&type=open_data

    欄位順序：
    0索引 1公司代號 2公司簡稱 3申請日期 4董事長 5申請時股本
    6上市審議委員會審議日期 7交易所董事會通過上市日期
    8上市契約報請主管機關備查/核准日期 9股票上市買賣日期
    10承銷商 11承銷價 12備註
    """
    if not csv_text or not csv_text.strip():
        return []

    rows = list(csv.reader(csv_text.splitlines()))
    if not rows:
        return []

    header_index = -1
    for idx, row in enumerate(rows[:20]):
        joined = " ".join(normalize_text(cell) for cell in row)
        if "公司代號" in joined and "公司簡稱" in joined and "申請日期" in joined:
            header_index = idx
            break

    if header_index < 0:
        return []

    data_rows = rows[header_index + 1:]
    ensure_reasonable_row_count("TWSE 申請上市公司 CSV", data_rows, max_rows=5000)

    events: list[dict[str, str]] = []
    for cells in data_rows:
        cells = [normalize_text(cell) for cell in cells]
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
            url=TWSE_APPLY_LISTING_PUBLIC_URL,
            detail="政府開放資料 CSV 顯示申請日期",
        ))
        events.append(make_event(
            source="twse_apply",
            event_type=event_type,
            stage="受理",
            event_date=app_date,
            company_code=code,
            company_name=name,
            title=f"{name}（{code}）出現在證交所申請上市公司名單",
            url=TWSE_APPLY_LISTING_PUBLIC_URL,
            detail="以新列入官方開放資料清單作為受理追蹤訊號",
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
                url=TWSE_APPLY_LISTING_PUBLIC_URL,
                detail="政府開放資料 CSV 已出現上市審議委員會審議日期",
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
                    url=TWSE_APPLY_LISTING_PUBLIC_URL,
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
                url=TWSE_APPLY_LISTING_PUBLIC_URL,
                detail=remarks,
            ))

    return dedupe_events(events)

# ----------------------------------------
# 3) TPEx 申請上櫃公司
# 欄位順序：
# 0索引 1股票代號 2公司名稱 3申請日期 4董事長 5股本
# 6上櫃審議日期 7董事會通過 8同意/核准契約 9上櫃買賣日期 10承銷商 11承銷價 12備註
# ----------------------------------------
def parse_tpex_apply(csv_text: str) -> list[dict[str, str]]:
    """
    解析櫃買中心申請上櫃 CSV。
    為避免官方輸出格式調整，支援：
    - 逗號分隔
    - Tab 分隔
    - 分號分隔
    並會尋找包含「申請日期、股票代號、公司名稱」的表頭列。
    """
    if not csv_text or not csv_text.strip():
        return []

    candidate_rows_sets: list[list[list[str]]] = []
    for delimiter in [",", "\t", ";"]:
        rows = list(csv.reader(csv_text.splitlines(), delimiter=delimiter))
        candidate_rows_sets.append(rows)

    best_rows: list[list[str]] = []
    best_header_index = -1

    for rows in candidate_rows_sets:
        for idx, row in enumerate(rows[:20]):
            joined = " ".join(normalize_text(cell) for cell in row)
            if "申請日期" in joined and "股票代號" in joined and "公司名稱" in joined:
                if len(rows) > len(best_rows):
                    best_rows = rows
                    best_header_index = idx
                break

    if not best_rows:
        return []

    data_rows = best_rows[best_header_index + 1:]
    ensure_reasonable_row_count("TPEx 申請上櫃公司 CSV", data_rows, max_rows=5000)

    events: list[dict[str, str]] = []
    for cells in data_rows:
        cells = [normalize_text(cell) for cell in cells]
        if len(cells) < 9:
            continue

        # 官方 CSV 正常欄位順序：
        # 0申請日期 1股票代號 2公司名稱 3董事長 4申請時股本
        # 5上櫃審議日期 6櫃買董事會通過 7契約同意/核准 8上櫃買賣日期
        app_date = normalize_text(cells[0])
        code = clean_stock_code(cells[1])
        name = normalize_text(cells[2])
        review_date = normalize_text(cells[5]) if len(cells) > 5 else ""
        board_date = normalize_text(cells[6]) if len(cells) > 6 else ""
        contract_date = normalize_text(cells[7]) if len(cells) > 7 else ""
        trading_date = normalize_text(cells[8]) if len(cells) > 8 else ""
        remarks = normalize_text(cells[11]) if len(cells) > 11 else ""
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
            detail="官方申請上櫃公司 CSV 顯示申請日期",
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
                detail="官方申請上櫃公司 CSV 已出現上櫃審議委員會審議日期",
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


def parse_tpex_apply_html(html: str) -> list[dict[str, str]]:
    """
    TPEx CSV 若暫時無法正確解析，改以官方 HTML 頁面做 fallback。
    """
    soup = BeautifulSoup(html, "html.parser")
    table = find_table_by_headers(soup, ["股票代號", "公司名稱", "申請日期"])
    rows = table_rows(table)
    ensure_reasonable_row_count("TPEx 申請上櫃公司 HTML", rows, max_rows=5000)

    events: list[dict[str, str]] = []
    for cells in rows:
        if len(cells) < 10:
            continue

        code = clean_stock_code(cells[1] if len(cells) > 1 else "")
        name = normalize_text(cells[2] if len(cells) > 2 else "")
        app_date = normalize_text(cells[3] if len(cells) > 3 else "")
        review_date = normalize_text(cells[6] if len(cells) > 6 else "")
        board_date = normalize_text(cells[7] if len(cells) > 7 else "")
        contract_date = normalize_text(cells[8] if len(cells) > 8 else "")
        trading_date = normalize_text(cells[9] if len(cells) > 9 else "")
        remarks = normalize_text(cells[12] if len(cells) > 12 else "")
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
            detail="官方申請上櫃公司 HTML 表格顯示申請日期",
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
                detail="官方申請上櫃公司 HTML 表格已出現上櫃審議委員會審議日期",
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


def parse_tib_news_api(payload: Any) -> list[dict[str, str]]:
    rows = normalize_api_rows(payload)
    if len(rows) > 10000:
        raise RuntimeError(f"TWSE 新聞 OpenAPI 回傳列數異常：{len(rows)}，停止寫入避免污染資料。")

    events: list[dict[str, str]] = []
    seen_text: set[str] = set()

    for row in rows:
        title = find_value(row, ["title", "Title", "標題", "新聞標題", "主旨"])
        if not title or title in seen_text:
            continue
        seen_text.add(title)

        date = find_value(row, ["date", "Date", "日期", "發佈日期", "發布日期", "發布時間", "publishDate"])
        if not date:
            date = extract_news_date(title)

        code = extract_company_code(title)
        name = extract_company_name_from_news(title)
        url = find_value(row, ["link", "Link", "url", "Url", "網址", "連結"]) or TIB_NEWS_PUBLIC_URL

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


def parse_tib_news_csv(csv_text: str) -> list[dict[str, str]]:
    """
    解析政府資料開放平臺所連結之「證交所新聞」CSV。
    主要欄位預期為：標題、連結、日期。
    """
    if not csv_text or not csv_text.strip():
        return []

    rows = list(csv.reader(csv_text.splitlines()))
    if not rows:
        return []

    header_index = -1
    header: list[str] = []
    for idx, row in enumerate(rows[:20]):
        normalized = [normalize_text(cell) for cell in row]
        joined = " ".join(normalized)
        if "標題" in joined and "連結" in joined and "日期" in joined:
            header_index = idx
            header = normalized
            break

    if header_index == -1:
        return []

    def col_index(candidates: list[str]) -> int:
        for candidate in candidates:
            for idx, name in enumerate(header):
                if candidate in name:
                    return idx
        return -1

    title_idx = col_index(["標題", "新聞標題", "主旨"])
    link_idx = col_index(["連結", "網址", "URL", "url"])
    date_idx = col_index(["日期", "發布日期", "發佈日期", "時間"])

    if title_idx == -1:
        return []

    data_rows = rows[header_index + 1:]
    if len(data_rows) > 20000:
        raise RuntimeError(f"TWSE 證交所新聞 CSV 解析列數異常：{len(data_rows)}，停止寫入避免污染資料。")

    events: list[dict[str, str]] = []
    seen_text: set[str] = set()

    for raw_cells in data_rows:
        cells = [normalize_text(cell) for cell in raw_cells]
        if title_idx >= len(cells):
            continue

        title = cells[title_idx]
        if not title or title in seen_text:
            continue
        seen_text.add(title)

        date = cells[date_idx] if date_idx != -1 and date_idx < len(cells) else ""
        if not date:
            date = extract_news_date(title)

        raw_link = cells[link_idx] if link_idx != -1 and link_idx < len(cells) else ""
        if raw_link.startswith("http"):
            url = raw_link
        elif raw_link.startswith("/"):
            url = "https://www.twse.com.tw" + raw_link
        else:
            url = TIB_NEWS_PUBLIC_URL

        code = extract_company_code(title)
        name = extract_company_name_from_news(title)

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
                    detail="證交所新聞開放資料 CSV",
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
    tz = timezone(timedelta(hours=8))

    # 1) 西元緊湊格式：20260512
    #    TPEx 申請上櫃 CSV 實際常見格式即為 YYYYMMDD。
    m = re.search(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)", text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=tz)
        except ValueError:
            pass

    # 2) 民國緊湊格式：1150512
    m = re.search(r"(?<!\d)(\d{3})(\d{2})(\d{2})(?!\d)", text)
    if m:
        try:
            return datetime(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)), tzinfo=tz)
        except ValueError:
            pass

    # 3) 民國年：115/05/12、115-05-12、115.05.12
    m = re.search(r"(?<!\d)(\d{2,3})[./-](\d{1,2})[./-](\d{1,2})(?!\d)", text)
    if m:
        roc_year_value = int(m.group(1))
        if 1 <= roc_year_value <= 999:
            try:
                return datetime(roc_year_value + 1911, int(m.group(2)), int(m.group(3)), tzinfo=tz)
            except ValueError:
                pass

    # 4) 西元年：2026/05/12、2026-05-12、2026.05.12
    m = re.search(r"(?<!\d)(\d{4})[./-](\d{1,2})[./-](\d{1,2})(?!\d)", text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=tz)
        except ValueError:
            pass

    # 5) 民國中文日期：115年05月12日
    m = re.search(r"(?<!\d)(\d{2,3})年(\d{1,2})月(\d{1,2})日?(?!\d)", text)
    if m:
        try:
            return datetime(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)), tzinfo=tz)
        except ValueError:
            pass

    # 6) 西元中文日期：2026年05月12日
    m = re.search(r"(?<!\d)(\d{4})年(\d{1,2})月(\d{1,2})日?(?!\d)", text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=tz)
        except ValueError:
            pass

    return datetime(1970, 1, 1, tzinfo=tz)


def is_event_within_retention_window(event: dict[str, str], window_start: datetime) -> bool:
    """
    只保留近三年資料。
    日期可解析者，依 window_start 判斷。
    日期無法解析者：
    - MOPS 即時重大訊息保留，避免漏掉可能的重要新公告
    - TWSE / TPEx / TIB 官方歷史清單不保留，避免舊資料或異常格式污染網站
    """
    parsed = parse_roc_or_iso_date(event.get("event_date", ""))
    if parsed.year == 1970:
        return event.get("source") == "mops"
    return parsed >= window_start


def filter_events_within_retention_window(
    events: list[dict[str, str]],
    window_start: datetime,
) -> list[dict[str, str]]:
    return [event for event in events if is_event_within_retention_window(event, window_start)]


def event_sort_key(event: dict[str, str]) -> tuple[datetime, str, str]:
    return (
        parse_roc_or_iso_date(event.get("event_date", "")),
        event.get("company_code", ""),
        event.get("stage", ""),
    )


def parse_email_recipients(raw: str) -> list[str]:
    recipients = []
    seen: set[str] = set()
    for item in re.split(r"[,;\n]+", raw or ""):
        email = item.strip().lower()
        if email and "@" in email and email not in seen:
            seen.add(email)
            recipients.append(email)
    return recipients


def parse_subscriber_emails_from_payload(payload_text: str) -> list[str]:
    text = (payload_text or "").strip()
    if not text:
        return []

    # 支援 JSON：
    # 1) ["a@example.com", "b@example.com"]
    # 2) {"emails": ["a@example.com"]}
    # 3) {"subscribers": [{"email": "a@example.com"}]}
    try:
        payload = json.loads(text)
        raw_items: list[Any] = []
        if isinstance(payload, list):
            raw_items = payload
        elif isinstance(payload, dict):
            value = payload.get("emails") or payload.get("subscribers") or payload.get("data") or []
            if isinstance(value, list):
                raw_items = value

        emails: list[str] = []
        for item in raw_items:
            if isinstance(item, str):
                emails.append(item)
            elif isinstance(item, dict):
                emails.append(str(item.get("email", "")))
        return parse_email_recipients("\n".join(emails))
    except json.JSONDecodeError:
        pass

    # 支援 CSV / 純文字：第一欄或 email 欄。
    rows = list(csv.reader(text.splitlines()))
    if not rows:
        return []

    header = [normalize_text(cell).lower() for cell in rows[0]]
    email_index = 0
    if "email" in header:
        email_index = header.index("email")

    emails = []
    start_index = 1 if any("email" == cell for cell in header) else 0
    for row in rows[start_index:]:
        if email_index < len(row):
            emails.append(row[email_index])
    return parse_email_recipients("\n".join(emails))


def fetch_subscriber_emails() -> list[str]:
    if not EMAIL_SUBSCRIBERS_URL:
        return []
    try:
        return parse_subscriber_emails_from_payload(get_text(EMAIL_SUBSCRIBERS_URL))
    except Exception as exc:
        print(f"讀取訂閱名單失敗：{exc}")
        return []


def get_email_recipients(extra_raw: str = "") -> list[str]:
    combined = []
    combined.extend(parse_email_recipients(EMAIL_RECIPIENTS_RAW))
    combined.extend(parse_email_recipients(extra_raw))
    combined.extend(fetch_subscriber_emails())

    out: list[str] = []
    seen: set[str] = set()
    for email in combined:
        normalized = email.strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def format_email_subject(events: list[dict[str, str]]) -> str:
    if len(events) == 1:
        event = events[0]
        company = event.get("company_name") or event.get("company_code") or "未解析公司"
        stage = event.get("stage") or "新事件"
        return f"【興櫃轉板監測】{company}｜{stage}"
    return f"【興櫃轉板監測】偵測到 {len(events)} 筆新事件"


def format_email_body(events: list[dict[str, str]]) -> str:
    lines = [
        "您好，",
        "",
        "興櫃轉板監測網站偵測到新的事件如下：",
        "",
    ]

    for index, event in enumerate(events, start=1):
        company = event.get("company_name") or "未解析公司名稱"
        code = event.get("company_code")
        company_line = f"{company}（{code}）" if code else company
        lines.extend([
            f"{index}. {event.get('event_type', '')}｜{event.get('stage', '')}",
            f"   公司：{company_line}",
            f"   日期：{event.get('event_date', '')}",
            f"   來源：{event.get('source_label', '')}",
            f"   標題：{event.get('title', '')}",
            f"   連結：{event.get('url', '')}",
            "",
        ])

    lines.extend([
        "此信件由興櫃轉板網站監測程式自動寄出。",
        "若本輪沒有新事件，系統不會寄信。",
    ])
    return "\n".join(lines)


def send_email_notification(events: list[dict[str, str]], *, test_recipients_raw: str = "") -> int:
    if not events:
        return 0

    recipients = get_email_recipients(test_recipients_raw)
    if not EMAIL_SENDER:
        raise RuntimeError("缺少 EMAIL_SENDER。")
    if not EMAIL_APP_PASSWORD:
        raise RuntimeError("缺少 EMAIL_APP_PASSWORD。")
    if not recipients:
        raise RuntimeError("缺少 EMAIL_RECIPIENTS 或 EMAIL_SUBSCRIBERS_URL 訂閱名單。")

    message = EmailMessage()
    message["Subject"] = format_email_subject(events)
    message["From"] = EMAIL_SENDER
    message["To"] = ", ".join(recipients)
    message.set_content(format_email_body(events))

    with smtplib.SMTP_SSL(EMAIL_SMTP_HOST, EMAIL_SMTP_PORT, timeout=40) as smtp:
        smtp.login(EMAIL_SENDER, EMAIL_APP_PASSWORD)
        smtp.send_message(message)

    return len(recipients)


def make_test_email_event() -> dict[str, str]:
    return make_event(
        source="mail_test",
        event_type="Mail 通知測試",
        stage="測試寄信",
        event_date=taipei_now().strftime("%Y-%m-%d %H:%M"),
        company_code="TEST",
        company_name="台灣興櫃轉板進度監測站",
        title="這是一封測試信：如果你收到此信，代表 Gmail SMTP 寄信設定正常。",
        url="https://qm0121.github.io/tw-esb-listing-alert/",
        detail="此信由 GitHub Actions 手動測試模式寄出，不代表有新轉板事件。",
    )


def run_mail_test() -> None:
    recipient_count = send_email_notification([make_test_email_event()], test_recipients_raw=MAIL_TEST_RECIPIENTS_RAW)
    print(f"Mail 測試完成：已寄出測試信給 {recipient_count} 位收件人。")


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
    if MAIL_TEST_MODE:
        run_mail_test()
        return

    checked_at = taipei_now_iso()
    retention_start, retention_end = current_data_window()
    status: dict[str, Any] = {
        "last_checked_at": checked_at,
        "last_run_ok": False,
        "schema_version": SCHEMA_VERSION,
        "message": "",
        "sources": {},
        "new_event_count": 0,
        "email_recipient_count": 0,
        "total_event_count": 0,
        "data_retention_years": DATA_RETENTION_YEARS,
        "data_window_start": retention_start.strftime("%Y-%m-%d"),
        "data_window_end": retention_end.strftime("%Y-%m-%d"),
        "data_window_label": f"近 {DATA_RETENTION_YEARS} 年資料",
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
        # TWSE OpenAPI / TPEx / TIB OpenAPI 官方來源每次重建，避免舊版錯誤資料殘留。
        preserved_mops_events = filter_events_within_retention_window([
            event for event in existing_events
            if isinstance(event, dict) and event.get("source") == "mops"
        ], retention_start)

        source_events: dict[str, list[dict[str, str]]] = {}
        source_errors: dict[str, str] = {}

        # MOPS 即時重大訊息：
        # 公開版先停用自動抓取，避免一般網站自動化擷取之條款疑慮。
        # 來源列保留於前端，狀態顯示為「停用」而非「異常」。
        status["sources"]["mops"] = {
            "label": SOURCE_LABELS.get("mops", "mops"),
            "fetched": 0,
            "ok": True,
            "status": "disabled",
            "display_status": "停用",
            "note": "公開版暫停自動監測；保留來源列供後續合規替代方案接回。",
        }
        source_events["mops"] = []

        def fetch_twse_apply_events() -> list[dict[str, str]]:
            """
            申請上市資料改採政府資料開放平臺所連結的 TWSE CSV。
            若 CSV 解析為 0 筆，視為異常並保留既有資料，避免整批消失。
            """
            csv_text = get_text(TWSE_APPLY_LISTING_CSV_URL)
            csv_events = parse_twse_apply_csv(csv_text)
            filtered_events = filter_events_within_retention_window(csv_events, retention_start)

            status.setdefault("debug", {})["twse_apply_csv"] = {
                "parsed": len(csv_events),
                "retained": len(filtered_events),
                "first_200_chars": normalize_text(csv_text[:200]),
            }

            return filtered_events
        def fetch_tib_news_events() -> list[dict[str, str]]:
            """
            創新板相關新聞改採政府資料開放平臺所連結的 TWSE 證交所新聞 CSV。
            """
            csv_text = get_text(TWSE_NEWS_CSV_URL)
            csv_events = parse_tib_news_csv(csv_text)
            filtered_events = filter_events_within_retention_window(csv_events, retention_start)

            status.setdefault("debug", {})["tib_news_csv"] = {
                "parsed": len(csv_events),
                "retained": len(filtered_events),
                "first_200_chars": normalize_text(csv_text[:200]),
            }

            return filtered_events

        def fetch_tpex_apply_events() -> list[dict[str, str]]:
            """
            申請上櫃資料優先改成「依民國年度分批抓 CSV」：
            - 近三年滾動區間可能跨 4 個民國年度
            - 避免 y=ALL 一次抓取時偶發回傳異常或解析為 0
            """
            combined_events: list[dict[str, str]] = []
            year_debug: dict[str, dict[str, int]] = {}

            for year in tpex_candidate_roc_years(retention_start, retention_end):
                url = TPEX_APPLY_OTC_CSV_URL_TEMPLATE.format(year=year)
                csv_events = parse_tpex_apply(get_text(url))
                filtered_events = filter_events_within_retention_window(csv_events, retention_start)
                year_debug[str(year)] = {
                    "parsed": len(csv_events),
                    "retained": len(filtered_events),
                }
                combined_events.extend(filtered_events)

            combined_events = dedupe_events(combined_events)

            # 將年度抓取狀況放入 status，方便日後檢查。
            status.setdefault("debug", {})["tpex_apply_by_year"] = year_debug

            if combined_events:
                return combined_events

            # 若分年 CSV 仍完全抓不到，再以 y=ALL 試一次。
            all_url = TPEX_APPLY_OTC_CSV_URL_TEMPLATE.format(year="ALL")
            all_csv_events = parse_tpex_apply(get_text(all_url))
            all_filtered_events = filter_events_within_retention_window(all_csv_events, retention_start)
            status.setdefault("debug", {})["tpex_apply_all_csv"] = {
                "parsed": len(all_csv_events),
                "retained": len(all_filtered_events),
            }
            if all_filtered_events:
                return all_filtered_events

            # CSV 都抓不到，再用官方 HTML 作最後備援。
            html_events = parse_tpex_apply_html(get_text(TPEX_APPLY_OTC_URL))
            html_events = filter_events_within_retention_window(html_events, retention_start)
            status.setdefault("debug", {})["tpex_apply_html_fallback"] = {
                "retained": len(html_events),
            }
            return html_events

        fetchers = {
            "twse_apply": fetch_twse_apply_events,
            "tpex_apply": fetch_tpex_apply_events,
            "tib_news": fetch_tib_news_events,
        }

        for source, fetcher in fetchers.items():
            try:
                fetched_events = fetcher()
                source_events[source] = filter_events_within_retention_window(fetched_events, retention_start)

                if source == "twse_apply" and len(source_events[source]) == 0:
                    raise RuntimeError("TWSE 申請上市公司開放資料 CSV 解析為 0 筆；已寫入 status.debug.twse_apply_csv，暫不覆寫既有資料。")

                if source == "tpex_apply" and len(source_events[source]) == 0:
                    raise RuntimeError("TPEx 申請上櫃來源分年 CSV、ALL CSV 與 HTML 備援皆解析為 0 筆，暫不覆寫既有資料。")

                if source == "tib_news" and len(source_events[source]) == 0:
                    raise RuntimeError("TWSE 證交所新聞開放資料 CSV 解析為 0 筆；已寫入 status.debug.tib_news_csv，暫不覆寫既有資料。")

                status["sources"][source] = {
                    "label": SOURCE_LABELS.get(source, source),
                    "fetched": len(source_events[source]),
                    "ok": True,
                }
            except Exception as exc:
                source_events[source] = []
                source_errors[source] = str(exc)
                status["sources"][source] = {
                    "label": SOURCE_LABELS.get(source, source),
                    "fetched": 0,
                    "ok": False,
                    "error": str(exc),
                }

        # 網站資料：
        # - 成功抓取的官方來源每次重建
        # - 單一來源暫時失敗時，保留既有資料，避免整站因官方網站偶發錯誤而缺資料
        preserved_failed_source_events = filter_events_within_retention_window([
            event for event in existing_events
            if isinstance(event, dict) and event.get("source") in source_errors
        ], retention_start)
        website_events = (
            source_events.get("twse_apply", [])
            + source_events.get("tpex_apply", [])
            + source_events.get("tib_news", [])
            + preserved_failed_source_events
            + preserved_mops_events
            + source_events.get("mops", [])
        )
        website_events = filter_events_within_retention_window(dedupe_events(website_events), retention_start)
        website_events.sort(key=event_sort_key, reverse=True)
        website_events = website_events[:MAX_EVENTS_TO_KEEP]

        new_events: list[dict[str, str]] = []
        sendable_events: list[dict[str, str]] = []

        for source, events in source_events.items():
            source_new = [event for event in events if event["id"] not in seen_ids]
            new_events.extend(source_new)
            seen_ids.update(event["id"] for event in source_new)

            # v3 首次修復執行時，官方清單來源先做靜默基線，
            # 避免舊資料重新整理後一次寄出大量 Email。
            if is_v3_migration and source in {"twse_apply", "tpex_apply", "tib_news"}:
                initialized_sources.add(source)
                continue

            if source in initialized_sources:
                sendable_events.extend(source_new)
            else:
                initialized_sources.add(source)

        email_recipient_count = send_email_notification(sendable_events)

        status["new_event_count"] = len(new_events)
        status["email_recipient_count"] = email_recipient_count
        status["total_event_count"] = len(website_events)
        status["summary"] = summarize(website_events)
        status["last_run_ok"] = True
        if source_errors:
            status["message"] = (
                f"完成但有部分來源暫時抓取失敗：{', '.join(source_errors.keys())}。"
                f"網站資料共 {len(website_events)} 筆事件，本輪新增 {len(new_events)} 筆，"
                f"Email 通知收件人數 {email_recipient_count} 位；目前網站僅保留近三年資料，超出期間會於每次更新時自動汰除。"
            )
        else:
            status["message"] = (
                f"完成：網站重建 {len(website_events)} 筆事件，"
                f"本輪新增事件 {len(new_events)} 筆，Email 通知收件人數 {email_recipient_count} 位；目前網站僅保留近三年資料，超出期間會於每次更新時自動汰除。"
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
