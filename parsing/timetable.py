"""Разбор PDF-расписания SkyTeam (27 тыс. страниц) и проверка рейсов по нему.

Страница: две колонки (туда / обратно). В колонке шапка FROM/TO и строки:
  01 Nov - 31 Jan  1 34567  06:00  07:25  KL1328  73W  1H25M
Days: цифра = день недели (1 = понедельник), пробел = в этот день не летает.
Продолжение маршрута на следующей странице идёт без шапки — маршрут
протаскивается с предыдущей страницы по каждой колонке отдельно.
Год в строках не указан: берётся из обложки «Covers period: ... through ...».
"""
import re
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta

MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}

ROW = re.compile(
    r"^(\d{1,2}\s*[A-Za-z]{3})\s*-\s*(\d{1,2}\s*[A-Za-z]{3})\s+"   # validity
    r"([1-7][1-7 ]*?)\s+"                                           # days
    r"(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})\s*(\+\s*\d)?\s*"            # dep, arr, +1
    r"([A-Z0-9]{2}\d{1,5}[A-Z]?)\s+(\S+)"                           # flight, aircraft
    r"(?:\s+(\d+H\s*\d+M))?\s*$")
HDR = re.compile(r"^(FROM|TO)\s*:\s*(.+?)\s*([A-Z]{3})\s*$")
PERIOD = re.compile(r"Covers period:?\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\s+through\s+"
                    r"(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})", re.I)
AIRCRAFT = re.compile(r"^([A-Z0-9]{3})\s*:\s*(.+)$")



def _backend():
    try:
        import fitz  # noqa: F401  PyMuPDF
        return "fitz"
    except ImportError:
        return "pdfplumber"


def _page_count(path, backend):
    if backend == "fitz":
        import fitz
        with fitz.open(path) as d:
            return d.page_count
    import pdfplumber
    with pdfplumber.open(path) as d:
        return len(d.pages)


def _lines(words, tol=3.0):
    """words: [(x0, yc, h, text)] -> строки текста сверху вниз."""
    words.sort(key=lambda w: (w[1], w[0]))
    lines, cur, cur_y = [], [], None
    for w in words:
        t = max(tol, w[2] * 0.6)
        if cur and abs(w[1] - cur_y) > t:
            lines.append(cur)
            cur = []
        if not cur:
            cur_y = w[1]
        cur.append(w)
    if cur:
        lines.append(cur)
    out = []
    for ln in lines:
        ln.sort(key=lambda w: w[0])
        out.append((ln[0][1], re.sub(r"\s+", " ", " ".join(w[3] for w in ln)).strip()))
    return out


def _parse_page(words, width):
    """-> dict(period, aircraft, cols=[[события колонки 0], [колонки 1]])"""
    res = {"period": None, "aircraft": {}, "cols": [[], []]}
    full = " ".join(t for _, t in _lines(list(words)))
    m = PERIOD.search(full)
    if m:
        res["period"] = (m.group(1), m.group(2))
    mid = width / 2
    has_table = False
    for ci, part in enumerate(([w for w in words if w[0] < mid], [w for w in words if w[0] >= mid])):
        ev = res["cols"][ci]
        hdr = {}
        seen = set()
        for _, text in _lines(part):
            h = HDR.match(text)
            if h:
                hdr[h.group(1).upper()] = (h.group(3), h.group(2))
                if "FROM" in hdr and "TO" in hdr:
                    ev.append(("route", hdr["FROM"], hdr["TO"]))
                    hdr = {}
                    has_table = True
                continue
            r = ROW.match(text)
            if r:
                has_table = True
                vf, vt, days, dep, arr, plus, fl, ac, tt = r.groups()
                row = (re.sub(r"\s+", " ", vf), re.sub(r"\s+", " ", vt),
                       "".join(sorted(set(days.replace(" ", "")))), dep.zfill(5),
                       arr.zfill(5), int(plus.replace("+", "").strip()) if plus else 0,
                       fl, ac)
                if row not in seen:           # строки внутри маршрута многократно повторяются
                    seen.add(row)
                    ev.append(("row", row))
        if not has_table:
            for _, text in _lines(part):
                a = AIRCRAFT.match(text)
                if a:
                    res["aircraft"][a.group(1)] = a.group(2).strip()
    return res


def _worker(args):
    path, start, end, backend = args
    out = []
    if backend == "fitz":
        import fitz
        with fitz.open(path) as doc:
            for i in range(start, end):
                pg = doc[i]
                ws = [(w[0], (w[1] + w[3]) / 2, w[3] - w[1], w[4]) for w in pg.get_text("words")]
                out.append(_parse_page(ws, pg.rect.width))
    else:
        import pdfplumber
        with pdfplumber.open(path) as doc:
            for i in range(start, end):
                pg = doc.pages[i]
                ws = [(w["x0"], (w["top"] + w["bottom"]) / 2, w["bottom"] - w["top"], w["text"])
                      for w in pg.extract_words()]
                out.append(_parse_page(ws, pg.width))
                pg.flush_cache()
    return start, out



