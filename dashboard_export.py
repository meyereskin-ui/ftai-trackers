#!/usr/bin/env python3
"""Turn data/ftai.db into the derived numbers the FTAI dashboard shows.

Read-only: it never changes the trackers' tables. Writes two JSON files:
  data/dashboard/latest.json        -> artifact db doc  dash/latest
  data/dashboard/history-<day>.json -> artifact db doc  history/<day>
The daily Claude run pushes both into the dashboard artifact with ArtifactData.

    python3 dashboard_export.py            # writes the files, prints a one-line summary
"""
import csv
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "ftai.db"
OUT = ROOT / "data" / "dashboard"

FTAI_OWNERS = ("UMB BANK", "FTAI", "FORTRESS")
FILING_AGENTS = ("DAUGHERTY FOWLER", "DFPH&J")
AIRLINE_WORDS = ("AIRLINES", "AIR LINES", "AIRWAYS", "AIRLINE", "SUN COUNTRY", "ALLEGIANT")
PARTOUT = ("AERSALE", "GA TELESIS", "AAR ", "UNICAL", "WILLIS", "KELLSTROM", "VALLAIR", "AERCAP",
           "MAINSTREAM", "JET MIDWEST", "STANDARDAERO", "MTU", "SAFRAN", "GE CAPITAL", "AIRBORNE MAINTENANCE",
           "SOURCE ONE", "HAITE", "AIR SALVAGE", "PROCURE AEROSPACE", "MAGELLAN", "VAS AERO", "WENCOR",
           "EIRSALE", "SKYTECH", "CORE AVIATION", "AERO CAPITAL")


def owner_class(name: str) -> str:  # mirrors fleet_tracker.owner_class
    n = (name or "").upper()
    if any(w in n for w in FTAI_OWNERS):
        return "FTAI/UMB trust"
    if any(w in n for w in PARTOUT):
        return "Part-out / competitor"
    if any(w in n for w in AIRLINE_WORDS):
        return "Airline"
    if "TRUST" in n:
        return "Other lessor trust"
    return "Other"


