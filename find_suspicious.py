import argparse
import json
import re
import time
import unicodedata
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd


CONFIG = {
    "sep": None,  # None = detect tab / ; / ,
    "output_sep": ";",
    "date_format": "%d.%m.%Y",
    "ticket_regex": r"\d{16}",
    "birth_day_month_swap_same": True,
    # For check 1, same passport + dates differing only by day/month swap:
    # "none" = no finding if surname is also compatible; or low/medium/high.
    "birth_swap_level": "none",
    "teleport_max_days": 2,
    "feminine_surname_same": True,
    "max_age": 100,
    "max_group": 50,  # larger shared groups are treated as placeholders
    "ticket_name_only_level": "medium",
    "timetable_not_found_values": ["not_found", "not_found_out_of_period"],
    "timetable_wrong_day_values": ["wrong_day"],
    "timetable_skip_codeshare": True,
    "timetable_min_airline_coverage": 0.95,
    "timetable_min_airline_rows": 200,
    "suspicious_statuses": ["CANCELLED", "CANCELED"],
    "freq_percentile": 0.99,
    "freq_month_min": 6,
    "freq_total_min": 20,
    "rare_airport_max_people": 3,
    "cotravel_min_flights": 3,
    "cotravel_max_pax": 30,
    "downgrade_ambiguous": False,
    "ambiguous_share": 0.5,
    "ambiguous_checks": ["2", "4", "7", "10"],
    "spy_subchecks": ["1"],  # a high finding in these checks = spy (own file spies.csv, not in suspicious_high.csv)
    "high_min_medium": 3,
    "high_min_medium_checks": 3,
    "low_min_low": 2,
    "low_min_low_checks": 2,
}

COLUMNS = {
    "person_id": "person_id",
    "last": "last_name",
    "first": "first_name",
    "middle": "middle_name",
    "gender": "gender",
    "birth": "birth_date",
    "doc": "travel_doc",
    "ff_cards": "ff_cards",
    "flight": "flight",
    "codeshare": "codeshare",
    "date": "depart_date",
    "time": "depart_time",
    "arr_date": "arrival_date",
    "arr_time": "arrival_time",
    "frm": "from_airport",
    "to": "to_airport",
    "cls": "travel_class",
    "fare": "fare",
    "pnr": "pnr",
    "ticket": "e_ticket",
    "status": "flight_status",
    "tt": "timetable_check",
    "flags": "flags",
}

STR_COLS = {"ticket", "pnr"}
LEVEL_RANK = {"low": 1, "medium": 2, "high": 3}
RANK_LEVEL = {v: k for k, v in LEVEL_RANK.items()}
MISSING_DOC_TOKENS = {
    "NA", "NAN", "NONE", "NULL", "UNKNOWN", "NOTAVAILABLE", "NOTPROVIDED"
}


def sniff(path, cfg):
    enc = "utf-8-sig"
    with open(path, "rb") as f:
        head = f.read(2_000_000)
    try:
        head.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        # Ignore a possible partial multibyte character at the end of the sample.
        if exc.start < len(head) - 4:
            enc = "cp1251"
    first = head.split(b"\n", 1)[0].decode(enc, errors="replace")
    sep = cfg["sep"] or max(["\t", ";", ","], key=first.count)
    return enc, sep


def read_input(path, cfg):
    enc, sep = sniff(path, cfg)
    header = pd.read_csv(path, sep=sep, nrows=0, encoding=enc).columns
    missing = [name for name in COLUMNS.values() if name not in header]
    use = [name for name in COLUMNS.values() if name in header]
    inverse = {value: key for key, value in COLUMNS.items()}
    dtypes = {name: ("str" if inverse[name] in STR_COLS else "category") for name in use}
    df = pd.read_csv(
        path,
        sep=sep,
        encoding=enc,
        usecols=use,
        dtype=dtypes,
        keep_default_na=False,
        na_values=[""],
        encoding_errors="replace",
        low_memory=False,
    ).rename(columns=inverse)
    for key in COLUMNS:
        if key not in df:
            df[key] = pd.Series(np.nan, index=df.index, dtype="object")
    return df, {
        "separator": repr(sep),
        "encoding": enc,
        "missing_columns": ", ".join(missing) or "none",
    }


def codes_cats(series):
    if not isinstance(series.dtype, pd.CategoricalDtype):
        series = series.astype("category")
    return series.cat.codes.to_numpy(), series.cat.categories


def map_cat(series, func, dtype=object, na=None):
    """Apply func once per distinct categorical value; code -1 maps to na."""
    codes, categories = codes_cats(series)
    values = np.empty(len(categories) + 1, dtype=dtype)
    for i, category in enumerate(categories):
        values[i] = func(category)
    values[-1] = na
    return values[codes]


def to_date(series, fmt):
    codes, categories = codes_cats(series)
    parsed = pd.to_datetime(
        pd.Series(categories.astype(str)), format=fmt, errors="coerce"
    ).to_numpy(dtype="datetime64[ns]")
    all_values = np.append(parsed, np.datetime64("NaT", "ns"))
    return pd.Series(all_values[codes], index=series.index)


def to_minutes(series):
    def parse(value):
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value).strip())
        return int(match.group(1)) * 60 + int(match.group(2)) if match else np.nan

    return pd.Series(map_cat(series, parse, float, np.nan), index=series.index)


def sv(series):
    """Convert a Series to text, rendering missing values as empty strings."""
    return series.astype(object).where(series.notna(), "").astype(str)


def split_multi(value):
    if value is None or value is pd.NA or (isinstance(value, float) and np.isnan(value)):
        return []
    result = []
    for part in re.split(r"\s*\|\s*", str(value)):
        cleaned = " ".join(part.split())
        if cleaned:
            result.append(cleaned)
    return result


def normalize_passport(value):
    """Normalize passport formatting without restricting its format or length.

    Leading zeroes are deliberately preserved. A separate check detects possible
    numeric matches that differ only by leading zeroes.
    """
    if value is None or pd.isna(value):
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip().upper()
    key = "".join(char for char in text if char.isalnum())
    if not key or key in MISSING_DOC_TOKENS:
        return None
    return key


def passport_family_key(value):
    """Comparison key for detecting likely same numeric passport with lost zeroes."""
    if value is None or pd.isna(value):
        return None
    key = str(value)
    if key.isdigit():
        return key.lstrip("0") or "0"
    return key


def collect_person_documents(person_codes, values, n_persons):
    """Collect raw and normalized passport tokens from every row per person.

    Returns:
        display_values: pipe-separated original representations per person
        raw_values: sorted distinct raw representations per person
        normalized_values: sorted distinct normalized passport keys per person
    """
    value_codes, categories = codes_cats(values)
    valid = value_codes >= 0
    pairs = pd.DataFrame({
        "person": np.asarray(person_codes, dtype=np.int32)[valid],
        "value": value_codes[valid].astype(np.int32),
    }).drop_duplicates()

    raw_by_category = []
    normalized_by_category = []
    for category in categories:
        raw_tokens = sorted(set(split_multi(str(category))))
        normalized = {
            key for token in raw_tokens
            if (key := normalize_passport(token)) is not None
        }
        raw_by_category.append(raw_tokens)
        normalized_by_category.append(normalized)

    raw_by_person = [set() for _ in range(n_persons)]
    docs_by_person = [set() for _ in range(n_persons)]
    for person, value_code in pairs.itertuples(index=False, name=None):
        raw_by_person[person].update(raw_by_category[value_code])
        docs_by_person[person].update(normalized_by_category[value_code])

    raw_values = [sorted(items) for items in raw_by_person]
    normalized_values = [sorted(items) for items in docs_by_person]
    display_values = [" | ".join(items) for items in raw_values]
    return display_values, raw_values, normalized_values


