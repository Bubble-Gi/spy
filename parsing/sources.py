"""Чтение 6 источников с пассажирами. Каждый парсер читает файл потоково
и складывает данные в Store: человек -> прото-персона, перелёт -> сегмент."""
import glob
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

from .normalize import (clean, gender_of, norm_airport, norm_card, norm_flight,
                        parse_date, parse_time, split_full_name, ticket_is_damaged,
                        ticket_suspicious)



BUF = 8 << 20
PASS_SHEETS_PER_TASK = 200


def set_buffer(mb):
    global BUF
    BUF = max(1, int(mb)) << 20


def progress(label, n, every=100000):
    if n % every == 0:
        print(f"  {label}: {n:,}".replace(",", " "), file=sys.stderr, flush=True)


def detect_encoding(path):
    with open(path, "rb") as f:
        head = f.read(1 << 20)
    for enc in ("utf-8-sig", "cp1251"):
        try:
            head.decode(enc)
            return enc
        except UnicodeDecodeError as e:
            if enc == "utf-8-sig" and e.start > len(head) - 4:   # обрезан посреди символа
                return enc
    return "utf-8-sig"


def ticket_flags(ticket, flags):
    if ticket_is_damaged(ticket):
        flags.add("ticket_damaged")
    elif ticket_suspicious(ticket):
        flags.add("ticket_suspicious")


class ErrLog:
    """errors.log: строки, которые не удалось разобрать (до 200 на категорию)."""

    def __init__(self, path, stats):
        self.f = open(path, "w", encoding="utf-8")
        self.stats = stats
        self.per = {}

    def __call__(self, source, reason, detail=""):
        key = f"{source}: отброшено — {reason}"
        self.stats[key] += 1
        n = self.per.get(key, 0)
        if n < 200:
            self.f.write(f"[{source}] {reason}: {str(detail)[:300]}\n")
        self.per[key] = n + 1

    def close(self):
        self.f.close()


SIRENA_SPANS = {
    "name": (0, 60), "birth": (60, 72), "dep_date": (72, 84), "dep_time": (84, 96),
    "arr_date": (96, 108), "arr_time": (108, 120), "flight": (120, 126),
    "codeshare": (126, 132), "from": (132, 138), "to": (138, 144), "pnr": (144, 150),
    "ticket": (150, 168), "doc": (168, 180), "seat": (180, 186), "meal": (186, 192),
    "class": (192, 198), "fare": (198, 204), "baggage": (204, 216),
    "info": (216, 276), "agent": (276, 400),
}
_FF = re.compile(r"FF#\s*([A-Z0-9]{2})\s*(\d+)")


def parse_sirena(path, store, airports, log):
    src = "sirena"
    enc = detect_encoding(path)
    n = 0
    with open(path, encoding=enc, errors="replace", buffering=BUF) as f:
        header = f.readline()
        if not header.startswith("PaxName"):
            log(src, "неожиданный заголовок", header[:80])
        for line in f:
            line = line.rstrip("\r\n")
            if not line.strip():
                continue
            n += 1
            progress(src, n)
            r = {k: line[a:b] for k, (a, b) in SIRENA_SPANS.items()}
            dep = parse_date(r["dep_date"])
            flight = norm_flight(r["flight"])
            if not dep or not flight:
                log(src, "нет даты или номера рейса", line)
                continue
            last, first, middle = split_full_name(r["name"], "LFM")
            if not (last and first):
                log(src, "нет ФИО", line)
                continue
            info = r["info"]
            m = _FF.search(info)
            card = norm_card(m.group(1), m.group(2)) if m else None
            mark = info[:1] if info[:1].strip() and not info.startswith("FF#") else None
            flags = set()
            ticket = clean(r["ticket"])
            ticket_flags(ticket, flags)
            cs = (clean(r["codeshare"]) or "").upper()
            pid = store.proto_id(src, last, first, middle, name_orig=clean(r["name"]),
                                 birth=parse_date(r["birth"]),
                                 docs=[r["doc"]], cards=[card] if card else [])
            store.add_segment(pid, src, {
                "flight": flight,
                "codeshare": {"NO": "NO", "N": "NO", "YES": "YES", "Y": "YES"}.get(cs),
                "depart_date": dep, "depart_time": parse_time(r["dep_time"]),
                "arrival_date": parse_date(r["arr_date"]),
                "arrival_time": parse_time(r["arr_time"]),
                "from_airport": norm_airport(r["from"], flags),
                "to_airport": norm_airport(r["to"], flags),
                "travel_class": clean(r["class"]), "fare": clean(r["fare"]),
                "ff_card_used": card, "pnr": clean(r["pnr"]), "e_ticket": ticket,
                "seat": clean(r["seat"]), "meal": clean(r["meal"]),
                "baggage_allowance": clean(r["baggage"]), "agent": clean(r["agent"]),
                "sirena_mark": mark, "flags": flags,
            })
    store.stats[f"{src}: строк прочитано"] += n


