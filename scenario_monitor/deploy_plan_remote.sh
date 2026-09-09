#!/usr/bin/env bash
# Install a validated scenario plan atomically and prove the service loaded it.
set -Eeuo pipefail
incoming=$1
service=$2
expected_sha=$3
clear_pause=$4
bot_dir="$HOME/scenario_monitor"
target="$bot_dir/plan.json"
stage="$bot_dir/.plan.json.new"
backup_dir=$(mktemp -d /tmp/scenario-plan-backup.XXXXXX)
backup="$backup_dir/plan.json"
had_previous=0
was_active=0
replaced=0
had_pause=0

healthy_with_plan() {
  local attempt
  for attempt in $(seq 1 12); do
    if sudo systemctl is-active --quiet "$service" && \
       sudo journalctl -u "$service" --since "$restart_at" --no-pager | \
         grep -Fq "Monitor ${plan_id};"; then
      sleep 2
      sudo systemctl is-active --quiet "$service" && return 0
    fi
    sudo systemctl is-failed --quiet "$service" && return 1
    sleep 1
  done
  return 1
}

cleanup() {
  local code=$?
  trap - EXIT
  rm -f "$incoming" "$stage"
  if [ "$code" -ne 0 ] && [ "$replaced" -eq 1 ]; then
    sudo systemctl stop "$service" || true
    if [ "$had_previous" -eq 1 ]; then
      install -m 0600 "$backup" "$stage"
      mv -f "$stage" "$target"
    else
      rm -f "$target"
    fi
    if [ "$clear_pause" = true ] && [ "$had_pause" -eq 1 ]; then
      touch "$bot_dir/data/PAUSE"
    fi
    if [ "$was_active" -eq 1 ]; then
      sudo systemctl reset-failed "$service" || true
      sudo systemctl start "$service" || true
    fi
    echo "Plan deployment failed; restored the previous plan." >&2
    sudo journalctl -u "$service" -n 60 --no-pager || true
  fi
  rm -rf "$backup_dir"
  exit "$code"
}
trap cleanup EXIT

[[ "$service" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.@-]*$ ]]
[[ "$expected_sha" =~ ^[a-f0-9]{64}$ ]]
[[ "$clear_pause" = true || "$clear_pause" = false ]]
test -f "$incoming"
test "$(wc -c < "$incoming")" -le 65536
test "$(sha256sum "$incoming" | awk '{print $1}')" = "$expected_sha"
test -f "$bot_dir/main.py"
python3 "$bot_dir/main.py" --plan "$incoming" --validate

plan_id=$(python3 - "$incoming" "$target" <<'PY'
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

incoming, current = map(Path, sys.argv[1:])
with incoming.open(encoding="utf-8") as handle:
    plan = json.load(handle)
plan_id = plan.get("plan_id", "")
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", plan_id):
    raise SystemExit("unsafe plan_id")
expires = datetime.fromisoformat(plan["expires_at"])
if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
    raise SystemExit("plan expired before remote installation")
if current.exists():
    try:
        with current.open(encoding="utf-8") as handle:
            old = json.load(handle)
        changed = hashlib.sha256(incoming.read_bytes()).digest() != hashlib.sha256(current.read_bytes()).digest()
        if changed and old.get("plan_id") == plan_id:
            raise SystemExit("changed plan must use a new plan_id")
    except (OSError, json.JSONDecodeError):
        pass
print(plan_id)
PY
)

mkdir -p "$bot_dir/data"
if sudo systemctl is-active --quiet "$service"; then was_active=1; fi
if [ -f "$target" ]; then
  cp -p "$target" "$backup"
  had_previous=1
fi
if [ -f "$bot_dir/data/PAUSE" ]; then had_pause=1; fi
install -m 0600 "$incoming" "$stage"
mv -f "$stage" "$target"
replaced=1
if [ "$clear_pause" = true ]; then rm -f "$bot_dir/data/PAUSE"; fi

restart_at=$(date --iso-8601=seconds)
sudo systemctl reset-failed "$service"
sudo systemctl restart "$service"
healthy_with_plan
replaced=0
echo "Installed plan ${plan_id}; service loaded it successfully."
