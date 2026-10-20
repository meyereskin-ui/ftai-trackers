"""CFM56 fleet tracker: the supply of FTAI's feedstock engines.

Every 737NG and A320ceo-family jet carries two CFM56s. When US airlines retire them,
those engines become cheap feedstock for FTAI (good for module margins) or for its
competitors. From the same FAA registry file the FAA tracker downloads, this tracks:
  - CFM56-powered aircraft leaving the US register (export, scrap, part-out);
  - ownership transfers from airlines to trusts, lessors and part-out firms;
  - transfers to FTAI-linked owners (UMB Bank trusts, FTAI) and to competitors.
Individual moves to part-out firms, competitors or UMB trusts are 🔴; other transfers ⚪.
On the first run of each month it adds a summary of last month's moves vs. prior months.

Usage: python fleet_tracker.py run [--zip file.zip]
       python fleet_tracker.py history   # monthly deregistrations since 2023 from DEREG
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import zipfile
from collections import Counter
from datetime import date

from common import connect, now_utc, record_alerts
from faa_tracker import download

AIRLINE_WORDS = ("AIRLINES", "AIR LINES", "AIRWAYS", "AIRLINE", "SUN COUNTRY", "ALLEGIANT")
# Teardown, parts and engine-aftermarket firms: CFM56 feedstock going to FTAI's rivals.
PARTOUT = ("AERSALE", "GA TELESIS", "AAR ", "UNICAL", "WILLIS", "KELLSTROM", "VALLAIR", "AERCAP",
           "MAINSTREAM", "JET MIDWEST", "STANDARDAERO", "MTU", "SAFRAN", "GE CAPITAL", "AIRBORNE MAINTENANCE",
           "SOURCE ONE", "HAITE", "AIR SALVAGE", "PROCURE AEROSPACE", "MAGELLAN", "VAS AERO", "WENCOR",
           "EIRSALE", "SKYTECH", "CORE AVIATION", "AERO CAPITAL")
FTAI_OWNERS = ("UMB BANK", "FTAI", "FORTRESS")


def _rows(z: zipfile.ZipFile, name: str):
    text = z.read(name).decode("utf-8-sig", errors="replace")
    r = csv.reader(io.StringIO(text))
    header = [h.strip().upper().replace("-", " ") for h in next(r)]
    for row in r:
        yield {h: v.strip() for h, v in zip(header, row) if h}


def owner_class(name: str) -> str:
    n = name.upper()
    if any(w in n for w in FTAI_OWNERS):
        return "FTAI/UMB trust"
    if any(w in n for w in PARTOUT):
        return "part-out/competitor"
    if any(w in n for w in AIRLINE_WORDS):
        return "airline"
    if "TRUST" in n:
        return "lessor trust"
    return "other"


def load(blob: bytes) -> tuple[dict, list[dict]]:
    z = zipfile.ZipFile(io.BytesIO(blob))
    eng = {r["CODE"] for r in _rows(z, "ENGINE.txt") if "CFM56" in r.get("MODEL", "").upper()}
    models = {r["CODE"]: f"{r.get('MFR', '')} {r.get('MODEL', '')}".strip() for r in _rows(z, "ACFTREF.txt")}
    fleet = {}
    for r in _rows(z, "MASTER.txt"):
        if r.get("ENG MFR MDL") in eng:
            fleet[r["N NUMBER"]] = {"owner": r.get("NAME", ""), "serial": r.get("SERIAL NUMBER", ""),
                                    "model": models.get(r.get("MFR MDL CODE", ""), "?"),
                                    "status": r.get("STATUS CODE", "")}
    dereg = [{"n": r["N NUMBER"], "cancel": r.get("CANCEL DATE", ""), "export": r.get("EXP COUNTRY", ""),
              "owner": r.get("NAME", ""), "model": models.get(r.get("MFR MDL CODE", ""), "?")}
             for r in _rows(z, "DEREG.txt") if r.get("ENG MFR MDL") in eng]
    return fleet, dereg


def cmd_run(args) -> None:
    blob = open(args.zip, "rb").read() if args.zip else download()
    fleet, dereg = load(blob)
    con = connect()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS fleet_state (n TEXT PRIMARY KEY, data TEXT);
        CREATE TABLE IF NOT EXISTS fleet_events (day TEXT, kind TEXT, n TEXT, model TEXT,
                                                 from_owner TEXT, to_owner TEXT, to_class TEXT);
        CREATE TABLE IF NOT EXISTS fleet_runs (day TEXT PRIMARY KEY, total INTEGER);""")
    today = date.today().isoformat()
    prev = {r[0]: json.loads(r[1]) for r in con.execute("SELECT n, data FROM fleet_state")}
    first_run = not prev
    alerts, events = [], []
    if not first_run:
        dmap = {d["n"]: d for d in dereg}
        for n in prev.keys() - fleet.keys():
            p, d = prev[n], dmap.get(n, {})
            where = f"exported to {d['export'].title()}" if d.get("export") else "deregistered"
            events.append((today, "LEFT REGISTER", n, p["model"], p["owner"], d.get("export", ""), "exit"))
            alerts.append({"kind": "CFM56 EXIT", "key": n, "text":
                f"{'🔴' if owner_class(p['owner']) == 'FTAI/UMB trust' else '⚪'} N{n} {p['model']} "
                f"s/n {p['serial']} ({p['owner']}) {where}"})
        for n in fleet.keys() & prev.keys():
            a, b = prev[n]["owner"], fleet[n]["owner"]
            if a == b:
                continue
            cls = owner_class(b)
            events.append((today, "OWNER CHANGE", n, fleet[n]["model"], a, b, cls))
            hot = cls in ("FTAI/UMB trust", "part-out/competitor")
            alerts.append({"kind": "CFM56 TRANSFER", "key": n, "text":
                f"{'🔴' if hot else '⚪'} N{n} {fleet[n]['model']} s/n {fleet[n]['serial']}: "
                f"{a} ({owner_class(a)}) → {b} ({cls})"})
        for n in fleet.keys() - prev.keys():
            cls = owner_class(fleet[n]["owner"])
            events.append((today, "NEW TO REGISTER", n, fleet[n]["model"], "", fleet[n]["owner"], cls))
            if cls == "FTAI/UMB trust":
                alerts.append({"kind": "CFM56 IMPORT", "key": n, "text":
                    f"🔴 N{n} {fleet[n]['model']} s/n {fleet[n]['serial']} registered to {fleet[n]['owner']}"})
    con.executemany("INSERT INTO fleet_events VALUES (?,?,?,?,?,?,?)", events)
    con.execute("DELETE FROM fleet_state")
    con.executemany("INSERT INTO fleet_state VALUES (?,?)", [(n, json.dumps(v)) for n, v in fleet.items()])
    con.execute("INSERT OR REPLACE INTO fleet_runs VALUES (?,?)", (today, len(fleet)))
    con.commit()
    by_class = Counter(owner_class(v["owner"]) for v in fleet.values())
    print(f"[fleet] {len(fleet)} CFM56-powered aircraft on the US register: "
          + ", ".join(f"{k} {v}" for k, v in by_class.most_common()))
    if first_run:
        print("[fleet] baseline stored; transfers and exits are tracked from the next run")
        return
    alerts += monthly_summary(con, dereg, today)
    record_alerts(con, "fleet", alerts)


