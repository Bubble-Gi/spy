"""Нормализация значений: пропуски, даты, имена (транслитерация), карты, билеты."""
import re
from datetime import date, datetime, time

NULLS = {"", "N/A", "NA", "NONE", "NOT PRESENTED", "NULL", "NAN", "-", "#N/A"}


def clean(v):
    """Строка без пробелов по краям или None, если значение пустое/заглушка."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip()
    if s.upper() in NULLS:
        return None
    return s

_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})")
_SLASH = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_DOTS = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")


def _mk(y, m, d):
    try:
        return date(int(y), int(m), int(d)).isoformat()
    except ValueError:
        return None


def parse_date(v, slash_order="MDY"):
    """Любой поддерживаемый формат -> ISO 'YYYY-MM-DD' (внутренний формат) или None.

    slash_order: как читать 'a/b/YYYY' ('MDY' — американский, как в BoardingData).
    Если по основному порядку дата невозможна (13/02/1990), пробуем обратный.
    """
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = clean(v)
    if not s:
        return None
    m = _ISO.match(s)
    if m:
        return _mk(*m.groups())
    m = _SLASH.match(s)
    if m:
        a, b, y = m.groups()
        first = _mk(y, a, b) if slash_order == "MDY" else _mk(y, b, a)
        return first or (_mk(y, b, a) if slash_order == "MDY" else _mk(y, a, b))
    m = _DOTS.match(s)
    if m:
        d, mo, y = m.groups()
        return _mk(y, mo, d)
    return None


def parse_time(v):
    """'6:05' / '06:05' / time -> 'HH:MM' или None."""
    if v is None:
        return None
    if isinstance(v, datetime):
        v = v.time()
    if isinstance(v, time):
        return f"{v.hour:02d}:{v.minute:02d}"
    s = clean(v)
    if not s:
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})", s)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


def out_date(iso):
    """ISO -> 'ДД.ММ.ГГГГ' для выходного файла."""
    if not iso:
        return ""
    y, m, d = iso.split("-")
    return f"{d}.{m}.{y}"



_CYR = re.compile(r"[А-Яа-яЁё]")


_ICAO = {
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Е": "E", "Ё": "E",
    "Ж": "ZH", "З": "Z", "И": "I", "Й": "I", "К": "K", "Л": "L", "М": "M",
    "Н": "N", "О": "O", "П": "P", "Р": "R", "С": "S", "Т": "T", "У": "U",
    "Ф": "F", "Х": "KH", "Ц": "TS", "Ч": "CH", "Ш": "SH", "Щ": "SHCH",
    "Ъ": "IE", "Ы": "Y", "Ь": "", "Э": "E", "Ю": "IU", "Я": "IA",
}

_VOWELS = "AEIOU"


def has_cyrillic(s):
    return bool(s and _CYR.search(s))


def cyr_to_icao(s):
    out = []
    for ch in s.upper():
        out.append(_ICAO.get(ch, ch))
    return "".join(out)


def latin_to_icao(word):
    """Приводит латиницу из других систем транслитерации к виду ИКАО.

    LIDIYA -> LIDIIA, YURY -> IURII, SERGEY -> SERGEI, IGNATYEVA -> IGNATEVA.
    Строки, уже записанные по ИКАО, не меняются.
    """
    w = word.upper()
    w = re.sub(r"TC(?!H)", "TS", w)          # старые паспорта: Ц = TC
    w = w.replace("YA", "IA").replace("YU", "IU").replace("YO", "E").replace("YE", "E")
    # Й: Y после гласной (перед согласной или в конце слова) -> I
    w = re.sub(r"(?<=[AEIOU])Y(?=[^AEIOU]|$)", "I", w)
    # -ИЙ, записанное одной Y: YURY, DMITRY, -SKY -> IURII, DMITRII, -SKII
    w = re.sub(r"(?<=[^AEIOU])Y$", "II", w)
    return w


def to_latin_name(s):
    """Любое написание имени -> ВЕРХНИЙ регистр, латиница ИКАО."""
    s = clean(s)
    if not s:
        return None
    s = re.sub(r"[’'`ʹ]", "", s)            # LUK'YANOVA -> LUKYANOVA
    s = re.sub(r"\s+", " ", s)
    if has_cyrillic(s):
        return cyr_to_icao(s)
    return " ".join(latin_to_icao(w) if not w.endswith(".") else w.upper()
                    for w in s.split(" "))


def canon(s):
    """Ключ для СРАВНЕНИЯ имён: схлопывает варианты транслитерации."""
    if not s:
        return ""
    s = to_latin_name(s) or ""
    s = re.sub(r"[^A-Z]", "", s.upper())
    s = s.replace("KH", "H").replace("X", "KS").replace("J", "I")
    s = s.replace("YA", "IA").replace("YU", "IU").replace("YE", "E").replace("YO", "E")
    s = s.replace("Y", "I")
    return s


def is_initial(s):
    return bool(s) and bool(re.fullmatch(r"[A-ZА-ЯЁ]{1,2}\.?", s.strip().upper())) \
        and (s.strip().endswith(".") or len(s.strip()) == 1)


def split_full_name(full, order="LFM"):
    """'ФАМИЛИЯ ИМЯ ОТЧЕСТВО' (LFM) или 'ИМЯ [ОТЧ] ФАМИЛИЯ' (FML) -> (last, first, middle)."""
    full = clean(full)
    if not full:
        return None, None, None
    t = full.split()
    if len(t) == 1:
        return t[0], None, None
    if order == "LFM":
        return t[0], t[1], (" ".join(t[2:]) or None)
    return t[-1], t[0], (" ".join(t[1:-1]) or None)


def gender_of(v):
    s = (clean(v) or "").upper()
    if s in ("M", "MALE", "MR", "MSTR"):
        return "M"
    if s in ("F", "FEMALE", "MRS", "MS", "MISS"):
        return "F"
    return None




_CARD = re.compile(r"^\s*([A-Z0-9]{2})\s*#?\s*(\d+)\s*$")


def norm_card(prog, number=None):
    """('FB', ' 171388778') или 'FB 171388778' -> 'FB 171388778'. Нули не дополняются."""
    if number is None:
        s = clean(prog)
        if not s:
            return None
        m = _CARD.match(s.upper())
        if not m:
            return None
        prog, number = m.groups()
    prog = (clean(prog) or "").upper()
    number = re.sub(r"\s+", "", clean(number) or "")
    if not prog or not number:
        return None
    return f"{prog} {number}"


def doc_key(doc):
    """Документ для сравнения: только буквы/цифры. В CSV пишется как в источнике."""
    return re.sub(r"[^0-9A-Za-zА-Яа-я]", "", doc).upper() if doc else None


def ticket_suspicious(t):
    return bool(t) and not re.fullmatch(r"\d{16}", t)


_SCI = re.compile(r"^\s*(\d)[.,](\d+)\s*[Ee]\+?(\d+)\s*$")


def ticket_is_damaged(t):
    """'7,33391E+15' — номер, который Excel превратил в число и обрезал."""
    return bool(t) and bool(_SCI.match(t))


def ticket_eq(a, b):
    """Равны ли билеты с учётом испорченных: '7,33391E+15' == '7333908401787617'."""
    if not a or not b or a == b:
        return True
    da, db_ = ticket_is_damaged(a), ticket_is_damaged(b)
    if da and db_:
        return False
    if da or db_:
        sci, full = (a, b) if da else (b, a)
        if not full.isdigit():
            return False
        m = _SCI.match(sci)
        mant = m.group(1) + m.group(2)
        exp = int(m.group(3))
        digits = str(int(full))                    # ведущие нули Excel тоже теряет
        if len(digits) != exp + 1:
            return False
        rounded = str(round(int(digits), -(len(digits) - len(mant))))[:len(mant)]
        return rounded == mant
    return False


def norm_flight(f):
    f = clean(f)
    return re.sub(r"\s+", "", f).upper() if f else None


def norm_airport(code, flags=None):
    """IATA-код; 'rea'/'REA' (Сеул) -> 'ICN' с флагом."""
    c = clean(code)
    if not c:
        return None
    c = c.upper()
    if c == "REA":
        if flags is not None:
            flags.add("airport_restored")
        return "ICN"
    return c
