"""Exercise module installation/rollback with a fake systemd, no SSH/exchange."""
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FILES = ("main.py", "execution.py", "execution_store.py", "binance_futures.py")


class DeployTests(unittest.TestCase):
    def deploy(self, active=False, fail_start=False):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            bot = home / "scenario_monitor"
            bot.mkdir(parents=True)
            for name in FILES:
                (bot / name).write_text("# original " + name)
            (bot / ".env").write_text("SCENARIO_EXECUTION_MODE=off\n")
            (bot / "plan.json").write_text("preserve-plan")
            (bot / "data").mkdir()
            (bot / "data" / "state.json").write_text("preserve-state")
            archive = base / "release.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                for name in FILES:
                    tar.add(ROOT / "scenario_monitor" / name, arcname=name)
            bins = base / "bin"
            bins.mkdir()
            sudo = bins / "sudo"
            sudo.write_text("""#!/usr/bin/env bash
if [ "$1" = journalctl ]; then exit 0; fi
case "$2" in
  is-active) [ "$TEST_ACTIVE" = 1 ]; exit $? ;;
  is-failed) exit 1 ;;
  start) [ "$TEST_FAIL_START" = 0 ]; exit $? ;;
  *) exit 0 ;;
esac
""")
            sudo.chmod(0o755)
            result = subprocess.run(["bash", str(ROOT / "scenario_monitor/deploy_remote.sh"),
                                     str(archive), "scenario-monitor", "test-sha"],
                                    env=dict(os.environ, HOME=str(home), PATH=str(bins) + ":" + os.environ["PATH"],
                                             TEST_ACTIVE=str(int(active)), TEST_FAIL_START=str(int(fail_start))),
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode == 0, not fail_start, result.stdout + result.stderr)
            for name in FILES:
                expected = "# original " + name if fail_start else (ROOT / "scenario_monitor" / name).read_text()
                self.assertEqual((bot / name).read_text(), expected)
            self.assertEqual((bot / "plan.json").read_text(), "preserve-plan")
            self.assertEqual((bot / "data/state.json").read_text(), "preserve-state")
            self.assertEqual((bot / ".env").read_text(), "SCENARIO_EXECUTION_MODE=off\n")

    def test_inactive_service_installs_all_modules_and_preserves_runtime(self):
        self.deploy()

    def test_failed_restart_restores_entire_previous_module_set(self):
        self.deploy(active=True, fail_start=True)


if __name__ == "__main__":
    unittest.main()