def collect_person_multi_values(person_codes, values, n_persons):
    """Collect distinct tokens from a multi-value column across all person rows."""
    value_codes, categories = codes_cats(values)
    valid = value_codes >= 0
    pairs = pd.DataFrame({
        "person": np.asarray(person_codes, dtype=np.int32)[valid],
        "value": value_codes[valid].astype(np.int32),
    }).drop_duplicates()
    tokens_by_category = [set(split_multi(str(category))) for category in categories]
    tokens_by_person = [set() for _ in range(n_persons)]
    for person, value_code in pairs.itertuples(index=False, name=None):
        tokens_by_person[person].update(tokens_by_category[value_code])
    return [" | ".join(sorted(tokens)) for tokens in tokens_by_person]


def surname_key(value, cfg):
    if not isinstance(value, str):
        return None
    key = re.sub(r"[^A-Z]", "", value.upper())
    if cfg["feminine_surname_same"] and len(key) > 4 and key.endswith(("OVA", "EVA", "INA", "YNA")):
        key = key[:-1]
    return key or None


def strict_name_key(value):
    """Normalize a name for exact identity comparison without merging surname variants."""
    if value is None or pd.isna(value):
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip().upper()
    if text in {"", "-", "N/A", "NA", "NAN", "NONE", "NULL", "UNKNOWN"}:
        return None
    key = "".join(char for char in text if char.isalnum())
    if not key or key in MISSING_DOC_TOKENS:
        return None
    return key


class Findings:
    def __init__(self):
        self.parts = []

    def add(self, subcheck, level, entity_ids, evidence, flight=None, date=None):
        entity_ids = np.asarray(entity_ids)
        if len(entity_ids) == 0:
            return
        self.parts.append(pd.DataFrame({
            "check": re.match(r"\d+", subcheck).group(),
            "subcheck": subcheck,
            "level": level,
            "eid": entity_ids,
            "flight": None if flight is None else np.asarray(flight, dtype=object),
            "date": pd.NaT if date is None else np.asarray(date),
            "evidence": np.asarray(evidence, dtype=object),
        }))

    def frame(self):
        if not self.parts:
            return pd.DataFrame(columns=[
                "check", "subcheck", "level", "eid", "flight", "date", "evidence"
            ])
        return pd.concat(self.parts, ignore_index=True)


def build_model(df, cfg, stats):
    df = df[df["person_id"].notna()].reset_index(drop=True)
    person_codes, person_categories = codes_cats(df["person_id"])
    n_persons = len(person_categories)
    stats["rows"] = len(df)
    stats["persons"] = n_persons

    P = pd.DataFrame({"person_id": person_categories.astype(str)})
    for key in ["last", "first", "middle", "gender", "birth", "doc", "ff_cards"]:
        grouped = pd.Series(df[key].astype(object).to_numpy()).groupby(person_codes).first()
        P[key] = grouped.reindex(range(n_persons)).to_numpy()

    # Keep the first non-empty source-cell value for record-level consistency checks.
    # In parallel, collect every passport token across all flight rows for cross-person matching.
    P["doc_primary_raw"] = P["doc"].copy()
    P["docs_primary"] = P["doc_primary_raw"].map(
        lambda value: sorted({
            key for token in split_multi(value)
            if (key := normalize_passport(token)) is not None
        })
    )
    display_docs, raw_docs, normalized_docs = collect_person_documents(
        person_codes, df["doc"], n_persons
    )
    P["doc"] = display_docs
    P["docs_all"] = raw_docs
    P["docs"] = normalized_docs
    P["docs_family"] = P["docs"].map(
        lambda values: sorted({passport_family_key(value) for value in values})
    )
    P["docs_primary_family"] = P["docs_primary"].map(
        lambda values: sorted({passport_family_key(value) for value in values})
    )
    P["has_doc"] = P["docs"].map(bool)
    stats["persons_with_multiple_passport_values_across_rows"] = int(
        P["docs"].map(len).gt(1).sum()
    )
    stats["persons_with_multiple_distinct_passports_across_rows"] = int(
        P["docs_family"].map(len).gt(1).sum()
    )
    stats["persons_with_multiple_passports_in_primary_record"] = int(
        P["docs_primary_family"].map(len).gt(1).sum()
    )
    # Frequent-flyer cards can also vary between flight rows, so collect them all.
    P["ff_cards"] = collect_person_multi_values(person_codes, df["ff_cards"], n_persons)

    P["eid"] = np.arange(n_persons)
    P["last_key"] = [surname_key(value, cfg) for value in P["last"]]
    P["first_init"] = P["first"].map(
        lambda value: value[:1].upper() if isinstance(value, str) and value else None
    )
    P["mid_init"] = P["middle"].map(
        lambda value: value[:1].upper() if isinstance(value, str) and value else None
    )

    birth_values = P["birth"].map(split_multi)
    P["birth_dt"] = pd.to_datetime(
        birth_values.map(lambda items: items[0] if items else None),
        format=cfg["date_format"], errors="coerce"
    )
    P["multi_birth"] = birth_values.map(lambda items: len(set(items)) > 1)
    P["multi_gender"] = P["gender"].map(lambda value: len(set(split_multi(value))) > 1)
    birth_dt = P["birth_dt"]
    lo = np.minimum(birth_dt.dt.day, birth_dt.dt.month)
    hi = np.maximum(birth_dt.dt.day, birth_dt.dt.month)
    P["birth_strict"] = birth_dt.dt.year * 10000 + birth_dt.dt.month * 100 + birth_dt.dt.day
    P["birth_key"] = birth_dt.dt.year * 10000 + (
        lo * 100 + hi if cfg["birth_day_month_swap_same"]
        else birth_dt.dt.month * 100 + birth_dt.dt.day
    )

    stats["persons_with_doc"] = int(P["has_doc"].sum())
    stats["persons_with_multiple_passports"] = int(P["docs_family"].map(len).gt(1).sum())
    stats["distinct_normalized_passports"] = int(
        len({doc for documents in P["docs"] for doc in documents})
    )
    stats["birth_dates_unparsed"] = int(
        (P["birth"].notna() & P["birth_dt"].isna() & ~P["multi_birth"]).sum()
    )

    R = pd.DataFrame({"p": person_codes, "eid": person_codes})
    R["flight"] = df["flight"].astype(object)
    R["d"] = to_date(df["date"], cfg["date_format"])
    R["dep_min"] = to_minutes(df["time"])
    R["arr_d"] = to_date(df["arr_date"], cfg["date_format"])
    R["arr_min"] = to_minutes(df["arr_time"])
    R["frm"] = df["frm"].astype(object)
    R["to"] = df["to"].astype(object)
    R["cls"] = df["cls"].astype(object)
    R["fare"] = df["fare"].astype(object)
    R["pnr"] = df["pnr"]
    R["tkt"] = df["ticket"]
    R["status"] = df["status"].astype(object)
    R["ttc"] = df["tt"].astype(object)
    R["codeshare"] = df["codeshare"].astype(object)
    flags = df["flags"]
    R["amb"] = map_cat(flags, lambda value: "identity_ambiguous" in str(value), bool, False)
    R["conflict"] = map_cat(
        flags,
        lambda value: re.search(r"(^|\|\s*)conflict(\s*\||$)", str(value)) is not None,
        bool, False
    )
    R["multileg"] = map_cat(flags, lambda value: "multi_leg_flight" in str(value), bool, False)
    R["damaged"] = map_cat(flags, lambda value: "ticket_damaged" in str(value), bool, False)
    R["tkt_ok"] = R["tkt"].astype(object).map(
        lambda value: isinstance(value, str) and re.fullmatch(cfg["ticket_regex"], value) is not None
    ) & ~R["damaged"]

    stats["rows_with_ticket"] = int(R["tkt"].notna().sum())
    stats["rows_with_valid_ticket"] = int(R["tkt_ok"].sum())
    stats["rows_without_date"] = int(R["d"].isna().sum())
    stats["rows_without_flight"] = int(R["flight"].isna().sum())

    airports = sorted(pd.concat([R["frm"], R["to"]]).dropna().unique())
    airport_index = {airport: idx for idx, airport in enumerate(airports)}
    R["frm_i"] = R["frm"].map(airport_index).fillna(-1).astype(int)
    R["to_i"] = R["to"].map(airport_index).fillna(-1).astype(int)
    airport_names = np.array(airports + [""], dtype=object)

    ambiguous_share = R.groupby("eid")["amb"].mean().reindex(range(n_persons)).fillna(0)
    E = pd.DataFrame({
        "eid": P["eid"], "entity_id": P["person_id"], "last": P["last"],
        "first": P["first"], "middle": P["middle"], "birth": P["birth"],
        "doc": P["doc"], "ff_cards": P["ff_cards"],
        "ambiguous": (ambiguous_share >= cfg["ambiguous_share"]).to_numpy(),
    })
    return P, R, E, airport_names


