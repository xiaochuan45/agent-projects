import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import UUID

from feishu_live_bridge import LiveBridge
from natural_language_classifier import NaturalLanguageClassifier, parse_suggestion
from project_manager import ProjectManager
from test_feishu_receiver import event


def suggestion(intent="existing_project", **changes):
    return json.dumps(dict({
        "intent": intent, "project_number": None, "project_name": None,
        "task_title": "实现导出", "confidence": 0.95, "clarification_question": None,
    }, **changes))


class NaturalLanguageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "workspace"
        for directory in ("alpha", "beta"):
            (self.workspace / directory).mkdir(parents=True)
        self.database = self.root / "test.sqlite3"
        self.manager = ProjectManager(self.database, self.workspace)
        self.backend = Mock(return_value=suggestion(project_number="P001"))
        self.bridge = self.make_bridge()

    def make_bridge(self, *, enabled=True, namespace="test-app.tenant"):
        bridge = LiveBridge(
            self.manager, ["ou_owner", "ou_second"], namespace,
            natural_language_enabled=enabled, classifier=NaturalLanguageClassifier(self.backend),
        )
        bridge.send_receipt = Mock(return_value=0)
        return bridge

    def route(self, text, message_id="om_request", sender="ou_owner", **changes):
        receiver = self.bridge.receivers[sender]
        return receiver.handle_event(event(text, message_id=message_id, sender_id=sender, **changes))

    def projects(self):
        self.manager.create_project("Alpha", "alpha", aliases=["甲方", "共同"])
        self.manager.create_project("Beta", "beta", aliases=["乙方", "共同"])

    def assert_no_task(self, number="T001"):
        with self.assertRaises(KeyError):
            self.manager.get_task(number)

    def test_new_project_uses_generated_directory_and_duplicate_skips_model(self):
        self.backend.return_value = suggestion("new_project", project_name="报表助手")
        text = "请新建报表助手项目，实现导出，目录建议 /tmp/outside"
        result = self.route(text)
        self.assertEqual(result["status"], "registered")
        project = self.manager.get_project(result["project_number"])
        directory = self.manager.project_directory(project.number)
        self.assertEqual(directory.parent, self.workspace)
        self.assertRegex(directory.name, r"^project-[0-9a-f]{32}$")
        self.assertEqual(self.route(text)["status"], "duplicate")
        self.backend.assert_called_once()
        self.assert_no_task("T002")

    def test_resolves_number_name_and_alias(self):
        self.projects()
        for index, name in enumerate(("P001", "Alpha", "甲方")):
            with self.subTest(name=name):
                result = self.route(f"给{name}加导出", f"om_{index}")
                self.assertEqual((result["status"], result["project_number"]), ("registered", "P001"))
        prompt = self.backend.call_args.args[0]
        self.assertIn("甲方", prompt)
        self.assertNotIn("Beta", prompt)
        self.assertNotIn("乙方", prompt)
        self.assertNotIn(str(self.workspace), prompt)

    def test_ambiguous_confirmation_is_scoped_persistent_and_atomic(self):
        self.projects()
        self.backend.return_value = suggestion(project_number="P001")
        pending = self.route("给共同项目加导出")
        self.assertEqual([p["project_number"] for p in pending["candidates"]], ["P001", "P002"])
        self.assert_no_task()
        self.manager = ProjectManager(self.database, self.workspace)
        other_bridge = self.make_bridge(namespace="other-app.tenant")
        self.assertEqual(other_bridge.receivers["ou_owner"].handle_event(
            event("确认 om_request P002", message_id="om_cross_namespace")
        )["status"], "invalid_confirmation")
        self.bridge = self.make_bridge()
        self.assertEqual(self.route("确认 om_request P002", "om_wrong_owner", "ou_second")["status"],
                         "invalid_confirmation")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(
                lambda i: self.route("确认 om_request P002", f"om_confirm_{i}"), range(8)
            ))
        self.assertEqual([r["status"] for r in results].count("registered"), 1)
        self.assertEqual({r["task_number"] for r in results}, {"T001"})
        task = self.manager.get_task("T001")
        self.assertEqual((task.project_number, task.external_message_id),
                         ("P002", "feishu:test-app.tenant:om_request"))
        self.assertEqual(self.route("确认 om_request P002", "om_confirm_0")["task_number"], "T001")
        self.assertEqual(self.route("给共同项目加导出")["status"], "duplicate")
        self.backend.assert_called_once()
        self.assert_no_task("T002")

    def test_confirmation_can_supply_missing_new_name(self):
        self.backend.return_value = suggestion("new_project")
        pending = self.route("请新建项目并实现导出")
        self.assertTrue(pending["allow_new"])
        self.assertEqual(self.route("确认 om_request 新建 ../outside", "om_bad")["status"],
                         "invalid_confirmation")
        self.assertEqual(self.route("确认 om_request 新建 报表", "om_good")["status"], "registered")
        self.assertEqual(self.manager.get_project("P001").name, "报表")

    def test_context_requires_persisted_confirmed_project_per_sender(self):
        self.projects()
        self.backend.return_value = suggestion(project_number="P001")
        self.assertEqual(self.route("刚刚那个项目也加导出", "om_no_context")["status"],
                         "clarification_required")
        self.assertEqual(self.route("任务\nP001\n整理需求", "om_fixed")["status"], "registered")
        self.manager = ProjectManager(self.database, self.workspace)
        self.bridge = self.make_bridge()
        self.assertEqual(self.route("刚刚那个项目也加导出", "om_context")["project_number"], "P001")
        self.assertEqual(self.route("刚刚那个项目也加导出", "om_other_owner", "ou_second")["status"],
                         "clarification_required")
        self.assert_no_task("T003")

    def test_backend_failure_timeout_and_illegal_output_do_not_register(self):
        self.projects()
        for index, failure in enumerate((TimeoutError(), RuntimeError(), "not JSON", "{}",
                                          suggestion(extra="unexpected"),
                                          suggestion(confidence=True), suggestion(confidence=float("nan")),
                                          suggestion(project_number="../../p001"))):
            with self.subTest(failure=failure):
                self.backend.side_effect = [failure]
                self.assertEqual(self.route("给Alpha加导出", f"om_error_{index}")["status"],
                                 "classification_failed")
        self.backend.side_effect = None
        self.assertEqual(self.route("给Alpha加导出", "om_error_0")["status"],
                         "classification_failed")
        self.assert_no_task()

    def test_unsupported_and_unverified_project_do_not_register(self):
        self.projects()
        self.backend.return_value = suggestion("unsupported")
        self.assertEqual(self.route("今天星期几", "om_unsupported")["status"], "unsupported")
        self.backend.return_value = suggestion(project_number="P999")
        self.assertEqual(self.route("给Alpha加导出", "om_unknown")["status"],
                         "clarification_required")
        self.backend.return_value = suggestion(project_number="P001")
        self.assertEqual(self.route("给未知项目加导出", "om_ungrounded")["status"],
                         "clarification_required")
        self.backend.return_value = suggestion(project_number="P001", project_name="Beta")
        self.assertEqual(self.route("给Alpha加导出", "om_conflicting")["status"],
                         "clarification_required")
        self.assert_no_task()

    def test_concurrent_new_project_and_repeated_event(self):
        self.backend.return_value = suggestion("new_project", project_name="报表")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda i: self.route("新建报表项目，实现导出", f"om_batch_{i % 2}"), range(12)
            ))
        self.assertEqual([r["status"] for r in results].count("registered"), 1)
        self.assertEqual(len(self.manager.find_projects("报表")), 1)
        self.assertEqual(len(list(self.workspace.glob("project-*"))), 1)
        self.assert_no_task("T002")

    def test_unauthorized_messages_switch_and_existing_receipt_channel(self):
        text = "新建报表项目，实现导出"
        for changes in ({"sender_id": "ou_stranger"}, {"chat_type": "group"},
                        {"message_type": "image"}, {"sender_type": "bot"}):
            self.assertIsNone(self.bridge.handle_line(json.dumps(event(text, **changes))))
        self.bridge.send_receipt.assert_not_called()
        disabled = self.make_bridge(enabled=False)
        self.assertEqual(disabled.handle_line(json.dumps(event(text))), 0)
        self.assertIn("登记失败", disabled.send_receipt.call_args.args[-1])
        self.backend.assert_not_called()
        self.assertEqual(self.route("新建项目\nAlpha\nalpha\n整理需求", "om_fixed")["status"],
                         "registered")
        self.backend.assert_not_called()
        self.backend.return_value = suggestion(project_number="P001")
        self.assertEqual(self.bridge.handle_line(json.dumps(event("给Alpha加导出"))), 0)
        self.assertIn("任务 T002", self.bridge.send_receipt.call_args.args[-1])

    def test_generated_directory_skips_external_symlink_and_cleans_failed_transaction(self):
        outside = self.root / "outside"
        outside.mkdir()
        collision, usable = UUID(int=1), UUID(int=2)
        escape = self.workspace / f"project-{collision.hex}"
        escape.symlink_to(outside, target_is_directory=True)
        self.backend.return_value = suggestion("new_project", project_name="报表")
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executescript("""
                CREATE TABLE deferred_check (
                    project_id REFERENCES projects(id) DEFERRABLE INITIALLY DEFERRED
                );
                CREATE TRIGGER block_commit AFTER INSERT ON tasks
                BEGIN INSERT INTO deferred_check VALUES (999); END;
            """)
        with patch("project_manager.uuid4", side_effect=[collision, usable]):
            with self.assertRaises(sqlite3.IntegrityError):
                self.route("新建报表项目，实现导出")
        self.assertEqual(list(self.workspace.glob("project-*")), [escape])
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.manager.find_projects("报表"), [])
        self.assert_no_task()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("DROP TRIGGER block_commit")
        self.assertEqual(self.route("新建报表项目，实现导出")["status"], "registered")

    def test_unsafe_model_name_never_becomes_directory(self):
        self.backend.return_value = suggestion("new_project", project_name="../outside")
        self.assertEqual(self.route("新建../outside并实现导出")["status"],
                         "clarification_required")
        self.assertEqual(list(self.workspace.glob("project-*")), [])
        self.assert_no_task()

    def test_duplicate_json_fields_rejected(self):
        raw = suggestion().replace('"intent":', '"intent": "unsupported", "intent":', 1)
        with self.assertRaises(ValueError):
            parse_suggestion(raw)
