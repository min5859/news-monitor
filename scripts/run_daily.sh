#!/usr/bin/env bash
set -euo pipefail

cd /srv/news-monitor/app
# Hold across collection, classification, artifact checks and push.
exec 9>/srv/news-monitor/data/.daily.lock
flock -n 9 || exit 1
result=0
python3 -B -m news_monitor.run \
  --data-dir /srv/news-monitor/data --output-dir /srv/news-monitor/dist || result=$?
# Partial data is eligible only if the artifact gate accepts it; failed runs stop.
if [[ "$result" != 0 && "$result" != 2 ]]; then
  exit "$result"
fi
python3 -B -m news_monitor.preflight \
  --data-dir /srv/news-monitor/data --output-dir /srv/news-monitor/dist
python3 -B -m news_monitor.publish \
  --data-dir /srv/news-monitor/data --output-dir /srv/news-monitor/dist \
  --checkout /srv/news-monitor/publish \
  --publish-config /srv/news-monitor/config/publish.json
