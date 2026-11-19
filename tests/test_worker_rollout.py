"""Rollout isolation tests with synthetic files and a mocked Docker daemon."""
import importlib.util
import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("worker_rollout", Path(__file__).resolve().parents[1] / "scripts/worker-rollout.py")
rollout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollout)


class RolloutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "candidate-code"
        self.serving = self.root / "serving"
        self.runtime = self.root / "candidate-runtime"
        self.source.mkdir()
        (self.serving / "envs").mkdir(parents=True)
        (self.serving / ".env").write_text("API_KEY=synthetic-key\n")
        (self.serving / "envs/account2.env").write_text("SECURE_1PSID=synthetic-two\nSECURE_1PSIDTS=synthetic-ts\n")
        (self.serving / "envs/account7.env").write_text("MUST_NOT_BE_READ\n")
        self.args = SimpleNamespace(source=self.source, serving=self.serving, runtime=self.runtime,
            slot=[2], exclude=[7], port=17860, name="synthetic-candidate", serving_container="synthetic-serving")

    def test_candidate_copies_only_selected_accounts_and_isolates_mounts(self):
        calls = []
        def command(*args):
            calls.append(args)
            if args[:2] == ("docker", "inspect"):
                return json.dumps([{"Config": {"Env": []}, "Image": "synthetic-image", "State": {"StartedAt": "unchanged"}}])
            if args[:2] == ("docker", "run"):
                return "synthetic-container"
            if "rev-parse" in args:
                return "synthetic-commit"
            return ""
        with patch.object(rollout, "command", side_effect=command), patch.object(rollout.socket, "socket"), patch("builtins.print"):
            rollout.start(self.args)
        self.assertEqual([path.name for path in (self.runtime / "envs").iterdir()], ["account2.env"])
        invocation = next(args for args in calls if args[:2] == ("docker", "run"))
        self.assertIn(str(self.runtime / "envs") + ":/app/envs:rw", invocation)
        self.assertIn(str(self.runtime / "state") + ":/app/state:rw", invocation)
        self.assertNotIn(str(self.serving / "envs") + ":/app/envs:rw", invocation)
        self.assertIn("127.0.0.1", invocation)
        self.assertEqual(stat.S_IMODE((self.runtime / "runtime.env").stat().st_mode), 0o600)
        self.assertFalse(any("restart" in args or "stop" in args or "kill" in args for args in calls))

    def test_excluded_account_is_rejected_before_read_or_docker(self):
        self.args.slot = [7]
        with patch.object(rollout, "command") as command, self.assertRaises(RuntimeError):
            rollout.start(self.args)
        command.assert_not_called()
        self.assertFalse(self.runtime.exists())

    def test_serving_runtime_is_rejected_before_docker(self):
        self.args.runtime = self.serving / "state"
        with patch.object(rollout, "command") as command, self.assertRaises(RuntimeError):
            rollout.start(self.args)
        command.assert_not_called()

    def test_failed_new_gateway_is_killed_then_rollback_verified(self):
        new, old = Mock(pid=2), Mock(pid=3)
        new.poll.return_value = None
        new.wait.side_effect = [rollout.subprocess.TimeoutExpired("synthetic", 15), 0]
        with patch.object(rollout.subprocess, "Popen", side_effect=[new, old]) as launch, \
             patch.object(rollout, "ready", side_effect=[False, True]) as ready:
            with self.assertRaisesRegex(RuntimeError, "restored and verified"):
                rollout.replace_gateway(["python3", "old/gateway.py"], self.serving, {}, self.source, {},
                                        self.root / "gateway.log", "http://offline.test", "synthetic")
        self.assertEqual(launch.call_count, 2)
        new.kill.assert_called_once()
        self.assertEqual(ready.call_count, 2)

    def test_new_gateway_spawn_failure_still_restores_old(self):
        old = Mock(pid=3)
        with patch.object(rollout.subprocess, "Popen", side_effect=[OSError("synthetic spawn error"), old]) as launch, \
             patch.object(rollout, "ready", return_value=True) as ready:
            with self.assertRaisesRegex(RuntimeError, "restored and verified"):
                rollout.replace_gateway(["python3", "old/gateway.py"], self.serving, {}, self.source, {},
                                        self.root / "gateway.log", "http://offline.test", "synthetic")
        self.assertEqual(launch.call_count, 2)
        ready.assert_called_once()


if __name__ == "__main__":
    unittest.main()
