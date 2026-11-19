"""Run Feishu natural-language intake with a dedicated test app and local state."""

import argparse
import logging
import os
import re
import sqlite3
from pathlib import Path

from codex_classification_backend import CodexClassificationBackend, add_codex_options
from feishu_live_bridge import LiveBridge, database_path, run_bridge
from natural_language_classifier import NaturalLanguageClassifier
from project_manager import ProjectManager


def isolated_paths(state_dir):
    root = state_dir.expanduser().resolve(strict=True)
    database = root / "nl-test.sqlite3"
    workspace = root / "workspace"
    codex_home = root / "codex-home"
    if any(path.is_symlink() for path in (database, workspace, codex_home)):
        raise ValueError("test state paths must not be symlinks")
    for path in (database, *(root / (database.name + suffix)
                             for suffix in ("-journal", "-wal", "-shm"))):
        if path.is_symlink() or (path.exists() and
                                 (not path.is_file() or path.stat().st_nlink != 1)):
            raise ValueError("SQLite files must be private to the test state directory")
    regular_database = os.environ.get("FEISHU_DB_PATH")
    if regular_database and database == Path(regular_database).expanduser().resolve():
        raise ValueError("test database must differ from the regular bridge database")
    if os.environ.get("FEISHU_WORKSPACE"):
        regular = Path(os.environ["FEISHU_WORKSPACE"]).expanduser().resolve()
        if workspace.is_relative_to(regular) or regular.is_relative_to(workspace):
            raise ValueError("test workspace must be separate from the regular bridge workspace")
    return database_path(database), workspace, codex_home


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True,
                        help="dedicated directory outside Git with a separately logged-in codex-home")
    parser.add_argument("--lark-profile", required=True, help="lark-cli profile for the test app")
    parser.add_argument("--namespace", required=True, help="test app and tenant identifier")
    parser.add_argument("--open-id", action="append", required=True,
                        help="authorized test sender Open ID (repeatable)")
    parser.add_argument("--once", action="store_true", help="exit after one eligible message")
    parser.add_argument("--timeout", type=int, default=120, help="event wait in seconds for --once")
    add_codex_options(parser)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if (not args.lark_profile.strip()
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", args.namespace)
            or any(not re.fullmatch(r"ou_[A-Za-z0-9_-]+", value) for value in args.open_id)):
        parser.error("provide a test profile, app/tenant namespace and valid sender Open IDs")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        database, workspace, codex_home = isolated_paths(args.state_dir)
        backend = CodexClassificationBackend(
            codex_home, model=args.model, timeout=args.classification_timeout,
        )
        workspace.mkdir(mode=0o700, exist_ok=True)
        bridge = LiveBridge(
            ProjectManager(database, workspace), args.open_id, args.namespace,
            cli=("lark-cli", "--profile", args.lark_profile),
            natural_language_enabled=True, classifier=NaturalLanguageClassifier(backend),
        )
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        logging.error("Test bridge initialization failed. Check test state and options.")
        return 2
    return run_bridge(bridge, once=args.once, timeout=args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
