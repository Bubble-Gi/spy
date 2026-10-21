"""Склейка людей и сегментов по согласованным правилам + запись итогового CSV.
"""
import csv
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta

from .normalize import out_date, ticket_eq, ticket_is_damaged
from .store import PRIO, SEG_COLS

OUT_COLS = [
    "person_id", "last_name", "first_name", "middle_name", "name_original", "gender",
    "birth_date", "travel_doc", "ff_cards", "ff_status", "forum_nickname", "pointz_uid",
    "flight", "codeshare", "depart_date", "depart_time", "arrival_date", "arrival_time",
    "from_airport", "from_city", "from_country", "to_airport", "to_city", "to_country",
    "travel_class", "fare", "ff_card_used", "pnr", "e_ticket", "seat", "meal",
    "baggage_allowance", "baggage_status", "agent", "flight_status", "aircraft",
    "sirena_mark", "timetable_check", "flags",
]

SEG_STRICT = ["from_airport", "to_airport", "depart_time", "arrival_date", "arrival_time",
              "travel_class", "fare", "ff_card_used", "pnr", "e_ticket", "seat", "meal",
              "baggage_allowance", "baggage_status", "agent", "flight_status",
              "sirena_mark", "codeshare"]

class Person:
    def __init__(self, p):
        self.last, self.first, self.middle = p.last, p.first, p.middle
        self.middlec, self.middle_init = p.middlec, p.middle_init
        self.gender, self.birth = p.gender, p.birth
        self.docs = dict(p.docs)
        self.cards = set(p.cards)
        self.card_status = dict(p.card_status)
        self.nickname, self.uid = p.nickname, p.uid
        self.originals = set(p.originals)
        self.prio = p.prio
        self.flags = set()
        self.id = None
        self.protos = [p.pid]


def _eq(a, b):
    return not a or not b or a == b


def _mid_ok(a, ai, b, bi):
    if not a or not b:
        return True
    if ai and bi:
        return a == b
    if ai:
        return b.startswith(a)
    if bi:
        return a.startswith(b)
    return a == b


def compatible(P, p):
    if not (_eq(P.gender, p.gender) and _eq(P.birth, p.birth)
            and _eq(P.nickname, p.nickname) and _eq(P.uid, p.uid)):
        return False
    if not _mid_ok(P.middlec, P.middle_init, p.middlec, p.middle_init):
        return False
    if P.docs and p.docs and not (P.docs.keys() & p.docs.keys()):
        return False
    progs = {}
    for c in P.cards:
        progs.setdefault(c.split(" ", 1)[0], set()).add(c)
    for c in p.cards:
        same = progs.get(c.split(" ", 1)[0])
        if same and c not in same:
            return False
    return True


def absorb(P, p):
    P.protos.append(p.pid)
    if p.prio < P.prio and p.last:
        P.last, P.first, P.prio = p.last, p.first, p.prio
    if p.middle and (not P.middle or (P.middle_init and not p.middle_init)):
        P.middle, P.middlec, P.middle_init = p.middle, p.middlec, p.middle_init
    P.gender = P.gender or p.gender
    P.birth = P.birth or p.birth
    P.nickname = P.nickname or p.nickname
    P.uid = P.uid or p.uid
    for k, v in p.docs.items():
        P.docs.setdefault(k, v)
    P.cards |= p.cards
    P.card_status.update(p.card_status)
    P.originals |= p.originals