def ent_text(P, idx):
    return f'{P["person_id"][idx]}:{P["last"][idx]} {P["first"][idx]} b={P["birth"][idx]}'


def check_passports(P, cfg, findings, stats):
    # Exact passport comparison after harmless formatting normalization.
    rows = [
        (idx, doc)
        for idx, documents in enumerate(P["docs"])
        for doc in documents
    ]
    D = pd.DataFrame(rows, columns=["p", "doc"]).drop_duplicates()
    stats["passport_zero_match_groups"] = 0
    stats["passport_zero_match_pairs"] = 0
    stats["passport_zero_groups_skipped_too_large"] = 0
    if D.empty:
        stats["doc_groups_skipped_too_large"] = 0
        stats["passports_shared_by_several_people"] = 0
        return

    counts = D.groupby("doc")["p"].transform("nunique")
    stats["doc_groups_skipped_too_large"] = int(D[counts > cfg["max_group"]]["doc"].nunique())
    shared = D[(counts >= 2) & (counts <= cfg["max_group"])].copy()
    stats["passports_shared_by_several_people"] = int(shared["doc"].nunique())

    if not shared.empty:
        person_idx = shared["p"].to_numpy()
        for key in ["last_key", "birth_key", "gender"]:
            shared[key] = P[key].to_numpy()[person_idx]
        shared["birth_dt"] = P["birth_dt"].to_numpy()[person_idx]
        shared["txt"] = [ent_text(P, idx) for idx in person_idx]
        grouped = shared.groupby("doc")
        surname_diff = grouped["last_key"].nunique() > 1
        birth_strict_diff = grouped["birth_dt"].nunique() > 1
        birth_canonical_diff = grouped["birth_key"].nunique() > 1
        swap_only = birth_strict_diff & ~birth_canonical_diff
        birth_diff = birth_canonical_diff if cfg["birth_day_month_swap_same"] else birth_strict_diff
        stats["passport_groups_birth_differs_only_by_day_month_swap"] = int(swap_only.sum())
        evidence_people = grouped["txt"].agg(" | ".join)

        both = surname_diff & birth_diff
        one = surname_diff ^ birth_diff
        level = pd.Series(
            cfg["birth_swap_level"] if cfg["birth_day_month_swap_same"] else "low",
            index=surname_diff.index, dtype=object
        )
        level[~swap_only] = "low"
        level[one] = "medium"
        level[both] = "high"

        reason = pd.Series(
            "neither surname nor birth date differ (possible duplicate or document reuse)",
            index=surname_diff.index, dtype=object
        )
        reason[swap_only & ~surname_diff] = "birth dates differ only by day/month swap (possible parsing artefact)"
        reason[birth_diff & ~surname_diff] = "birth date differs"
        reason[surname_diff & ~birth_diff] = "surname differs"
        reason[both] = "surname and birth date differ"

        # "none" for day/month-only differences deliberately suppresses that finding.
        for level_name in ["high", "medium", "low"]:
            documents = level[level == level_name].index
            selected = shared[shared["doc"].isin(documents)]
            if selected.empty:
                continue
            evidence = (
                "passport " + selected["doc"] + " at " + selected["doc"].map(reason)
                + ": " + selected["doc"].map(evidence_people)
            ).to_numpy()
            findings.add("1", level_name, selected["p"].to_numpy(), evidence)

        gender_count = grouped["gender"].nunique()
        selected = shared[shared["doc"].isin(gender_count[gender_count > 1].index)]
        findings.add(
            "8a", "medium", selected["p"].to_numpy(),
            ("passport " + selected["doc"] + ", different gender: "
             + selected["doc"].map(evidence_people)).to_numpy()
        )
    else:
        stats["passport_groups_birth_differs_only_by_day_month_swap"] = 0

    # Candidate match only: numeric values differing solely by leading zeroes.
    # These are not merged into the exact passport key and do not replace check 1.
    numeric = D[D["doc"].str.fullmatch(r"\d+")].copy()
    if numeric.empty:
        return
    numeric["zero_key"] = numeric["doc"].str.lstrip("0").replace("", "0")
    candidate_people = []
    candidate_evidence = []
    candidate_groups = 0
    candidate_pairs = 0
    skipped_large = 0

    for zero_key, group in numeric.groupby("zero_key", sort=False):
        if group["doc"].nunique() < 2:
            continue
        person_docs = {
            int(person): set(part["doc"])
            for person, part in group.groupby("p", sort=False)
        }
        if len(person_docs) < 2:
            continue
        if len(person_docs) > cfg["max_group"]:
            skipped_large += 1
            continue

        found_in_group = False
        people = sorted(person_docs.items())
        for (person_a, docs_a), (person_b, docs_b) in combinations(people, 2):
            # Only mark a pair if these people have no exact passport key in common.
            if not docs_a.isdisjoint(docs_b):
                continue
            found_in_group = True
            candidate_pairs += 1
            variants_a = ", ".join(sorted(docs_a))
            variants_b = ", ".join(sorted(docs_b))
            text_a = (
                f"possible passport match after removing leading zeroes; "
                f"zero-normalized key {zero_key}; own value(s) [{variants_a}], "
                f"other person {ent_text(P, person_b)} has [{variants_b}]"
            )
            text_b = (
                f"possible passport match after removing leading zeroes; "
                f"zero-normalized key {zero_key}; own value(s) [{variants_b}], "
                f"other person {ent_text(P, person_a)} has [{variants_a}]"
            )
            candidate_people.extend([person_a, person_b])
            candidate_evidence.extend([text_a, text_b])
        if found_in_group:
            candidate_groups += 1

    stats["passport_zero_match_groups"] = candidate_groups
    stats["passport_zero_match_pairs"] = candidate_pairs
    stats["passport_zero_groups_skipped_too_large"] = skipped_large
    findings.add("1b", "low", candidate_people, candidate_evidence)