def _dm(s):
    m = re.match(r"(\d{1,2})\s*([A-Za-z]{3})", s)
    return (MONTHS[m.group(2).upper()], int(m.group(1))) if m and m.group(2).upper() in MONTHS else None


def _resolve(dm, period):
    """(месяц, день) -> дата внутри периода обложки (± 45 дней)."""
    if not dm or not period:
        return None
    ps, pe = period
    for y in range(ps.year - 1, pe.year + 2):
        try:
            d = date(y, dm[0], dm[1])
        except ValueError:
            continue
        if ps - timedelta(days=45) <= d <= pe + timedelta(days=45):
            return d
    return None


class Timetable:
    def __init__(self):
        self.periods = []                   # [(date_from, date_to)]
        self.idx = defaultdict(list)        # flight -> [entry]
        self.aircraft = {}
        self.stats = {}


    @classmethod
    def load(cls, path, airports, workers=10, chunk=200):
        tt = cls()
        backend = _backend()
        if backend != "fitz":
            print("  ВНИМАНИЕ: PyMuPDF не установлен, разбор через pdfplumber будет "
                  "очень медленным. Установите: pip install pymupdf", file=sys.stderr)
        n = _page_count(path, backend)
        jobs = [(path, s, min(s + chunk, n), backend) for s in range(0, n, chunk)]
        routes = [None, None]
        period = None
        seen = set()
        orphan = rows_total = 0
        done = 0
        t0 = datetime.now()
        import multiprocessing
        with ProcessPoolExecutor(max_workers=max(1, workers),
                                 mp_context=multiprocessing.get_context("spawn")) as ex:
            for start, pages in ex.map(_worker, jobs):
                for pg in pages:
                    if pg["period"]:
                        try:
                            p = tuple(datetime.strptime(re.sub(r"\s+", " ", x), "%d %b %Y").date()
                                      for x in pg["period"])
                            tt.periods.append(p)
                            period = p
                        except ValueError:
                            pass
                    tt.aircraft.update(pg["aircraft"])
                    for ci in (0, 1):
                        for ev in pg["cols"][ci]:
                            if ev[0] == "route":
                                (fc, fcity), (tc, tcity) = ev[1], ev[2]
                                routes[ci] = (fc, tc)
                                for code, txt in ((fc, fcity), (tc, tcity)):
                                    city, _, country = txt.partition(",")
                                    airports.add(code, city, country, prio=1)
                                continue
                            rows_total += 1
                            if routes[ci] is None:
                                orphan += 1
                                continue
                            vf, vt, days, dep, arr, plus, fl, ac = ev[1]
                            key = (routes[ci], ev[1], period)
                            if key in seen:
                                continue
                            seen.add(key)
                            d1, d2 = _resolve(_dm(vf), period), _resolve(_dm(vt), period)
                            if d1 and d2 and d2 < d1:
                                d2 = date(d2.year + 1, d2.month, d2.day)
                            tt.idx[fl].append((routes[ci][0], routes[ci][1], d1, d2,
                                               days, dep, arr, plus, ac))
                done += len(pages)
                el = (datetime.now() - t0).total_seconds()
                print(f"  timetable: {done}/{n} стр. ({el:.0f} c)", file=sys.stderr, flush=True)
        tt.stats = {"страниц": n, "строк рейсов в PDF": rows_total,
                    "уникальных рейсов (с маршрутом)": len(seen),
                    "строк без маршрута (не привязались)": orphan,
                    "номеров рейсов": len(tt.idx), "типов самолётов": len(tt.aircraft),
                    "периоды": ", ".join(f"{a:%d.%m.%Y}–{b:%d.%m.%Y}" for a, b in tt.periods)
                    or "не найдены"}
        return tt


    def in_period(self, d):
        return any(a <= d <= b for a, b in self.periods)

    def check(self, flight, depart_iso, from_codes, to_codes):
        """-> (статус, подходящие записи расписания)."""
        cands = self.idx.get(flight, [])
        route = [e for e in cands
                 if (not from_codes or e[0] in from_codes) and (not to_codes or e[1] in to_codes)]
        d = date.fromisoformat(depart_iso)
        if self.in_period(d):
            wd = str(d.isoweekday())
            ok = [e for e in route if e[2] and e[3] and e[2] <= d <= e[3] and wd in e[4]]
            if ok:
                return "confirmed", ok
            return ("wrong_day" if route else "not_found"), route
        return ("route_only" if route else "not_found_out_of_period"), route

    def aircraft_name(self, code):
        if not code:
            return None
        name = self.aircraft.get(code)
        return f"{code} {name}" if name else code
