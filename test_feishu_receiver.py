import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from feishu_receiver import MESSAGE_EVENT, FeishuMessageReceiver
from project_manager import ProjectManager


def event(text="任务\nAlpha\n整理需求", **changes):
    return {
        "type": MESSAGE_EVENT,
        "event_id": "delivery-1",
        "sender_id": "ou_owner",
        "sender_type": "user",
        "chat_type": "p2p",
        "message_type": "text",
        "message_id": "om_message",
        "content": text,
        **changes,
    }


class FeishuReceiverTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.workspace = root / "workspace"
        for directory in ("alpha", "beta"):
            (self.workspace / directory).mkdir(parents=True)
        self.database = root / "projects.sqlite3"
        self.manager = ProjectManager(self.database, self.workspace)
        self.receiver = self.make_receiver(self.manager)

    @staticmethod
    def make_receiver(manager, namespace="test-app.tenant"):
        return FeishuMessageReceiver(manager, allowed_open_id="ou_owner", namespace=namespace)

    def test_cli_event_routes_by_alias_and_returns_receipt(self):
        self.manager.create_project("Alpha", "alpha", aliases=["甲"])
        result = self.receiver.handle_event(event("任务\r\n甲\r\n整理需求\n补充说明"))
        self.assertEqual(result, {
            "status": "registered", "project_number": "P001",
            "task_number": "T001", "task_status": "pending",
        })
        self.assertEqual(self.manager.get_task("T001").title, "整理需求\n补充说明")

    def test_raw_event_creates_project_and_task(self):
        raw = {
            "schema": "2.0",
            "header": {"event_type": MESSAGE_EVENT},
            "event": {
                "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_owner"}},
                "message": {
                    "chat_type": "p2p", "message_type": "text", "message_id": "om_message",
                    "content": json.dumps({"text": "新建项目\nAlpha\nalpha\n整理需求"}),
                },
            },
        }
        self.assertEqual(self.receiver.handle_event(raw)["status"], "registered")
        self.assertEqual(self.manager.get_project("P001").directory, "alpha")
        self.assertEqual(self.manager.get_task("T001").project_number, "P001")
        self.assertEqual(self.receiver.handle_event(event("新建项目\nAlpha\nalpha\n整理需求"))["status"],
                         "duplicate")
        raw["event"]["sender"]["sender_id"] = {"open_id": "ou_stranger"}
        self.assertEqual(self.receiver.handle_event(raw), {"status": "ignored"})

    def test_ignores_wrong_sender_chat_type_message_type_and_card_event(self):
        self.manager.create_project("Alpha", "alpha")
        for changes in (
            {"sender_id": "ou_stranger"}, {"sender_id": None}, {"chat_type": "group"},
            {"message_type": "image"}, {"sender_type": "bot"},
            {"type": "card.action.trigger"},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(self.receiver.handle_event(event(**changes)), {"status": "ignored"})
        with self.assertRaises(KeyError):
            self.manager.get_task("T001")

    def test_invalid_payloads_do_not_register(self):
        for payload in (
            event(message_id=None), event(" "), event("任务\n\n标题"),
            event("新建项目\nAlpha\n\n标题"), event("任务\nAlpha\n满\x00足"),
            {
                "schema": "2.0", "header": {"event_type": MESSAGE_EVENT},
                "event": {
                    "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_owner"}},
                    "message": dict(event(), content="{"),
                },
            },
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self.receiver.handle_event(payload)["status"], "invalid")
        self.assertEqual(self.receiver.handle_event([]), {"status": "ignored"})
        with self.assertRaises(KeyError):
            self.manager.get_task("T001")

    def test_ambiguous_lookup_returns_all_candidates_then_explicit_number_routes(self):
        self.manager.create_project("Alpha", "alpha", aliases=["共同"])
        self.manager.create_project("Beta", "beta", aliases=["共同"])
        result = self.receiver.handle_event(event("任务\n共同\n整理需求"))
        self.assertEqual(result, {"status": "clarification_required", "candidates": [
            {"project_number": "P001", "name": "Alpha"},
            {"project_number": "P002", "name": "Beta"},
        ]})
        with self.assertRaises(KeyError):
            self.manager.get_task("T001")
        self.assertEqual(self.receiver.handle_event(event("任务\nP002\n整理需求"))["project_number"],
                         "P002")

    def test_unknown_project_and_new_project_name_collision_require_clarification(self):
        self.assertEqual(self.receiver.handle_event(event()),
                         {"status": "clarification_required", "candidates": []})
        self.manager.create_project("Alpha", "alpha", aliases=["甲"])
        self.assertEqual(self.receiver.handle_event(event("新建项目\n甲\nbeta\n整理需求")), {
            "status": "clarification_required",
            "candidates": [{"project_number": "P001", "name": "Alpha"}],
        })
        with self.assertRaises(KeyError):
            self.manager.get_task("T001")

    def test_new_project_rejects_reserved_names_and_invalid_path(self):
        for name, directory in (("P001", "alpha"), ("p999", "alpha"), ("T001", "alpha"),
                                ("Alpha", "../outside"), ("Alpha", "missing")):
            with self.subTest(name=name, directory=directory):
                response = self.receiver.handle_event(event(f"新建项目\n{name}\n{directory}\n任务"))
                self.assertEqual(response["status"], "invalid")
        with self.assertRaises(KeyError):
            self.manager.get_project("P001")

    def test_redelivery_uses_message_id_and_survives_reopen(self):
        self.manager.create_project("Alpha", "alpha")
        self.receiver.handle_event(event())
        self.manager.update_task("T001", status="completed")
        reopened = self.make_receiver(ProjectManager(self.database, self.workspace))
        self.assertEqual(reopened.handle_event(event(event_id="delivery-2")), {
            "status": "duplicate", "project_number": "P001",
            "task_number": "T001", "task_status": "completed",
        })
        other = self.make_receiver(self.manager, "other-app.tenant")
        self.assertEqual(other.handle_event(event())["task_number"], "T002")
        self.assertEqual(reopened.handle_event(event(message_id="om_other"))["task_number"], "T003")

    def test_concurrent_redelivery_creates_one_project_and_one_task(self):
        payload = event("新建项目\nAlpha\nalpha\n整理需求")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(self.receiver.handle_event, [payload] * 16))
        self.assertEqual([r["status"] for r in results].count("registered"), 1)
        self.assertEqual([r["status"] for r in results].count("duplicate"), 15)
        self.assertEqual({(r["project_number"], r["task_number"]) for r in results},
                         {("P001", "T001")})

    def test_task_insert_failure_rolls_back_new_project(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("""
                CREATE TRIGGER block_task BEFORE INSERT ON tasks
                BEGIN SELECT RAISE(ABORT, 'simulated failure'); END
            """)
        payload = event("新建项目\nAlpha\nalpha\n整理需求")
        with self.assertRaises(sqlite3.IntegrityError):
            self.receiver.handle_event(payload)
        self.assertEqual(self.manager.find_projects("Alpha"), [])
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("DROP TRIGGER block_task")
        self.assertEqual(self.receiver.handle_event(payload)["project_number"], "P001")