def check_person_level(P, cfg, findings, stats):
    # A person having different passport values on different flight rows is not by itself
    # enough to declare a high-level anomaly: it can be a merge/source discrepancy.
    # Keep the original record-level 6a rule for multiple passports explicitly present
    # in the person's passport field, while all row-level values remain available to
    # checks 1, 5, 6b, and the leading-zero candidate check 1b.
    idx = np.flatnonzero(
        P["docs_primary_family"].map(lambda values: len(set(values)) >= 2).to_numpy()
    )
    findings.add(
        "6a", "high", idx,
        np.array([
            f'one source record contains several distinct passports: {", ".join(P["docs_primary"][i])}'
            for i in idx
        ], dtype=object),
    )

    # A cross-person 6a finding is high only when the FULL name is available
    # and matches exactly (including middle name), the strict birth dates match,
    # and the passport identifiers differ beyond leading-zero variants.
    # Do not use last_key here: it deliberately merges some surname variants
    # for check 1, which is too permissive for a high-level identity finding.
    K = P.copy()
    K["last_exact"] = K["last"].map(strict_name_key)
    K["first_exact"] = K["first"].map(strict_name_key)
    K["middle_exact"] = K["middle"].map(strict_name_key)
    K = K[
        K["birth_strict"].notna()
        & K["last_exact"].notna()
        & K["first_exact"].notna()
        & K["middle_exact"].notna()
        & K["docs"].map(bool)
    ].copy()
    # Use every distinct passport collected across this person's flight rows.
    K["passport_pairs"] = K["docs"].map(
        lambda values: [(value, passport_family_key(value)) for value in values]
    )
    K = K.explode("passport_pairs")
    K["doc_exact"] = K["passport_pairs"].map(lambda pair: pair[0])
    K["doc_family"] = K["passport_pairs"].map(lambda pair: pair[1])
    keys = ["last_exact", "first_exact", "middle_exact", "birth_strict"]
    matching = (
        (K.groupby(keys)["doc_family"].transform("nunique") >= 2)
        & (K.groupby(keys)["person_id"].transform("nunique") >= 2)
    )
    K = K[matching]
    if len(K):
        group_size = K.groupby(keys)["person_id"].transform("nunique")
        K = K[group_size <= cfg["max_group"]]
    stats_6a_groups = 0
    if len(K):
        passport_evidence = K.groupby(keys)["doc_exact"].agg(
            lambda values: " / ".join(sorted(set(values)))
        )
        K = K.drop_duplicates(keys + ["eid"])
        stats_6a_groups = len(passport_evidence)
        evidence = np.array([
            "same full name and exact birth date, different passports: "
            + passport_evidence[key]
            for key in zip(
                K["last_exact"], K["first_exact"], K["middle_exact"], K["birth_strict"]
            )
        ], dtype=object)
        # A matching full name and birth date across different person_id values can
        # describe namesakes or upstream identity fragmentation. Different passports
        # alone are not enough to make every such passenger HIGH; keep it as a
        # MEDIUM finding. HIGH remains reserved above for multiple distinct passport
        # identifiers present in one person's primary source record.
        findings.add("6a", "medium", K["eid"].to_numpy(), evidence)
    # These are candidate identity collisions, not confirmed high-level cases.
    stats["6a_same_full_name_birth_groups_medium"] = int(stats_6a_groups)
    stats["6a_same_full_name_birth_people_medium"] = int(K["eid"].nunique()) if len(K) else 0
    idx = np.flatnonzero(P["multi_gender"].to_numpy())
    findings.add(
        "8a", "medium", idx,
        np.array([f'conflicting gender values: {P["gender"][i]}' for i in idx], dtype=object),
    )

    card_rows = [
        (idx, card, doc, passport_family_key(doc))
        for idx, cards in enumerate(P["ff_cards"].map(split_multi))
        for card in cards
        for doc in P["docs"][idx]
    ]
    D = pd.DataFrame(card_rows, columns=["p", "card", "doc", "doc_family"]).drop_duplicates()
    card_evidence = []
    card_people = []
    if len(D):
        for card, group in D.groupby("card", sort=False):
            docs_by_person = {
                int(person): set(rows["doc_family"])
                for person, rows in group.groupby("p", sort=False)
            }
            if len(docs_by_person) > cfg["max_group"]:
                continue
            people = sorted(docs_by_person.items())
            for (person_a, docs_a), (person_b, docs_b) in combinations(people, 2):
                if not docs_a or not docs_b or not docs_a.isdisjoint(docs_b):
                    continue
                card_people.extend([person_a, person_b])
                card_evidence.extend([
                    f"FF card {card}: people have different passport sets; "
                    f"{ent_text(P, person_a)} passports [{', '.join(P['docs'][person_a])}], "
                    f"{ent_text(P, person_b)} passports [{', '.join(P['docs'][person_b])}]",
                    f"FF card {card}: people have different passport sets; "
                    f"{ent_text(P, person_b)} passports [{', '.join(P['docs'][person_b])}], "
                    f"{ent_text(P, person_a)} passports [{', '.join(P['docs'][person_a])}]",
                ])
    findings.add("6b", "medium", card_people, card_evidence)


def check_age(P, R, cfg, findings):
    flights = R[R["d"].notna()].groupby("p")["d"].agg(["min", "max"])
    birth = P["birth_dt"].reindex(flights.index)
    valid_birth = birth.notna()
    first_age = (flights["min"] - birth).dt.days / 365.25
    last_age = (flights["max"] - birth).dt.days / 365.25
    conditions = [
        (valid_birth & (first_age < 0), "flight before the birth date"),
        (valid_birth & (last_age > cfg["max_age"]), f'age over {cfg["max_age"]}'),
    ]
    for condition, description in conditions:
        indices = flights.index[condition.to_numpy()]
        evidence = np.array([
            f'{description}: birth {P["birth"][idx]}, flights '
            f'{flights.loc[idx, "min"]:%d.%m.%Y}..{flights.loc[idx, "max"]:%d.%m.%Y}'
            for idx in indices
        ], dtype=object)
        findings.add("8b", "medium", indices, evidence)


