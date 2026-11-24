"""quant-portfolio — print portfolio metrics from a snapshot or a demo portfolio.

Offline and keyless: loads a persisted portfolio snapshot (a live trader's
`trader_state.json` type payload) or builds a small in-memory demo portfolio,
then prints the same pure account/PnL metrics backtest and live share (spec
`portfolio`).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from quant.portfolio import Fill, Portfolio, Side


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quant-portfolio",
        description=(
            "Print portfolio metrics from a snapshot file, or a small offline "
            "demo portfolio when no snapshot is given."
        ),
    )
    parser.add_argument(
        "--snapshot",
        default=None,
        metavar="PATH",
        help="JSON snapshot to load (e.g. --snapshot trader_state.json)",
    )
    parser.add_argument(
        "--starting-equity", type=float, default=100_000.0, help="demo starting equity"
    )
    parser.add_argument(
        "--prices",
        default="{}",
        metavar="JSON",
        help="price map for mark-to-market, e.g. '{\"AAPL\": 160.0}'",
    )
    return parser


def _demo_portfolio(starting_equity: float) -> Portfolio:
    """A small opened+closed portfolio to print metrics without a snapshot."""
    pf = Portfolio(starting_equity=starting_equity, name="demo")
    pf.apply_fill(
        Fill(
            venue="paper",
            symbol="AAPL",
            side=Side.BUY,
            qty=10,
            price=150.0,
            strategy="mom",
        )
    )
    pf.apply_fill(
        Fill(
            venue="paper",
            symbol="AAPL",
            side=Side.SELL,
            qty=10,
            price=160.0,
            strategy="mom",
        )
    )
    return pf


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.snapshot:
        payload = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
        pf = Portfolio.from_snapshot(payload)
    else:
        pf = _demo_portfolio(args.starting_equity)

    prices = json.loads(args.prices)
    combined = pf.metrics(prices)

    print(f"portfolio:        {pf.name}")
    print(f"starting_equity:  {pf.starting_equity:,.2f}")
    print(f"cash:             {pf.cash:,.2f}")
    print(f"equity:           {combined['equity']:,.2f}")
    print(f"realized_pnl:     {combined['realized_pnl']:,.2f}")
    print(f"unrealized_pnl:   {combined['unrealized_pnl']:,.2f}")
    print(f"total_pnl:        {combined['total_pnl']:,.2f}")
    print(
        f"positions:        {combined['open_positions']} open "
        f"/ {combined['closed_positions']} closed"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - console entry
    sys.exit(main())
