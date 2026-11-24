#!/usr/bin/env bash
# deploy.sh — Triggered via AWS SSM Run Command by GitHub Actions.
# Pulls the latest code for the target branch, re-syncs deps, and restarts
# the trader service.
#
# Usage (invoked by SSM, not directly):
#   BRANCH=main /opt/trading/scripts/deploy.sh
set -euo pipefail

DEPLOY_DIR="/opt/trading"
TRADER_USER="trader"
BRANCH="${BRANCH:-main}"

echo "=== Deploy started at $(date -u) ==="
echo "Branch : $BRANCH"
echo "Dir    : $DEPLOY_DIR"

# ---------------------------------------------------------------------------
# 1. Pull latest code
# ---------------------------------------------------------------------------
cd "$DEPLOY_DIR"

# Run git operations as the trader user (owns the repo)
sudo -u "$TRADER_USER" git fetch origin
sudo -u "$TRADER_USER" git checkout "$BRANCH"
sudo -u "$TRADER_USER" git reset --hard "origin/$BRANCH"
sudo -u "$TRADER_USER" git submodule update --init --recursive

COMMIT=$(sudo -u "$TRADER_USER" git rev-parse --short HEAD)
echo "Deployed commit: $COMMIT"

# git reset --hard can strip execute bits if they weren't committed; re-apply.
sudo chmod +x "$DEPLOY_DIR/scripts/"*.sh

# Sync service file so systemd always reflects the repo version.
sudo cp "$DEPLOY_DIR/scripts/trader.service" /etc/systemd/system/trader.service
sudo systemctl daemon-reload

# ---------------------------------------------------------------------------
# 2. Sync Python dependencies
# python/pyproject.toml owns the venv; quant path dep synced automatically.
# ---------------------------------------------------------------------------
cd "$DEPLOY_DIR/python"
sudo -u "$TRADER_USER" /usr/local/bin/uv sync

# ---------------------------------------------------------------------------
# 3. Refresh secrets from SSM, then restart the trader service
# ---------------------------------------------------------------------------
echo "Fetching latest secrets from SSM..."
sudo "$DEPLOY_DIR/scripts/fetch-secrets.sh"

echo "Restarting trader service..."
sudo systemctl restart trader.service

# Give the service a moment to start
sleep 5
sudo systemctl is-active --quiet trader.service \
    && echo "Trader service is running." \
    || { echo "ERROR: Trader service failed to start."; sudo systemctl status trader.service --no-pager; exit 1; }

echo "=== Deploy finished at $(date -u) (commit: $COMMIT) ==="
