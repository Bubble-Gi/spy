"""Промежуточное хранилище.

Люди: «прото-персоны» в памяти — уникальные наборы идентификационных полей
(у Pointz-пользователя с 50 перелётами одна прото-персона, а не 50).
Сегменты: таблица SQLite на диске — их миллионы, в память не грузим.
"""
import os
import sqlite3
from collections import Counter

from .normalize import canon, clean, doc_key, is_initial, split_full_name, to_latin_name

SEG_COLS = [
    "flight", "codeshare", "depart_date", "depart_time", "arrival_date", "arrival_time",
    "from_airport", "from_city", "to_airport", "to_city",
    "travel_class", "fare", "ff_card_used", "pnr", "e_ticket", "seat", "meal",
    "baggage_allowance", "baggage_status", "agent", "flight_status", "sirena_mark",
    "flags",
]


PRIO = {"sirena": 1, "boarding_data": 2, "boarding_pass": 3, "pointz": 4,
        "forum": 5, "exchange": 6}


_NO_DICT = {}
_NO_SET = frozenset()


class Proto:
    __slots__ = ("pid", "last", "first", "middle", "lastc", "firstc", "middlec",
                 "middle_init", "gender", "birth", "docs", "cards", "card_status",
                 "nickname", "uid", "originals", "prio", "card_only")

    def completeness(self):
        return sum(bool(x) for x in (self.birth, self.docs, self.gender, self.cards,
                                     self.nickname, self.uid)) \
            + (2 if self.middle and not self.middle_init else 1 if self.middle else 0)


class Store:
    def __init__(self, db_path):
        if os.path.exists(db_path):
            os.remove(db_path)
        self.db_path = db_path
        self.db = sqlite3.connect(db_path)
        self.db.executescript("""
            PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
            PRAGMA temp_store=FILE; PRAGMA cache_size=-200000;
        """)
        cols = ", ".join(f"{c} TEXT" for c in SEG_COLS)
        self.db.execute(f"CREATE TABLE seg (proto INTEGER, prio INTEGER, {cols})")
        self._ins = (f"INSERT INTO seg (proto, prio, {', '.join(SEG_COLS)}) VALUES "
                     f"({', '.join('?' * (len(SEG_COLS) + 2))})")
        self.buf = []
        self.batch = 50000
        self.protos = []
        self.keys = {}
        self.stats = Counter()
        # имена из источников, где порядок полей известен, — подсказка для талонов
        self.first_names = Counter()
        self.last_names = Counter()

    def split_ambiguous_name(self, full):
        """'LIDIYA ZHDANOVA' и 'ANDRIANOV YAROMIR A' в одной колонке талона:
        порядок определяем по тому, какие слова встречались как имена, а какие как фамилии."""
        full = clean(full)
        if not full:
            return None, None, None
        t = full.split()
        if len(t) >= 3 and is_initial(t[-1]):
            return split_full_name(full, "LFM")          # ФАМИЛИЯ ИМЯ И
        if len(t) < 2:
            return split_full_name(full, "FML")
        f, l_ = self.first_names, self.last_names
        a, b = canon(t[0]), canon(t[-1] if len(t) == 2 else t[1])
        fml = (f[a] > 0) + (l_[canon(t[-1])] > 0)        # ИМЯ ... ФАМИЛИЯ
        lfm = (f[b] > 0) + (l_[a] > 0)                   # ФАМИЛИЯ ИМЯ ...
        if lfm > fml or (lfm == fml and f[b] + l_[a] > f[a] + l_[canon(t[-1])]):
            return split_full_name(full, "LFM")
        return split_full_name(full, "FML")


    def proto_id(self, source, last=None, first=None, middle=None, name_orig=None,
                 gender=None, birth=None, docs=(), cards=(), card_status=None,
                 nickname=None, uid=None):
        last, first = to_latin_name(last), to_latin_name(first)
        middle = to_latin_name(middle)
        docs = {doc_key(d): clean(d) for d in docs if clean(d)} or _NO_DICT
        cards = frozenset(c for c in cards if c)
        lastc, firstc = canon(last), canon(first)
        mid_init = bool(middle) and is_initial(middle)
        middlec = canon(middle)
        if not (lastc or firstc or docs or gender or birth or nickname or uid) and len(cards) == 1:
            key = next(iter(cards))                  # только карта (SkyTeam-Exchange)
        else:
            key = (lastc, firstc, middlec, mid_init, gender, birth,
                   frozenset(docs), cards, nickname, uid)
        if lastc and firstc and source != "boarding_pass":
            self.first_names[firstc] += 1
            self.last_names[lastc] += 1
        pid = self.keys.get(key)
        if pid is not None:
            p = self.protos[pid]
            if name_orig and name_orig not in p.originals:
                p.originals = set(p.originals) | {name_orig}
            if card_status:
                p.card_status = {**p.card_status, **card_status}
            if PRIO[source] < p.prio:
                p.prio, p.last, p.first, p.middle = PRIO[source], last, first, middle
            return pid
        p = Proto()
        p.pid = len(self.protos)
        p.last, p.first, p.middle = last, first, middle
        p.lastc, p.firstc, p.middlec, p.middle_init = lastc, firstc, middlec, mid_init
        p.gender, p.birth, p.docs, p.cards = gender, birth, docs, cards
        p.card_status = dict(card_status) if card_status else _NO_DICT
        p.nickname, p.uid = nickname, uid
        p.originals = frozenset((name_orig,)) if name_orig else _NO_SET
        p.prio = PRIO[source]
        p.card_only = not (lastc and firstc)
        self.protos.append(p)
        self.keys[key] = p.pid
        return p.pid


    def add_segment(self, proto, source, seg):
        flags = seg.get("flags")
        if isinstance(flags, (set, list, tuple)):
            seg["flags"] = " | ".join(sorted(flags)) if flags else None
        self.buf.append((proto, PRIO[source], *[seg.get(c) for c in SEG_COLS]))
        self.stats[f"{source}: сегментов принято"] += 1
        if len(self.buf) >= self.batch:
            self.flush()

    def flush(self):
        if self.buf:
            self.db.executemany(self._ins, self.buf)
            self.db.commit()
            self.buf = []

    def close(self, keep=False):
        self.flush()
        self.db.close()
        if not keep and os.path.exists(self.db_path):
            os.remove(self.db_path)