def monthly_summary(con, dereg: list[dict], today: str) -> list[dict]:
    """On the first run of a month, compare last month's exits/transfers with prior months."""
    month = today[:7]
    seen = con.execute("SELECT COUNT(*) FROM fleet_runs WHERE substr(day,1,7)=?", (month,)).fetchone()[0]
    if seen > 1:
        return []
    by_month = Counter(d["cancel"][:6] for d in dereg if d["cancel"][:4] >= "2024")
    y, m = int(month[:4]), int(month[5:])
    last = f"{y if m > 1 else y - 1}{(m - 1 or 12):02d}"
    trailing = [by_month.get(k, 0) for k in sorted(by_month) if k < last][-12:]
    avg = sum(trailing) / len(trailing) if trailing else 0
    moved = Counter(r[0] for r in con.execute(
        "SELECT to_class FROM fleet_events WHERE kind='OWNER CHANGE' AND replace(substr(day,1,7),'-','')=?", (last,)))
    hot = by_month.get(last, 0) > 1.5 * avg and avg > 0
    return [{"kind": "CFM56 MONTHLY", "key": last, "text":
             f"{'🔴' if hot else '⚪'} {last[:4]}-{last[4:]}: {by_month.get(last, 0)} CFM56 aircraft left the "
             f"US register (12-month avg {avg:.1f}); transfers: "
             + (", ".join(f"{k} {v}" for k, v in moved.most_common()) or "none tracked")}]


def cmd_history(args) -> None:
    blob = open(args.zip, "rb").read() if args.zip else download()
    _, dereg = load(blob)
    by_month = Counter(d["cancel"][:6] for d in dereg if d["cancel"][:4] >= "2023")
    exports = Counter(d["export"] for d in dereg if d["cancel"][:4] >= "2025" and d["export"])
    print("CFM56-powered aircraft leaving the US register, by month")
    for k in sorted(by_month):
        print(f"  {k[:4]}-{k[4:]}  {by_month[k]:4d}  {'#' * by_month[k]}")
    print("Export destinations since 2025:", ", ".join(f"{k.title()} {v}" for k, v in exports.most_common(10)))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("run", cmd_run), ("history", cmd_history)):
        p = sub.add_parser(name)
        p.add_argument("--zip")
        p.set_defaults(func=fn)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
