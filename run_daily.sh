#!/usr/bin/env bash
# Run both trackers. For cron on a Mac/Linux box, e.g. weekdays 7:15am:
#   15 7 * * 1-5 /path/to/ftai-trackers/run_daily.sh >> /path/to/ftai-trackers/data/cron.log 2>&1
set -euo pipefail
cd "$(dirname "$0")"
python3 faa_tracker.py run
python3 edgar_tracker.py run --days 7