def parse_boarding_data(path, store, airports, log):
    src = "boarding_data"
    enc = detect_encoding(path)
    n = 0
    with open(path, encoding=enc, errors="replace", buffering=BUF) as f:
        header = f.readline().rstrip("\r\n")
        sep = "\t" if "\t" in header else (";" if ";" in header else ",")
        cols = [c.strip() for c in header.split(sep)]
        idx = {c: i for i, c in enumerate(cols) if c}
        need = ["PassengerFirstName", "PassengerLastName", "FlightDate", "FlightNumber"]
        missing = [c for c in need if c not in idx]
        if missing:
            raise ValueError(f"BoardingData: нет колонок {missing}")

        def g(row, name):
            i = idx.get(name)
            return row[i] if i is not None and i < len(row) else None

        for line in f:
            line = line.rstrip("\r\n")
            if not line.replace(sep, "").strip():
                continue
            n += 1
            progress(src, n)
            row = line.split(sep)
            dep = parse_date(g(row, "FlightDate"))
            flight = norm_flight(g(row, "FlightNumber"))
            first, last = clean(g(row, "PassengerFirstName")), clean(g(row, "PassengerLastName"))
            if not dep or not flight:
                log(src, "нет даты или номера рейса", line)
                continue
            if not (first and last):
                log(src, "нет ФИО", line)
                continue
            middle = clean(g(row, "PassengerSecondName"))
            flags = set()
            ticket = clean(g(row, "TicketNumber"))
            ticket_flags(ticket, flags)
            cs = (clean(g(row, "CodeShare")) or "").upper()
            pid = store.proto_id(
                src, last, first, middle,
                name_orig=" ".join(x for x in (first, middle, last) if x),
                gender=gender_of(g(row, "PassengerSex")),
                birth=parse_date(g(row, "PassengerBirthDate"), "MDY"),
                docs=[g(row, "PassengerDocument")])
            store.add_segment(pid, src, {
                "flight": flight,
                "codeshare": None if not cs else ("NO" if cs in ("OWN", "NO") else "YES"),
                "depart_date": dep, "depart_time": parse_time(g(row, "FlightTime")),
                "to_city": clean(g(row, "Destination")),
                "pnr": clean(g(row, "BookingCode")), "e_ticket": ticket,
                "baggage_status": clean(g(row, "Baggage")), "flags": flags,
            })
    store.stats[f"{src}: строк прочитано"] += n


_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_CELL_REF = re.compile(r"([A-Z]+)(\d+)")


class _Num(str):
    """Числовая ячейка: храним исходный текст (16-значный билет во float теряет цифры)."""


def _col_num(letters):
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def xlsx_sheet_count(path):
    """Сколько листов в книге (читается только оглавление)."""
    import zipfile
    with zipfile.ZipFile(path) as z:
        try:
            wb = ET.fromstring(z.read("xl/workbook.xml"))
            return sum(1 for _ in wb.iter(_NS + "sheet"))
        except KeyError:
            return sum(1 for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))


