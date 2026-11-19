"""Resolve model suggestions using trusted project data and Feishu confirmations."""

import json
import re
from contextlib import closing

from project_manager import _number_id


CONFIRM = re.compile(r"确认\s+(om_[A-Za-z0-9_-]+)\s+(P[0-9]{3,19}|新建\s+[^\r\n]+)", re.I)
NUMBER = re.compile(r"(?<![a-z0-9])p[0-9]+(?![a-z0-9])")
CONTEXT = re.compile(r"(?:刚刚|刚才|上一个|上次)(?:的|那个|这个)?项目")
CONFIRM_PREFIX = re.compile(r"^确认(?:\s|$)")


def task_result(status, task):
    return {"status": status, "project_number": task.project_number,
            "task_number": task.number, "task_status": task.status}


def _mentioned(text, name):
    # Latin identifiers need boundaries, while Chinese project names may
    # appear next to verbs or particles without any whitespace.
    name = name.casefold()
    left = r"(?<![a-z0-9])" if name[0].isascii() and name[0].isalnum() else ""
    right = r"(?![a-z0-9])" if name[-1].isascii() and name[-1].isalnum() else ""
    return re.search(left + re.escape(name) + right, text) is not None


def _looks_like_name(name):
    return bool(name and len(name) <= 120 and name not in (".", "..")
                and not any(char in name for char in "/\\\n\r\x00"))


