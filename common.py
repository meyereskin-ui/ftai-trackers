"""Shared storage and alerting for the FTAI trackers."""
from __future__ import annotations

import json
import os
import smtplib
import sqlite3
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("FTAI_DB", ROOT / "data" / "ftai.db"))
ALERT_LOG = Path(os.environ.get("FTAI_ALERT_LOG", ROOT / "data" / "alerts.md"))

# SEC asks for a descriptive User-Agent with contact info. Set FTAI_UA to your own.
SEC_UA = os.environ.get("FTAI_UA", "FTAI research tracker (set FTAI_UA to 'Name email')")
# The FAA site rejects some non-browser user agents.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS faa_snapshot (
            snap_date TEXT, source TEXT, n_number TEXT, data TEXT,
            PRIMARY KEY (snap_date, source, n_number));
        CREATE TABLE IF NOT EXISTS edgar_seen (
            hit_id TEXT PRIMARY KEY, query TEXT, form TEXT, file_date TEXT,
            filer TEXT, url TEXT, first_seen TEXT);
        CREATE TABLE IF NOT EXISTS alerts (
            ts TEXT, tracker TEXT, kind TEXT, key TEXT, detail TEXT);
        """
    )
    return con


def record_alerts(con: sqlite3.Connection, tracker: str, alerts: list[dict]) -> None:
    """Store alerts, append them to the markdown log, print them, optionally email."""
    if not alerts:
        print(f"[{tracker}] no changes")
        return
    ts = now_utc()
    con.executemany(
        "INSERT INTO alerts VALUES (?,?,?,?,?)",
        [(ts, tracker, a["kind"], a["key"], json.dumps(a, default=str)) for a in alerts],
    )
    con.commit()
    lines = [f"\n## {ts} · {tracker} · {len(alerts)} alert(s)\n"]
    lines += [f"- **{a['kind']}** {a['text']}" for a in alerts]
    block = "\n".join(lines) + "\n"
    ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ALERT_LOG.open("a", encoding="utf-8") as f:
        f.write(block)
    print(block)
    _maybe_email(f"[FTAI {tracker}] {len(alerts)} alert(s)", block)


def _maybe_email(subject: str, body: str) -> None:
    """Email alerts if SMTP env vars are set (e.g. Gmail with an app password)."""
    host, to = os.environ.get("SMTP_HOST"), os.environ.get("ALERT_TO")
    if not (host and to):
        return
    msg = EmailMessage()
    msg["Subject"], msg["To"] = subject, to
    msg["From"] = os.environ.get("SMTP_USER", to)
    msg.set_content(body)
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", 587))) as s:
        s.starttls()
        if os.environ.get("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)