def _xlsx_sheets(path, max_row=20, max_col=12, start=0, end=None):
    """Все листы книги -> [(имя_листа, {(row, col): value})].

    Читает XML внутри .xlsx напрямую (в разы быстрее openpyxl на книгах
    с сотнями листов). Числа отдаются как float — дату/время/билет
    разбирает вызывающий код.
    """
    import zipfile
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            with z.open("xl/sharedStrings.xml") as f:
                for _, el in ET.iterparse(f):
                    if el.tag == _NS + "si":
                        shared.append("".join(t.text or "" for t in el.iter(_NS + "t")))
                        el.clear()
        # порядок и имена листов
        sheets = []
        try:
            wb = ET.fromstring(z.read("xl/workbook.xml"))
            rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
            rmap = {r.get("Id"): r.get("Target") for r in rels}
            rid = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
            for sh in wb.iter(_NS + "sheet"):
                tgt = rmap.get(sh.get(rid), "")
                tgt = tgt.lstrip("/")
                tgt = tgt if tgt.startswith("xl/") else "xl/" + tgt
                if tgt in names:
                    sheets.append((sh.get("name"), tgt))
        except KeyError:
            pass
        if not sheets:
            sheets = [(n, n) for n in sorted(names) if re.match(r"xl/worksheets/sheet\d+\.xml$", n)]
        out = []
        for sname, sp in sheets[start:end]:
            cells = {}
            with z.open(sp) as f:
                for _, el in ET.iterparse(f):
                    if el.tag == _NS + "row":
                        if int(el.get("r") or 0) > max_row:
                            break
                        el.clear()
                        continue
                    if el.tag != _NS + "c":
                        continue
                    m = _CELL_REF.match(el.get("r") or "")
                    if not m:
                        continue
                    col, row = _col_num(m.group(1)), int(m.group(2))
                    if row > max_row or col > max_col:
                        continue
                    t = el.get("t")
                    v = el.find(_NS + "v")
                    if t == "s" and v is not None:
                        val = shared[int(v.text)]
                    elif t == "inlineStr":
                        val = "".join(x.text or "" for x in el.iter(_NS + "t"))
                    elif v is not None and v.text is not None:
                        val = v.text if t in ("str", "e") else (
                            v.text == "1" if t == "b" else _Num(v.text))
                    else:
                        continue
                    if str(val).strip() != "":
                        cells[(row, col)] = val
            out.append((sname, cells))
        return out


def _excel_date(v):
    from datetime import date, timedelta
    if isinstance(v, _Num):
        return (date(1899, 12, 30) + timedelta(days=int(float(v)))).isoformat()
    return v


def _excel_time(v):
    if isinstance(v, _Num):
        v = float(v)
        frac = v - int(v)
        mins = int(round(frac * 1440))
        return f"{mins // 60 % 24:02d}:{mins % 60:02d}"
    return v


def _read_passes_chunk(task):
    """Выполняется в отдельном процессе: (файл, первый лист, последний лист) -> ячейки."""
    path, start, end = task
    try:
        return [(f"{path}#{sname}", cells, None)
                for sname, cells in _xlsx_sheets(path, start=start, end=end)]
    except Exception as e:                       # битый файл
        return [(f"{path}#{start}-{end}", None, str(e))]


def _bounded_map(fn, items, workers):
    """Как ex.map, но:
    * процессы запускаются через spawn (чистый интерпретатор), а не fork —
      fork копировал бы гигабайты уже загруженных данных в каждый процесс;
    * в работе одновременно не больше 2×workers заданий — результаты
      не копятся в памяти, если основной процесс не успевает их забирать."""
    import multiprocessing
    from collections import deque
    from concurrent.futures import ProcessPoolExecutor
    if workers <= 1 or len(items) <= 1:
        for it in items:
            yield fn(it)
        return
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
        it = iter(items)
        pending = deque()
        for x in it:
            pending.append(ex.submit(fn, x))
            if len(pending) >= workers * 2:
                break
        while pending:
            res = pending.popleft().result()
            nxt = next(it, None)
            if nxt is not None:
                pending.append(ex.submit(fn, nxt))
            yield res


