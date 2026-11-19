import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from feishu_live_bridge import LiveBridge, database_path
from project_manager import ProjectManager
from test_feishu_receiver import event


FAKE_CLI = r"""
import json
import select
import sys
from pathlib import Path

root = Path(sys.argv[1])
args = sys.argv[2:]
if args[:2] == ["event", "consume"]:
    assert args == ["event", "consume", "im.message.receive_v1", "--as", "bot"]
    counter = root / "starts"
    count = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(count))
    mode = (root / "mode").read_text()
    if mode == "restart" and count == 1:
        print('{"ok":false}', file=sys.stderr, flush=True)
        sys.exit(4)
    # An unbounded CLI would exit if its stdin was already readable as EOF.
    assert not select.select([sys.stdin], [], [], 0)[0]
    if mode == "stalled":
        sys.stdin.read()
        sys.exit()
    print("[event] ready event_key=im.message.receive_v1", file=sys.stderr, flush=True)
    if mode == "flap":
        sys.exit()
    print("diagnostic " * 10000, file=sys.stderr, flush=True)
    print((root / "events").read_text(), end="", flush=True)
    (root / "waiting").touch()
    assert sys.stdin.read() == ""
    (root / "stopped").touch()
else:
    assert args[:4] == ["im", "+messages-send", "--as", "bot"]
    assert args[4] == "--user-id" and args[6] == "--text"
    assert args[8] == "--idempotency-key" and len(args[9]) <= 50
    with (root / "sends").open("a") as log:
        log.write(json.dumps(args) + "\n")
    if (root / "fail_send").exists():
        sys.exit(1)
    print(json.dumps({"ok": True, "data": {"message_id": "om_receipt"}}))
"""


class LiveBridgeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "alpha").mkdir()
        self.database = self.root / "tasks.sqlite3"
        self.manager = ProjectManager(self.database, self.root)
        self.script = self.root / "fake_cli.py"
        self.script.write_text(FAKE_CLI)
        self.make_bridge()
        self.mode("normal")

    def make_bridge(self):
        self.bridge = LiveBridge(
            self.manager, ["ou_owner", "ou_second"], "test-app.tenant",
            cli=(sys.executable, str(self.script), str(self.root)),
        )

    def mode(self, name):
        (self.root / "mode").write_text(name)

    def publish(self, *events):
        (self.root / "events").write_text(
            "malformed line\n" + "".join(json.dumps(item) + "\n" for item in events),
            encoding="utf-8",
        )

    def sends(self):
        path = self.root / "sends"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_single_run_skips_unapproved_events_before_registering_and_sending(self):
        command = "新建项目\nAlpha\nalpha\n整理需求"
        self.publish(
            event(command, sender_id="ou_stranger", message_id="om_stranger"),
            event(command, chat_type="group", message_id="om_group"),
            event(command, sender_type="bot", message_id="om_bot"),
            event(command, message_type="image", message_id="om_image"),
            event(command, type="card.action.trigger", message_id="om_card"),
            event(command, sender_id="ou_second", message_id="om_authorized"),
        )
        self.assertEqual(self.bridge.run(once=True, timeout=5), 0)
        self.assertTrue((self.root / "stopped").exists())
        self.assertEqual(self.manager.get_project("P001").name, "Alpha")
        task = self.manager.get_task("T001")
        self.assertTrue(task.external_message_id.endswith(":om_authorized"))
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.sends()[0][5], "ou_second")
        self.assertIn("P001", self.sends()[0][7])
        self.assertIn("T001", self.sends()[0][7])

    def test_duplicate_after_failed_send_reuses_key_and_task_then_suppresses_receipt(self):
        self.manager.create_project("Alpha", "alpha")
        self.publish(event())
        failure = self.root / "fail_send"
        failure.touch()
        self.assertEqual(self.bridge.run(once=True, timeout=5), 1)
        self.assertEqual(self.manager.get_task("T001").title, "整理需求")
        failure.unlink()
        self.make_bridge()
        self.publish(event(event_id="redelivery"))
        self.assertEqual(self.bridge.run(once=True, timeout=5), 0)
        self.make_bridge()
        self.assertEqual(self.bridge.run(once=True, timeout=5), 0)
        with self.assertRaises(KeyError):
            self.manager.get_task("T002")
        self.assertEqual(len(self.sends()), 2)
        self.assertEqual(self.sends()[0][7:], self.sends()[1][7:])

    def test_invalid_command_sends_error_without_task(self):
        self.publish(event("格式错误"))
        self.assertEqual(self.bridge.run(once=True, timeout=5), 0)
        self.assertIn("登记失败", self.sends()[0][7])
        with self.assertRaises(KeyError):
            self.manager.get_task("T001")

    def test_redelivery_after_clarification_does_not_register_a_task(self):
        self.publish(event())
        self.assertEqual(self.bridge.run(once=True, timeout=5), 0)
        self.assertIn("未找到项目", self.sends()[0][7])
        self.manager.create_project("Alpha", "alpha")
        self.assertEqual(self.bridge.run(once=True, timeout=5), 0)
        self.assertEqual(len(self.sends()), 1)
        with self.assertRaises(KeyError):
            self.manager.get_task("T001")
        self.publish(event(message_id="om_new"))
        self.assertEqual(self.bridge.run(once=True, timeout=5), 0)
        self.assertEqual(self.manager.get_task("T001").project_number, "P001")

    def test_consumer_reconnects_after_failure(self):
        self.mode("restart")
        self.publish(event("新建项目\nAlpha\nalpha\n整理需求"))
        with patch("feishu_live_bridge.RETRY_MIN", 0.05):
            self.assertEqual(self.bridge.run(once=True, timeout=5), 0)
        self.assertEqual((self.root / "starts").read_text(), "2")

    def test_repeated_ready_then_exit_uses_exponential_backoff(self):
        self.mode("flap")
        with self.assertLogs("feishu_live_bridge") as logs, \
                patch("feishu_live_bridge.RETRY_MIN", 0.1):
            self.assertEqual(self.bridge.run(once=True, timeout=0.8), 1)
        self.assertIn("Retrying in 0.1 seconds", "\n".join(logs.output))
        self.assertIn("Retrying in 0.2 seconds", "\n".join(logs.output))
        self.assertLessEqual(int((self.root / "starts").read_text()), 4)

    def test_startup_readiness_timeout_reaps_child(self):
        self.mode("stalled")
        with self.assertLogs("feishu_live_bridge") as logs:
            self.assertEqual(self.bridge.run(once=True, timeout=0.5), 1)
        self.assertIn("Timed out waiting", "\n".join(logs.output))
        self.assertIn("exited with code 0", "\n".join(logs.output))

    def test_sigterm_stops_bridge_and_reaps_consumer(self):
        self.publish()
        fake_cli = self.root / "lark-cli"
        fake_cli.write_text(
            f"#!{sys.executable}\nimport sys\nsys.argv.insert(1, {str(self.root)!r})\n"
            + FAKE_CLI
        )
        fake_cli.chmod(0o700)
        bridge_script = Path(__file__).with_name("feishu_live_bridge.py")
        with subprocess.Popen(
            [sys.executable, "-B", str(bridge_script)],
            env={
                **os.environ,
                "PATH": str(self.root),
                "FEISHU_DB_PATH": str(self.database),
                "FEISHU_WORKSPACE": str(self.root),
                "FEISHU_ALLOWED_OPEN_IDS": "ou_owner",
                "FEISHU_NAMESPACE": "test-app",
            },
        ) as process:
            try:
                # The simulated CLI writes its stop marker only after stdin EOF.
                deadline = time.monotonic() + 5
                while not (self.root / "waiting").exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue((self.root / "waiting").exists())
                process.send_signal(signal.SIGTERM)
                self.assertEqual(process.wait(timeout=5), 0)
                self.assertTrue((self.root / "stopped").exists())
            finally:
                if process.poll() is None:
                    process.kill()

    def test_database_rejects_repo_and_symlink_into_repo(self):
        repo = Path(__file__).resolve().parent
        link = self.root / "repo-link"
        link.symlink_to(repo, target_is_directory=True)
        for candidate in (repo / "live.sqlite3", link / "live.sqlite3"):
            with self.assertRaisesRegex(ValueError, "outside Git"):
                database_path(candidate)
        self.assertEqual(database_path(self.database), self.database)