class NaturalLanguageRouter:
    def __init__(self, manager, classifier):
        self.manager = manager
        self.classifier = classifier

    def route(self, text, namespace, sender, message_id):
        scope = (namespace, sender)
        key = f"feishu:{namespace}:{message_id}"
        is_confirmation = bool(CONFIRM_PREFIX.match(text))
        with closing(self.manager._connect()) as db, db:
            db.execute("BEGIN")
            previous = self._previous(db, scope, key)
            if previous is not None:
                return previous
            if not is_confirmation and 0 < len(text) <= 4000:
                projects = self.manager._list_projects(db)
                last_project = self.manager._confirmed_project(db, scope)
            else:
                projects, last_project = [], None

        suggestion = None
        if is_confirmation:
            result = {"status": "invalid_confirmation"}
        elif not text or len(text) > 4000:
            result = {"status": "invalid_natural_text"}
        else:
            try:
                suggestion = self.classifier.classify(text, [
                    {"number": p.number, "name": p.name, "aliases": p.aliases}
                    for p in projects
                ], last_project)
            except Exception:
                # Transport failures and malformed output both fail closed.
                result = {"status": "classification_failed"}

        with self.manager._directory_transaction() as (db, allocate):
            previous = self._previous(db, scope, key)
            if previous is not None:
                return previous
            title = None
            confirmation = CONFIRM.fullmatch(text)
            if confirmation:
                result = self._confirm(db, allocate, scope, key, *confirmation.groups())
            elif suggestion is not None:
                result, title = self._resolve(
                    db, allocate, scope, key, message_id, text, suggestion, last_project
                )
            db.execute(
                """INSERT INTO feishu_natural_messages
                   (message_id, namespace, sender, result, pending_title) VALUES (?, ?, ?, ?, ?)""",
                (key, *scope, json.dumps(result), title),
            )
            return result

    def _previous(self, db, scope, key):
        row = db.execute(
            "SELECT * FROM feishu_natural_messages WHERE message_id = ?", (key,)
        ).fetchone()
        if row is not None and (row["namespace"], row["sender"]) != scope:
            return {"status": "invalid_confirmation"}
        task = self.manager._message_task(db, key)
        if task is not None:
            return task_result("duplicate", task)
        if row is not None:
            result = json.loads(row["result"])
            if result["status"] in ("registered", "duplicate"):
                task = self.manager._task(self.manager._get(
                    db, "tasks", _number_id(result["task_number"], "T")
                ))
                return task_result("duplicate", task)
            return result
        return None

    def _resolve(self, db, allocate, scope, key, message_id, text, suggestion, last_project):
        if suggestion.intent == "unsupported":
            return {"status": "unsupported"}, None
        projects = self.manager._list_projects(db)
        by_number = {project.number: project for project in projects}
        folded = text.casefold()
        explicit = {reference.upper() for reference in NUMBER.findall(folded)}
        mentioned = {p.number for p in projects
                     if any(_mentioned(folded, name) for name in (p.name, *p.aliases))}
        candidates = (explicit & by_number.keys()) | mentioned
        unknown_number = bool(explicit - by_number.keys()) or (
            suggestion.project_number is not None and suggestion.project_number not in by_number
        )
        contextual = bool(CONTEXT.search(text))
        context_valid = (last_project in by_number and
                         last_project == self.manager._confirmed_project(db, scope))
        if contextual and context_valid:
            candidates.add(last_project)
        title = suggestion.task_title
        allow_new = False
        if suggestion.intent == "new_project":
            name = suggestion.project_name
            matches = self.manager._find_projects(db, name) if name else []
            candidates.update(project.number for project in matches)
            allow_new = not candidates and not unknown_number and not contextual
            if (allow_new and suggestion.project_number is None and title
                    and suggestion.confidence >= 0.8 and _looks_like_name(name)
                    and not re.fullmatch(r"[pt][0-9]+", name.casefold())
                    and _mentioned(folded, name)):
                project = self.manager._create_allocated_project(db, allocate, name)
                return self._register(db, scope, project.number, title, key), None
        elif (suggestion.intent == "existing_project" and title and len(candidates) == 1
              and not unknown_number and (not contextual or context_valid)
              and suggestion.confidence >= 0.8):
            number = next(iter(candidates))
            named = (self.manager._find_projects(db, suggestion.project_name)
                     if suggestion.project_name else [])
            number_agrees = suggestion.project_number in (None, number)
            name_agrees = not suggestion.project_name or (
                len(named) == 1 and named[0].number == number
            )
            grounded = suggestion.project_number == number or bool(named) or (
                contextual and last_project == number
            )
            if grounded and number_agrees and name_agrees:
                return self._register(db, scope, number, title, key), None

        return {
            "status": "clarification_required",
            "request_id": message_id,
            "candidates": [{"project_number": p.number, "name": p.name}
                           for p in projects if p.number in candidates],
            "can_confirm": title is not None,
            "allow_new": allow_new,
        }, title

    def _register(self, db, scope, number, title, key):
        task = self.manager._register_project_task(db, number, title, key, scope)
        return task_result("registered", task)

    def _confirm(self, db, allocate, scope, key, original_id, selection):
        original_key = f"feishu:{scope[0]}:{original_id}"
        pending = db.execute(
            """SELECT * FROM feishu_natural_messages
               WHERE message_id = ? AND namespace = ? AND sender = ? AND pending_title IS NOT NULL""",
            (original_key, *scope),
        ).fetchone()
        if pending is None or original_key == key:
            return {"status": "invalid_confirmation"}
        existing = self.manager._message_task(db, original_key)
        if existing is not None:
            return task_result("duplicate", existing)
        original_result = json.loads(pending["result"])
        if selection.startswith("新建"):
            name = selection.split(None, 1)[1].strip()
            if (not original_result["allow_new"] or not _looks_like_name(name)
                    or re.fullmatch(r"[pt][0-9]+", name.casefold())
                    or self.manager._find_projects(db, name)):
                return {"status": "invalid_confirmation"}
            number = self.manager._create_allocated_project(db, allocate, name).number
        else:
            number = selection.upper()
            candidates = {p["project_number"] for p in original_result["candidates"]}
            if ((candidates and number not in candidates)
                    or not self.manager._find_projects(db, number)):
                return {"status": "invalid_confirmation"}
        return self._register(db, scope, number, pending["pending_title"], original_key)