def parse_boarding_passes(paths, store, airports, log, workers=1):
    """Талоны читаются порциями по PASS_SHEETS_PER_TASK листов: в памяти одновременно
    не больше 2×workers порций, независимо от того, сколько листов в одном файле."""
    src = "boarding_pass"
    tasks, total = [], 0
    for path in paths:
        try:
            k = xlsx_sheet_count(path)
        except Exception as e:
            log(src, "не открылся", f"{path}: {e}")
            continue
        total += k
        for a in range(0, max(k, 1), PASS_SHEETS_PER_TASK):
            tasks.append((path, a, a + PASS_SHEETS_PER_TASK))
    print(f"  {src}: файлов {len(paths)}, листов {total}, порций {len(tasks)}",
          file=sys.stderr, flush=True)
    n = done = 0
    for batch in _bounded_map(_read_passes_chunk, tasks, workers):
        done += 1
        for path, c, err in batch:
            if err is not None:
                log(src, "не открылся", f"{path}: {err}")
                continue
            n += 1
            _pass_to_store(path, c, store, airports, log)
        if done % 20 == 0 or done == len(tasks):
            print(f"  {src}: талонов {n} / {total}", file=sys.stderr, flush=True)
    store.stats[f"{src}: файлов прочитано"] += len(paths)
    store.stats[f"{src}: листов (талонов) прочитано"] += n


def _pass_to_store(path, c, store, airports, log):
    src = "boarding_pass"
    def s(r, col):
        v = c.get((r, col))
        if isinstance(v, _Num) and v.endswith(".0"):
            v = v[:-2]
        return clean(v)
    anchors_ok = ("BOARDING" in str(s(1, 1) or "").upper()
                  and "PNR" in str(s(13, 1) or "").upper())
    if not anchors_ok:
        log(src, "раскладка ячеек не совпала с шаблоном", path)
        return
    flight, dep = norm_flight(s(5, 1)), parse_date(_excel_date(c.get((9, 1))))
    last, first, middle = store.split_ambiguous_name(s(3, 2))
    if not dep or not flight:
        log(src, "нет даты или номера рейса", path)
        return
    if not (last and first):
        log(src, "нет ФИО", path)
        return
    flags = set()
    ticket = s(13, 5)
    ticket_flags(ticket, flags)
    fa, ta = norm_airport(s(7, 4), flags), norm_airport(s(7, 8), flags)
    airports.add(fa, s(5, 4), prio=8)
    airports.add(ta, s(5, 8), prio=8)
    op = str(s(9, 5) or "")
    pid = store.proto_id(src, last, first, middle, name_orig=s(3, 2),
                         gender=gender_of(s(3, 1)))
    store.add_segment(pid, src, {
        "flight": flight, "codeshare": "YES" if "OPERATED BY" in op.upper() else None,
        "depart_date": dep, "depart_time": parse_time(_excel_time(c.get((9, 3)))),
        "from_airport": fa, "from_city": s(5, 4), "to_airport": ta, "to_city": s(5, 8),
        "travel_class": s(3, 8), "pnr": s(13, 2), "e_ticket": ticket,
        "seat": s(11, 8), "flags": flags,
    })


def parse_pointz(path, store, airports, log):
    src = "pointz"
    n = 0
    fh = open(path, "rb", buffering=BUF)
    ctx = ET.iterparse(fh, events=("start", "end"))
    _, root = next(ctx)
    for ev, el in ctx:
        if ev != "end" or el.tag != "user":
            continue
        uid = clean(el.get("uid"))
        nm = el.find("name")
        first = clean(nm.get("first")) if nm is not None else None
        last = clean(nm.get("last")) if nm is not None else None
        for card_el in el.iter("card"):
            card = norm_card(card_el.get("number") or "")
            pid = store.proto_id(src, last, first,
                                 name_orig=" ".join(x for x in (first, last) if x) or None,
                                 cards=[card] if card else [], uid=uid)
            for act in card_el.iter("activity"):
                n += 1
                progress(src, n)
                if (act.get("type") or "Flight") != "Flight":
                    log(src, f"activity type={act.get('type')} (не перелёт)", uid)
                    continue
                g = lambda t: clean(act.findtext(t))   # noqa: E731
                dep, flight = parse_date(g("Date")), norm_flight(g("Code"))
                if not dep or not flight:
                    log(src, "нет даты или номера рейса", f"uid={uid}")
                    continue
                flags = set()
                store.add_segment(pid, src, {
                    "flight": flight, "depart_date": dep,
                    "from_airport": norm_airport(g("Departure"), flags),
                    "to_airport": norm_airport(g("Arrival"), flags),
                    "fare": g("Fare"), "ff_card_used": card, "flags": flags,
                })
        el.clear()
        root.clear()          # иначе дерево копится в памяти
    fh.close()
    store.stats[f"{src}: записей прочитано"] += n


