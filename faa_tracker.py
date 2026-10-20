"""Daily FAA registry diff for FTAI-style "FT" tail numbers.

FTAI registers aircraft through UMB Bank as trustee with N-numbers ending in "FT".
The FAA's free ReleasableAircraft.zip carries three relevant files:
  MASTER.txt    registered aircraft          -> new registrations (a deal closing)
  RESERVED.txt  N-numbers reserved for later -> reservations (a deal in progress)
  DEREG.txt     cancelled registrations      -> exits (sale / export / part-out)
Each run snapshots every FT tail from all three, diffs against the previous
snapshot and alerts on the changes.

Usage:
  python faa_tracker.py run                 # download today's file, diff, alert
  python faa_tracker.py run --zip file.zip  # use a local copy instead
  python faa_tracker.py report              # all FT tails in the latest snapshot, by date
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import zipfile
from datetime import date, timedelta

import requests

from common import BROWSER_UA, connect, record_alerts

URL = "https://registry.faa.gov/database/ReleasableAircraft.zip"
SUFFIX = "FT"
FTAI_NAMES = ("UMB BANK", "FTAI", "FORTRESS TRANSPORTATION")
TRANSPORT_MFRS = ("BOEING", "AIRBUS", "EMBRAER", "BOMBARDIER", "ATR", "MCDONNELL")
FILES = {"MASTER.txt": "registered", "RESERVED.txt": "reserved", "DEREG.txt": "deregistered"}


# ---------- parsing ----------

def _norm(h: str) -> str:
    return h.strip().upper().replace("-", " ").lstrip("﻿")


def _rows(raw: bytes):
    raw = raw.removeprefix(b"\xef\xbb\xbf")  # the FAA files ship with a UTF-8 BOM
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    reader = csv.reader(io.StringIO(text))
    header = [_norm(h) for h in next(reader)]
    for row in reader:
        rec = {h: v.strip() for h, v in zip(header, row) if h and v.strip()}
        if rec:
            yield rec


def _nnum(rec: dict) -> str:
    return rec.get("N NUMBER", "").upper()


def parse_zip(blob: bytes) -> dict[str, dict[str, dict]]:
    """Return {source: {n_number: record}} for FT tails only."""
    out: dict[str, dict[str, dict]] = {s: {} for s in FILES.values()}
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = {n.upper(): n for n in z.namelist()}
        models = {}
        if "ACFTREF.TXT" in names:
            for r in _rows(z.read(names["ACFTREF.TXT"])):
                models[r.get("CODE", "")] = f"{r.get('MFR', '')} {r.get('MODEL', '')}".strip()
        for fname, source in FILES.items():
            if fname.upper() not in names:
                continue
            for r in _rows(z.read(names[fname.upper()])):
                n = _nnum(r)
                if not n.endswith(SUFFIX):
                    continue
                code = r.get("MFR MDL CODE", "")
                if code in models:
                    r["MODEL"] = models[code]
                key = n
                if source == "deregistered":  # DEREG keeps history: key by cancel date
                    key = f"{n}|{r.get('CANCEL DATE', '')}"
                out[source][key] = r
    return out


# ---------- classification ----------

BATCH_MIN = 3  # this many FT reservations by one registrant on one day looks like a fleet deal
BATCH_MAX_AGE_DAYS = 365  # older batches are stale history, not a live deal
# The aviation law firm CV traced the WestJet and Aug 28 reservations to.
KNOWN_AGENTS = {"DAUGHERTY FOWLER PEREGRIN HAUGHT & JENSON", "DFPH&J"}
# Registrants whose FT batches are not FTAI: FAA admin holds, flight schools, OEMs.
IGNORE_REGISTRANTS = ("SBS PRGM OFC", "CANCELLED/NOT ASSIGNED", "FLORIDA TECH",
                      "BOMBARDIER", "FLIGHT SAFETY", "FLIGHTSAFETY")


def _registrant(rec: dict) -> str:
    return (rec.get("REGISTRANT") or rec.get("NAME") or "").upper()


def build_ctx(snap: dict[str, dict[str, dict]], agents: set[str] | None = None) -> dict:
    """Reservation batches and known FTAI filing agents, used to classify reservations."""
    counts: dict[tuple, int] = {}
    cutoff = (date.today() - timedelta(days=BATCH_MAX_AGE_DAYS)).strftime("%Y%m%d")
    for r in snap.get("reserved", {}).values():
        if r.get("RSV DATE", "") < cutoff or any(x in _registrant(r) for x in IGNORE_REGISTRANTS):
            continue
        k = (_registrant(r), r.get("RSV DATE", ""))
        counts[k] = counts.get(k, 0) + 1
    return {"batches": {k for k, c in counts.items() if c >= BATCH_MIN},
            "agents": {a.upper() for a in (agents or set()) | KNOWN_AGENTS}}


def likely_ftai(rec: dict, ctx: dict | None = None) -> bool:
    names = " ".join(v for k, v in rec.items() if "NAME" in k or k == "REGISTRANT").upper()
    if any(t in names for t in FTAI_NAMES):
        return True
    if any(m in rec.get("MODEL", "").upper() for m in TRANSPORT_MFRS):
        return True
    if ctx and "RSV DATE" in rec:
        reg = _registrant(rec)
        if reg in ctx["agents"] or (reg, rec.get("RSV DATE", "")) in ctx["batches"]:
            return True
    return False


def describe(n: str, rec: dict) -> str:
    who = rec.get("NAME") or rec.get("REGISTRANT") or "?"
    loc = ", ".join(x for x in (rec.get("CITY"), rec.get("STATE")) if x)
    bits = [f"N{n.split('|')[0]}", who + (f" ({loc})" if loc else "")]
    if rec.get("MODEL"):
        bits.append(rec["MODEL"])
    if rec.get("SERIAL NUMBER"):
        bits.append(f"s/n {rec.get('SERIAL NUMBER')}")
    for label, k in (("reserved", "RSV DATE"), ("cert", "CERT ISSUE DATE"),
                     ("cancelled", "CANCEL DATE"), ("last action", "LAST ACTION DATE")):
        if rec.get(k):
            bits.append(f"{label} {rec[k]}")
    return " · ".join(bits)


# ---------- diff ----------

WATCH_FIELDS = ("NAME", "REGISTRANT", "STATUS CODE", "SERIAL NUMBER", "MFR MDL CODE", "EXP DATE")


def diff(prev: dict[str, dict[str, dict]], cur: dict[str, dict[str, dict]],
         agents: set[str] | None = None) -> list[dict]:
    """Compare two snapshots. Returns alerts; alerts of kind NEW AGENT carry the
    registrant of a reservation that turned into an FTAI-looking registration."""
    alerts = []
    ctx = build_ctx(cur, agents)

    def add(kind, n, rec, extra=""):
        hit = likely_ftai(rec, ctx)
        flag = "🔴" if hit else "⚪"
        alerts.append({"kind": kind, "key": n, "likely_ftai": hit,
                       "record": rec, "text": f"{flag} {describe(n, rec)}{extra}"})

    p_reg, c_reg = prev.get("registered", {}), cur.get("registered", {})
    p_rsv, c_rsv = prev.get("reserved", {}), cur.get("reserved", {})
    p_dr, c_dr = prev.get("deregistered", {}), cur.get("deregistered", {})

    for n in c_rsv.keys() - p_rsv.keys():
        add("NEW RESERVATION", n, c_rsv[n])
    for n in c_reg.keys() - p_reg.keys():
        was = f" (was reserved by {p_rsv[n].get('REGISTRANT', '?')})" if n in p_rsv else ""
        add("NEW REGISTRATION", n, c_reg[n], was)
        if n in p_rsv and likely_ftai(c_reg[n]):
            agent = _registrant(p_rsv[n])
            if agent and agent not in ctx["agents"]:
                ctx["agents"].add(agent)
                alerts.append({"kind": "NEW AGENT", "key": agent, "likely_ftai": True,
                               "record": p_rsv[n], "text": f"🔴 {agent} reserved N{n}, now an "
                               "FTAI-looking registration; its future FT reservations are flagged"})
    for n in c_dr.keys() - p_dr.keys():
        add("DEREGISTERED", n, c_dr[n])
    for n in p_reg.keys() - c_reg.keys():
        if not any(k.startswith(n + "|") for k in c_dr):
            add("DROPPED FROM REGISTRY", n, p_reg[n])
    for n in p_rsv.keys() - c_rsv.keys() - c_reg.keys():
        add("RESERVATION LAPSED", n, p_rsv[n])
    for src, p, c in (("registered", p_reg, c_reg), ("reserved", p_rsv, c_rsv)):
        for n in p.keys() & c.keys():
            changes = [f"{f}: {p[n].get(f, '')!r} → {c[n].get(f, '')!r}"
                       for f in WATCH_FIELDS if p[n].get(f) != c[n].get(f)]
            if changes:
                add(f"CHANGED ({src})", n, c[n], " · " + "; ".join(changes))
    order = {True: 0, False: 1}
    alerts.sort(key=lambda a: (order[a["likely_ftai"]], a["kind"], a["key"]))
    return alerts


# ---------- storage ----------

def load_snapshot(con, snap_date: str | None = None, before: str | None = None):
    if snap_date is None:
        q = "SELECT MAX(snap_date) FROM faa_snapshot" + (" WHERE snap_date < ?" if before else "")
        snap_date = con.execute(q, (before,) if before else ()).fetchone()[0]
    out: dict[str, dict[str, dict]] = {s: {} for s in FILES.values()}
    if snap_date:
        for r in con.execute("SELECT source, n_number, data FROM faa_snapshot WHERE snap_date=?",
                             (snap_date,)):
            out[r["source"]][r["n_number"]] = json.loads(r["data"])
    return snap_date, out


def save_snapshot(con, snap_date: str, snap: dict[str, dict[str, dict]]) -> None:
    con.execute("DELETE FROM faa_snapshot WHERE snap_date=?", (snap_date,))
    con.executemany(
        "INSERT INTO faa_snapshot VALUES (?,?,?,?)",
        [(snap_date, s, n, json.dumps(r)) for s, recs in snap.items() for n, r in recs.items()],
    )
    con.commit()


def download() -> bytes:
    r = requests.get(URL, headers={"User-Agent": BROWSER_UA}, timeout=300)
    r.raise_for_status()
    return r.content


# ---------- commands ----------

def cmd_run(args) -> None:
    blob = open(args.zip, "rb").read() if args.zip else download()
    cur = parse_zip(blob)
    snap_date = args.date or date.today().isoformat()
    con = connect()
    prev_date, prev = load_snapshot(con, before=snap_date)
    save_snapshot(con, snap_date, cur)
    counts = {s: len(v) for s, v in cur.items()}
    print(f"[faa] snapshot {snap_date}: {counts}")
    if prev_date is None:
        print("[faa] first snapshot stored as the baseline; alerts start on the next run. "
              "Run `report` to see the current FT tails.")
        return
    con.execute("CREATE TABLE IF NOT EXISTS faa_agents (name TEXT PRIMARY KEY, added TEXT)")
    agents = {r[0] for r in con.execute("SELECT name FROM faa_agents")}
    alerts = diff(prev, cur, agents)
    con.executemany("INSERT OR IGNORE INTO faa_agents VALUES (?,?)",
                    [(a["key"], snap_date) for a in alerts if a["kind"] == "NEW AGENT"])
    con.commit()
    if args.only_likely:
        alerts = [a for a in alerts if a["likely_ftai"]]
    print(f"[faa] compared with {prev_date}")
    record_alerts(con, "faa", alerts)


def cmd_report(args) -> None:
    con = connect()
    snap_date, snap = load_snapshot(con)
    if not snap_date:
        print("No snapshot yet. Run `python faa_tracker.py run` first.")
        return
    con.execute("CREATE TABLE IF NOT EXISTS faa_agents (name TEXT PRIMARY KEY, added TEXT)")
    ctx = build_ctx(snap, {r[0] for r in con.execute("SELECT name FROM faa_agents")})
    print(f"FT tails in snapshot {snap_date} (🔴 = UMB/FTAI name, airliner type, "
          f"known agent, or a batch of {BATCH_MIN}+ same-day reservations)\n")
    for src, recs in snap.items():
        rows = [(r.get("RSV DATE") or r.get("CERT ISSUE DATE") or r.get("CANCEL DATE") or "",
                 n, r) for n, r in recs.items()]
        if args.only_likely:
            rows = [x for x in rows if likely_ftai(x[2], ctx)]
        print(f"== {src} ({len(rows)})")
        for d, n, r in sorted(rows, key=lambda x: x[0], reverse=True)[: args.limit]:
            print(("🔴 " if likely_ftai(r, ctx) else "⚪ ") + describe(n, r))
        print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--zip", help="local ReleasableAircraft.zip instead of downloading")
    r.add_argument("--date", help="snapshot date YYYY-MM-DD (default today)")
    r.add_argument("--only-likely", action="store_true", help="alert only on likely-FTAI tails")
    r.set_defaults(func=cmd_run)
    p = sub.add_parser("report")
    p.add_argument("--only-likely", action="store_true")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=cmd_report)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
