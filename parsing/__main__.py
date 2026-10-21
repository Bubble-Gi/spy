"""
Ищет в папке файлы по именам (sirena*.tab, BoardingData*, PointzAggregator*,
SkyTeam-Exchange*, FrequentFlyerForum*, Skyteam_Timetable*.pdf, *.xlsx талонов),
либо их можно указать явно ключами ниже.
"""
import argparse
import os
import sys
import time

from .airports import AirportRef
from .merge import cluster, write_output
from .sources import (ErrLog, find_inputs, parse_boarding_data, parse_boarding_passes,
                      parse_exchange, parse_forum, parse_pointz, parse_sirena)
from .store import Store


def main(argv=None):
    ap = argparse.ArgumentParser(description="Склейка и очистка данных о перелётах")
    ap.add_argument("--data-dir", default=".", help="папка с исходными файлами")
    ap.add_argument("--out", default="flights_clean.csv")
    ap.add_argument("--sirena"), ap.add_argument("--boarding-data")
    ap.add_argument("--pointz"), ap.add_argument("--exchange"), ap.add_argument("--forum")
    ap.add_argument("--timetable"), ap.add_argument("--passes-dir",
                                                    help="папка с .xlsx посадочными")
    ap.add_argument("--skip-timetable", action="store_true", help="не разбирать PDF")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--tmp-db", default="flightparser_tmp.sqlite")
    ap.add_argument("--keep-db", action="store_true", help="не удалять временную БД")
    ap.add_argument("--buffer-mb", type=int, default=8,
                    help="буфер чтения файлов, Мб (по умолчанию 8)")
    ap.add_argument("--soft-fields", default="",
                    help="поля сегмента, расхождение в которых не делит поездку на две строки, "
                         "через запятую (например: codeshare,ff_card_used)")
    ap.add_argument("--pass-sheets", type=int, default=200,
                    help="листов талонов на одну порцию чтения (по умолчанию 200)")
    a = ap.parse_args(argv)
    from . import merge, sources
    sources.set_buffer(a.buffer_mb)
    soft = {x.strip() for x in a.soft_fields.split(",") if x.strip()}
    bad = soft - set(merge.SEG_STRICT)
    if bad:
        ap.error(f"неизвестные поля в --soft-fields: {', '.join(sorted(bad))}. "
                 f"Допустимые: {', '.join(merge.SEG_STRICT)}")
    merge.SOFT_FIELDS = soft
    sources.PASS_SHEETS_PER_TASK = max(1, a.pass_sheets)

    inp = find_inputs(a.data_dir)
    for k in ("sirena", "pointz", "exchange", "forum", "timetable"):
        if getattr(a, k):
            inp[k] = getattr(a, k)
    if a.boarding_data:
        inp["boarding_data"] = a.boarding_data
    if a.passes_dir:
        inp["boarding_passes"] = sorted(
            os.path.join(r, f) for r, _, fs in os.walk(a.passes_dir)
            for f in fs if f.lower().endswith(".xlsx"))

    out_dir = os.path.dirname(os.path.abspath(a.out))
    os.makedirs(out_dir, exist_ok=True)
    store = Store(a.tmp_db)
    log = ErrLog(os.path.join(out_dir, "errors.log"), store.stats)
    airports = AirportRef()
    timings = []

    def rss_mb():
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) // 1024
        except OSError:
            pass
        return 0

    def step(name, fn, *args):
        t = time.time()
        print(f"[{name}]  память: {rss_mb()} Мб", file=sys.stderr, flush=True)
        res = fn(*args)
        timings.append((name, time.time() - t, rss_mb()))
        return res

    print("Найдено файлов:", file=sys.stderr)
    for k, v in inp.items():
        shown = f"{len(v)} шт." if isinstance(v, list) else (v or "— НЕ НАЙДЕН —")
        print(f"  {k:16} {shown}", file=sys.stderr)

    tt = None
    if inp["timetable"] and not a.skip_timetable:
        from .timetable import Timetable
        tt = step("расписание PDF", Timetable.load, inp["timetable"], airports, a.workers)

    parsers = [("sirena", parse_sirena), ("boarding_data", parse_boarding_data),
               ("pointz", parse_pointz), ("forum", parse_forum), ("exchange", parse_exchange)]
    for name, fn in parsers:
        if inp.get(name):
            step(name, fn, inp[name], store, airports, log)
    if inp["boarding_passes"]:
        step("посадочные талоны", parse_boarding_passes, inp["boarding_passes"], store,
             airports, log, a.workers)
    store.flush()
    store.flush()

    persons, owner, by_card = step("склейка людей", cluster, store)
    step("склейка сегментов и запись CSV", write_output, store, persons, owner, by_card,
         airports, tt, a.out)

    # ------------------------------------------------------------------ отчёт
    rep = os.path.join(out_dir, "report.txt")
    with open(rep, "w", encoding="utf-8") as f:
        f.write("ОТЧЁТ ПО СКЛЕЙКЕ ДАННЫХ О ПЕРЕЛЁТАХ\n\n")
        f.write("Файлы:\n")
        for k, v in inp.items():
            f.write(f"  {k}: {len(v) if isinstance(v, list) else v}\n")
        f.write("\nСтатистика:\n")
        for k in sorted(store.stats):
            f.write(f"  {k}: " + f"{store.stats[k]:,}".replace(",", " ") + "\n")
        f.write(f"\nМягкие поля (--soft-fields): {', '.join(sorted(merge.SOFT_FIELDS)) or 'нет'}\n")
        if merge.CONFLICT_EXAMPLES:
            f.write("\nПримеры конфликтов (рейс дата: маршрут  «значение 1» vs «значение 2»):\n")
            for c, ex in sorted(merge.CONFLICT_EXAMPLES.items()):
                f.write(f"  {c}:\n")
                for e in ex:
                    f.write(f"    {e}\n")
        if tt is not None:
            f.write("\nРасписание:\n")
            for k, v in tt.stats.items():
                f.write(f"  {k}: {v}\n")
        f.write(f"\nАэропортов в справочнике: {len(airports.by_code)}\n")
        f.write("\nВремя этапов (и память процесса после этапа):\n")
        for n, s, mem in timings:
            f.write(f"  {n}: {s:.1f} c, {mem} Мб\n")
    log.close()
    store.close(keep=a.keep_db)
    print(f"\nГотово: {a.out}\nОтчёт: {rep}\nОшибки разбора: "
          f"{os.path.join(out_dir, 'errors.log')}", file=sys.stderr)


if __name__ == "__main__":
    main()
