"""하나은행 환율 수집 스크립트.

- 일별: '매매기준율 최초 고시' 환율 (올해 1/1부터 누락된 영업일을 채움)
- 평균: 하나은행 '평균환율' 메뉴의 기간 평균 (당월 1일~오늘, 1/1~오늘), 최초 고시 기준

하나은행 페이지 구조는 공식 문서가 없으므로, 조회 화면 HTML에서 폼(action, 입력 필드)을
읽어 날짜/고시구분만 바꿔 요청한다. 원본 응답은 debug/ 폴더에 저장한다(Actions 아티팩트).
파싱에 실패하면 기존 JSON을 덮어쓰지 않고 오류로 종료한다.
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))
CURRENCIES = ["USD", "EUR", "GBP", "CNY"]

BASE = "https://www.kebhana.com"
INDEX = BASE + "/cms/rate/index.do?contentUrl={}"
# 일자별 환율조회 화면
DAILY_PAGE = "/cms/rate/wpfxd651_01i.do"
# 평균환율 화면 후보 (실제 번호를 모르므로 '평균환율' 문구가 있는 화면을 찾는다)
AVG_PAGE_CANDIDATES = [f"/cms/rate/wpfxd651_{i:02d}i.do" for i in range(2, 13)]

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


def get(url: str, **kw) -> str:
    r = S.get(url, timeout=30, **kw)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


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


# ---------------------------------------------------------------- 폼 분석
class Form:
    def __init__(self, action: str, fields: dict, date_fields: list[str],
                 kind_field: tuple[str, str] | None, referer: str):
        self.action = action
        self.fields = fields
        self.date_fields = date_fields          # 날짜 입력 필드 이름(순서: 시작, 종료)
        self.kind_field = kind_field            # ('최초' 고시 선택 필드, 값)
        self.referer = referer

    def __repr__(self):
        return (f"Form(action={self.action}, date_fields={self.date_fields}, "
                f"kind={self.kind_field}, fields={self.fields})")


def label_text(soup, inp) -> str:
    if inp.get("id"):
        lb = soup.find("label", attrs={"for": inp["id"]})
        if lb:
            return lb.get_text(" ", strip=True)
    parent = inp.find_parent("label")
    if parent:
        return parent.get_text(" ", strip=True)
    sib = inp.next_sibling
    return sib.get_text(" ", strip=True) if hasattr(sib, "get_text") else str(sib or "").strip()


def parse_form(html: str, page_url: str) -> Form:
    soup = BeautifulSoup(html, "html.parser")
    forms = soup.find_all("form")
    if not forms:
        raise ScrapeError(f"폼 없음: {page_url}")
    # 날짜 입력이 있는 폼을 고른다
    form = max(forms, key=lambda f: len(f.find_all("input")))
    action = urljoin(page_url, form.get("action") or page_url)

    fields: dict[str, str] = {}
    date_fields: list[str] = []
    kind_field = None
    for inp in form.find_all(["input", "select"]):
        name = inp.get("name")
        if not name:
            continue
        typ = (inp.get("type") or "").lower()
        if inp.name == "select":
            opts = inp.find_all("option")
            sel = next((o for o in opts if o.has_attr("selected")), opts[0] if opts else None)
            fields[name] = sel.get("value", "") if sel else ""
            for o in opts:
                if "최초" in o.get_text():
                    kind_field = (name, o.get("value", ""))
            continue
        if typ in ("radio", "checkbox"):
            txt = label_text(soup, inp)
            if "최초" in txt:
                kind_field = (name, inp.get("value", ""))
            if inp.has_attr("checked"):
                fields[name] = inp.get("value", "")
            continue
        if typ in ("button", "submit", "image"):
            continue
        fields[name] = inp.get("value", "")
        if re.search(r"dt|date", name, re.I) and "tmp" not in name.lower():
            date_fields.append(name)
    return Form(action, fields, date_fields, kind_field, page_url)


def set_date(fields: dict, name: str, d: date):
    # 기존 기본값 형식(YYYYMMDD / YYYY-MM-DD / YYYY.MM.DD)을 따른다
    cur = fields.get(name, "")
    sep = "-" if "-" in cur else "." if "." in cur else ""
    fields[name] = d.strftime(f"%Y{sep}%m{sep}%d")
    # 화면용 보조 필드(tmpXXX)도 같이 맞춘다
    for k in list(fields):
        if k.lower().startswith("tmp") and k[3:].lower() == name.lower():
            fields[k] = d.strftime("%Y-%m-%d")


def build(form: Form, start: date, end: date | None = None) -> dict:
    f = dict(form.fields)
    f["ajax"] = "true"
    if not form.date_fields:
        raise ScrapeError(f"날짜 필드를 찾지 못함: {form}")
    set_date(f, form.date_fields[0], start)
    if end and len(form.date_fields) > 1:
        set_date(f, form.date_fields[1], end)
    if form.kind_field:
        f[form.kind_field[0]] = form.kind_field[1]
    return f


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
def load_form(content_path: str) -> tuple[Form, str]:
    page_url = BASE + content_path
    html = get(page_url, headers={"Referer": INDEX.format(content_path)})
    return parse_form(html, page_url), html


def find_avg_form() -> Form:
    for path in AVG_PAGE_CANDIDATES:
        try:
            html = get(BASE + path, headers={"Referer": INDEX.format(path)})
        except requests.RequestException:
            continue
        if "평균환율" in html and "매매기준율" in html:
            dump(f"avg_page_{path.rsplit('/', 1)[-1]}.html", html)
            try:
                form = parse_form(html, BASE + path)
            except ScrapeError:
                continue
            if len(form.date_fields) >= 2:
                log(f"평균환율 화면: {path}")
                return form
    raise ScrapeError("평균환율 화면을 찾지 못함 (debug/ 확인)")


def fetch_daily(form: Form, d: date) -> dict[str, float]:
    html = post(form.action, build(form, d), form.referer)
    dump(f"daily_{d:%Y%m%d}.html", html)
    return extract_rates(html)


def fetch_avg(form: Form, start: date, end: date) -> dict[str, float]:
    html = post(form.action, build(form, start, end), form.referer)
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

    # 1) 일별 최초 고시 (누락분 채우기, 처음이면 1/1부터)
    form, page_html = load_form(DAILY_PAGE)
    dump("daily_page.html", page_html)
    log(f"일별 폼: {form}")
    if not form.kind_field:
        raise ScrapeError("'최초' 고시 선택 항목을 찾지 못함 (debug/daily_page.html 확인)")

    start = date(today.year, 1, 1)
    todo = [d for d in weekdays(start, today) if d.isoformat() not in daily]
    log(f"일별 수집 대상: {len(todo)}일")
    for d in todo:
        rates = fetch_daily(form, d)
        if len(rates) == len(CURRENCIES):
            daily[d.isoformat()] = rates
            log(f"  {d} {rates}")
        else:
            log(f"  {d} 고시 없음(휴일 등) {rates}")
        time.sleep(0.5)
    if not daily:
        raise ScrapeError("일별 데이터가 비어 있음")

    latest_day = max(daily)
    base = date.fromisoformat(latest_day)

    # 2) 하나은행 평균환율 (최초 고시 기준)
    aform = find_avg_form()
    log(f"평균 폼: {aform}")
    m_start = base.replace(day=1)
    y_start = date(base.year, 1, 1)
    month_avg = fetch_avg(aform, m_start, base)
    year_avg = fetch_avg(aform, y_start, base)
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