def check_tickets(P, R, cfg, findings, stats):
    T = R[R["tkt_ok"] & R["flight"].notna()][["tkt", "p", "flight", "d", "dep_min"]]
    if T.empty:
        return

    ticket_people = T[["tkt", "p"]].drop_duplicates()
    people_count = ticket_people.groupby("tkt")["p"].transform("nunique")
    stats["tickets_skipped_too_large"] = int(ticket_people[people_count > cfg["max_group"]]["tkt"].nunique())
    C = ticket_people[(people_count >= 2) & (people_count <= cfg["max_group"])].copy()
    stats["tickets_shared_by_several_people"] = int(C["tkt"].nunique())

    if len(C):
        # Compare the complete passport sets for each person, not an arbitrary first passport.
        C["docs"] = [set(P["docs_family"][idx]) for idx in C["p"]]
        C["nk"] = [f"{P['last_key'][idx]}/{P['first_init'][idx]}" for idx in C["p"]]
        C["txt"] = [ent_text(P, idx) for idx in C["p"]]
        grouped = C.groupby("tkt")

        def conflicting_passports(values):
            nonempty = [value for value in values if value]
            return any(
                set_a.isdisjoint(set_b)
                for i, set_a in enumerate(nonempty)
                for set_b in nonempty[i + 1:]
            )

        passport_conflict = C.groupby("tkt")["docs"].apply(conflicting_passports)
        high_tickets = passport_conflict[passport_conflict].index
        name_count = grouped["nk"].nunique()
        named_tickets = name_count[
            (name_count >= 2) & ~name_count.index.isin(high_tickets)
        ].index
        other_tickets = name_count.index[
            ~name_count.index.isin(high_tickets)
            & ~name_count.index.isin(named_tickets)
        ]
        evidence_people = grouped["txt"].agg(" | ".join)
        for level, tickets, reason in (
            ("high", high_tickets, "passport sets differ"),
            (cfg["ticket_name_only_level"], named_tickets, "names differ"),
            ("low", other_tickets, "same names, no conflicting passports"),
        ):
            selected = C[C["tkt"].isin(tickets)]
            findings.add(
                "5", level, selected["p"].to_numpy(),
                ("ticket " + selected["tkt"] + f" shared ({reason}): "
                 + selected["tkt"].map(evidence_people)).to_numpy(),
            )

    grouped_ticket_flight = T.groupby(["tkt", "flight"])
    different_trip = T[
        (grouped_ticket_flight["d"].transform("nunique") > 1)
        | (grouped_ticket_flight["dep_min"].transform("nunique") > 1)
    ]
    if len(different_trip):
        date_evidence = different_trip.groupby(["tkt", "flight"])["d"].agg(
            lambda values: ", ".join(sorted({value.strftime("%d.%m.%Y") for value in values.dropna()}))
        )
        unique = different_trip.drop_duplicates(["tkt", "flight", "p"])
        evidence = np.array([
            f"ticket {ticket} on {flight} has different dates/times: {date_evidence[(ticket, flight)]}"
            for ticket, flight in zip(unique["tkt"], unique["flight"])
        ], dtype=object)
        findings.add("4", "medium", unique["p"].to_numpy(), evidence, flight=unique["flight"].to_numpy())

    dated = T[T["d"].notna()]
    two_tickets = dated[
        dated.groupby(["p", "flight", "d"])["tkt"].transform("nunique") >= 2
    ].drop_duplicates(["p", "flight", "d", "tkt"])
    if len(two_tickets):
        evidence_by_person_flight = two_tickets.groupby(["p", "flight", "d"])["tkt"].agg(" / ".join)
        unique = two_tickets.drop_duplicates(["p", "flight", "d"])
        evidence = np.array([
            f"tickets {evidence_by_person_flight[key]}"
            for key in zip(unique["p"], unique["flight"], unique["d"])
        ], dtype=object)
        findings.add(
            "7", "medium", unique["p"].to_numpy(), evidence,
            flight=unique["flight"].to_numpy(), date=unique["d"].to_numpy(),
        )


def check_movement(R, L, cfg, findings, airport_names):
    base = L[
        L["d"].notna() & ~L["conflict"] & ~L["multileg"] & L["flight"].notna()
    ].copy()
    A = base[base["dep_min"].notna()].copy()
    A["fk"] = sv(A["flight"]) + ":" + sv(A["frm"])
    A["txt"] = sv(A["flight"]) + " " + sv(A["frm"]) + ">" + sv(A["to"])
    keys = ["eid", "d", "dep_min"]
    simultaneous = A[A.groupby(keys)["fk"].transform("nunique") >= 2]
    if len(simultaneous):
        evidence_by_time = simultaneous.groupby(keys)["txt"].agg("; ".join)
        unique = simultaneous.drop_duplicates(keys)
        evidence = np.array([
            f"same departure time {int(key[2]) // 60:02d}:{int(key[2]) % 60:02d}: {evidence_by_time[key]}"
            for key in zip(unique["eid"], unique["d"], unique["dep_min"])
        ], dtype=object)
        findings.add(
            "2a", "high", unique["eid"].to_numpy(), evidence,
            flight=unique["flight"].to_numpy(), date=unique["d"].to_numpy(),
        )

    B = base[(base["frm_i"] >= 0) & (base["to_i"] >= 0)]
    grouped = B.groupby(["eid", "d", "frm_i"])
    candidates = B[
        (grouped["flight"].transform("nunique") >= 2)
        & (grouped["to_i"].transform("nunique") >= 2)
    ]
    if len(candidates):
        return_routes = (
            B[["eid", "d", "to_i"]].drop_duplicates()
            .rename(columns={"to_i": "frm_i"}).assign(_back=1)
        )
        candidates = candidates.merge(return_routes, on=["eid", "d", "frm_i"], how="left")
        candidates = candidates[candidates["_back"].isna()]
        if len(candidates):
            candidates = candidates.assign(txt=sv(candidates["flight"]) + ">" + sv(candidates["to"]))
            evidence_by_origin = candidates.groupby(["eid", "d", "frm_i"])["txt"].agg("; ".join)
            unique = candidates.drop_duplicates(["eid", "d", "frm_i"])
            evidence = np.array([
                f"same day from {airport_names[key[2]]}: {evidence_by_origin[key]}"
                for key in zip(unique["eid"], unique["d"], unique["frm_i"])
            ], dtype=object)
            findings.add(
                "2c", "medium", unique["eid"].to_numpy(), evidence,
                flight=unique["flight"].to_numpy(), date=unique["d"].to_numpy(),
            )

    S = base[base["dep_min"].notna()].sort_values(
        ["eid", "d", "dep_min"], kind="stable"
    )
    if len(S) > 1:
        departure_dt = S["d"] + pd.to_timedelta(S["dep_min"], unit="m")
        arrival_dt = S["arr_d"] + pd.to_timedelta(S["arr_min"], unit="m")
        entities = S["eid"].to_numpy()
        previous_dest = S["to_i"].to_numpy()
        next_origin = S["frm_i"].to_numpy()
        impossible = (
            (entities[:-1] == entities[1:])
            & (previous_dest[:-1] == next_origin[1:])
            & (previous_dest[:-1] >= 0)
            & arrival_dt.iloc[:-1].notna().to_numpy()
            & departure_dt.iloc[1:].notna().to_numpy()
            & (departure_dt.iloc[1:].to_numpy() < arrival_dt.iloc[:-1].to_numpy())
        )
        indices = np.flatnonzero(impossible)
        if len(indices):
            flight_a = S["flight"].to_numpy()[indices]
            flight_b = S["flight"].to_numpy()[indices + 1]
            evidence = np.array([
                f"{a} arrives {arr:%d.%m.%Y %H:%M}, {b} departs {dep:%d.%m.%Y %H:%M} from the same airport"
                for a, b, arr, dep in zip(
                    flight_a, flight_b, arrival_dt.iloc[indices], departure_dt.iloc[indices + 1]
                )
            ], dtype=object)
            findings.add(
                "2b", "medium", entities[indices], evidence,
                flight=flight_b, date=S["d"].to_numpy()[indices + 1],
            )

    C = base[(base["frm_i"] >= 0) & (base["to_i"] >= 0)].copy()
    if len(C):
        min_day = C["d"].min()
        C["day"] = (C["d"] - min_day).dt.days.astype(np.int64)
        C = C.sort_values(["eid", "day", "dep_min"], kind="stable")
        day_count = int(C["day"].max()) + 1
        airport_count = len(airport_names)
        day_key = C["eid"].to_numpy(np.int64) * day_count + C["day"].to_numpy()
        from_key = day_key * airport_count + C["frm_i"].to_numpy()
        to_key = day_key * airport_count + C["to_i"].to_numpy()
        C["is_start"] = ~np.isin(from_key, to_key)
        C["is_end"] = ~np.isin(to_key, from_key)
        group_keys = ["eid", "day"]
        daily = pd.DataFrame({
            "start": C.groupby(group_keys)["frm_i"].first(),
            "end": C.groupby(group_keys)["to_i"].last(),
            "flight": C.groupby(group_keys)["flight"].first(),
        })
        daily["start"] = (
            C[C["is_start"]].groupby(group_keys)["frm_i"].first()
            .reindex(daily.index).fillna(daily["start"]).astype(int)
        )
        daily["end"] = (
            C[C["is_end"]].groupby(group_keys)["to_i"].last()
            .reindex(daily.index).fillna(daily["end"]).astype(int)
        )
        daily = daily.reset_index()
        previous_end = daily["end"].shift()
        previous_day = daily["day"].shift()
        bad = (daily["eid"] == daily["eid"].shift()) & (previous_end != daily["start"])
        if cfg["teleport_max_days"] is not None:
            bad &= (daily["day"] - previous_day) <= cfg["teleport_max_days"]
        bad = bad.to_numpy()
        selected = daily[bad]
        previous_end_values = previous_end[bad].astype(int).to_numpy()
        previous_day_values = previous_day[bad].to_numpy()
        evidence = np.array([
            f"flights ended at {airport_names[end]} ({min_day + pd.Timedelta(days=int(day)):%d.%m.%Y}), "
            f"the next flying day starts at {airport_names[start]}"
            for end, day, start in zip(
                previous_end_values, previous_day_values, selected["start"]
            )
        ], dtype=object)
        findings.add(
            "2d", "low", selected["eid"].to_numpy(), evidence,
            flight=selected["flight"].to_numpy(),
            date=(min_day + pd.to_timedelta(selected["day"].to_numpy(), unit="D")).to_numpy(),
        )


