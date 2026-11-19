"""Run a single, bounded classification using an isolated Codex CLI login."""

import json
import logging
import math
import os
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from natural_language_classifier import OUTPUT_SCHEMA


LOG = logging.getLogger(__name__)
DISABLED_FEATURES = (
    "shell_tool", "incremental_exec_tools", "shell_snapshot",
    "apps", "plugins", "hooks", "memories", "browser_use", "browser_use_external",
    "multi_agent", "multi_agent_v2",
)
USAGE_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


def add_codex_options(parser):
    parser.add_argument("--model", help="Codex model ID (default: CLI default)")
    parser.add_argument("--classification-timeout", type=float, default=45, metavar="SECONDS")


class CodexClassificationBackend:
    def __init__(self, codex_home, *, model=None, timeout=45):
        self.codex_home = Path(codex_home).expanduser().resolve()
        default_home = Path.home() / ".codex"
        active_home = Path(os.environ.get("CODEX_HOME", default_home)).expanduser()
        if (not self.codex_home.is_dir()
                or any(self.codex_home == home.resolve()
                       or self.codex_home.is_relative_to(home.resolve())
                       for home in (default_home, active_home))):
            raise ValueError("use a separate existing Codex test login directory")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("classification timeout must be finite and positive")
        self.model = model
        self.timeout = timeout

    def __call__(self, prompt):
        started = time.monotonic()
        status = "failed"
        usage = {}
        try:
            with TemporaryDirectory(prefix="codex-classification-") as directory:
                root = Path(directory)
                schema = root / "schema.json"
                schema.write_text(json.dumps(OUTPUT_SCHEMA), encoding="utf-8")
                output = root / "suggestion.json"
                command = [
                    "codex", "-a", "never", "exec", "--sandbox", "read-only",
                    "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
                    "--ephemeral", "--json", "--color", "never",
                    "-c", 'model_reasoning_effort="low"', "-c", 'web_search="disabled"',
                    "--output-schema", str(schema), "--output-last-message", str(output),
                ]
                for feature in DISABLED_FEATURES:
                    command.extend(("--disable", feature))
                if self.model is not None:
                    command.extend(("--model", self.model))

                # Keep CLI diagnostics out of application logs and off the terminal.
                try:
                    reply = subprocess.run(
                        [*command, "-"], input=prompt, encoding="utf-8",
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
                        timeout=self.timeout, cwd=root,
                        env={"PATH": os.environ.get("PATH", os.defpath),
                             "HOME": str(root), "CODEX_HOME": str(self.codex_home)},
                    )
                except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
                    usage, _ = _read_events(exc.stdout)
                    raise
                usage, failed = _read_events(reply.stdout)
                if failed:
                    raise ValueError("Codex reported a failed turn")
                with output.open(encoding="utf-8") as result:
                    raw = result.read(16001)
            status = "returned"
            return raw
        except subprocess.TimeoutExpired:
            status = "timeout"
            raise
        finally:
            LOG.info("Codex classification status=%s elapsed_ms=%d usage=%s",
                     status, round((time.monotonic() - started) * 1000), usage)


def _read_events(events):
    usage = {}
    failed = False
    for line in (events or "").splitlines():
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind in ("turn.failed", "error"):
            failed = True
        if kind == "turn.completed" and isinstance(event.get("usage"), dict):
            counts = event["usage"]
            usage = {key: counts[key] for key in USAGE_FIELDS
                     if type(counts.get(key)) is int and counts[key] >= 0}
    return usage, failed
