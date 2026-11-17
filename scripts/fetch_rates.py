"""하나은행 환율 수집 스크립트.

- 일별: '매매기준율 최초 고시' 환율 (올해 1/1부터 누락된 영업일을 채움)
- 평균: 하나은행 '평균환율' 메뉴의 기간 평균 (당월 1일~오늘, 1/1~오늘), 최초 고시 기준

하나은행 환율 조회 AJAX 엔드포인트(일별 wpfxd651_01i_01.do, 평균 wpfxd651_06i_01.do)로
직접 요청하여 통화별(USD, EUR, GBP, CNY) 매매기준율을 수집한다.
파싱에 실패하면 기존 JSON을 덮어쓰지 않고 오류로 종료한다.
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))
CURRENCIES = ["USD", "EUR", "GBP", "CNY"]

BASE = "https://www.kebhana.com"
DAILY_API = BASE + "/cms/rate/wpfxd651_01i_01.do"
DAILY_REFERER = BASE + "/cms/rate/index.do?contentUrl=/cms/rate/wpfxd651_01i.do"
AVG_API = BASE + "/cms/rate/wpfxd651_06i_01.do"
AVG_REFERER = BASE + "/cms/rate/index.do?contentUrl=/cms/rate/wpfxd651_06i.do"

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "docs" / "data" / "rates.json"
DEBUG_DIR = ROOT / "debug"

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
})


class ScrapeError(RuntimeError):
    pass


# ---------------------------------------------------------------- 유틸
def log(*a):
    print(*a, flush=True)


def dump(name: str, text: str):
    DEBUG_DIR.mkdir(exist_ok=True)
    (DEBUG_DIR / name).write_text(text, encoding="utf-8")


def post(url: str, data: dict, referer: str) -> str:
    for attempt in range(4):
        try:
            r = S.post(url, data=data, timeout=30,
                       headers={"Referer": referer, "X-Requested-With": "XMLHttpRequest"})
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except requests.RequestException as e:
            if attempt == 3:
                raise
            log(f"  retry {attempt + 1}: {e}")
            time.sleep(2 ** attempt)
    raise AssertionError


def to_num(s: str) -> float | None:
    s = s.replace(",", "").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


# ---------------------------------------------------------------- 표 분석
def table_grid(table):
    """rowspan/colspan을 펼친 2차원 셀 배열과 헤더 행 수."""
    grid: list[list[str]] = []
    spans: dict[tuple[int, int], str] = {}
    header_rows = 0
    for ri, tr in enumerate(table.find_all("tr")):
        row: list[str] = []
        ci = 0
        cells = tr.find_all(["th", "td"])
        if cells and all(c.name == "th" for c in cells) and header_rows == ri:
            header_rows += 1
        it = iter(cells)
        while True:
            if (ri, ci) in spans:
                row.append(spans.pop((ri, ci)))
                ci += 1
                continue
            c = next(it, None)
            if c is None:
                break
            txt = c.get_text(" ", strip=True)
            rs, cs = int(c.get("rowspan", 1) or 1), int(c.get("colspan", 1) or 1)
            for dc in range(cs):
                row.append(txt)
                for dr in range(1, rs):
                    spans[(ri + dr, ci + dc)] = txt
                ci += 1
        grid.append(row)
    return grid, header_rows


def extract_rates(html: str) -> dict[str, float]:
    """응답 HTML에서 통화별 '매매기준율' 값을 뽑는다."""
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, float] = {}
    for table in soup.find_all("table"):
        grid, hr = table_grid(table)
        if hr == 0 or len(grid) <= hr:
            continue
        width = max(len(r) for r in grid[:hr])
        headers = [" ".join(dict.fromkeys(grid[r][c] for r in range(hr) if c < len(grid[r])))
                   for c in range(width)]
        col = next((i for i, h in enumerate(headers) if "매매기준율" in h.replace(" ", "")), None)
        if col is None:
            continue
        for row in grid[hr:]:
            if len(row) <= col:
                continue
            for code in CURRENCIES:
                if re.search(rf"\b{code}\b", row[0]) or (len(row) > 1 and re.search(rf"\b{code}\b", row[1])):
                    v = to_num(row[col])
                    if v:
                        out[code] = v
    return out


# ---------------------------------------------------------------- 수집
def fetch_daily(d: date) -> dict[str, float]:
    data = {
        "ajax": "true",
        "curCd": "",
        "tmpInqStrDt": d.strftime("%Y-%m-%d"),
        "inqStrDt": d.strftime("%Y%m%d"),
        "inqKindCd": "1",
        "pbldDvCd": "1",  # 최초
        "pbldSqn": "",
    }
    html = post(DAILY_API, data, DAILY_REFERER)
    dump(f"daily_{d:%Y%m%d}.html", html)
    # 기준일이 조회일과 일치하는지 확인 (휴일이면 직전 영업일이 반환됨)
    m = re.search(r"기준일\s*</em>\s*:\s*<strong>(\d{4})년(\d{2})월(\d{2})일</strong>", html)
    if m:
        base_dt = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        if base_dt != d.isoformat():
            return {}
    return extract_rates(html)


def fetch_avg(start: date, end: date) -> dict[str, float]:
    data = {
        "ajax": "true",
        "inqDvCd": "4",  # 기간평균
        "inqStrDt": start.strftime("%Y%m%d"),
        "inqEndDt": end.strftime("%Y%m%d"),
        "pbldDvCd": "1",  # 최초
        "tmpInqStrDt_p": start.strftime("%Y-%m-%d"),
        "tmpInqEndDt_p": end.strftime("%Y-%m-%d"),
        "tmpPbldDvCd": "1",
    }
    html = post(AVG_API, data, AVG_REFERER)
    dump(f"avg_{start:%Y%m%d}_{end:%Y%m%d}.html", html)
    return extract_rates(html)


def weekdays(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def main():
    now = datetime.now(KST)
    today = now.date()
    data = json.loads(DATA_FILE.read_text("utf-8")) if DATA_FILE.exists() else {}
    daily: dict = {k: v for k, v in data.get("daily", {}).items() if k.startswith(str(today.year))}
    holidays: set[str] = set(data.get("holidays", []))

    start = date(today.year, 1, 1)
    todo = [d for d in weekdays(start, today) if d.isoformat() not in daily and d.isoformat() not in holidays]
    log(f"일별 수집 대상: {len(todo)}일")
    for d in todo:
        rates = fetch_daily(d)
        if len(rates) == len(CURRENCIES):
            daily[d.isoformat()] = rates
            log(f"  {d} {rates}")
        else:
            holidays.add(d.isoformat())
            log(f"  {d} 고시 없음(휴일 등) {rates}")
        time.sleep(0.1)
    if not daily:
        raise ScrapeError("일별 데이터가 비어 있음")

    latest_day = max(daily)
    base = date.fromisoformat(latest_day)

    # 2) 하나은행 평균환율 (최초 고시 기준)
    m_start = base.replace(day=1)
    y_start = date(base.year, 1, 1)
    month_avg = fetch_avg(m_start, base)
    year_avg = fetch_avg(y_start, base)
    log(f"당월 평균({m_start}~{base}): {month_avg}")
    log(f"연간 평균({y_start}~{base}): {year_avg}")
    if len(month_avg) != len(CURRENCIES) or len(year_avg) != len(CURRENCIES):
        raise ScrapeError(f"평균환율 파싱 실패: month={month_avg} year={year_avg}")

    def days_in(prefix_from: date):
        return sum(1 for k in daily if prefix_from.isoformat() <= k <= latest_day)

    def calc_avg(code, frm: date):
        vals = [v[code] for k, v in daily.items() if frm.isoformat() <= k <= latest_day]
        return round(sum(vals) / len(vals), 2)

    currencies = {}
    for c in CURRENCIES:
        currencies[c] = {
            "latest": {"date": latest_day, "rate": daily[latest_day][c]},
            "month_avg": {"from": m_start.isoformat(), "to": latest_day, "rate": month_avg[c],
                          "days": days_in(m_start), "check_calc": calc_avg(c, m_start)},
            "year_avg": {"from": y_start.isoformat(), "to": latest_day, "rate": year_avg[c],
                         "days": days_in(y_start), "check_calc": calc_avg(c, y_start)},
        }

    out = {
        "source": "하나은행 매매기준율 최초 고시 / 평균환율",
        "updated_at": now.isoformat(timespec="seconds"),
        "currencies": currencies,
        "daily": dict(sorted(daily.items())),
        "holidays": sorted(holidays),
    }
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=1), "utf-8")
    log(f"저장 완료: {DATA_FILE}")


if __name__ == "__main__":
    try:
        main()
    except (ScrapeError, requests.RequestException) as e:
        log(f"ERROR: {e}")
        sys.exit(1)
