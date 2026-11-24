#!/usr/bin/env bash
# bootstrap.sh — EC2 user-data script (runs ONCE at instance creation as root)
# Installs system deps, uv, clones the repo, installs the quant package, and
# registers the systemd trader service.
#
# Expected instance metadata tag: Environment=staging|prod
# Called from OpenTofu via aws_instance.user_data.
set -euo pipefail
trap 'echo "BOOTSTRAP FAILED at line $LINENO — exit code $?" >&2' ERR

# ---------------------------------------------------------------------------
# Variables injected by OpenTofu templatefile()
# ---------------------------------------------------------------------------
ENVIRONMENT="${environment}"       # staging | prod
REPO_URL="${repo_url}"             # e.g. git@github.com:nodeicode/quant-engine.git
BRANCH="${branch}"                 # main | staging
DEPLOY_DIR="/opt/trading"
TRADER_USER="trader"

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------
dnf update -y
dnf install -y \
    git \
    gcc \
    gcc-c++ \
    make \
    python3.12 \
    python3.12-devel \
    jq \
    unzip

# AWS CLI v2 (pre-installed on Amazon Linux 2023, but ensure it's fresh)
which aws || {
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
    unzip -q /tmp/awscliv2.zip -d /tmp && /tmp/aws/install && rm -rf /tmp/aws /tmp/awscliv2.zip
}

# TA-Lib C library (not in AL2023 repos — build from source)
curl -fsSL https://sourceforge.net/projects/ta-lib/files/ta-lib/0.4.0/ta-lib-0.4.0-src.tar.gz/download \
    -o /tmp/ta-lib-0.4.0-src.tar.gz
tar -xzf /tmp/ta-lib-0.4.0-src.tar.gz -C /tmp
cd /tmp/ta-lib && ./configure --prefix=/usr && make && make install
cd /
ldconfig
rm -rf /tmp/ta-lib /tmp/ta-lib-0.4.0-src.tar.gz

# ---------------------------------------------------------------------------
# 2. Install uv (fast Python package manager)
# ---------------------------------------------------------------------------
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# ---------------------------------------------------------------------------
# 3. Deploy user
# ---------------------------------------------------------------------------
useradd -r -m -d /home/$TRADER_USER -s /bin/bash $TRADER_USER

# ---------------------------------------------------------------------------
# 4. Install credential helper BEFORE cloning so submodules can authenticate.
# ---------------------------------------------------------------------------
cat > /usr/local/bin/git-credential-ssm <<'HELPER'
#!/usr/bin/env bash
ENV=$(cat /opt/trading/.environment 2>/dev/null || echo "staging")
PAT=$(aws ssm get-parameter \
    --name "/trading/$ENV/github_pat" \
    --with-decryption \
    --query "Parameter.Value" \
    --output text 2>/dev/null)
echo "username=x-token"
echo "password=$PAT"
HELPER
chmod +x /usr/local/bin/git-credential-ssm

# Write .environment before configuring the helper so it can read the env.
mkdir -p $DEPLOY_DIR
echo "$ENVIRONMENT" > $DEPLOY_DIR/.environment

git config --system credential.helper ssm

# ---------------------------------------------------------------------------
# 5. Clone the repository (credential helper handles auth for repo + submodules)
# ---------------------------------------------------------------------------
git clone --branch "$BRANCH" --recurse-submodules \
    "$REPO_URL" \
    "$DEPLOY_DIR/repo"
# Move contents into DEPLOY_DIR (git won't clone into non-empty dir)
shopt -s dotglob
mv "$DEPLOY_DIR/repo"/* "$DEPLOY_DIR/repo"/.* "$DEPLOY_DIR/" 2>/dev/null || true
rmdir "$DEPLOY_DIR/repo"
shopt -u dotglob

chown -R $TRADER_USER:$TRADER_USER $DEPLOY_DIR

# ---------------------------------------------------------------------------
# 6. Install Python deps with uv
# python/pyproject.toml is the project root; quant is an editable path dep
# defined via [tool.uv.sources] — no separate install step needed.
# ---------------------------------------------------------------------------
cd $DEPLOY_DIR/python
sudo -u $TRADER_USER /usr/local/bin/uv sync --python python3.12

# ---------------------------------------------------------------------------
# 7. Make deploy scripts executable
# ---------------------------------------------------------------------------
chmod +x $DEPLOY_DIR/scripts/fetch-secrets.sh
chmod +x $DEPLOY_DIR/scripts/deploy.sh

# ---------------------------------------------------------------------------
# 8. Install and configure the CloudWatch agent
# Streams trader.log + monitor_metrics.jsonl to /trading/{env}/{trader|monitor}
# ---------------------------------------------------------------------------
dnf install -y amazon-cloudwatch-agent

# Ensure log files exist so the agent does not warn on missing paths
touch /opt/trading/trader.log /opt/trading/monitor_metrics.jsonl
chown $TRADER_USER:$TRADER_USER /opt/trading/trader.log /opt/trading/monitor_metrics.jsonl

mkdir -p /opt/aws/amazon-cloudwatch-agent/etc
cat > /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json << EOF
{
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "/opt/trading/trader.log",
            "log_group_name": "/trading/$ENVIRONMENT/trader",
            "log_stream_name": "{instance_id}",
            "timezone": "UTC"
          },
          {
            "file_path": "/opt/trading/monitor_metrics.jsonl",
            "log_group_name": "/trading/$ENVIRONMENT/monitor",
            "log_stream_name": "{instance_id}",
            "timezone": "UTC"
          }
        ]
      }
    },
    "log_stream_name": "{instance_id}"
  }
}
EOF

/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
    -a fetch-config -m ec2 \
    -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json -s

systemctl enable amazon-cloudwatch-agent

# ---------------------------------------------------------------------------
# 9. Fetch secrets from SSM into .env (one-time; deploy.sh can refresh later)
# ---------------------------------------------------------------------------
sudo $DEPLOY_DIR/scripts/fetch-secrets.sh

# ---------------------------------------------------------------------------
# 10. Register and enable the systemd service
# ---------------------------------------------------------------------------
cp $DEPLOY_DIR/scripts/trader.service /etc/systemd/system/trader.service
systemctl daemon-reload
systemctl enable trader.service

echo "Bootstrap complete — trader service enabled for environment: $ENVIRONMENT"
echo "Instance will start the trader automatically on next boot."
