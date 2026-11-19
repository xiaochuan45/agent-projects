"""Run a supervised Codex classification with synthetic input and test credentials."""

import argparse
import json
import os
import subprocess
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from natural_language_classifier import NaturalLanguageClassifier, OUTPUT_SCHEMA


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path, required=True,
                        help="separately authenticated Codex home in an isolated test account")
    parser.add_argument("text", help="synthetic message to classify")
    args = parser.parse_args()
    codex_home = args.codex_home.expanduser().resolve()
    default_homes = {Path.home() / ".codex",
                     Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))}
    if not codex_home.is_dir() or any(codex_home == path.resolve() for path in default_homes):
        parser.error("--codex-home must be a separate existing test directory")
    if not args.text.strip() or len(args.text) > 4000:
        parser.error("supply 1 to 4000 characters of synthetic text")

    with TemporaryDirectory(prefix="codex-classification-") as temp:
        root = Path(temp)
        schema = root / "schema.json"
        schema.write_text(json.dumps(OUTPUT_SCHEMA), encoding="utf-8")
        output = root / "suggestion.json"

        def classify(prompt):
            subprocess.run(
                ["codex", "-a", "never", "exec", "--sandbox", "read-only",
                 "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
                 "--ephemeral", "--disable", "shell_tool", "--disable", "unified_exec",
                 "--disable", "incremental_exec_tools", "--disable", "incremental_exec_tools_legacy",
                 "--disable", "apps", "--disable", "plugins", "--disable", "hooks",
                 "--output-schema", str(schema), "--output-last-message", str(output), "-"],
                input=prompt, encoding="utf-8", stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, check=True, timeout=45, cwd=root,
                env={"PATH": os.environ.get("PATH", os.defpath),
                     "HOME": str(root), "CODEX_HOME": str(codex_home)},
            )
            with output.open(encoding="utf-8") as result:
                return result.read(16001)

        try:
            suggestion = NaturalLanguageClassifier(classify).classify(args.text, [], None)
        except (OSError, subprocess.SubprocessError, ValueError, RecursionError) as exc:
            parser.exit(1, f"Classification failed ({type(exc).__name__}).\n")
        print(json.dumps(asdict(suggestion), ensure_ascii=False))


if __name__ == "__main__":
    main()