def cluster(store):
    """-> (persons, proto->Person, set(proto, приклеенных по карте))."""
    stats = store.stats
    blocks = defaultdict(list)
    card_only = []
    for p in store.protos:
        (card_only if p.card_only else blocks[(p.lastc, p.firstc)]).append(p)

    # ключи перелётов прото-записи: (рейс, дата) и номер билета; читаются из SQLite по запросу
    store.flush()
    db = store.db
    db.execute("CREATE INDEX IF NOT EXISTS seg_proto ON seg(proto)")
    cache = {}

    def keys_of(pid):
        k = cache.get(pid)
        if k is None:
            k = set()
            for fl, d, t in db.execute(
                    "SELECT flight, depart_date, e_ticket FROM seg WHERE proto = ?", (pid,)):
                k.add(("F", fl, d))
                if t:
                    k.add(("T", t))
            if len(cache) > 300000:
                cache.clear()
            cache[pid] = k
        return k

    owner = {}
    persons = []
    for plist in blocks.values():
        plist.sort(key=lambda p: (-p.completeness(), p.prio))
        # индекс по дате рождения: совместимый человек обязан иметь ту же дату или пустую
        local, by_birth, no_birth = {}, defaultdict(dict), {}
        for p in plist:
            if p.birth:
                pool = list(by_birth[p.birth].values()) + list(no_birth.values())
            else:
                pool = local.values()
            comp = [P for P in pool if compatible(P, p)]
            if len(comp) > 1:
                # уточняющие данные: общая карта, документ, uid или ник решают неоднозначность
                strong = [P for P in comp
                          if (P.cards & p.cards) or (P.docs.keys() & p.docs.keys())
                          or (P.uid and P.uid == p.uid)
                          or (P.nickname and P.nickname == p.nickname)]
                if len(strong) == 1:
                    comp = strong
                    stats["люди: неоднозначность снята по карте/документу"] += 1
                else:
                    # общий билет или тот же рейс в ту же дату у тёзки — тоже уточнение
                    mine = keys_of(p.pid)
                    if mine:
                        hit = [P for P in comp
                               if any(keys_of(q) & mine for q in P.protos)]
                        if len(hit) == 1:
                            comp = hit
                            stats["люди: неоднозначность снята по общему билету/рейсу"] += 1
            if len(comp) == 1:
                P = comp[0]
                had_birth = P.birth
                absorb(P, p)
                owner[p.pid] = P
                if P.birth and not had_birth:
                    no_birth.pop(id(P), None)
                    by_birth[P.birth][id(P)] = P
            else:
                P = Person(p)
                if len(comp) > 1:
                    P.flags.add("identity_ambiguous")
                    stats["люди: неоднозначных (подходят к нескольким)"] += 1
                    src = {v: k for k, v in PRIO.items()}.get(p.prio, "?")
                    stats[f"люди: неоднозначных, источник {src}"] += 1
                local[id(P)] = P
                (by_birth[P.birth] if P.birth else no_birth)[id(P)] = P
                persons.append(P)
                owner[p.pid] = P

    card_map = defaultdict(set)
    for P in persons:
        for c in P.cards:
            card_map[c].add(P)

    by_card = set()
    card_only.sort(key=lambda p: -len(p.cards))
    for p in card_only:
        hit = set()
        for c in p.cards:
            hit |= card_map.get(c, set())
        if len(hit) == 1:
            P = next(iter(hit))
            if compatible(P, p):
                absorb(P, p)
                owner[p.pid] = P
                by_card.add(p.pid)
                for c in p.cards:
                    card_map[c].add(P)
                continue
            stats["без ФИО: отброшено — карта у человека с противоречащими данными"] += 1
        elif not hit:
            stats["без ФИО: отброшено — карта не найдена у людей с ФИО"] += 1
        else:
            stats["без ФИО: отброшено — карта у нескольких разных людей"] += 1

    persons.sort(key=lambda P: (P.last or "", P.first or "", P.middle or "", P.birth or ""))
    for i, P in enumerate(persons, 1):
        P.id = f"P{i:07d}"
    stats["люди: итого"] = len(persons)
    stats["люди: прото-записей с ФИО"] = sum(len(v) for v in blocks.values())
    stats["без ФИО: прото-записей (карт)"] = len(card_only)
    stats["без ФИО: приклеено по карте"] = len(by_card)
    return persons, owner, by_card


# ====================================================================== сегменты

# Поля, расхождение в которых НЕ разделяет поездку на две строки: оба значения
# записываются через ' | '. Задаётся ключом --soft-fields (по умолчанию пусто = всё строго).
SOFT_FIELDS = set()
# Примеры конфликтов для report.txt: поле -> [строки]
CONFLICT_EXAMPLES = defaultdict(list)


LEG_FIELDS = {"from_airport", "to_airport", "depart_time", "arrival_date", "arrival_time"}


def _is_multi_leg(merged, strict):
    """Один номер рейса с промежуточной посадкой: MNL→NRT и NRT→ATL — два плеча,
    а не противоречие. Строки отличаются только маршрутом/временем и стыкуются."""
    for i, a in enumerate(merged):
        for b in merged[i + 1:]:
            for c in strict:
                if c in LEG_FIELDS:
                    continue
                if (not ticket_eq(a[c], b[c])) if c == "e_ticket" else not _eq(a[c], b[c]):
                    return False
    ok = set()
    for i, a in enumerate(merged):
        for j, b in enumerate(merged):
            if i != j and a["to_airport"] and a["to_airport"] == b["from_airport"] \
                    and a["from_airport"] != b["to_airport"]:
                ok |= {i, j}
    return len(ok) == len(merged)


