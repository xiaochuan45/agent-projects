"""Register tasks from the local lark-cli event bus and send bot receipts."""

import argparse
import hashlib
import json
import logging
import os
import selectors
import signal
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path

from codex_classification_backend import CodexClassificationBackend
from feishu_receiver import MESSAGE_EVENT, FeishuMessageReceiver, valid_message_id
from natural_language_classifier import NaturalLanguageClassifier
from natural_language_router import NaturalLanguageRouter
from project_manager import ProjectManager


LOG = logging.getLogger(__name__)
READY = f"[event] ready event_key={MESSAGE_EVENT}".encode()
STARTUP_TIMEOUT = 30
SEND_TIMEOUT = 15
RETRY_MIN = 2
RETRY_MAX = 60


def database_path(value):
    path = Path(value).expanduser().resolve()
    if any((directory / ".git").is_file() or (directory / ".git" / "HEAD").is_file()
           or ((directory / "HEAD").is_file() and (directory / "objects").is_dir()
               and (directory / "refs").is_dir()) for directory in path.parents):
        raise ValueError("FEISHU_DB_PATH must be outside Git repositories")
    if not path.parent.is_dir():
        raise ValueError("FEISHU_DB_PATH parent directory must already exist")
    return path


def receipt_text(result):
    if result["status"] in ("registered", "duplicate"):
        return f'已登记。项目 {result["project_number"]}，任务 {result["task_number"]}。'
    if result["status"] == "clarification_required":
        candidates = result["candidates"]
        if "request_id" in result:
            request_id = result["request_id"]
            if not result["can_confirm"]:
                return "请补充具体开发任务和项目名称，然后重新发送完整需求。"
            options = "、".join(f'{p["project_number"]}（{p["name"][:120]}）'
                               for p in candidates[:20])
            prompt = f"请选择项目。候选项目为 {options}。" if options else "请核对项目归属。"
            prompt += f" 回复「确认 {request_id} 项目编号」以登记原任务。"
            if result["allow_new"]:
                prompt += f" 如需新建，请回复「确认 {request_id} 新建 项目名称」。"
            return prompt
        if not candidates:
            return "未找到项目。请核对项目编号、名称或别名，或使用「新建项目」。"
        numbers = "、".join(project["project_number"] for project in candidates[:20])
        return f"请使用明确的项目编号重新发送任务。候选编号（最多 20 个）为 {numbers}。"
    natural_errors = {
        "classification_failed": "暂时无法识别需求。请重新发送完整需求或使用固定格式。",
        "unsupported": "这条消息未识别为开发需求。请说明要开发的功能和所属项目。",
        "invalid_confirmation": "无法确认该请求。请检查原回执中的请求编号和项目选项。",
        "invalid_natural_text": "需求文本无效。请检查项目名称和任务内容后重新发送。",
    }
    if result["status"] in natural_errors:
        return natural_errors[result["status"]]
    return (
        "登记失败。请核对命令、项目名称、目录和任务内容。\n"
        "已有项目格式为「任务 / 项目 / 内容」。\n"
        "新建项目格式为「新建项目 / 名称 / 已有目录 / 内容」。每项单独一行。"
    )


