"""Form D monitor for FTAI's private fund vehicles (the aircraft fund, SCI).

FTAI's aircraft fund raises money through LPs that each file a Form D with the
SEC, e.g. "FTAI Aircraft Leasing SPV (2026) LP". This tracker
  1. lists every EDGAR entity whose name starts with FTAI and alerts when a new one
     appears (a new fund vintage or vehicle, usually before any announcement);
  2. checks each fund vehicle for new Form D / D/A filings and reports the amount
     sold, the investor count and the placement agent.
Initial Form Ds usually show $0 sold ("first sale yet to occur"); amendments are
where the fundraising totals show up.

Usage: python formd_tracker.py run
"""
from __future__ import annotations

import argparse
import re
import time

import requests

from common import SEC_UA, connect, now_utc, record_alerts

BROWSE = "https://www.sec.gov/cgi-bin/browse-edgar"
NAME_PREFIXES = ("FTAI",)
FUND_HINTS = ("AIRCRAFT LEASING", "SPV", "FUND", "PARTNERS", " LP", " L.P.")
OWN = ("FTAI AVIATION LTD", "FTAI INFRASTRUCTURE")


def _get(s: requests.Session, url: str, **params) -> str:
    for attempt in range(5):  # SEC returns 429/503 when throttling; back off and retry
        time.sleep(0.3 + attempt * 5)
        r = s.get(url, params=params or None, timeout=60)
        if r.status_code not in (429, 500, 502, 503):
            break
    r.raise_for_status()
    return r.text


def list_entities(s: requests.Session) -> dict[str, str]:
    out = {}
    for prefix in NAME_PREFIXES:
        t = _get(s, BROWSE, company=prefix, action="getcompany", count=100)
        for cik, name in re.findall(r'CIK=(\d+)[^>]*>\d+</a></td>\s*<td[^>]*>(.*?)</td>', t, re.S):
            name = re.sub(r"<[^>]+>", " ", name)
            name = re.sub(r"\s*SIC:.*$", "", name).replace("&amp;", "&").strip()
            out[cik.lstrip("0")] = name
    return out


def form_d_filings(s: requests.Session, cik: str) -> list[dict]:
    t = _get(s, BROWSE, action="getcompany", CIK=cik, type="D", count=40, output="atom")
    out = []
    for e in re.findall(r"<entry>.*?</entry>", t, re.S):
        g = lambda k: (re.search(f"<{k}>(.*?)</{k}>", e, re.S) or [None, ""])[1].strip()
        if g("filing-type").upper().startswith("D"):
            out.append({"acc": g("accession-number"), "date": g("filing-date"), "form": g("filing-type")})
    return out


def parse_form_d(s: requests.Session, cik: str, acc: str) -> dict:
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/primary_doc.xml"
    x = _get(s, url)
    g = lambda k: (re.search(f"<{k}>(.*?)</{k}>", x, re.S) or [None, ""])[1].strip()
    agents = re.findall(r"<recipientName>(.*?)</recipientName>", x)
    first = "yet to occur" if "<yetToOccur>true" in x else g("value")
    return {"offering": g("totalOfferingAmount"), "sold": g("totalAmountSold"),
            "investors": g("totalNumberAlreadyInvested"), "first_sale": first,
            "agents": ", ".join(dict.fromkeys(a.strip() for a in agents)), "url": url}


def _money(v: str) -> str:
    return f"${int(v):,}" if v.isdigit() else v or "?"


def cmd_run(args) -> None:
    con = connect()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS formd_entities (cik TEXT PRIMARY KEY, name TEXT, first_seen TEXT);
        CREATE TABLE IF NOT EXISTS formd_filings (acc TEXT PRIMARY KEY, cik TEXT, name TEXT, form TEXT,
            file_date TEXT, offering TEXT, sold TEXT, investors TEXT, agents TEXT, url TEXT);""")
    s = requests.Session()
    s.headers.update({"User-Agent": SEC_UA})
    known = {r[0] for r in con.execute("SELECT cik FROM formd_entities")}
    baseline = not known
    entities = list_entities(s)
    alerts = []
    for cik, name in entities.items():
        if cik in known:
            continue
        con.execute("INSERT INTO formd_entities VALUES (?,?,?)", (cik, name, now_utc()))
        if not baseline:
            alerts.append({"kind": "NEW FTAI ENTITY", "key": cik,
                           "text": f"🔴 {name} (CIK {cik}) appeared on EDGAR: possibly a new fund "
                                   f"vehicle or subsidiary · https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"})
    con.commit()
    funds = {c: n for c, n in entities.items()
             if not any(o in n.upper() for o in OWN) and any(h in n.upper() for h in FUND_HINTS)}
    for cik, name in funds.items():
        for f in form_d_filings(s, cik):
            if con.execute("SELECT 1 FROM formd_filings WHERE acc=?", (f["acc"],)).fetchone():
                continue
            d = parse_form_d(s, cik, f["acc"])
            con.execute("INSERT INTO formd_filings VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (f["acc"], cik, name, f["form"], f["date"], d["offering"], d["sold"],
                         d["investors"], d["agents"], d["url"]))
            raised = d["sold"].isdigit() and int(d["sold"]) > 0
            alerts.append({"kind": f"FORM {f['form']}", "key": f["acc"],
                           "text": f"{'🔴' if raised or not baseline else '⚪'} {name} · {f['form']} filed {f['date']} · "
                                   f"sold {_money(d['sold'])} of {_money(d['offering'])} · "
                                   f"{d['investors'] or '?'} investors · first sale {d['first_sale'] or '?'}"
                                   f"{' · agent ' + d['agents'] if d['agents'] else ''} · [filing]({d['url']})"})
    con.commit()
    if baseline:
        print(f"[formd] baseline: {len(entities)} FTAI entities, {len(funds)} fund vehicles")
    record_alerts(con, "formd", alerts)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run").set_defaults(func=cmd_run)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
