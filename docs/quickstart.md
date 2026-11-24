# Quickstart — Qi in under 5 minutes, no AWS

Goal: clone → install → run a backtest → run a paper dry-run, **without touching
AWS or entering any API keys**. Everything here runs on the reference
`example_equity` strategy against synthetic bars, so it works on a fresh
machine with nothing but Python 3.12 and `uv`.

## Prereqs

- Python 3.12 (the SDK pins exactly this minor version).
- [`uv`](https://docs.astral.sh/uv/) — the project uses one uv-managed workspace.

## 1. Clone and install

```bash
git clone https://github.com/nodeicode/qi.git
cd qi
uv sync
```

`uv sync` installs the single `quant` distribution plus the dev toolchain
(pytest, ruff, ty). The base install is brokerless — alpaca-py/boto3 are
optional extras (`quant[alpaca]`, `quant[ssm]`) and are **not** required for
anything in this guide.

> Reminder: the broker/AWS deps **are** present in this repo's own dev group so
> `uv run test` can run the live/golden suites in CI. A clean `pip install
> quant` elsewhere is truly brokerless — `quant-paper`, `quant-backtest`, and
> `quant-portfolio` still work.

## 2. Backtest the reference strategy

```bash
uv run quant-backtest
```

You should see a one-line summary of a deterministic offline backtest
(`example_equity` on `SPY`, synthetic bars):

```text
strategy:        example_equity
ticker:          SPY
period:          2024-01-02 -> 2024-06-28
entries:         2
final_equity:    123,314.24
```

Export the equity curve to a CSV if you want it:

```bash
uv run quant-backtest --out-csv equity.csv
```

`--help` documents the rest (`--start`, `--end`, `--ticker`, `--verbose`).

## 3. Paper dry-run (zero-AWS dev mode)

```bash
uv run quant-paper --config paper_config.json --iterations 5
```

This starts a local, keyless "paper" runtime: it writes a default
`paper_config.json` if none exists, then polls that file once per tick through
the local `FileConfigSource` backend (no boto3 anywhere), resolves
`example_equity`, and logs what it would do. It exits cleanly after the
configured number of ticks:

```text
[quant-paper] paper runtime starting (backend=file, config=paper_config.json)
[quant-paper] tick 0: config adopted (1 strategy(s))
[quant-paper] tick 0: example_equity ready (paper)
[quant-paper] tick 1: config unchanged
[quant-paper] paper dry-run complete; exiting cleanly
```

Use `--no-sleep` to skip the inter-tick delay (CI/docs friendly).

## 4. Verify the full suite (optional)

```bash
uv run test
```

Runs the whole suite (engine + data + backtest + live goldens) under the same
environment. Expect ~830 tests, all green.

## Next steps

- `docs/extensibility.md` — add your own strategy / data source / venue / executor.
- `docs/architecture.md` — how the one pipeline stays parity-safe.
- `docs/deployment.md` — paper is the default; the SSM-backed prod path.

## Notes

- **No AWS:** no IMDS, no boto3, no credentials — `quant-paper`, `quant-backtest`,
  and `quant-portfolio` are fully offline and keyless.
- **Deterministic:** synthetic bars mean the same invocation yields the same
  result on any machine.