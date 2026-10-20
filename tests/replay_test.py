"""Offline replay of the Aug-Sep 2026 WestJet sequence from CV Research's piece,
using synthetic registry files in the FAA's format. Run: python tests/replay_test.py
Registrant names below are placeholders; the tail numbers and dates follow CV's account."""
import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
tmp = Path(tempfile.mkdtemp())
os.environ["FTAI_DB"] = str(tmp / "test.db")
os.environ["FTAI_ALERT_LOG"] = str(tmp / "alerts.md")

MASTER_H = ("N-NUMBER,SERIAL NUMBER,MFR MDL CODE,ENG MFR MDL,YEAR MFR,TYPE REGISTRANT,NAME,STREET,"
            "STREET2,CITY,STATE,ZIP CODE,REGION,COUNTY,COUNTRY,LAST ACTION DATE,CERT ISSUE DATE,"
            "CERTIFICATION,TYPE AIRCRAFT,TYPE ENGINE,STATUS CODE,MODE S CODE,FRACT OWNER,"
            "AIR WORTH DATE,OTHER NAMES(1),OTHER NAMES(2),OTHER NAMES(3),OTHER NAMES(4),"
            "OTHER NAMES(5),EXPIRATION DATE,UNIQUE ID,KIT MFR, KIT MODEL,MODE S CODE HEX,")
RSV_H = "N-NUMBER,REGISTRANT,STREET,STREET2,CITY,STATE,ZIP CODE,RSV DATE,TR,EXP DATE,N-NUM-CHG,PURGE DATE,"
DEREG_H = "N-NUMBER,SERIAL-NUMBER,MFR-MDL-CODE,STATUS-CODE,NAME,CITY-MAIL,STATE-ABBREV-MAIL,CANCEL-DATE,"
REF_H = "CODE,MFR,MODEL,TYPE-ACFT,"
FIRM = "EXAMPLE AVIATION LAW FIRM"


def master(n, sn, code, name, city, state, cert):
    f = [n, sn, code, "", "2004", "7", name, "", "", city, state, "", "", "", "US",
         cert, cert, "", "5", "5", "V", "", "", "", "", "", "", "", "", "", "", "", "", ""]
    return ",".join(f"{x:<12}" if i == 0 else x for i, x in enumerate(f)) + ","


def rsv(n, date, who=FIRM):
    return f"{n:<6},{who},PO BOX 1,,OKLAHOMA CITY,OK,73101,{date},,20271231,,,"


def make_zip(master_rows, rsv_rows, dereg_rows=()):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("MASTER.txt", "﻿" + "\n".join([MASTER_H, *master_rows]))
        z.writestr("RESERVED.txt", "\n".join([RSV_H, *rsv_rows]))
        z.writestr("DEREG.txt", "\n".join([DEREG_H, *dereg_rows]))
        z.writestr("ACFTREF.txt", "\n".join([REF_H, "1384410,BOEING,737-7CT,5,", "2072738,CESSNA,172S,4,",
                                             "1385505,BOEING,737-8AS,5,"]))
        z.writestr("ENGINE.txt", "CODE,MFR,")
    p = tmp / f"z{len(list(tmp.glob('z*')))}.zip"
    p.write_bytes(b.getvalue())
    return p


def run(*a):
    out = subprocess.run([sys.executable, *a], cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8")
    if out.returncode:
        print(out.stdout, out.stderr)
        raise SystemExit(1)
    return out.stdout


ga = master("123FT", "17281234", "2072738", "SMITH JOHN", "TULSA", "OK", "20190301")
old = master("500FT", "29931", "1385505", "UMB BANK NA TRUSTEE", "KANSAS CITY", "MO", "20240210")
aug = [rsv(n, "20260828") for n in ("287FT", "295FT", "296FT", "864FT")]
sep = [rsv(n, "20260918") for n in ("754FT", "755FT", "756FT", "759FT", "760FT", "761FT", "762FT")]

# Day 0: baseline (Aug 27)
d0 = make_zip([ga, old], [rsv("757FT", "20260901")])
# Day 1: Aug 28 batch reservation
d1 = make_zip([ga, old], [rsv("757FT", "20260901"), *aug])
# Day 2: Sep 18 - N757FT registered to UMB, seven more reserved, a GA plane changes owner
n757 = master("757FT", "33000", "1384410", "UMB BANK NA TRUSTEE", "KANSAS CITY", "MO", "20260918")
ga2 = ga.replace("SMITH JOHN", "DOE JANE")
d2 = make_zip([ga2, old, n757], [*aug, *sep])
# Day 3: Sep 21 - N336FT registered; the old jet is deregistered (exported)
n336 = master("336FT", "33001", "1384410", "UMB BANK NA TRUSTEE", "KANSAS CITY", "MO", "20260921")
d3 = make_zip([ga2, n757, n336], [*aug, *sep],
              ["500FT,29931,1385505,E,UMB BANK NA TRUSTEE,KANSAS CITY,MO,20260921,"])

for day, z in zip(("2026-08-27", "2026-08-28", "2026-09-18", "2026-09-21"), (d0, d1, d2, d3)):
    print(f"\n######## {day}")
    print(run("faa_tracker.py", "run", "--zip", str(z), "--date", day))
print(run("faa_tracker.py", "report", "--only-likely"))

alerts = (tmp / "alerts.md").read_text(encoding="utf-8")
checks = {
    "Aug 28 batch flagged red": "🔴 N287FT · EXAMPLE AVIATION LAW FIRM" in alerts,
    "Sep 18 registration flagged": "🔴 N757FT · UMB BANK NA TRUSTEE" in alerts and "was reserved by" in alerts,
    "filing agent learned": "NEW AGENT" in alerts,
    "Sep 18 batch flagged red": "🔴 N762FT" in alerts,
    "GA owner change stays grey": "⚪ N123FT" in alerts,
    "deregistration caught": "DEREGISTERED" in alerts and "N500FT" in alerts,
}

# EDGAR: one third-party lease exhibit, one FTAI own filing (excluded), one repeat (deduped)
resp = {"hits": {"hits": [
    {"_id": "0001628280-25-050001:ex10-1.htm", "_source": {
        "ciks": ["0001846084"], "display_names": ["Global Crossing Airlines Group Inc.  (JET)  (CIK 0001846084)"],
        "form": "10-Q", "file_type": "EX-10.1", "file_description": "Engine lease agreement",
        "file_date": "2025-11-14"}},
    {"_id": "0001590364-25-000099:ftai-20250930.htm", "_source": {
        "ciks": ["0001590364"], "display_names": ["FTAI Aviation Ltd.  (FTAI)  (CIK 0001590364)"],
        "form": "10-Q", "file_date": "2025-10-30"}},
]}}
j = tmp / "efts.json"
j.write_text(json.dumps(resp))
print(run("edgar_tracker.py", "run", "--from-json", str(j), "--query", '"FTAI"'))
second = run("edgar_tracker.py", "run", "--from-json", str(j), "--query", '"FTAI"')
alerts = (tmp / "alerts.md").read_text(encoding="utf-8")
checks |= {
    "third-party exhibit caught": "Global Crossing Airlines Group Inc. · 10-Q" in alerts,
    "FTAI's own filing excluded": "FTAI Aviation Ltd." not in alerts,
    "no duplicate on rerun": "no changes" in second,
    "SEC link built": "data/1846084/000162828025050001/ex10-1.htm" in alerts,
}
print("\n".join(f"{'PASS' if v else 'FAIL'}  {k}" for k, v in checks.items()))
sys.exit(0 if all(checks.values()) else 1)
