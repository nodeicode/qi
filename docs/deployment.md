# Deployment — paper by default, SSM for prod

Qi is designed to run **paper by default and prod only when you opt into it**.
The base install is brokerless and needs no cloud: every development path works
offline with synthetic bars. Live/prod paths add optional extras and
credentials, never inline secrets.

## Tiers

| Tier | How | Needs | Notes |
|---|---|---|---|
| Dev / paper dry-run | `FileConfigSource` + `ConfigSync` loop | none | offline, keyless; polls a local JSON config |
| Backtest (offline) | `Backtester` + `SyntheticDataHandler` | none | synthetic bars, deterministic |
| Portfolio report | `Portfolio.from_snapshot(...)` | none | snapshot or demo metrics |
| Paper/live trader | `UnifiedTrader().run()` | `quant[alpaca]` + Alpaca creds | real venue fills |
| Prod config backend | `QI_CONFIG_BACKEND=ssm` + trader run | `quant[ssm]` (boto3) + AWS | SSM Parameter Store |

## Paper is the default (zero-AWS dev mode)

The development path never touches AWS or Alpaca. A dry-run is the runtime-config
path driving the reference strategy — see `docs/quickstart.md` §3:

- Uses the **file config backend** (`FileConfigSource` + `ConfigSync`), a
  single stat per poll — no boto3, no IMDS.
- Resolves the named strategy (`example_equity`) through the registry.
- Exits cleanly after the configured ticks.

The config payload is a validated dict:

```json
{ "strategies": [ { "name": "example_equity", "clients": ["paper"], "config": {} } ] }
```

A named-but-unknown strategy is refused at load time (spec `runtime-config`),
and the runtime keeps already-running strategies untouched.

## Paper/live with a real venue

To run the trader, install the broker extra, export credentials, and invoke the
`UnifiedTrader` from a script of your own (a cron/systemd unit just runs that
script):

```bash
uv sync --extra alpaca            # or: pip install 'quant[alpaca]'
export APCA_API_KEY_ID=<...>      # paper or live key
export APCA_API_SECRET_KEY=<...>
cat > run_trader.py <<'PY'
from quant.live.trader import UnifiedTrader

UnifiedTrader().run()      # resolves the config backend from the environment
PY
python run_trader.py
```

The trader fails fast with an actionable message if the extra or credentials
are missing (never silently half-starts). Point it at a paper/hardened Alpaca
account for a true paper run; it persists a portfolio snapshot to
`trader_state.json` and recovers it on restart.

## Prod config: SSM Parameter Store (`quant[ssm]`)

The optional prod config backend reads validated config from an SSM Parameter,
so **no config/credentials are committed to the repo**.

```bash
uv sync --extra ssm               # or: pip install 'quant[ssm]'
```

Select the backend explicitly and (optionally) pin the region:

```bash
QI_CONFIG_BACKEND=ssm QI_AWS_REGION=us-east-1 python run_trader.py
```


- **Backend**: `SSMConfigSource` (fetched lazily — importing the SDK never
  imports boto3).
- **Parameter**: `/trading/<env>/strategies_config`, where `<env>` is
  `staging` or `prod`, read from the `<project_root>/.environment` marker
  (defaults to `staging` when absent).
- **Version semantics**: unchanged SSM `Version` short-circuits to a silent
  `None` (no re-parse); an invalid payload is refused once per version and
  prior config stays active; any poll error is silent.
- **Backend selection** is by config, not code: `file` / `env` / `ssm`
  (`QI_CONFIG_BACKEND`, or `QI_CONFIG_JSON` selects `env`; otherwise `file`).

## Environment contract

| Env var | Purpose |
|---|---|
| `QI_CONFIG_BACKEND` | `file`, `env`, or `ssm` (explicit; otherwise auto) |
| `QI_CONFIG_FILE` | local JSON config path for the file backend (default `strategies_config.json`) |
| `QI_CONFIG_JSON` | inline JSON config for the env backend |
| `QI_AWS_REGION` | SSM region override (default: IMDS) |
| `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` | Alpaca credentials (paper or live) |

## Security

- No credentials, `.env`, or `*.pem`/`*.key`/`*.p12` files are tracked.
- Real credentials run in the private ops path only (AWS SSM, fetched at
  runtime, never inline).
- The public repo ships no secrets by construction; report vulnerabilities via
  `SECURITY.md`.

## Prod runbook sanity

1. Base install green: `uv run test` (suite incl. the offline data/backtest/live paths).
2. Paper dry-run green: the `FileConfigSource` + `ConfigSync` loop runs.
3. Paper (real venue) green: `UnifiedTrader().run()` on a paper account.
4. Then enable the SSM backend and point it at a hardened Alpaca account.