def check_timetable(R, cfg, findings, stats):
    X = R[R["flight"].notna() & R["ttc"].notna()].copy()
    stats["timetable_check_values"] = "; ".join(
        f"{key}={value}" for key, value in X["ttc"].value_counts().items()
    )
    X["air"] = X["flight"].astype(str).str[:2]
    X["cs_yes"] = X["codeshare"].astype(str).str.upper().eq("YES")
    X["nf"] = X["ttc"].isin(cfg["timetable_not_found_values"])
    base = X[~X["cs_yes"]] if cfg["timetable_skip_codeshare"] else X
    coverage = base.groupby("air").agg(rows=("nf", "size"), not_found=("nf", "sum"))
    coverage["coverage"] = 1 - coverage["not_found"] / coverage["rows"]
    coverage["checked"] = (
        (coverage["coverage"] >= cfg["timetable_min_airline_coverage"])
        & (coverage["rows"] >= cfg["timetable_min_airline_rows"])
    )
    stats["timetable_not_found_share"] = round(float(X["nf"].mean()), 4) if len(X) else None
    stats["timetable_airlines_checked_3a"] = ", ".join(coverage.index[coverage["checked"]])
    selected = base[
        base["nf"] & base["air"].isin(set(coverage.index[coverage["checked"]]))
    ].drop_duplicates(["eid", "flight", "d"])
    findings.add(
        "3a", "medium", selected["eid"].to_numpy(),
        ("flight " + sv(selected["flight"]) + " (" + sv(selected["ttc"])
         + ") is not in the timetable; airline coverage "
         + (selected["air"].map(coverage["coverage"]) * 100).round(1).astype(str) + "%").to_numpy(),
        flight=selected["flight"].to_numpy(), date=selected["d"].to_numpy(),
    )
    wrong_day = X[X["ttc"].isin(cfg["timetable_wrong_day_values"])].drop_duplicates(
        ["eid", "flight", "d"]
    )
    findings.add(
        "3c", "medium", wrong_day["eid"].to_numpy(),
        ("flight " + sv(wrong_day["flight"]) + " exists on this route but not on this day").to_numpy(),
        flight=wrong_day["flight"].to_numpy(), date=wrong_day["d"].to_numpy(),
    )
    return coverage.reset_index().rename(columns={"air": "airline"})


def check_status(R, cfg, findings, stats):
    stats["flight_status_values"] = "; ".join(
        f"{key}={value}" for key, value in R["status"].value_counts().head(10).items()
    )
    selected = R[
        R["status"].astype(str).str.upper().isin(cfg["suspicious_statuses"])
        & R["flight"].notna()
    ].drop_duplicates(["eid", "flight", "d"])
    findings.add(
        "12", "medium", selected["eid"].to_numpy(),
        ("flight status " + sv(selected["status"])).to_numpy(),
        flight=selected["flight"].to_numpy(), date=selected["d"].to_numpy(),
    )


def check_consistency(R, L, findings):
    X = L[L["cls"].notna() & L["fare"].notna()]
    bad = X[X["cls"].astype(str).str.upper() != X["fare"].astype(str).str.upper().str[:1]]
    findings.add(
        "9a", "low", bad["eid"].to_numpy(),
        ("class " + sv(bad["cls"]) + " but fare " + sv(bad["fare"])).to_numpy(),
        flight=bad["flight"].to_numpy(), date=bad["d"].to_numpy(),
    )
    X = L[L["arr_d"].notna() & L["d"].notna() & (L["arr_d"] < L["d"])]
    findings.add(
        "9b", "low", X["eid"].to_numpy(),
        ("arrival date " + X["arr_d"].dt.strftime("%d.%m.%Y")
         + " is before the departure date").to_numpy(),
        flight=X["flight"].to_numpy(), date=X["d"].to_numpy(),
    )
    X = L[L["frm"].notna() & (L["frm"] == L["to"])]
    findings.add(
        "9c", "low", X["eid"].to_numpy(),
        ("departure and arrival airport are the same: " + sv(X["frm"])).to_numpy(),
        flight=X["flight"].to_numpy(), date=X["d"].to_numpy(),
    )
    Z = R[R["conflict"] & R["flight"].notna()]
    if len(Z):
        Z = Z.assign(txt=sv(Z["frm"]) + ">" + sv(Z["to"]) + " pnr=" + sv(Z["pnr"]))
        evidence_by_flight = Z.groupby(["eid", "flight", "d"], dropna=False)["txt"].agg(
            lambda values: " vs ".join(sorted(set(values)))
        )
        unique = Z.drop_duplicates(["eid", "flight", "d"])
        evidence = np.array([
            f"contradicting data for one flight: {evidence_by_flight[key]}"
            for key in zip(unique["eid"], unique["flight"], unique["d"])
        ], dtype=object)
        findings.add(
            "9d", "low", unique["eid"].to_numpy(), evidence,
            flight=unique["flight"].to_numpy(), date=unique["d"].to_numpy(),
        )


def check_frequency(L, cfg, findings, stats):
    X = L[L["d"].notna()]
    year_month = (X["d"].dt.year * 12 + X["d"].dt.month).rename("ym")
    month_counts = X.groupby(["eid", year_month]).size()
    threshold = max(month_counts.quantile(cfg["freq_percentile"]), cfg["freq_month_min"] - 1)
    stats["freq_month_threshold"] = float(threshold)
    hits = month_counts[month_counts > threshold].reset_index(name="n")
    findings.add(
        "10a", "low", hits["eid"].to_numpy(),
        np.array([
            f"{count} flights in {(year_month_value - 1) // 12}-{(year_month_value - 1) % 12 + 1:02d} "
            f"(threshold {threshold:.0f})"
            for count, year_month_value in zip(hits["n"], hits["ym"])
        ], dtype=object),
    )
    total_counts = X.groupby("eid").size()
    threshold = max(total_counts.quantile(cfg["freq_percentile"]), cfg["freq_total_min"] - 1)
    stats["freq_total_threshold"] = float(threshold)
    hits = total_counts[total_counts > threshold]
    findings.add(
        "10b", "low", hits.index.to_numpy(),
        np.array([f"{count} flights in total (threshold {threshold:.0f})" for count in hits], dtype=object),
    )
    Y = L[(L["to_i"] >= 0) & L["flight"].notna()]
    people_by_airport = Y.groupby("to_i")["eid"].nunique()
    rare_airports = people_by_airport[people_by_airport <= cfg["rare_airport_max_people"]].index
    selected = Y[Y["to_i"].isin(rare_airports)].drop_duplicates(["eid", "to_i"])
    stats["rare_airports"] = int(len(rare_airports))
    findings.add(
        "10c", "low", selected["eid"].to_numpy(),
        ("arrives at a rarely visited airport " + sv(selected["to"])
         + " (people: " + selected["to_i"].map(people_by_airport).astype(str) + ")").to_numpy(),
        flight=selected["flight"].to_numpy(), date=selected["d"].to_numpy(),
    )


