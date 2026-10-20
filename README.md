# FTAI trackers

Two daily monitors for public data that has led FTAI's own disclosures.

| Tracker | Source | What it alerts on |
| --- | --- | --- |
| `faa_tracker.py` | FAA ReleasableAircraft.zip (free, updated nightly) | Any change to N-numbers ending in **FT**: new reservations, new registrations, deregistrations, owner/status changes |
| `edgar_tracker.py` | SEC full-text search (efts.sec.gov) | New filings by *other* companies mentioning FTAI, FTAI Power, the Strategic Capital fund or J&F Power |

Alerts go to the terminal, `data/alerts.md`, a SQLite DB (`data/ftai.db`) and, optionally, email.
🔴 = likely FTAI, ⚪ = probably noise (e.g. a Cessna that happens to end in FT).

## How the FAA tracker decides "likely FTAI"

A tail is 🔴 if any of these hold:
- the owner name includes UMB BANK, FTAI or Fortress Transportation;
- the aircraft is an airliner type (Boeing, Airbus, Embraer, Bombardier, ATR);
- it's a reservation in a **batch of 3+ FT tails reserved by one registrant on one day** (the Aug 28 and Sep 18 pattern CV described);
- it's a reservation by a **learned filing agent**: whenever a reserved FT tail turns into an FTAI-looking registration, the registrant that reserved it is remembered, and its later FT reservations are flagged even one at a time.

Reservations are the earliest signal (before registration, before the press release). Deregistrations of UMB/FT tails show exits: sales, exports or part-outs.

## Setup

```bash
pip install -r requirements.txt
export FTAI_UA="Meyer Eskin your-email@example.com"   # SEC requires a contact User-Agent

python faa_tracker.py run          # first run stores the baseline (~60 MB download)
python faa_tracker.py report --only-likely   # every likely-FTAI tail with its dates
python edgar_tracker.py run --days 365       # one-time backfill of the last year
```

Then run `./run_daily.sh` every day (cron line inside the script), or use GitHub Actions:
push this folder to a **private** repo, add the `FTAI_UA` secret (plus `SMTP_*` / `ALERT_TO` for email), and `.github/workflows/ftai-trackers.yml` runs daily and commits the database back so each run diffs against the last.

Email via Gmail: `SMTP_HOST=smtp.gmail.com`, `SMTP_USER` = your address, `SMTP_PASS` = an app password, `ALERT_TO` = where to send.

## Using it for the backtest (Step 4)

`report` prints reservation and certificate dates for every FT tail in today's file. Line those dates up against FTAI's and the fund's deal press releases to see whether WestJet was a one-off or a repeatable lead. Only the current file is public, so a lead time can only be measured for tails still registered or reserved.

## Caveats

- The FAA site sometimes refuses cloud IP ranges (GitHub Actions included). If the FAA step fails there, run the FAA tracker from your own machine.
- `"FTAI"` alone also matches FTAI Infrastructure's ecosystem; FTAI's and FTAI Infrastructure's own filings are excluded by filer name, everything else is kept.
- Public data only, and nothing from work or deal teams goes in.

`python tests/replay_test.py` replays CV's Aug–Sep 2026 WestJet sequence with synthetic files in the FAA's format and checks every alert type.
