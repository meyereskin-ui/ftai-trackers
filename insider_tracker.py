"""Form 4 monitor: FTAI officers' and directors' trades.

Open-market purchases (code P) are the rare, meaningful signal and are always 🔴.
Open-market sales (S) are 🔴 when they are large (> $1M) and NOT under a pre-planned
10b5-1 plan; planned sales, grants, option exercises and tax withholding are ⚪.

Usage: python insider_tracker.py run
"""
from __future__ import annotations

import argparse
import re
import time

import requests

from common import SEC_UA, connect, now_utc, record_alerts

ISSUER_CIK = "1590364"  # FTAI Aviation Ltd.
BROWSE = "https://www.sec.gov/cgi-bin/browse-edgar"
CODES = {"P": "open-market BUY", "S": "open-market sale", "A": "grant/award", "M": "option exercise",
         "F": "tax withholding", "G": "gift", "D": "disposition to issuer", "C": "conversion"}
BIG_SALE = 1_000_000


def _get(s, url, **params):
    for attempt in range(5):  # SEC returns 429/503 when throttling; back off and retry
        time.sleep(0.3 + attempt * 5)
        r = s.get(url, params=params or None, timeout=60)
        if r.status_code not in (429, 500, 502, 503):
            break
    r.raise_for_status()
    return r


def _val(block: str, tag: str) -> str:
    m = re.search(f"<{tag}>.*?<value>(.*?)</value>", block, re.S)
    return m.group(1).strip() if m else ""


def parse_form4(xml: str) -> dict:
    owner = (re.search(r"<rptOwnerName>(.*?)</rptOwnerName>", xml) or [0, "?"])[1].strip()
    title = (re.search(r"<officerTitle>(.*?)</officerTitle>", xml) or [0, ""])[1].strip()
    if not title and "<isDirector>1</isDirector>" in xml.replace("true", "1"):
        title = "Director"
    plan = bool(re.search(r"<aff10b5One>(1|true)</aff10b5One>", xml)) or "10b5-1" in xml
    txs = []
    for b in re.findall(r"<nonDerivativeTransaction>.*?</nonDerivativeTransaction>", xml, re.S):
        code = (re.search(r"<transactionCode>(.*?)</transactionCode>", b) or [0, ""])[1].strip()
        try:
            sh = float(_val(b, "transactionShares") or 0)
            px = float(_val(b, "transactionPricePerShare") or 0)
        except ValueError:
            sh, px = 0.0, 0.0
        txs.append({"date": _val(b, "transactionDate"), "code": code, "shares": sh, "price": px,
                    "after": _val(b, "sharesOwnedFollowingTransaction")})
    return {"owner": owner, "title": title, "plan": plan, "txs": txs}


def cmd_run(args) -> None:
    con = connect()
    con.execute("""CREATE TABLE IF NOT EXISTS insider_trades (acc TEXT, owner TEXT, title TEXT, date TEXT,
                   code TEXT, shares REAL, price REAL, plan INTEGER, PRIMARY KEY (acc, date, code, shares))""")
    con.execute("CREATE TABLE IF NOT EXISTS insider_seen (acc TEXT PRIMARY KEY, seen TEXT)")
    baseline = con.execute("SELECT COUNT(*) FROM insider_seen").fetchone()[0] == 0
    s = requests.Session()
    s.headers.update({"User-Agent": SEC_UA})
    feed = _get(s, BROWSE, action="getcompany", CIK=ISSUER_CIK, type="4", count=100, output="atom").text
    alerts = []
    for e in re.findall(r"<entry>.*?</entry>", feed, re.S):
        acc = (re.search(r"<accession-number>(.*?)</accession-number>", e) or [0, ""])[1]
        if not acc or con.execute("SELECT 1 FROM insider_seen WHERE acc=?", (acc,)).fetchone():
            continue
        folder = f"https://www.sec.gov/Archives/edgar/data/{ISSUER_CIK}/{acc.replace('-', '')}"
        items = _get(s, folder + "/index.json").json()["directory"]["item"]
        xml_name = next((i["name"] for i in items if i["name"].endswith(".xml")), None)
        con.execute("INSERT INTO insider_seen VALUES (?,?)", (acc, now_utc()))
        if not xml_name:
            continue
        f = parse_form4(_get(s, f"{folder}/{xml_name}").text)
        for t in f["txs"]:
            con.execute("INSERT OR IGNORE INTO insider_trades VALUES (?,?,?,?,?,?,?,?)",
                        (acc, f["owner"], f["title"], t["date"], t["code"], t["shares"], t["price"], int(f["plan"])))
            value = t["shares"] * t["price"]
            hot = t["code"] == "P" or (t["code"] == "S" and value > BIG_SALE and not f["plan"])
            if baseline and not (t["code"] in "PS"):
                continue
            px = f" @ ${t['price']:,.2f} (${value:,.0f})" if t["price"] else ""
            alerts.append({"kind": "FORM 4", "key": acc, "text":
                f"{'🔴' if hot else '⚪'} {f['owner'].title()}{', ' + f['title'] if f['title'] else ''} · "
                f"{CODES.get(t['code'], t['code'])} {t['shares']:,.0f} sh"
                f"{px}"
                f" on {t['date']}{' · 10b5-1 plan' if f['plan'] and t['code'] == 'S' else ''}"
                f" · holds {float(t['after'] or 0):,.0f} after · [filing]({folder}/)"})
    con.commit()
    if baseline:
        print("[insider] baseline: last 100 Form 4s stored; open-market trades listed below")
    record_alerts(con, "insider", alerts)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run").set_defaults(func=cmd_run)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
