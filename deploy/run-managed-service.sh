#!/bin/sh

set -eu

run_migrations() {
  attempts=0
  until alembic -c apps/api/alembic.ini upgrade head; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 12 ]; then
      echo "Timed out waiting for PostgreSQL migrations to succeed." >&2
      exit 1
    fi
    sleep 5
  done
}

run_api() {
  exec uvicorn apps.api.main:app --host 0.0.0.0 --port 8000
}

run_worker() {
  exec python - <<'PY'
from __future__ import annotations

import os
import time

from apps.api.notification_delivery import NotificationDeliveryError
from apps.api.worker import (
    process_notification_delivery_once,
    process_snapshot_build_job_once,
)


poll_interval = float(os.environ.get("WORKER_POLL_INTERVAL_SECONDS", "5"))
notification_batch_size = int(os.environ.get("WORKER_NOTIFICATION_BATCH_SIZE", "25"))

while True:
    snapshot_result = process_snapshot_build_job_once()
    try:
        notification_result = process_notification_delivery_once(limit=notification_batch_size)
    except NotificationDeliveryError:
        time.sleep(poll_interval)
        continue

    if snapshot_result.status == "idle" and notification_result.processed == 0:
        time.sleep(poll_interval)
PY
}

run_migrations

case "${1:-}" in
  api)
    run_api
    ;;
  worker)
    run_worker
    ;;
  *)
    echo "Usage: run-managed-service.sh [api|worker]" >&2
    exit 1
    ;;
esac
