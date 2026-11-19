import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from project_manager import ProjectManager


class ProjectManagerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.project_path = self.workspace / "app"
        self.project_path.mkdir()
        self.second_path = self.workspace / "app-other"
        self.second_path.mkdir()
        self.database = self.root / "projects.sqlite3"
        self.manager = ProjectManager(self.database, self.workspace)

    def test_numbering_and_project_relationship(self):
        first = self.manager.create_project("Alpha", "app")
        second = self.manager.create_project("Beta", self.second_path)
        self.assertEqual((first.number, second.number), ("P001", "P002"))
        self.assertEqual(second.directory, "app-other")
        task = self.manager.create_task("p002", "Write README")
        self.assertEqual((task.number, task.project_number), ("T001", "P002"))
        self.assertEqual(self.manager.create_task("P001", "Review").number, "T002")
        with self.assertRaises(KeyError):
            self.manager.create_task("P999", "Orphan")

    def test_lookup_returns_all_distinct_candidates(self):
        first = self.manager.create_project("Straße", "app", aliases=["Shared", "shared"])
        second = self.manager.create_project("Shared", "app-other", aliases=["Beta", "Shared"])
        self.assertEqual(first.aliases, ("Shared",))
        self.assertEqual(self.manager.find_projects(" STRASSE "), [first])
        self.assertEqual(self.manager.find_projects("beta"), [second])
        self.assertEqual(self.manager.find_projects("shared"), [first, second])
        self.assertEqual(self.manager.find_projects("p001"), [first])
        self.assertEqual(self.manager.get_project("P001"), first)
        self.assertEqual(self.manager.find_projects("unknown"), [])
        self.assertEqual(self.manager.find_projects("P9223372036854775808"), [])

    def test_project_number_takes_precedence_and_alias_cannot_claim_another_number(self):
        for name in ("P001", " p002 ", "T003", "P1000"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "reserved"):
                self.manager.create_project(name, "app")
        first = self.manager.create_project("Alpha", "app", aliases=["p001"])
        self.assertEqual(self.manager.find_projects("P002"), [])
        for alias in (" p001 ", "P003"):
            with self.subTest(alias=alias), self.assertRaisesRegex(ValueError, "alias"):
                self.manager.create_project("Beta", "app-other", aliases=[alias])
        second = self.manager.create_project("Beta", "app-other")
        self.assertEqual(second.number, "P002")
        self.assertEqual(self.manager.find_projects(" P001 "), [first])
        self.assertEqual(self.manager.find_projects("p002"), [second])

    def test_status_and_session_persist_and_can_be_cleared(self):
        project = self.manager.create_project("Alpha", "app", codex_session_id="project-session")
        task = self.manager.create_task(project.number, "Task")
        self.assertEqual((project.status, task.status), ("pending", "pending"))
        self.assertIsNone(task.codex_session_id)
        project = self.manager.update_project(project.number, status="in_progress")
        task = self.manager.update_task(task.number, status="completed", codex_session_id="task-session")
        reopened = ProjectManager(self.database, self.workspace)
        self.assertEqual(reopened.get_project(project.number), project)
        self.assertEqual(reopened.get_task(task.number), task)
        cleared = reopened.update_task(task.number, codex_session_id=None)
        self.assertEqual(cleared.status, "completed")
        self.assertIsNone(cleared.codex_session_id)
        with self.assertRaises(ValueError):
            reopened.update_task(task.number, status="invalid")
        self.assertEqual(reopened.get_task(task.number), cleared)

    def test_repeated_external_message_returns_original_task(self):
        first = self.manager.create_project("Alpha", "app")
        second = self.manager.create_project("Beta", "app-other")
        original = self.manager.create_task(first.number, "Original", external_message_id="event-1")
        original = self.manager.update_task(original.number, status="completed", codex_session_id="session")
        reopened = ProjectManager(self.database, self.workspace)
        duplicate = reopened.create_task("p001", " Original ", external_message_id=" event-1 ")
        self.assertEqual(duplicate, original)
        for project, title in ((second, "Original"), (first, "Changed")):
            with self.subTest(project=project.number, title=title), self.assertRaisesRegex(ValueError, "conflicts"):
                reopened.create_task(project.number, title, external_message_id="event-1")
        self.assertEqual(reopened.create_task(first.number, "Manual").number, "T002")
        self.assertEqual(reopened.create_task(first.number, "Manual").number, "T003")

    def test_concurrent_external_message_creates_one_task(self):
        project = self.manager.create_project("Alpha", "app")

        def submit(_):
            return self.manager.create_task(project.number, "Task", external_message_id="event-1")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit, range(16)))
        self.assertEqual({task.number for task in results}, {"T001"})
        with self.assertRaises(KeyError):
            self.manager.get_task("T002")

    def test_rejects_unsafe_or_invalid_directories(self):
        outside = self.root / "workspace-other"
        outside.mkdir()
        (self.workspace / "escape").symlink_to(outside, target_is_directory=True)
        (self.workspace / "file").touch()
        git_objects = self.workspace / ".git" / "objects"
        git_objects.mkdir(parents=True)
        (self.workspace / "git-link").symlink_to(git_objects, target_is_directory=True)
        for directory in ("../workspace-other", "app/../app", outside, "escape", "missing",
                          "file", ".", self.workspace, ".git", ".git/objects", "git-link"):
            with self.subTest(directory=directory), self.assertRaises(ValueError):
                self.manager.create_project("Invalid", directory)
        self.assertEqual(self.manager.create_project("Valid", "app").number, "P001")

    def test_rejects_duplicate_and_nested_directories(self):
        (self.project_path / "child").mkdir()
        (self.second_path / "child").mkdir()
        (self.workspace / "link").symlink_to(self.project_path, target_is_directory=True)
        self.manager.create_project("Alpha", "app")
        self.manager.create_project("Beta", "app-other/child")
        for directory in ("app", "link", "app/child", "app-other"):
            with self.subTest(directory=directory), self.assertRaisesRegex(ValueError, "conflicts"):
                self.manager.create_project("Conflict", directory)

    def test_concurrent_project_creation_reserves_one_directory(self):
        (self.project_path / "child").mkdir()

        def submit(directory):
            try:
                return self.manager.create_project("Concurrent", directory)
            except ValueError as exc:
                self.assertIn("conflicts", str(exc))
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit, ["app", "app/child"] * 8))
        self.assertEqual([project.number for project in results if project], ["P001"])
        self.assertEqual(len(self.manager.find_projects("Concurrent")), 1)

    def test_resolves_internal_link_and_rechecks_stored_path(self):
        (self.workspace / "link").symlink_to(self.project_path, target_is_directory=True)
        project = self.manager.create_project("Alpha", "link")
        self.assertEqual(project.directory, "app")
        self.assertEqual(self.manager.project_directory(project.number), self.project_path)
        self.project_path.rmdir()
        self.project_path.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.manager.project_directory(project.number)

    def test_rechecks_directory_conflict_after_symlink_replacement(self):
        first = self.manager.create_project("Alpha", "app")
        second = self.manager.create_project("Beta", "app-other")
        self.project_path.rmdir()
        self.project_path.symlink_to(self.second_path, target_is_directory=True)
        for project in (first, second):
            with self.subTest(project=project.number), self.assertRaisesRegex(ValueError, "conflicts"):
                self.manager.project_directory(project.number)


if __name__ == "__main__":
    unittest.main()
