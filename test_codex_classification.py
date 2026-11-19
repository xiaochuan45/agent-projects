import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_classification_backend import CodexClassificationBackend, DISABLED_FEATURES
from feishu_live_bridge import main as live_main
from feishu_natural_test_bridge import main
from natural_language_classifier import NaturalLanguageClassifier, OUTPUT_SCHEMA
from test_feishu_receiver import event
from test_natural_language import suggestion


class CodexClassificationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.home = self.root / "codex-home"
        self.home.mkdir()
        self.args = [
            "--state-dir", str(self.root), "--lark-profile", "nl-test",
            "--open-id", "ou_owner", "--namespace", "test-app.tenant", "--once",
            "--timeout", "20", "--model", "example-model", "--classification-timeout", "3",
        ]

    @patch("feishu_live_bridge.run_bridge", return_value=0)
    @patch("sys.argv", ["feishu_live_bridge.py"])
    def test_live_entry_configures_classifier_only_when_enabled(self, run_bridge):
        env = {
            "FEISHU_DB_PATH": str(self.root / "live.sqlite3"),
            "FEISHU_WORKSPACE": str(self.root),
            "FEISHU_NAMESPACE": "test-app.tenant",
            "FEISHU_ALLOWED_OPEN_IDS": "ou_owner",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(live_main(), 0)
            bridge = run_bridge.call_args.args[0]
            self.assertIsNone(bridge.receivers["ou_owner"].natural_router)

            os.environ["FEISHU_NATURAL_LANGUAGE_ENABLED"] = "1"
            os.environ["FEISHU_CODEX_HOME"] = str(self.home)
            self.assertEqual(live_main(), 0)
            bridge = run_bridge.call_args.args[0]
            router = bridge.receivers["ou_owner"].natural_router
            self.assertEqual(router.classifier.backend.codex_home, self.home)

    def mock_reply(self, command, **options):
        self.assertEqual(command[:4], ["codex", "-a", "never", "exec"])
        self.assertEqual(command[-1], "-")
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertIn('model_reasoning_effort="low"', command)
        self.assertEqual(command[command.index("--model") + 1], "example-model")
        self.assertEqual({command[i + 1] for i, arg in enumerate(command) if arg == "--disable"},
                         set(DISABLED_FEATURES))
        self.assertNotIn("incremental_exec_tools_legacy", command)
        self.assertEqual(options["timeout"], 3)
        self.assertEqual(options["env"]["CODEX_HOME"], str(self.home))
        self.assertEqual(options["env"].keys(), {"PATH", "HOME", "CODEX_HOME"})
        schema = Path(command[command.index("--output-schema") + 1])
        self.assertEqual(json.loads(schema.read_text()), OUTPUT_SCHEMA)
        self.assertNotIn("private message", command)
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text(suggestion("new_project"), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({
            "type": "turn.completed", "usage": {"input_tokens": 123, "output_tokens": 20,
                                                "cached_input_tokens": "private credential"},
        }))

    @patch("codex_classification_backend.subprocess.run")
    def test_schema_low_timeout_and_safe_metrics(self, run):
        run.side_effect = self.mock_reply
        classifier = NaturalLanguageClassifier(
            CodexClassificationBackend(self.home, model="example-model", timeout=3)
        )
        with self.assertLogs("codex_classification_backend", level="INFO") as logs:
            result = classifier.classify("private message", [], None)
        self.assertEqual(result.intent, "new_project")
        log = "".join(logs.output)
        self.assertIn("elapsed_ms=", log)
        self.assertIn("'input_tokens': 123", log)
        self.assertNotIn("private", log)
        self.assertIn("private message", run.call_args.kwargs["input"])

    @patch("codex_classification_backend.subprocess.run")
    def test_entry_injects_backend_and_reuses_confirmation_state(self, codex):
        codex.side_effect = self.mock_reply

        def run(bridge, *, once, timeout):
            self.assertTrue(once)
            self.assertEqual(timeout, 20)
            self.assertEqual(bridge.cli, ("lark-cli", "--profile", "nl-test"))
            self.assertEqual(bridge.manager.workspace, self.root / "workspace")
            self.assertEqual(bridge.manager.database, self.root / "nl-test.sqlite3")
            with patch.object(bridge, "send_receipt", return_value=0) as send:
                self.assertIsNone(bridge.handle_line(json.dumps(event("ignored", sender_id="ou_other"))))
                self.assertEqual(bridge.handle_line(json.dumps(messages.pop(0))), 0)
                self.assertIn(receipts.pop(0), send.call_args.args[-1])
            if messages:
                self.assertEqual(bridge.manager.find_projects("报表"), [])
            else:
                self.assertEqual(bridge.manager.get_project("P001").name, "报表")
                self.assertEqual(bridge.manager.get_task("T001").title, "实现导出")
                with self.assertRaises(KeyError):
                    bridge.manager.get_task("T002")
            return 0

        messages = [
            event("请新建项目并实现导出", message_id="om_request"),
            event("请新建项目并实现导出", message_id="om_request"),
            event("确认 om_request 新建 报表", message_id="om_confirm"),
        ]
        receipts = ["确认 om_request 新建", "确认 om_request 新建", "任务 T001"]
        with patch("feishu_natural_test_bridge.run_bridge", side_effect=run):
            for _ in range(3):
                self.assertEqual(main(self.args), 0)
        codex.assert_called_once()

    @patch("codex_classification_backend.subprocess.run")
    def test_cli_failures_cannot_register_projects_or_tasks(self, codex):
        def respond(command, **options):
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(suggestion("new_project", project_name="报表"), encoding="utf-8")
            if case == "timeout":
                raise subprocess.TimeoutExpired(command, options["timeout"])
            if case == "exit":
                raise subprocess.CalledProcessError(1, command)
            if case == "invalid":
                output.write_text("{}", encoding="utf-8")
            stdout = '{"type":"turn.failed"}' if case == "failed_turn" else ""
            return subprocess.CompletedProcess(command, 0, stdout=stdout)

        def run(bridge, **_):
            receiver = bridge.receivers["ou_owner"]
            result = receiver.handle_event(event("请新建报表项目", message_id="om_" + case))
            self.assertEqual(result, {"status": "classification_failed"})
            self.assertEqual(bridge.manager.find_projects("报表"), [])
            self.assertEqual(list(bridge.manager.workspace.iterdir()), [])
            with self.assertRaises(KeyError):
                bridge.manager.get_task("T001")
            return 0

        codex.side_effect = respond
        with patch("feishu_natural_test_bridge.run_bridge", side_effect=run):
            for case in ("timeout", "exit", "invalid", "failed_turn"):
                with self.subTest(case=case):
                    self.assertEqual(main(self.args), 0)

    def test_existing_database_link_is_rejected_without_starting_bridge(self):
        original = self.root / "other.sqlite3"
        original.touch()
        (self.root / "nl-test.sqlite3").hardlink_to(original)
        with patch("feishu_natural_test_bridge.run_bridge") as run, self.assertLogs():
            self.assertEqual(main(self.args), 2)
        run.assert_not_called()
