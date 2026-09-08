#!/usr/bin/env bash
# Invoked over SSH by deploy-scenario-monitor.yml. Preserve .env/plan/data.
set -Eeuo pipefail
archive=$1
service=$2
deploy_sha=$3
bot_dir="$HOME/scenario_monitor"
stage=$(mktemp -d /tmp/scenario-release.XXXXXX)
backup=$(mktemp -d /tmp/scenario-backup.XXXXXX)
files=(main.py execution.py execution_store.py binance_futures.py)
was_active=0
installing=0

healthy() {
  local attempt
  for attempt in $(seq 1 10); do
    if sudo systemctl is-active --quiet "$service"; then
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
  if [ "$code" -ne 0 ] && [ "$installing" -eq 1 ]; then
    sudo systemctl stop "$service" || true
    for file in "${files[@]}"; do
      if [ -f "$backup/$file" ]; then
        cp -p "$backup/$file" "$bot_dir/$file"
      else
        rm -f "$bot_dir/$file"
      fi
    done
    if [ "$was_active" -eq 1 ]; then
      sudo systemctl reset-failed "$service" || true
      sudo systemctl start "$service" || true
    fi
    echo "Deployment failed; restored previous code. Exchange-hosted exits remain active." >&2
    sudo journalctl -u "$service" -n 40 --no-pager || true
  fi
  rm -rf "$stage" "$backup"
  rm -f "$archive"
  exit "$code"
}
trap cleanup EXIT

tar -xzf "$archive" -C "$stage" --no-same-owner
for file in "${files[@]}"; do
  test -f "$stage/$file"
  python3 -m py_compile "$stage/$file"
done
# Validation has no signed calls and never submits orders, even with live env.
python3 "$stage/main.py" --validate
mkdir -p "$bot_dir/data"
if sudo systemctl is-active --quiet "$service"; then was_active=1; fi
for file in "${files[@]}"; do
  if [ -f "$bot_dir/$file" ]; then cp -p "$bot_dir/$file" "$backup/$file"; fi
done
# Stop before replacing modules so an old process cannot import mixed versions.
if [ "$was_active" -eq 1 ]; then sudo systemctl stop "$service"; fi
installing=1
for file in "${files[@]}"; do install -m 0644 "$stage/$file" "$bot_dir/$file"; done
if [ "$was_active" -eq 1 ]; then
  sudo systemctl reset-failed "$service"
  sudo systemctl start "$service"
  healthy
fi
installing=0
echo "Installed scenario modules at commit $deploy_sha; .env, plan and data preserved."
if [ "$was_active" -eq 0 ]; then echo "Service was inactive; start it manually after configuration."; fi