def _merge_group(rows, stats=None):
    """rows: список dict одного (человек, рейс, дата), уже по приоритету источника."""
    strict = [c for c in SEG_STRICT if c not in SOFT_FIELDS]
    merged = []
    for r in rows:
        for m in merged:
            if all((ticket_eq(m[c], r[c]) if c == "e_ticket" else _eq(m[c], r[c]))
                   for c in strict):
                for c in SEG_COLS:
                    if c == "flags":
                        continue
                    if c == "e_ticket" and r[c] and m[c] and ticket_is_damaged(m[c]) \
                            and not ticket_is_damaged(r[c]):
                        m[c] = r[c]                 # полный номер вместо '7,33E+15'
                    elif not m[c] and r[c]:
                        m[c] = r[c]
                    elif c in SOFT_FIELDS and r[c] and r[c] not in m[c].split(" | "):
                        m[c] = m[c] + " | " + r[c]
                m["_flags"] |= r["_flags"]
                m["_protos"].add(r["_proto"])
                break
        else:
            r = dict(r)
            r["_protos"] = {r["_proto"]}
            merged.append(r)
    if len(merged) > 1:
        legs = _is_multi_leg(merged, strict)
        for m in merged:
            m["_flags"].add("multi_leg_flight" if legs else "conflict")
        if stats is not None and legs:
            stats["рейсов с несколькими плечами (multi_leg_flight)"] += 1
        if stats is not None and not legs:     # какие поля мешают склейке
            base = merged[0]
            for m in merged[1:]:
                for c in strict:
                    differ = (not ticket_eq(base[c], m[c])) if c == "e_ticket" else (
                        base[c] and m[c] and base[c] != m[c])
                    if differ:
                        stats[f"конфликт по полю: {c}"] += 1
                        ex = CONFLICT_EXAMPLES[c]
                        if len(ex) < 8:
                            ex.append(f"{base['flight']} {base['depart_date']}: "
                                      f"{base['from_airport'] or '?'}→{base['to_airport'] or '?'}"
                                      f"  «{base[c]}» vs «{m[c]}»")
    return merged


def _enrich(r, airports, tt, stats):
    fl = r["_flags"]
    for side in ("from", "to"):
        code, city = r[f"{side}_airport"], r[f"{side}_city"]
        if not code and city:
            codes = airports.codes_for_city(city)
            if len(codes) == 1:
                r[f"{side}_airport"] = next(iter(codes))
                fl.add(f"{side}_airport_by_city")
    if tt is not None:
        def codes(side):
            if r[f"{side}_airport"]:
                return {r[f"{side}_airport"]}
            return airports.codes_for_city(r[f"{side}_city"]) or None
        fc, tc = codes("from"), codes("to")
        # восстановить отсутствующий аэропорт по расписанию (BoardingData без пункта вылета)
        for side, mine, other in (("from", fc, tc), ("to", tc, fc)):
            if not r[f"{side}_airport"]:
                pos = 0 if side == "from" else 1
                cands = {e[pos] for e in tt.idx.get(r["flight"], [])
                         if (not other or e[1 - pos] in other) and (not mine or e[pos] in mine)}
                if len(cands) == 1:
                    r[f"{side}_airport"] = next(iter(cands))
                    fl.add(f"{side}_airport_by_timetable")
        fc, tc = codes("from"), codes("to")
        status, ents = tt.check(r["flight"], r["depart_date"], fc, tc)
        r["timetable_check"] = status
        stats[f"timetable_check: {status}"] += 1
        if status == "confirmed":
            deps = {e[5] for e in ents}
            arrs = {(e[6], e[7]) for e in ents}
            acs = {e[8] for e in ents}
            if not r["depart_time"] and len(deps) == 1:
                r["depart_time"] = next(iter(deps))
                fl.add("time_by_timetable")
            if not r["arrival_time"] and len(arrs) == 1 and len(deps) == 1:
                at, plus = next(iter(arrs))
                r["arrival_time"] = at
                r["arrival_date"] = (date.fromisoformat(r["depart_date"])
                                     + timedelta(days=plus)).isoformat()
                fl.add("time_by_timetable")
            if len(acs) == 1:
                r["aircraft"] = tt.aircraft_name(next(iter(acs)))
    for side in ("from", "to"):
        code = r[f"{side}_airport"]
        if code:
            r[f"{side}_city"] = airports.city(code) or r[f"{side}_city"]
            r[f"{side}_country"] = airports.country(code)
        elif r[f"{side}_city"]:
            c = r[f"{side}_city"]
            r[f"{side}_city"] = c.title() if c.isupper() else c


