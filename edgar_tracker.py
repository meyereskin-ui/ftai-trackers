"""EDGAR full-text monitor: other companies' filings that mention FTAI.

Customers, lessees, counterparties and funds disclose things FTAI doesn't:
engine lease terms (Global Crossing's 10-Q exhibit), Power customers, fund
marks. This searches SEC full-text search for each query over a recent window,
drops FTAI's own filings, and alerts once per new document.

Usage:
  python edgar_tracker.py run                  # last 7 days, all queries
  python edgar_tracker.py run --days 365       # backfill a year (first run)
  python edgar_tracker.py run --from-json f.json --query '"FTAI"'   # offline test

Set FTAI_UA="Your Name you@example.com"; SEC requires a contact User-Agent.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import date, timedelta

import requests

from common import SEC_UA, connect, now_utc, record_alerts

EFTS = "https://efts.sec.gov/LATEST/search-index"
QUERIES = [
    '"FTAI Aviation"',
    '"FTAI" -"FTAI Infrastructure"',  # sister company FIP (Long Ridge etc.) is a different stock
    '"FTAI Aircraft"',            # the aircraft-fund vehicles: BDC loans, aircraft sale agreements
    '"Fortress Transportation and Infrastructure"',
    '"FTAI Strategic Capital"',   # the aircraft fund (Strategic Capital Initiative)
    '"FTAI Power"',
    '"J&F Power"',                # FTAI/Jereh packaging venture; a US customer may name it
]
# Holdings forms are excluded from the queries above (thousands of funds just own the shares)...
EXCLUDE_FORMS = ",".join("-" + f for f in (
    "NPORT-P", "NPORT-P/A", "NT NPORT-P", "13F-HR", "13F-HR/A", "N-PX", "N-PX/A",
    "N-CSR", "N-CSRS", "424B2", "FWP", "497", "497K", "485BPOS"))
# ...and mined separately here: mutual funds' marks on the aircraft-fund vehicles
# (e.g. Guggenheim's funded/unfunded delayed-draw loan to FTAI Aircraft Leasing).
FUND_QUERIES = ['"FTAI Aircraft"']
# Holdings filings: thousands of funds just own FTAI shares. These forms are skipped,
# except fund-report lines that are NOT common stock (e.g. the aircraft fund's
# warehouse loan), which are parsed and alerted as FUND MARK.
HOLDINGS_FORMS = ("NPORT", "NT NPORT", "13F", "N-PX", "N-CSR", "N-Q", "424B2", "FWP", "485", "497")
# Filers whose own filings are noise: FTAI itself and its affiliates.
EXCLUDE_FILER_TERMS = ("FTAI AVIATION", "FTAI INFRASTRUCTURE",
                       "FORTRESS TRANSPORTATION & INFRASTRUCTURE",
                       "FORTRESS TRANSPORTATION AND INFRASTRUCTURE")
# Forms worth flagging first. N-PORT = fund holdings/marks; 8-K/10-Q/10-K exhibits = contracts.
PRIORITY_FORMS = ("10-Q", "10-K", "8-K", "S-1", "F-1", "20-F", "6-K", "NPORT-P", "N-PORT",
                  "424B", "SC 13D", "SC 13G", "SCHEDULE 13D")  # proxies = peer-group noise
PAGE = 100
MIN_INTERVAL = 0.15  # keep well under SEC's 10 requests/second


def _filer(src: dict) -> str:
    names = src.get("display_names") or []
    # "Global Crossing Airlines Group Inc.  (JET)  (CIK 0001846084)" -> name only
    return "; ".join(re.sub(r"\s+\(.*$", "", n).strip() for n in names) or "?"


def _url(hit: dict) -> str:
    src = hit["_source"]
    adsh, _, fname = hit["_id"].partition(":")
    cik = (src.get("ciks") or ["0"])[0].lstrip("0") or "0"
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh.replace('-', '')}/{fname}"


def is_own_filing(src: dict) -> bool:
    filer = _filer(src).upper()
    return any(t in filer for t in EXCLUDE_FILER_TERMS)


def search(query: str, start: date, end: date, forms: str | None, session: requests.Session):
    """Yield every hit for a query, paging through results."""
    offset, last = 0, 0.0
    while True:
        params = {"q": query, "dateRange": "custom", "startdt": start.isoformat(),
                  "enddt": end.isoformat(), "from": offset}
        if forms:
            params["forms"] = forms
        wait = MIN_INTERVAL - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        last = time.time()
        r = session.get(EFTS, params=params, timeout=60)
        if r.status_code in (429, 500, 502, 503):
            retries = getattr(search, "_retries", 0)
            if retries >= 3:
                print(f"[edgar] {query}: giving up at offset {offset} ({r.status_code})")
                search._retries = 0
                return
            search._retries = retries + 1
            time.sleep(5 * (retries + 1))
            continue
        search._retries = 0
        r.raise_for_status()
        hits = r.json().get("hits", {}).get("hits", [])
        yield from hits
        if len(hits) < PAGE or offset >= 9900:  # EFTS caps paging at 10k
            break
        offset += PAGE


def nport_lines(url: str, session: requests.Session | None) -> list[dict]:
    """Non-equity FTAI positions in an N-PORT filing (loans, LP stakes, notes)."""
    if session is None:
        return []
    xml_url = url.rsplit("/", 1)[0] + "/primary_doc.xml"
    t = session.get(xml_url, timeout=90).text
    out = []
    for b in re.findall(r"<invstOrSec>.*?</invstOrSec>", t, re.S):
        if "FTAI" not in b.upper():
            continue
        g = lambda k: (re.search(f"<{k}>(.*?)</{k}>", b, re.S) or [None, ""])[1].strip()
        if g("assetCat") == "EC":  # plain FTAI shares
            continue
        out.append({"name": g("name") or g("title"), "balance": g("balance"), "units": g("units"),
                    "value": g("valUSD"), "cat": g("assetCat"), "level": g("fairValLevel")})
    return out


def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _fmt_lines(lines: list[dict]) -> str:
    parts = []
    for l in lines:
        bal, val = _num(l["balance"]), _num(l["value"])
        px = f", {val / bal * 100:.1f}% of par" if bal and val and l["units"] == "PA" else ""
        parts.append(f"{l['name']}: bal {bal:,.0f} {l['units']} → value ${val:,.0f}{px}")
    return "; ".join(parts)


def process(con, query: str, hits, session: requests.Session | None = None) -> list[dict]:
    alerts, ts = [], now_utc()
    con.execute("""CREATE TABLE IF NOT EXISTS fund_marks (hit_id TEXT, filer TEXT, file_date TEXT,
                   name TEXT, balance REAL, value REAL, cat TEXT)""")
    for hit in hits:
        src = hit.get("_source", {})
        if is_own_filing(src):
            continue
        hit_id = hit["_id"]
        if con.execute("SELECT 1 FROM edgar_seen WHERE hit_id=?", (hit_id,)).fetchone():
            continue
        form = src.get("form") or src.get("file_type") or "?"
        filer, url = _filer(src), _url(hit)
        con.execute("INSERT INTO edgar_seen VALUES (?,?,?,?,?,?,?)",
                    (hit_id, query, form, src.get("file_date"), filer, url, ts))
        if form.upper().startswith(HOLDINGS_FORMS):
            if not form.upper().startswith("NPORT") or not hit_id.endswith("primary_doc.xml"):
                continue
            try:
                lines = nport_lines(url, session)
            except requests.RequestException:
                lines = []
            if not lines:
                continue
            con.executemany("INSERT INTO fund_marks VALUES (?,?,?,?,?,?,?)",
                            [(hit_id, filer, src.get("file_date"), l["name"],
                              _num(l["balance"]), _num(l["value"]), l["cat"]) for l in lines])
            alerts.append({"kind": "FUND MARK", "key": hit_id, "priority": True,
                           "text": f"🔴 {filer} · N-PORT filed {src.get('file_date')} · "
                                   f"{_fmt_lines(lines)} · [filing]({url})"})
            continue
        doc = src.get("file_type") or ""
        desc = src.get("file_description") or ""
        priority = any(form.upper().startswith(f) for f in PRIORITY_FORMS)
        alerts.append({
            "kind": f"{form} mention", "key": hit_id, "priority": priority,
            "text": f"{'🔴' if priority else '⚪'} {filer} · {form} filed {src.get('file_date')}"
                    f"{f' · {doc}' if doc and doc != form else ''}{f' ({desc})' if desc else ''}"
                    f" · matched {query} · [document]({url})",
        })
    con.commit()
    return alerts


def cmd_run(args) -> None:
    con = connect()
    queries = [args.query] if args.query else QUERIES
    alerts: list[dict] = []
    if args.from_json:
        hits = json.load(open(args.from_json)).get("hits", {}).get("hits", [])
        alerts = process(con, queries[0], hits)
    else:
        end = date.today()
        start = end - timedelta(days=args.days)
        s = requests.Session()
        s.headers.update({"User-Agent": SEC_UA, "Accept": "application/json"})
        jobs = [(q, args.forms or EXCLUDE_FORMS) for q in queries]
        if not args.query:
            jobs += [(q, "NPORT-P") for q in FUND_QUERIES]
        for q, forms in jobs:
            new = process(con, q, search(q, start, end, forms, s), s)
            print(f"[edgar] {q}{' (fund reports)' if forms == 'NPORT-P' else ''}: {len(new)} new")
            alerts += new
    alerts.sort(key=lambda a: (not a["priority"], a["text"]))
    record_alerts(con, "edgar", alerts)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--days", type=int, default=7, help="look-back window (default 7)")
    r.add_argument("--forms", help="comma-separated form filter, e.g. 10-Q,8-K,NPORT-P")
    r.add_argument("--query", help="run one query instead of the default list")
    r.add_argument("--from-json", help="process a saved EFTS response (offline testing)")
    r.set_defaults(func=cmd_run)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
