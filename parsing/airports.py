"""Справочник аэропортов: код -> (город, страна) и город -> коды.

Наполняется из расписания SkyTeam, форума и посадочных талонов.
Источник с меньшим приоритетом не перезаписывает уже известное значение.
"""
import bisect
import re

COUNTRY_FIX = {
    "UNITED STATES OF": "United States of America",
    "UNITED STATES": "United States of America",
    "USA": "United States of America",
}
CITY_FIX = {
    "BIRMINGHAM AI": "Birmingham AL",
}


def norm_country(c):
    if not c:
        return None
    c = re.sub(r"\s+", " ", c).strip(" ,")
    return COUNTRY_FIX.get(c.upper(), c) or None


def norm_city(c):
    if not c:
        return None
    c = re.sub(r"\s+", " ", c).strip(" ,")
    c = re.sub(r"\s+,", ",", c)
    if not c:
        return None
    c = CITY_FIX.get(c.upper(), c)
    if c.isupper():           # VLADIVOSTOK -> Vladivostok
        c = c.title()
    return c


class AirportRef:
    def __init__(self):
        self.by_code = {}       # code -> [city, country, prio]
        self.by_city = {}       # CITY (upper) -> set(codes)
        self._sorted = None

    def add(self, code, city=None, country=None, prio=9):
        if not code or not re.fullmatch(r"[A-Z0-9]{3}", code):
            return
        city, country = norm_city(city), norm_country(country)
        cur = self.by_code.get(code)
        if cur is None:
            self.by_code[code] = [city, country, prio]
        else:
            if city and (not cur[0] or prio < cur[2]):
                cur[0] = city
            if country and (not cur[1] or prio < cur[2]):
                cur[1] = country
            cur[2] = min(cur[2], prio)
        if city:
            self.by_city.setdefault(city.upper(), set()).add(code)
            self._sorted = None

    def city(self, code):
        r = self.by_code.get(code)
        return r[0] if r else None

    def country(self, code):
        r = self.by_code.get(code)
        return r[1] if r else None

    def codes_for_city(self, city):
        """Все коды города. 'Yuzhno' (обрезанное) ищется по префиксу."""
        if not city:
            return set()
        key = city.upper().strip()
        if key in self.by_city:
            return set(self.by_city[key])
        # по первому слову ('MOSCOW' при 'Moscow, Russian Federation' и т. п.)
        if len(key) >= 4:
            if self._sorted is None:
                self._sorted = sorted(self.by_city)
            i = bisect.bisect_left(self._sorted, key)
            found = set()
            while i < len(self._sorted) and self._sorted[i].startswith(key):
                found |= self.by_city[self._sorted[i]]
                i += 1
            return found
        return set()
