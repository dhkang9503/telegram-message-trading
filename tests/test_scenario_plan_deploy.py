"""Exercise atomic scenario plan installation and rollback without SSH/exchange."""
import copy
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scenario_monitor.main import DEFAULT_PLAN


ROOT = Path(__file__).resolve().parents[1]
MODULES = ("main.py", "execution.py", "execution_store.py", "binance_futures.py")


class PlanDeployTests(unittest.TestCase):
    def plan(self, plan_id):
        raw = copy.deepcopy(DEFAULT_PLAN)
        now = datetime.now(timezone.utc)
        raw.update(plan_id=plan_id, as_of=(now - timedelta(minutes=5)).isoformat(),
                   expires_at=(now + timedelta(hours=2)).isoformat())
        return json.dumps(raw)

    def deploy(self, fail_restart=False, incoming_text=None):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            bot = home / "scenario_monitor"
            bot.mkdir(parents=True)
            for name in MODULES:
                (bot / name).write_text((ROOT / "scenario_monitor" / name).read_text())
            old = self.plan("old-plan")
            new = incoming_text or self.plan("new-plan")
            (bot / "plan.json").write_text(old)
            (bot / "data").mkdir()
            (bot / "data/PAUSE").touch()
            incoming = base / "incoming.json"
            incoming.write_text(new)
            digest = hashlib.sha256(incoming.read_bytes()).hexdigest()

            bins = base / "bin"
            bins.mkdir()
            sudo = bins / "sudo"
            sudo.write_text("""#!/usr/bin/env bash
if [ "$1" = journalctl ]; then
  if [ "$2" = -u ] && [ "$4" = --since ]; then echo "INFO Monitor new-plan; execution=live"; fi
  exit 0
fi
if [ "$1" != systemctl ]; then exit 1; fi
case "$2" in
  is-active) [ ! -f "$TEST_STATE/restart-failed" ]; exit $? ;;
  is-failed) [ -f "$TEST_STATE/restart-failed" ]; exit $? ;;
  restart)
    if [ "$TEST_FAIL_RESTART" = 1 ]; then touch "$TEST_STATE/restart-failed"; exit 1; fi
    exit 0 ;;
  stop|reset-failed|start) rm -f "$TEST_STATE/restart-failed"; exit 0 ;;
  *) exit 0 ;;
esac
""")
            sudo.chmod(0o755)
            result = subprocess.run(
                ["bash", str(ROOT / "scenario_monitor/deploy_plan_remote.sh"), str(incoming),
                 "scenario-monitor", digest, "true"],
                env=dict(os.environ, HOME=str(home), PATH=str(bins) + ":" + os.environ["PATH"],
                         TEST_STATE=str(base), TEST_FAIL_RESTART=str(int(fail_restart))),
                capture_output=True, text=True, timeout=25)
            return result, (bot / "plan.json").read_text(), (bot / "data/PAUSE").exists(), old, new

    def test_installs_plan_clears_pause_and_confirms_loaded_id(self):
        result, installed, paused, _, new = self.deploy()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(installed), json.loads(new))
        self.assertFalse(paused)
        self.assertIn("service loaded it successfully", result.stdout)

    def test_failed_restart_restores_previous_plan(self):
        result, installed, paused, old, _ = self.deploy(fail_restart=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(installed), json.loads(old))
        self.assertTrue(paused)
        self.assertIn("restored the previous plan", result.stderr)

    def test_rejects_expired_plan_before_replacing_current(self):
        expired = copy.deepcopy(DEFAULT_PLAN)
        now = datetime.now(timezone.utc)
        expired.update(plan_id="expired-plan", as_of=(now - timedelta(hours=2)).isoformat(),
                       expires_at=(now - timedelta(hours=1)).isoformat())
        result, installed, _, old, _ = self.deploy(incoming_text=json.dumps(expired))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(installed), json.loads(old))


if __name__ == "__main__":
    unittest.main()