def d(s):
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def main():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    today = date.today()
    out = {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}

    # ---- announcements + lead time -------------------------------------
    anns = []
    p = ROOT / "data" / "announcements.csv"
    if p.exists():
        anns = [r for r in csv.DictReader(p.open()) if r.get("date")]
    scored = {r["ann"]: r["result"] for r in c.execute("select * from leadtime_scored")}
    leads = []
    for a in anns:
        res = scored.get(f"{a['date']}|{a['headline']}", "")
        m = re.search(r"led by (-?\d+) day", res)
        leads.append({"date": a["date"], "headline": a["headline"], "url": a.get("url", ""),
                      "lead_days": int(m.group(1)) if m else None, "result": res})
    out["announcements"] = leads

    # ---- FT tail pipeline ------------------------------------------------
    tails = []
    for r in c.execute("select * from faa_first_seen"):
        owner = (r["owner"] or "").upper()
        likely = any(w in owner for w in FTAI_OWNERS + FILING_AGENTS)
        if not likely:
            continue
        rsv, reg = r["first_rsv"] or "", r["first_reg"] or ""
        stage = "Registered" if reg else "Reserved"
        start = d(rsv) or d(reg)
        tails.append({"n": "N" + r["n"], "reserved": rsv, "registered": reg, "owner": r["owner"],
                      "model": r["model"] or "", "stage": stage,
                      "age_days": (today - start).days if start else None})
    # attach each tail to the announcement that scored its batch, if any
    for t in tails:
        t["announced"] = next((a["date"] for a in leads if t["n"] in a["result"]), "")
    # later registrations of the same type as an announced batch are that deal's deliveries
    for a in leads:
        models = {t["model"] for t in tails if t["n"] in a["result"] and t["model"]}
        for t in tails:
            if not t["announced"] and t["model"] in models and t["registered"] >= (d(a["date"]) - timedelta(days=30)).isoformat():
                t["announced"] = a["date"]
    # batches = same stage-start date
    batches = defaultdict(list)
    for t in tails:
        batches[t["reserved"] or t["registered"]].append(t)
    unannounced = []
    for day, ts in sorted(batches.items()):
        if day >= "2026-01-01" and not any(t["announced"] for t in ts) and len(ts) >= 2:
            unannounced.append({"date": day, "tails": [t["n"] for t in ts], "count": len(ts),
                                "stage": ts[0]["stage"], "age_days": (today - d(day)).days,
                                "who": ts[0]["owner"]})
    out["tails"] = sorted(tails, key=lambda t: (t["reserved"] or t["registered"]), reverse=True)
    out["unannounced_batches"] = unannounced
    recent = [t for t in tails if (t["reserved"] or t["registered"]) >= (today - timedelta(days=90)).isoformat()]
    westjet = [t for t in tails if "737-7CT" in t["model"]]
    out["pipeline"] = {
        "reserved_open": sum(t["stage"] == "Reserved" for t in recent),
        "registered_90d": sum(t["stage"] == "Registered" for t in recent),
        "oldest_open_days": max([t["age_days"] for t in recent if t["stage"] == "Reserved"] or [0]),
        "westjet_737_7ct_registered": len(westjet),
        "avg_lead_days": (sum(x["lead_days"] for x in leads if x["lead_days"] is not None) /
                          max(1, sum(x["lead_days"] is not None for x in leads))) if leads else None,
    }

    # ---- aircraft-fund warehouse loan (N-PORT marks) ---------------------
    by_month = defaultdict(lambda: {"funded": 0.0, "unfunded": 0.0, "funded_value": 0.0})
    spv = []
    other_marks = []
    for r in c.execute("select * from fund_marks order by file_date"):
        nm = r["name"].upper()
        mo = r["file_date"][:7]
        if "DDTL" in nm:
            k = "unfunded" if "UNF" in nm else "funded"
            by_month[mo][k] += r["balance"] or 0
            if k == "funded":
                by_month[mo]["funded_value"] += r["value"] or 0
        elif "SPV" in nm:
            spv.append({"date": r["file_date"], "filer": r["filer"], "name": r["name"], "value": r["value"]})
        else:
            other_marks.append({"date": r["file_date"], "filer": r["filer"], "name": r["name"],
                                "balance": r["balance"], "value": r["value"],
                                "pct_par": (r["value"] / r["balance"] * 100) if r["balance"] else None})
    ddtl = []
    for mo, v in sorted(by_month.items()):
        tot = v["funded"] + v["unfunded"]
        ddtl.append({"month": mo, "funded": round(v["funded"]), "unfunded": round(v["unfunded"]),
                     "draw_pct": round(v["funded"] / tot * 100, 1) if tot else None,
                     "pct_par": round(v["funded_value"] / v["funded"] * 100, 2) if v["funded"] else None})
    out["warehouse"] = ddtl
    out["spv_marks"] = spv
    out["other_marks"] = other_marks

    # ---- Form D vehicles -------------------------------------------------
    out["formd"] = [dict(r) for r in c.execute(
        "select name, form, file_date, offering, sold, investors, agents, url from formd_filings order by file_date desc")]

    # ---- insiders --------------------------------------------------------
    trades = [dict(r) for r in c.execute("select * from insider_trades order by date")]
    yr = (today - timedelta(days=365)).isoformat()
    buys = [t for t in trades if t["code"] == "P"]
    sells = [t for t in trades if t["code"] == "S"]
    q = defaultdict(lambda: {"buy": 0.0, "sell": 0.0})
    for t in buys + sells:
        dt = d(t["date"])
        key = f"{dt.year}-Q{(dt.month - 1)//3 + 1}"
        q[key]["buy" if t["code"] == "P" else "sell"] += t["shares"] * t["price"]
    out["insider_quarters"] = [{"q": k, "buy": round(v["buy"]), "sell": round(v["sell"])} for k, v in sorted(q.items())]
    out["insider_open_market"] = [
        {"date": t["date"], "owner": t["owner"].title(), "title": t["title"], "side": "Buy" if t["code"] == "P" else "Sell",
         "shares": t["shares"], "price": t["price"], "value": round(t["shares"] * t["price"]), "plan": bool(t["plan"])}
        for t in sorted(buys + sells, key=lambda t: t["date"], reverse=True)]
    out["insiders"] = {
        "buys_12m": sum(t["date"] >= yr for t in buys),
        "buy_value_12m": round(sum(t["shares"] * t["price"] for t in buys if t["date"] >= yr)),
        "sell_value_12m": round(sum(t["shares"] * t["price"] for t in sells if t["date"] >= yr)),
        "last_buy": max((t["date"] for t in buys), default=""),
    }

    # ---- CFM56 fleet -----------------------------------------------------
    fleet = [json.loads(x) for (x,) in c.execute("select data from fleet_state")]
    mix = Counter(owner_class(f["owner"]) for f in fleet)
    out["fleet_mix"] = [{"cls": k, "n": v} for k, v in mix.most_common()]
    fam = Counter()
    for f in fleet:
        m = f["model"]
        fam["A320ceo family" if re.search(r"A3(19|20|21)", m) else "737NG"] += 1
    out["fleet_family"] = dict(fam)
    out["fleet_runs"] = [dict(r) for r in c.execute("select * from fleet_runs order by day")]
    out["fleet_events"] = [dict(r) for r in c.execute("select * from fleet_events order by day desc limit 200")]
    ev = out["fleet_events"]
    m30 = (today - timedelta(days=30)).isoformat()
    out["fleet"] = {"total": out["fleet_runs"][-1]["total"] if out["fleet_runs"] else len(fleet),
                    "delta": (out["fleet_runs"][-1]["total"] - out["fleet_runs"][-2]["total"]) if len(out["fleet_runs"]) > 1 else 0,
                    "exits_30d": sum(e["to_class"] == "exit" and e["day"] >= m30 for e in ev),
                    "to_partout_30d": sum("part-out" in (e["to_class"] or "") and e["day"] >= m30 for e in ev),
                    "to_ftai_30d": sum("FTAI" in (e["to_class"] or "") and e["day"] >= m30 for e in ev)}

    # ---- EIA power -------------------------------------------------------
    units = [json.loads(r["data"]) | {"first_seen": r["first_seen"], "as_of": r["as_of"]}
             for r in c.execute("select * from eia_units")]
    plants = defaultdict(lambda: {"units": 0, "mw": 0.0})
    for u in units:
        p = plants[(u["plant"], u["entity"], u["state"], u["county"], u["status"], u["sheet"])]
        p["units"] += 1
        p["mw"] += u["mw"]
        p["unit_mw"] = u["mw"]
        p["online"] = f"{u['year']}-{u['month']:02d}"
        p["lat"], p["lon"] = u["lat"], u["lon"]
    out["power_plants"] = [{"plant": k[0], "entity": k[1], "state": k[2], "county": k[3], "status": k[4],
                            "sheet": k[5], **v, "mw": round(v["mw"], 1)} for k, v in plants.items()]
    out["power"] = {"units": len(units), "mw": round(sum(u["mw"] for u in units), 1),
                    "plants": len(plants), "as_of": units[0]["as_of"] if units else ""}

    # ---- EDGAR + alert feed ---------------------------------------------
    forms = Counter()
    for r in c.execute("select form from edgar_seen"):
        forms[re.sub(r"/A$", "", r["form"] or "?")] += 1
    out["edgar_forms"] = [{"form": k, "n": v} for k, v in forms.most_common(12)]
    alerts = []
    for r in c.execute("select * from alerts order by ts desc limit 400"):
        try:
            det = json.loads(r["detail"])
        except Exception:
            det = {}
        txt = det.get("text", "")
        link = re.search(r"\]\((https?://[^)]+)\)", txt)
        clean = re.sub(r"\s*·\s*\[[^\]]+\]\([^)]+\)", "", txt).replace("🔴", "").replace("⚪", "").strip()
        alerts.append({"ts": r["ts"], "tracker": r["tracker"], "kind": r["kind"],
                       "red": bool(det.get("priority")) or "🔴" in txt, "text": clean,
                       "url": link.group(1) if link else ""})
    out["alerts"] = alerts
    runs = sorted({a["ts"][:10] for a in alerts})
    last = runs[-1] if runs else ""
    out["alert_days"] = [{"day": day, "red": sum(a["red"] for a in alerts if a["ts"].startswith(day)),
                          "white": sum(not a["red"] for a in alerts if a["ts"].startswith(day))} for day in runs]
    out["last_run"] = last

    OUT.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(out, default=str)
    (OUT / "latest.json").write_text(blob)
    hist = {"day": today.isoformat(), "fleet_total": out["fleet"]["total"],
            "reserved_open": out["pipeline"]["reserved_open"], "registered_90d": out["pipeline"]["registered_90d"],
            "ft_tails": len(tails), "red_alerts_today": next((x["red"] for x in out["alert_days"] if x["day"] == today.isoformat()), 0),
            "power_mw": out["power"]["mw"], "edgar_hits": sum(forms.values()),
            "draw_pct": ddtl[-1]["draw_pct"] if ddtl else None}
    (OUT / f"history-{today.isoformat()}.json").write_text(json.dumps(hist))
    print(f"dashboard export: {len(blob)//1024} KB · {len(tails)} FT tails · fleet {out['fleet']['total']} · "
          f"{len(alerts)} alerts · files in data/dashboard/")


if __name__ == "__main__":
    main()