def cotravel(L, cfg, stats):
    X = L[L["d"].notna() & L["flight"].notna()][["eid", "d", "flight"]].drop_duplicates()
    X = X.assign(
        gid=pd.factorize(pd.MultiIndex.from_arrays([X["d"], X["flight"]]))[0]
    ).sort_values(["gid", "eid"], kind="stable")
    group_size = X.groupby("gid")["eid"].transform("size").to_numpy()
    keep = (group_size >= 2) & (group_size <= cfg["cotravel_max_pax"])
    entity_ids = X["eid"].to_numpy(np.int64)[keep]
    sizes = group_size[keep]
    encoded_pairs = []
    for size in np.unique(sizes):
        members = entity_ids[sizes == size].reshape(-1, size)
        left, right = np.triu_indices(size, 1)
        a, b = members[:, left].ravel(), members[:, right].ravel()
        encoded_pairs.append(np.minimum(a, b) * (1 << 32) + np.maximum(a, b))
    if not encoded_pairs:
        return pd.DataFrame({"a": [], "b": [], "n": []}, dtype="int64")
    unique_pairs, counts = np.unique(np.concatenate(encoded_pairs), return_counts=True)
    keep_pair = counts >= cfg["cotravel_min_flights"]
    result = pd.DataFrame({
        "a": unique_pairs[keep_pair] >> 32,
        "b": unique_pairs[keep_pair] & 0xFFFFFFFF,
        "n": counts[keep_pair],
    })
    stats["cotravel_pairs"] = len(result)
    return result


def classify(findings_frame, entities, cfg):
    frame = findings_frame.copy()
    if frame.empty:
        frame["ambiguous_identity"] = pd.Series(dtype=bool)
        frame["level_effective"] = pd.Series(dtype=object)
        return frame
    frame["ambiguous_identity"] = entities["ambiguous"].to_numpy()[frame["eid"].to_numpy()]
    ranks = frame["level"].map(LEVEL_RANK).to_numpy()
    if cfg["downgrade_ambiguous"]:
        downgrade = frame["ambiguous_identity"] & frame["check"].isin(cfg["ambiguous_checks"])
        ranks = np.where(downgrade, np.maximum(ranks - 1, 1), ranks)
    frame["level_effective"] = pd.Series(ranks, index=frame.index).map(RANK_LEVEL)
    return frame


def entity_table(findings_frame, entities, cfg):
    if findings_frame.empty:
        return pd.DataFrame(columns=[
            "eid", "entity_id", "last", "first", "middle", "birth", "doc", "ff_cards",
            "ambiguous", "class", "n_high", "n_medium", "n_low", "checks", "top_evidence"
        ])
    counts = pd.crosstab(findings_frame["eid"], findings_frame["level_effective"]).reindex(
        columns=["high", "medium", "low"], fill_value=0
    )
    table = counts.rename(columns={"high": "n_high", "medium": "n_medium", "low": "n_low"})
    table["medium_checks"] = findings_frame[findings_frame["level_effective"] == "medium"].groupby("eid")["check"].nunique()
    table["low_checks"] = findings_frame[findings_frame["level_effective"] == "low"].groupby("eid")["check"].nunique()
    table = table.fillna(0).astype(int)
    high = (table["n_high"] >= 1) | (
        (table["n_medium"] >= cfg["high_min_medium"])
        & (table["medium_checks"] >= cfg["high_min_medium_checks"])
    )
    low = ~high & (
        (table["n_medium"] >= 1)
        | ((table["n_low"] >= cfg["low_min_low"]) & (table["low_checks"] >= cfg["low_min_low_checks"]))
    )
    table["class"] = np.where(high, "high", np.where(low, "low", ""))
    subset = findings_frame[findings_frame["eid"].isin(table.index[table["class"] != ""])]
    table["checks"] = subset.assign(
        key=subset["subcheck"] + ":" + subset["level_effective"]
    ).groupby("eid")["key"].agg(
        lambda values: "; ".join(f"{key} x{count}" for key, count in values.value_counts().sort_index().items())
    )
    top = subset.assign(rank=subset["level_effective"].map(LEVEL_RANK)).sort_values(
        ["eid", "rank"], ascending=[True, False], kind="stable"
    )
    table["top_evidence"] = top.groupby("eid")["evidence"].agg(
        lambda values: " || ".join(values.head(3).astype(str))
    )
    return table.reset_index().merge(entities, on="eid", how="left")