class LiveBridge:
    def __init__(self, manager, open_ids, namespace, cli=("lark-cli",), *,
                 natural_language_enabled=False, classifier=None):
        self.manager = manager
        self.namespace = namespace
        self.cli = cli
        self.stopping = False
        if natural_language_enabled and classifier is None:
            raise ValueError("natural language routing requires an injected classifier")
        router = NaturalLanguageRouter(manager, classifier) if natural_language_enabled else None
        self.receivers = {
            open_id: FeishuMessageReceiver(
                manager, allowed_open_id=open_id, namespace=namespace, natural_router=router
            )
            for open_id in open_ids
        }
        with closing(sqlite3.connect(self.manager.database)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS feishu_sent_receipts (key TEXT PRIMARY KEY)")

    def handle_line(self, line):
        """Return None for ignored input or an exit code for a handled event."""
        try:
            payload = json.loads(line)
        except (ValueError, RecursionError):
            LOG.warning("Discarded malformed event NDJSON")
            return None
        if (not isinstance(payload, dict) or "header" in payload or "event" in payload
                or not isinstance(payload.get("sender_id"), str)):
            return None
        recipient = payload["sender_id"]
        receiver = self.receivers.get(recipient)
        if receiver is None:
            return None
        if receiver.authorized_message(payload) is None:
            return None
        message_id = payload.get("message_id")
        if not valid_message_id(message_id):
            LOG.warning("Discarded event without a valid message ID")
            return None
        identity = f"feishu-receipt:{self.namespace}:{message_id}"
        key = hashlib.sha256(identity.encode()).hexdigest()[:50]
        try:
            with closing(sqlite3.connect(self.manager.database)) as db:
                if db.execute(
                    "SELECT 1 FROM feishu_sent_receipts WHERE key = ?", (key,)
                ).fetchone():
                    return 0
            result = receiver.handle_event(payload)
            return self.send_receipt(recipient, key, receipt_text(result))
        except (OSError, sqlite3.Error, UnicodeError):
            LOG.error("Could not process Feishu event")
            return 1

    def send_receipt(self, recipient, key, message):
        with closing(sqlite3.connect(self.manager.database)) as db, db:
            try:
                reply = subprocess.run(
                    [*self.cli, "im", "+messages-send", "--as", "bot",
                     "--user-id", recipient, "--text", message, "--idempotency-key", key],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    timeout=SEND_TIMEOUT, check=True,
                )
                output = json.loads(reply.stdout)
            except (OSError, subprocess.SubprocessError, ValueError, RecursionError):
                LOG.warning("Feishu receipt send failed or could not be confirmed")
                return 1
            data = output.get("data") if isinstance(output, dict) else None
            if (not isinstance(data, dict) or not data.get("message_id")
                    or output.get("ok") is False or output.get("error")):
                LOG.warning("Feishu receipt response did not confirm a message ID")
                return 1
            db.execute("INSERT OR IGNORE INTO feishu_sent_receipts VALUES (?)", (key,))
        return 0

    def consume(self, once, deadline):
        try:
            child = subprocess.Popen(
                [*self.cli, "event", "consume", MESSAGE_EVENT, "--as", "bot"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except OSError:
            LOG.warning("Could not start the Feishu consumer")
            return None
        try:
            return self.read_events(child, once, deadline)
        finally:
            # Keep stdin open through listening. EOF requests graceful exit on this key.
            child.stdin.close()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            child.stdout.close()
            child.stderr.close()
            LOG.info("Feishu consumer exited with code %s", child.returncode)

    def read_events(self, child, once, deadline):
        ready = False
        startup_deadline = time.monotonic() + STARTUP_TIMEOUT
        buffers = {child.stderr: b"", child.stdout: b""}
        with selectors.DefaultSelector() as selector:
            selector.register(child.stderr, selectors.EVENT_READ)
            while not self.stopping:
                limit = deadline if ready else min(startup_deadline, deadline)
                remaining = limit - time.monotonic()
                if remaining <= 0:
                    if not ready:
                        LOG.warning("Timed out waiting for Feishu consumer readiness")
                    return None
                for selected, _ in selector.select(min(remaining, 0.25)):
                    stream = selected.fileobj
                    chunk = os.read(stream.fileno(), 65536)
                    if not chunk:
                        selector.unregister(stream)
                        if stream is child.stdout or not ready:
                            return None
                        continue
                    lines = (buffers[stream] + chunk).split(b"\n")
                    buffers[stream] = lines.pop()
                    if len(buffers[stream]) > 1024 * 1024:
                        LOG.warning("Feishu consumer emitted an oversized line")
                        return None
                    for line in lines:
                        if self.stopping or time.monotonic() >= deadline:
                            return None
                        if stream is child.stderr:
                            if not ready and line.rstrip(b"\r") == READY:
                                ready = True
                                selector.register(child.stdout, selectors.EVENT_READ)
                                LOG.info("Feishu consumer ready")
                            elif b"drop" in line.lower():
                                LOG.warning("Feishu consumer reported possible event loss")
                        else:
                            outcome = self.handle_line(line)
                            if once and outcome is not None:
                                return outcome
        return None

    def run(self, *, once=False, timeout=120):
        deadline = time.monotonic() + timeout if once else float("inf")
        delay = RETRY_MIN
        while not self.stopping and time.monotonic() < deadline:
            started = time.monotonic()
            result = self.consume(once, deadline)
            if result is not None:
                return result
            if self.stopping or time.monotonic() >= deadline:
                break
            if time.monotonic() - started >= RETRY_MAX:
                delay = RETRY_MIN
            LOG.warning("Feishu consumer disconnected. Retrying in %s seconds", delay)
            retry_at = min(time.monotonic() + delay, deadline)
            while not self.stopping and time.monotonic() < retry_at:
                time.sleep(min(0.25, max(0, retry_at - time.monotonic())))
            delay = min(delay * 2, RETRY_MAX)
        if not self.stopping:
            LOG.warning("No qualifying event was handled before the single-run timeout")
        return 0 if self.stopping else 1


def main(*, classifier=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="exit after one eligible message")
    parser.add_argument("--timeout", type=int, default=120,
                        help="event wait deadline in seconds for --once (default: 120)")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        database = database_path(os.environ["FEISHU_DB_PATH"])
        workspace = Path(os.environ["FEISHU_WORKSPACE"]).expanduser()
        namespace = os.environ["FEISHU_NAMESPACE"]
        open_ids = [value.strip() for value in os.environ["FEISHU_ALLOWED_OPEN_IDS"].split(",")]
        natural_language_enabled = os.environ.get("FEISHU_NATURAL_LANGUAGE_ENABLED", "0")
        if natural_language_enabled not in ("0", "1"):
            raise ValueError("FEISHU_NATURAL_LANGUAGE_ENABLED must be 0 or 1")
        if natural_language_enabled == "1" and classifier is None:
            codex_home = os.environ["FEISHU_CODEX_HOME"]
            if not codex_home.strip():
                parser.error("FEISHU_CODEX_HOME must point to a separate Codex login")
            classifier = NaturalLanguageClassifier(CodexClassificationBackend(codex_home))
        bridge = LiveBridge(
            ProjectManager(database, workspace), open_ids, namespace,
            natural_language_enabled=natural_language_enabled == "1", classifier=classifier,
        )
    except KeyError as exc:
        parser.error(f"missing environment variable {exc.args[0]}")
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        LOG.error("Bridge initialization failed (%s). Check environment and database location.",
                  type(exc).__name__)
        return 2

    return run_bridge(bridge, once=args.once, timeout=args.timeout)


def run_bridge(bridge, *, once, timeout):
    def stop(_signum, _frame):
        bridge.stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return bridge.run(once=once, timeout=timeout)


if __name__ == "__main__":
    raise SystemExit(main())
