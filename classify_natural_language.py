"""Run a supervised Codex classification with synthetic input and test credentials."""

import argparse
import json
import logging
import subprocess
from dataclasses import asdict
from pathlib import Path

from codex_classification_backend import CodexClassificationBackend, add_codex_options
from natural_language_classifier import NaturalLanguageClassifier


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path, required=True,
                        help="separately authenticated Codex home in an isolated test account")
    add_codex_options(parser)
    parser.add_argument("text", help="synthetic message to classify")
    args = parser.parse_args()
    if not args.text.strip() or len(args.text) > 4000:
        parser.error("supply 1 to 4000 characters of synthetic text")
    try:
        backend = CodexClassificationBackend(
            args.codex_home, model=args.model, timeout=args.classification_timeout,
        )
    except (OSError, RuntimeError, ValueError):
        parser.error("check the isolated Codex home and classification timeout")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        suggestion = NaturalLanguageClassifier(backend).classify(args.text, [], None)
    except (OSError, subprocess.SubprocessError, ValueError, RecursionError):
        parser.exit(1, "Classification failed.\n")
    print(json.dumps(asdict(suggestion), ensure_ascii=False))


if __name__ == "__main__":
    main()