def run(df, cfg, initial_stats=None):
    stats = dict(initial_stats or {})
    start = time.time()
    P, R, E, airport_names = build_model(df, cfg, stats)
    L = R.drop_duplicates(["eid", "d", "flight", "frm", "to"]).copy()
    findings = Findings()

    check_passports(P, cfg, findings, stats)
    check_person_level(P, cfg, findings, stats)
    check_age(P, R, cfg, findings)
    check_tickets(P, R, cfg, findings, stats)
    check_movement(R, L, cfg, findings, airport_names)
    coverage = check_timetable(R, cfg, findings, stats)
    check_status(R, cfg, findings, stats)
    check_consistency(R, L, findings)
    check_frequency(L, cfg, findings, stats)
    pairs = cotravel(L, cfg, stats)

    raw_findings = findings.frame()
    effective_findings = classify(raw_findings, E, cfg)
    table = entity_table(effective_findings, E, cfg)

    rename = {
        "entity_id": "person_id", "last": "last_name", "first": "first_name",
        "middle": "middle_name", "birth": "birth_date", "doc": "travel_doc",
    }
    all_findings = effective_findings.merge(
        E[["eid", "entity_id", "last", "first", "birth", "doc"]],
        on="eid", how="left"
    )
    all_findings["date"] = pd.to_datetime(all_findings["date"], errors="coerce").dt.strftime("%d.%m.%Y")
    all_findings = all_findings[[
        "entity_id", "last", "first", "birth", "doc", "check", "subcheck", "level",
        "level_effective", "ambiguous_identity", "flight", "date", "evidence"
    ]].rename(columns=rename).sort_values(["person_id", "check", "subcheck"], kind="stable")

    output_columns = [
        "entity_id", "last", "first", "middle", "birth", "doc", "ff_cards", "ambiguous",
        "class", "n_high", "n_medium", "n_low", "checks", "top_evidence"
    ]
    if len(table):
        high = table[table["class"] == "high"][output_columns].rename(
            columns=rename | {"ambiguous": "ambiguous_identity"}
        )
        low = table[table["class"] == "low"][output_columns].rename(
            columns=rename | {"ambiguous": "ambiguous_identity"}
        )
    else:
        output_high_columns = [
            "person_id", "last_name", "first_name", "middle_name", "birth_date", "travel_doc",
            "ff_cards", "ambiguous_identity", "class", "n_high", "n_medium", "n_low", "checks", "top_evidence"
        ]
        high = pd.DataFrame(columns=output_high_columns)
        low = pd.DataFrame(columns=output_high_columns)

    # Spies: high finding in a spy check. They go to their own table instead of suspicious_high.
    spy_mask = (
        effective_findings["subcheck"].isin(cfg["spy_subchecks"])
        & (effective_findings["level_effective"] == "high")
    ) if len(effective_findings) else pd.Series(dtype=bool)
    spy_person_ids = set(E.loc[E["eid"].isin(set(effective_findings.loc[spy_mask, "eid"])), "entity_id"]) \
        if len(effective_findings) else set()
    spies = high[high["person_id"].isin(spy_person_ids)].copy()
    spies["class"] = "spy"
    high = high[~high["person_id"].isin(spy_person_ids)]

    associates = pd.DataFrame(columns=[
        "person_id", "last_name", "first_name", "linked_to", "shared_flights"
    ])
    if len(pairs) and len(table):
        high_entities = set(table.loc[table["class"] == "high", "eid"])
        people_with_findings = set(effective_findings["eid"])
        side_a = pairs[pairs["a"].isin(high_entities) & ~pairs["b"].isin(people_with_findings)].rename(
            columns={"a": "suspect", "b": "eid"}
        )
        side_b = pairs[pairs["b"].isin(high_entities) & ~pairs["a"].isin(people_with_findings)].rename(
            columns={"b": "suspect", "a": "eid"}
        )
        linked = pd.concat([side_a, side_b])
        if len(linked):
            linked = linked.merge(
                E[["eid", "entity_id", "last", "first"]], on="eid"
            ).merge(
                E[["eid", "entity_id"]].rename(columns={"eid": "suspect", "entity_id": "linked_to"}),
                on="suspect"
            )
            associates = linked[["entity_id", "last", "first", "linked_to", "n"]].rename(
                columns={
                    "entity_id": "person_id", "last": "last_name", "first": "first_name",
                    "n": "shared_flights",
                }
            ).sort_values(["shared_flights", "person_id"], ascending=[False, True])

    if len(pairs):
        pairs_out = pairs.merge(
            E[["eid", "entity_id"]].rename(columns={"eid": "a", "entity_id": "person_a"}), on="a"
        ).merge(
            E[["eid", "entity_id"]].rename(columns={"eid": "b", "entity_id": "person_b"}), on="b"
        )[["person_a", "person_b", "n"]].rename(columns={"n": "shared_flights"})
    else:
        pairs_out = pd.DataFrame(columns=["person_a", "person_b", "shared_flights"])

    summary_rows = [("input", key, value) for key, value in stats.items()]
    if len(effective_findings):
        counts = effective_findings.groupby(["subcheck", "level_effective"]).agg(
            findings=("eid", "size"), people=("eid", "nunique")
        ).reset_index()
        summary_rows += [
            ("findings", f"{row.subcheck} / {row.level_effective}",
             f"{row.findings} findings, {row.people} people")
            for row in counts.itertuples()
        ]
    summary_rows += [
        ("classification", "spies", len(spies)),
        ("classification", "suspicious_high", len(high)),
        ("classification", "suspicious_low", len(low)),
        ("classification", "associates", len(associates)),
        ("seconds", "checks", round(time.time() - start, 1)),
    ]
    # Three cumulative screening stages for the frontend.
    # Stage 1: broad screening — every person with at least one finding.
    # Stage 2: narrower cross-check — keep people with an identity/document/ticket
    # finding, or findings from at least two independent checks. A high finding in a
    # configured spy check always passes this stage so that a confirmed spy cannot
    # disappear before the final stage.
    # Stage 3: strict rule — high finding in one of cfg["spy_subchecks"].
    identity_checks = {"1", "1b", "5", "6a", "6b", "8a"}
    if len(effective_findings):
        person_findings = effective_findings.merge(
            E[["eid", "entity_id", "last", "first", "middle", "birth", "doc", "ff_cards"]],
            on="eid", how="left", suffixes=("", "_person")
        )
        person_findings["date"] = pd.to_datetime(person_findings["date"], errors="coerce").dt.strftime("%d.%m.%Y")
        per_person = person_findings.groupby("entity_id", sort=False).agg(
            checks=("check", lambda x: ", ".join(sorted(set(map(str, x)), key=lambda v: int(v)))),
            subchecks=("subcheck", lambda x: ", ".join(sorted(set(map(str, x))))),
            evidence=("evidence", lambda x: " | ".join(dict.fromkeys(str(v) for v in x if pd.notna(v)))),
            n_checks=("check", "nunique"),
        ).reset_index().rename(columns={"entity_id": "person_id"})
        person_details = E[["entity_id", "last", "first", "middle", "birth", "doc", "ff_cards"]].drop_duplicates("entity_id").rename(columns={
            "entity_id": "person_id", "last": "last_name", "first": "first_name",
            "middle": "middle_name", "birth": "birth_date", "doc": "travel_doc",
        })
        stage1 = person_details.merge(per_person, on="person_id", how="inner")

        identity_people = set(effective_findings.loc[
            effective_findings["check"].astype(str).isin(identity_checks), "eid"
        ].map(E.set_index("eid")["entity_id"]))
        multi_check_people = set(effective_findings.groupby("eid")["check"].nunique().loc[lambda x: x >= 2].index.map(
            E.set_index("eid")["entity_id"]
        ))
        # Preserve the configured definitive spy rule through the intermediate stage.
        spy_people = set(E.loc[E["eid"].isin(set(effective_findings.loc[spy_mask, "eid"])), "entity_id"])
        stage2_ids = identity_people | multi_check_people | spy_people
        stage2 = stage1[stage1["person_id"].isin(stage2_ids)].copy()
        stage3 = stage1[stage1["person_id"].isin(spy_people)].copy()
    else:
        stage_columns = ["person_id", "last_name", "first_name", "middle_name", "birth_date", "travel_doc", "ff_cards", "checks", "subchecks", "evidence", "n_checks"]
        stage1 = pd.DataFrame(columns=stage_columns)
        stage2 = pd.DataFrame(columns=stage_columns)
        stage3 = pd.DataFrame(columns=stage_columns)

    stage1.insert(0, "stage", "1_initial_screening")
    stage1.insert(1, "filter_rule", "At least one anomaly finding")
    stage2.insert(0, "stage", "2_cross_check")
    stage2.insert(1, "filter_rule", "Identity/document/ticket finding OR findings in at least two checks; configured spy rule is retained")
    stage3.insert(0, "stage", "3_confirmed_spies")
    stage3.insert(1, "filter_rule", "High-level finding in configured spy check(s): " + ", ".join(cfg["spy_subchecks"]))

    return {
        "stage_1_initial.csv": stage1,
        "stage_2_cross_checked.csv": stage2,
        "stage_3_spies.csv": stage3,
    }


def main():
    parser = argparse.ArgumentParser(description="Find suspicious passengers in merged flight data")
    parser.add_argument("--input", required=True, help="Path to flights_clean.csv")
    parser.add_argument("--outdir", default="results", help="Directory for result CSV files")
    parser.add_argument("--config", help="JSON file with CONFIG overrides")
    args = parser.parse_args()

    cfg = dict(CONFIG)
    if args.config:
        cfg.update(json.loads(Path(args.config).read_text(encoding="utf-8")))

    start = time.time()
    df, input_info = read_input(args.input, cfg)
    print(f"read {len(df):,} rows in {time.time() - start:.0f}s", input_info)
    results = run(df, cfg, input_info)

    output_dir = Path(args.outdir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, frame in results.items():
        path = output_dir / name
        frame.to_csv(path, index=False, sep=cfg["output_sep"], encoding="utf-8-sig")
        print(f"{name}: {len(frame):,} rows")


if __name__ == "__main__":
    main()
