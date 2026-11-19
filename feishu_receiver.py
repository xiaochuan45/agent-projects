"""Route authenticated Feishu message events without opening a connection."""

import json
import re


MESSAGE_EVENT = "im.message.receive_v1"


def _object(value):
    return value if isinstance(value, dict) else {}


def valid_message_id(value):
    return isinstance(value, str) and re.fullmatch(r"om_[A-Za-z0-9_-]+", value) is not None


class FeishuMessageReceiver:
    """Accept events only from a trusted SDK or local lark-cli consumer."""

    def __init__(self, manager, *, allowed_open_id, namespace):
        if not isinstance(allowed_open_id, str) or not re.fullmatch(
            r"ou_[A-Za-z0-9_-]+", allowed_open_id
        ):
            raise ValueError("allowed_open_id must be a Feishu Open ID")
        if not isinstance(namespace, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+", namespace
        ):
            raise ValueError("namespace must identify the Feishu app and tenant")
        self.manager = manager
        self.allowed_open_id = allowed_open_id
        self.namespace = namespace

    def authorized_message(self, payload):
        """Return the message and envelope format if it passes the sender and chat checks."""
        payload = _object(payload)
        raw = "header" in payload or "event" in payload
        if raw:
            header = _object(payload.get("header"))
            if payload.get("schema") != "2.0" or header.get("event_type") != MESSAGE_EVENT:
                return None
            event = _object(payload.get("event"))
            message = _object(event.get("message"))
            sender = _object(event.get("sender"))
            sender_id = _object(sender.get("sender_id")).get("open_id")
        else:
            if payload.get("type") != MESSAGE_EVENT:
                return None
            sender = message = payload
            sender_id = payload.get("sender_id")

        if (sender.get("sender_type") != "user" or sender_id != self.allowed_open_id
                or message.get("chat_type") != "p2p" or message.get("message_type") != "text"):
            return None

        return message, raw

    def handle_event(self, payload):
        """Return a registration result from a decoded V2 event or CLI record."""
        authorized = self.authorized_message(payload)
        if authorized is None:
            return {"status": "ignored"}
        message, raw = authorized

        message_id = message.get("message_id")
        if not valid_message_id(message_id):
            return {"status": "invalid", "reason": "invalid_message_id"}

        content = message.get("content")
        if raw and isinstance(content, str):
            try:
                content = _object(json.loads(content)).get("text")
            except (ValueError, RecursionError):
                content = None
        if not isinstance(content, str) or "\x00" in content:
            return {"status": "invalid", "reason": "invalid_text"}

        command = content.strip().split("\n", 1)
        action = command[0].strip()
        if len(command) != 2 or action not in ("任务", "新建项目"):
            return {"status": "invalid", "reason": "invalid_command"}
        new_project = action == "新建项目"
        fields = [field.strip() for field in command[1].split("\n", 2 if new_project else 1)]
        if len(fields) != (3 if new_project else 2) or not all(fields):
            return {"status": "invalid", "reason": "invalid_command"}

        try:
            status, task, candidates = self.manager.register_message_task(
                fields[0], fields[-1],
                new_directory=fields[1] if new_project else None,
                external_message_id=f"feishu:{self.namespace}:{message_id}",
            )
        except ValueError as exc:
            return {"status": "invalid", "reason": str(exc)}

        if task is not None:
            return {
                "status": status,
                "project_number": task.project_number,
                "task_number": task.number,
                "task_status": task.status,
            }
        return {
            "status": status,
            "candidates": [
                {"project_number": project.number, "name": project.name}
                for project in candidates
            ],
        }
