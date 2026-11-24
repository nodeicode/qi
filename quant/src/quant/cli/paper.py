"""quant-paper — zero-AWS dev-mode dry run.

Starts a paper/offline runtime that polls a *local* JSON config file (design D6
local-first), resolves the reference strategy, and logs what it would do — no
boto3, no Alpaca credentials. It runs a bounded number of ticks and exits
cleanly, so it is a safe, keyless stand-in for a full paper run in CI/docs.

Writing a default `paper_config.json` in the current directory when `--config`
does not exist makes the command self-bootstrapping for first-time users.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from quant.cli._common import make_equity_config, resolve_strategy
from quant.config import ConfigSync, FileConfigSource

DEFAULT_CONFIG = "paper_config.json"
DEFAULT_ITERATIONS = 5

_LOG = logging.getLogger("quant-paper")


def _setup_logging() -> None:
    _LOG.setLevel(logging.INFO)
    if not _LOG.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
        _LOG.addHandler(handler)


def _default_payload() -> dict:
    return {"strategies": [{"name": "example_equity", "clients": ["paper"]}]}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quant-paper",
        description=(
            "Zero-AWS dev-mode dry run: poll a local JSON config, resolve the "
            "reference strategy, log intended behavior, exit cleanly."
        ),
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        metavar="PATH",
        help=f"local JSON config file to watch (default {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=DEFAULT_ITERATIONS,
        help=f"ticks to run before exiting (default {DEFAULT_ITERATIONS})",
    )
    parser.add_argument(
        "--no-sleep",
        action="store_true",
        help="do not sleep between ticks (faster, CI/docs-friendly)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    args = build_parser().parse_args(argv)

    path = Path(args.config)
    if not path.exists():
        path.write_text(
            json.dumps(_default_payload(), indent=2) + "\n", encoding="utf-8"
        )
        _LOG.info("wrote default config to %s (paper, example_equity)", path)

    sync = ConfigSync(FileConfigSource(path))
    _LOG.info("paper runtime starting (backend=%s, config=%s)", "file", path)

    for tick in range(args.iterations):
        cfg = sync.poll()
        if cfg is not None:
            n = len(cfg.get("strategies", []))
            _LOG.info(
                "tick %d: config adopted (%d strategy(s))",
                tick,
                n,
            )
            for entry in cfg.get("strategies", []):
                if isinstance(entry, dict):
                    # Resolve each named strategy through the registry — this is
                    # the exact load path live uses (dry-run: no execution).
                    strategy = resolve_strategy(
                        entry.get("name", "example_equity"), make_equity_config()
                    )
                    _LOG.info("tick %d: %s ready (paper)", tick, strategy.name)
        else:
            _LOG.info("tick %d: config unchanged", tick)
        if not args.no_sleep:
            time.sleep(0.1)

    _LOG.info("paper dry-run complete; exiting cleanly")
    return 0


if __name__ == "__main__":  # pragma: no cover - console entry
    sys.exit(main())