def write_output(store, persons, owner, by_card, airports, tt, out_path):
    stats = store.stats
    store.flush()
    db = store.db
    db.execute("CREATE TABLE pmap (proto INTEGER PRIMARY KEY, pnum INTEGER)")
    pnum = {id(P): i for i, P in enumerate(persons)}
    db.executemany("INSERT INTO pmap VALUES (?, ?)",
                   ((pr, pnum[id(P)]) for pr, P in owner.items()))
    db.commit()

    dropped = db.execute("SELECT COUNT(*) FROM seg WHERE proto NOT IN (SELECT proto FROM pmap)"
                         ).fetchone()[0]
    stats["сегментов отброшено (нет ФИО, карта не сопоставилась)"] = dropped

    shared = {t for (t,) in db.execute(
        "SELECT s.e_ticket FROM seg s JOIN pmap m ON s.proto = m.proto "
        "WHERE s.e_ticket IS NOT NULL AND s.e_ticket NOT LIKE '%E+%' AND s.e_ticket NOT LIKE '%e+%' "
        "GROUP BY s.e_ticket HAVING COUNT(DISTINCT m.pnum) > 1")}
    stats["билетов у нескольких разных людей (ticket_shared)"] = len(shared)

    cols = ", ".join(f"s.{c}" for c in SEG_COLS)
    cur = db.execute(
        f"SELECT m.pnum, s.proto, {cols} FROM seg s JOIN pmap m ON s.proto = m.proto "
        f"ORDER BY m.pnum, s.depart_date, s.flight, s.prio")

    written = 0
    in_rows = 0
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(OUT_COLS)

        def flush_person(pn, rows):
            nonlocal written
            P = persons[pn]
            rows.sort(key=lambda r: (r["depart_date"], r["depart_time"] or "", r["flight"]))
            for r in rows:
                fl = r["_flags"]
                if r["e_ticket"] in shared:
                    fl.add("ticket_shared")
                if r["_protos"] & by_card:
                    fl.add("name_restored_by_card")
                fl |= P.flags
                for x in fl:
                    stats[f"флаг: {x}"] += 1
                w.writerow([
                    P.id, P.last, P.first, P.middle or "",
                    " | ".join(sorted(P.originals)), P.gender or "", out_date(P.birth),
                    " | ".join(sorted(P.docs.values())), " | ".join(sorted(P.cards)),
                    " | ".join(f"{c}={s}" for c, s in sorted(P.card_status.items())),
                    P.nickname or "", P.uid or "",
                    r["flight"], r["codeshare"] or "", out_date(r["depart_date"]),
                    r["depart_time"] or "", out_date(r["arrival_date"]), r["arrival_time"] or "",
                    r["from_airport"] or "", r["from_city"] or "", r.get("from_country") or "",
                    r["to_airport"] or "", r["to_city"] or "", r.get("to_country") or "",
                    r["travel_class"] or "", r["fare"] or "", r["ff_card_used"] or "",
                    r["pnr"] or "", r["e_ticket"] or "", r["seat"] or "", r["meal"] or "",
                    r["baggage_allowance"] or "", r["baggage_status"] or "", r["agent"] or "",
                    r["flight_status"] or "", r.get("aircraft") or "", r["sirena_mark"] or "",
                    r.get("timetable_check") or "", " | ".join(sorted(fl)),
                ])
                written += 1

        cur_p, cur_key, group, prow = None, None, [], []
        for rec in cur:
            in_rows += 1
            if in_rows % 500000 == 0:
                print(f"  склейка: {in_rows:,} сегментов".replace(",", " "), file=sys.stderr,
                      flush=True)
            pn, proto = rec[0], rec[1]
            r = dict(zip(SEG_COLS, rec[2:]))
            r["_proto"] = proto
            r["_flags"] = set(r["flags"].split(" | ")) if r["flags"] else set()
            key = (pn, r["flight"], r["depart_date"])
            if key != cur_key and group:
                for m in _merge_group(group, stats):
                    _enrich(m, airports, tt, stats)
                    prow.append(m)
                group = []
            if pn != cur_p and cur_p is not None:
                flush_person(cur_p, prow)
                prow = []
            cur_p, cur_key = pn, key
            group.append(r)
        if group:
            for m in _merge_group(group, stats):
                _enrich(m, airports, tt, stats)
                prow.append(m)
        if prow:
            flush_person(cur_p, prow)

    stats["сегментов на входе склейки"] = in_rows
    stats["строк в итоговом CSV"] = written
    return written
