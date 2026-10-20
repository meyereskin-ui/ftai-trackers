#!/usr/bin/env bash
# Run all trackers. For cron on a Mac/Linux box, e.g. weekdays 7:15am:
#   15 7 * * 1-5 /path/to/ftai-trackers/run_daily.sh >> /path/to/ftai-trackers/data/cron.log 2>&1
cd "$(dirname "$0")"
status=0
python3 faa_tracker.py run            || status=1   # FT tails (downloads + caches the registry)
python3 fleet_tracker.py run          || status=1   # CFM56 fleet, same registry file
python3 edgar_tracker.py run --days 7 || status=1
python3 formd_tracker.py run          || status=1
python3 insider_tracker.py run        || status=1
python3 eia_tracker.py run            || status=1   # no-op except when EIA posts a new month
python3 leadtime.py run               || status=1   # scores any new rows in data/announcements.csv
exit $status
