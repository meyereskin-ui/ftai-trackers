"""Lead-time scorer: how many days did the FAA registry lead FTAI's announcements?

FTAI's deal press releases are not filed on EDGAR, so announcements live in
data/announcements.csv (date,headline,url). The daily Claude run appends new FTAI
aircraft/fund deal releases it finds; you can add rows by hand too.

For each announcement it finds FTAI-looking FT tails (UMB/FTAI-owned, or reserved by
the known filing agent) whose first registry signal came in the 90 days before it,
and reports the lead. Each tail's first-seen reservation and registration dates are
kept in the database, since the registry file only shows the current state.

Usage: python leadtime.py run      # update first-seen dates; score new announcements
       python leadtime.py report   # score every announcement
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import date, datetime

from common import DB_PATH, connect, now_utc, record_alerts
from faa_tracker import KNOWN_AGENTS, likely_ftai

ANNOUNCEMENTS = DB_PATH.parent / "announcements.csv"
WINDOW_DAYS = 90
SEED = [("2026-09-28", "FTAI acquires 10 retired WestJet 737-700s (per CV Research)", "")]


def _d(s: str) -> date | None:
    s = (s or "").strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def update_first_seen(con) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS faa_first_seen (n TEXT PRIMARY KEY, first_rsv TEXT,
                   first_reg TEXT, owner TEXT, model TEXT)""")
    snap = con.execute("SELECT MAX(snap_date) FROM faa_snapshot").fetchone()[0]
    if not snap:
        return
    for src, n, data in con.execute("SELECT source, n_number, data FROM faa_snapshot WHERE snap_date=?", (snap,)):
        r = json.loads(data)
        agent = (r.get("REGISTRANT") or "").upper() in KNOWN_AGENTS
        if src == "deregistered" or not (likely_ftai(r) or agent):
            continue
        rsv = (_d(r.get("RSV DATE", "")) or "") if src == "reserved" else ""
        reg = (_d(r.get("CERT ISSUE DATE", "")) or "") if src == "registered" else ""
        con.execute("""INSERT INTO faa_first_seen VALUES (?,?,?,?,?) ON CONFLICT(n) DO UPDATE SET
                       first_rsv=COALESCE(NULLIF(faa_first_seen.first_rsv,''), excluded.first_rsv),
                       first_reg=COALESCE(NULLIF(faa_first_seen.first_reg,''), excluded.first_reg),
                       owner=COALESCE(NULLIF(excluded.owner,''), faa_first_seen.owner),
                       model=COALESCE(NULLIF(excluded.model,''), faa_first_seen.model)""",
                    (n, str(rsv), str(reg), r.get("NAME") or r.get("REGISTRANT", ""), r.get("MODEL", "")))
    con.commit()


def announcements() -> list[tuple[date, str, str]]:
    if not ANNOUNCEMENTS.exists():
        ANNOUNCEMENTS.parent.mkdir(parents=True, exist_ok=True)
        with ANNOUNCEMENTS.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "headline", "url"])
            w.writerows(SEED)
    with ANNOUNCEMENTS.open() as f:
        return [(d, r["headline"], r.get("url", "")) for r in csv.DictReader(f) if (d := _d(r["date"]))]


BATCH_GAP_DAYS = 3  # tails first seen within this many days of each other form one batch


def batches(con) -> list[list[tuple[str, date]]]:
    tails = []
    for n, rsv, reg in con.execute("SELECT n, first_rsv, first_reg FROM faa_first_seen"):
        ds = [x for x in (_d(rsv), _d(reg)) if x]
        if ds:
            tails.append((n, min(ds)))
    tails.sort(key=lambda t: t[1])
    out: list[list[tuple[str, date]]] = []
    for t in tails:
        if out and (t[1] - out[-1][-1][1]).days <= BATCH_GAP_DAYS:
            out[-1].append(t)
        else:
            out.append([t])
    return out


def score(con, ann: date):
    """The most recent batch that started within WINDOW_DAYS before the announcement."""
    before = [b for b in batches(con) if 0 <= (ann - b[0][1]).days <= WINDOW_DAYS]
    return before[-1] if before else None


def describe(ann, headline, batch) -> str:
    if not batch:
        return f"{ann} {headline}: no FT-tail batch in the prior {WINDOW_DAYS} days"
    lead = (ann - batch[0][1]).days
    tails = ", ".join(f"N{n}" for n, _ in batch[:12]) + (" …" if len(batch) > 12 else "")
    return f"{ann} {headline}: registry led by {lead} days (batch of {len(batch)} from {batch[0][1]}: {tails})"


def cmd_run(args) -> None:
    con = connect()
    update_first_seen(con)
    con.execute("CREATE TABLE IF NOT EXISTS leadtime_scored (ann TEXT PRIMARY KEY, result TEXT)")
    alerts = []
    for ann, headline, url in announcements():
        key = f"{ann}|{headline}"
        if con.execute("SELECT 1 FROM leadtime_scored WHERE ann=?", (key,)).fetchone():
            continue
        text = describe(ann, headline, score(con, ann))
        con.execute("INSERT INTO leadtime_scored VALUES (?,?)", (key, text))
        alerts.append({"kind": "LEAD TIME", "key": key, "text": f"🔴 {text}{f' · {url}' if url else ''}"})
    con.commit()
    record_alerts(con, "leadtime", alerts)


def cmd_report(args) -> None:
    con = connect()
    update_first_seen(con)
    leads = []
    for ann, headline, _ in announcements():
        batch = score(con, ann)
        print(describe(ann, headline, batch))
        if batch:
            leads.append((ann - batch[0][1]).days)
    used = {b[0][1] for a, _, _ in announcements() if (b := score(con, a))}
    recent = date.today().toordinal() - 365
    open_batches = [b for b in batches(con) if b[0][1] not in used and len(b) >= 2
                    and b[0][1].toordinal() >= recent]
    if open_batches:
        print("\nBatches with no announcement yet:")
        for b in open_batches:
            print(f"  {b[0][1]}: {len(b)} tails ({', '.join('N' + n for n, _ in b[:12])})")
    if leads:
        print(f"\nAverage lead across {len(leads)} announcement(s): {sum(leads) / len(leads):.0f} days")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run").set_defaults(func=cmd_run)
    sub.add_parser("report").set_defaults(func=cmd_report)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