_Y_DATE = re.compile(r"^'?(\d{4}-\d{2}-\d{2})'?\s*:\s*$")
_Y_KEY = re.compile(r"^(\s+)(['\"]?)(.+?)\2\s*:\s*(.*?)\s*$")


def _yaml_inline(s):
    """'{CLASS: Y, FARE: YRSTFN}' -> {'CLASS': 'Y', 'FARE': 'YRSTFN'}"""
    s = s.strip()
    if not (s.startswith("{") and s.endswith("}")):
        return {}
    out = {}
    for part in s[1:-1].split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip().strip("'\"")] = v.strip().strip("'\"")
    return out


def parse_exchange(path, store, airports, log):
    """Построчный разбор: yaml.load на 155 Мб требует гигабайты памяти."""
    src = "exchange"
    n = 0
    day = flight = None
    fields, cards = {}, []
    flight_ind = field_ind = None

    def emit():
        nonlocal n
        if not (day and flight):
            return
        if not cards:
            log(src, "рейс без карт", f"{day} {flight}")
        for card, attrs in cards:
            n += 1
            progress(src, n)
            flags = set()
            pid = store.proto_id(src, cards=[card])
            store.add_segment(pid, src, {
                "flight": norm_flight(flight), "depart_date": day,
                "from_airport": norm_airport(fields.get("FROM"), flags),
                "to_airport": norm_airport(fields.get("TO"), flags),
                "flight_status": clean(fields.get("STATUS")),
                "travel_class": clean(attrs.get("CLASS")), "fare": clean(attrs.get("FARE")),
                "ff_card_used": card, "flags": flags,
            })

    with open(path, encoding="utf-8", errors="replace", buffering=BUF) as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            m = _Y_DATE.match(line)
            if m:
                emit()
                day, flight, fields, cards = parse_date(m.group(1)), None, {}, []
                continue
            m = _Y_KEY.match(line)
            if not m:
                log(src, "непонятная строка", line)
                continue
            ind, key, val = len(m.group(1)), m.group(3), m.group(4)
            if flight_ind is None:
                flight_ind = ind
            if ind == flight_ind:
                emit()
                flight, fields, cards = key, {}, []
                continue
            if field_ind is None and ind > flight_ind:
                field_ind = ind
            if ind == field_ind:
                if key.upper() != "FF":
                    fields[key.upper()] = val
            elif field_ind is not None and ind > field_ind:
                card = norm_card(key)
                if card:
                    cards.append((card, _yaml_inline(val)))
                else:
                    log(src, "непонятный номер карты", line)
        emit()
    store.stats[f"{src}: записей (карта×рейс) прочитано"] += n


def iter_json_array(path, chunk=None):
    """Потоково отдаёт элементы первого массива верхнего уровня
    ({"Forum Profiles": [ {...}, {...} ]}) без загрузки всего файла."""
    dec = json.JSONDecoder()
    chunk = chunk or BUF
    with open(path, encoding="utf-8-sig", errors="replace", buffering=BUF) as f:
        buf = f.read(chunk)
        i = buf.find("[")
        while i < 0:
            more = f.read(chunk)
            if not more:
                return
            buf += more
            i = buf.find("[")
        buf, eof = buf[i + 1:], False
        while True:
            j = 0
            while j < len(buf) and buf[j] in " \t\r\n,":
                j += 1
            if j < len(buf) and buf[j] == "]":
                return
            buf = buf[j:]
            try:
                obj, end = dec.raw_decode(buf)
            except json.JSONDecodeError:
                if eof:
                    raise
                more = f.read(chunk)
                if not more:
                    eof = True
                buf += more
                continue
            yield obj
            buf = buf[end:]


