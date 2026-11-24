"""quant-backtest — run an offline, deterministic backtest on the reference strategy.

No network, no API keys: bars come from a synthetic source and the equity
strategy (`example_equity`) runs end-to-end through the shared pipeline.
"""

from __future__ import annotations

import argparse
import sys

from quant.cli._common import (
    DEFAULT_END,
    DEFAULT_START,
    DEFAULT_TICKER,
    run_offline_backtest,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quant-backtest",
        description=(
            "Run an offline, deterministic backtest on the reference equity "
            "strategy (synthetic bars — no network, no API keys)."
        ),
    )
    parser.add_argument(
        "--strategy",
        default="example_equity",
        help="strategy name (built-in or entry-point)",
    )
    parser.add_argument(
        "--start", default=DEFAULT_START, help="start date (YYYY-MM-DD)"
    )
    parser.add_argument("--end", default=DEFAULT_END, help="end date (YYYY-MM-DD)")
    parser.add_argument(
        "--ticker", default=DEFAULT_TICKER, help="ticker symbol (default SPY)"
    )
    parser.add_argument(
        "--out-csv",
        default=None,
        metavar="PATH",
        help="write the equity curve to a CSV",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="surface engine/data logs"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    bt, trades = run_offline_backtest(
        args.start, args.end, args.ticker, args.strategy, verbose=args.verbose
    )
    final = (
        float(bt.equity_curve.iloc[-1])
        if not bt.equity_curve.empty
        else float(bt.initial_capital)
    )
    print(f"strategy:        {args.strategy}")
    print(f"ticker:          {args.ticker}")
    print(f"period:          {args.start} -> {args.end}")
    print(f"entries:         {len(trades)}")
    print(f"final_equity:    {final:,.2f}")
    if args.out_csv:
        bt.equity_curve.to_frame("equity").to_csv(args.out_csv)
        print(f"equity curve:    {args.out_csv}")
    return 0


if __name__ == "__main__":  # pragma: no cover - console entry
    sys.exit(main())
