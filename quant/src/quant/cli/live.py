"""quant-live — precondition-checked launcher for the real-time trader.

The base install is brokerless, so this launcher fails with a clear, actionable
message when the broker extra or live credentials are missing — it never
silently half-starts. With the ``quant[alpaca]`` extra and Alpaca credentials
present, ``--run`` delegates to the unified trader's main loop; ``quant-paper``
is the keyless dry-run stand-in for development.
"""

from __future__ import annotations

import argparse
import os
import sys

_REQUIRED_KEYS = ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY")


def _alpaca_available() -> bool:
    try:
        import alpaca  # noqa: F401 - availability probe only

        return True
    except Exception:
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quant-live",
        description=(
            "Live (and paper) trading via Alpaca. Requires the 'quant[alpaca]' "
            "extra and Alpaca API credentials; `--run` starts the trader loop."
        ),
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="start the trader main loop (requires credentials)",
    )
    parser.add_argument(
        "--config-backend",
        choices=["file", "env", "ssm"],
        default=None,
        help="runtime-config backend (default: resolved from the environment)",
    )
    parser.add_argument(
        "--config-file",
        default=None,
        metavar="PATH",
        help="local JSON config file for the file backend",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not _alpaca_available():
        print(
            "quant-live requires the 'quant[alpaca]' extra (alpaca-py).\n"
            "  uv sync --extra alpaca        (repo installs)\n"
            "  pip install 'quant[alpaca]'   (installed distribution)\n"
            "For a keyless dev dry-run, use `quant-paper`.",
            file=sys.stderr,
        )
        return 2

    missing = [k for k in _REQUIRED_KEYS if not os.environ.get(k)]
    if missing:
        print(
            "quant-live requires Alpaca credentials; missing env var(s): "
            f"{', '.join(missing)}.",
            file=sys.stderr,
        )
        return 2

    if not args.run:
        print("quant-live is configured and credentials are present.")
        print("Start the trader loop with: quant-live --run")
        return 0

    from quant.live.trader import UnifiedTrader

    _apply_optional_env(args)
    trader = UnifiedTrader()
    trader.run()
    return 0


def _apply_optional_env(args: argparse.Namespace) -> None:
    """Map `--config-backend` / `--config-file` onto the trader's env contract."""
    if args.config_backend:
        os.environ["QI_CONFIG_BACKEND"] = args.config_backend
    if args.config_file:
        os.environ["QI_CONFIG_FILE"] = args.config_file


if __name__ == "__main__":  # pragma: no cover - console entry
    sys.exit(main())