def _passports(v):
    """Passports бывает null, строкой, списком строк или объектов — достаём номера."""
    out = []
    if v is None:
        return out
    if isinstance(v, (str, int)):
        s = clean(v)
        if s:
            out.append(str(s))
    elif isinstance(v, list):
        for x in v:
            out += _passports(x)
    elif isinstance(v, dict):
        for k in ("Number", "number", "Passport", "Num", "No"):
            if k in v:
                out += _passports(v[k])
                break
        else:
            for x in v.values():
                out += _passports(x)
    return out


def parse_forum(path, store, airports, log):
    src = "forum"
    n = profiles = 0
    for prof in iter_json_array(path):
        profiles += 1
        progress(src + " (профилей)", profiles, 20000)
        rn = prof.get("Real Name") or {}
        last, first = clean(rn.get("Last Name")), clean(rn.get("First Name"))
        cards, status = [], {}
        for lp in prof.get("Loyality Programm") or []:
            card = norm_card(lp.get("programm"), str(lp.get("Number") or ""))
            if card:
                cards.append(card)
                if clean(lp.get("Status")):
                    status[card] = clean(lp.get("Status"))
        docs = []
        for td in prof.get("Travel Documents") or []:
            docs += _passports(td.get("Passports") if isinstance(td, dict) else td)
        if not (last and first) and not cards:
            log(src, "нет ни ФИО, ни карты", prof.get("NickName"))
            continue
        pid = store.proto_id(src, last, first,
                             name_orig=" ".join(x for x in (first, last) if x) or None,
                             gender=gender_of(prof.get("Sex")), docs=docs, cards=cards,
                             card_status=status, nickname=clean(prof.get("NickName")))
        for fl in prof.get("Registered Flights") or []:
            n += 1
            dep, flight = parse_date(fl.get("Date")), norm_flight(fl.get("Flight"))
            if not dep or not flight:
                log(src, "нет даты или номера рейса", fl)
                continue
            flags = set()
            d, a = fl.get("Departure") or {}, fl.get("Arrival") or {}
            fa, ta = norm_airport(d.get("Airport"), flags), norm_airport(a.get("Airport"), flags)
            airports.add(fa, d.get("City"), d.get("Country"), prio=5)
            airports.add(ta, a.get("City"), a.get("Country"), prio=5)
            cs = fl.get("Codeshare")
            store.add_segment(pid, src, {
                "flight": flight, "depart_date": dep,
                "codeshare": None if cs is None else ("YES" if cs else "NO"),
                "from_airport": fa, "to_airport": ta, "flags": flags,
            })
    store.stats[f"{src}: профилей прочитано"] += profiles
    store.stats[f"{src}: перелётов прочитано"] += n


def find_inputs(data_dir):
    """Ищет файлы по именам в папке (и подпапках для .xlsx)."""
    def one(*patterns):
        for p in patterns:
            hits = sorted(glob.glob(os.path.join(data_dir, "**", p), recursive=True))
            if hits:
                return hits[0]
        return None
    return {
        "sirena": one("sirena*.tab", "*irena*"),
        "boarding_data": one("BoardingData*.csv", "BoardingData*"),
        "pointz": one("PointzAggregator*"),
        "exchange": one("SkyTeam-Exchange*.yaml", "SkyTeam-Exchange*"),
        "forum": one("FrequentFlyerForum*.json", "FrequentFlyerForum*"),
        "timetable": one("Skyteam_Timetable*.pdf", "Skyteam_Timetable*", "*imetable*.pdf"),
        "boarding_passes": sorted(glob.glob(os.path.join(data_dir, "**", "*.xlsx"),
                                            recursive=True)),
    }
