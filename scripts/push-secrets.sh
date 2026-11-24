#!/usr/bin/env bash
# push-secrets.sh — Read a local .env file and write every key as a
# SecureString to AWS SSM Parameter Store under /trading/{environment}/.
#
# Usage:
#   ./scripts/push-secrets.sh staging [path/to/.env]
#   ./scripts/push-secrets.sh prod    [path/to/.env]
#
# The .env format is standard KEY=VALUE, with # comments and blank lines
# ignored.  Multi-line values (e.g. SSH private keys) are not supported in
# .env files — use --ssh-key for those.
#
# Optional env var:
#   SSH_KEY_FILE  Path to the GitHub deploy key (PEM/ed25519) to push as
#                 github_deploy_key.  e.g.:
#                   SSH_KEY_FILE=~/.ssh/id_ed25519 ./scripts/push-secrets.sh staging
#
# Prerequisites:
#   aws CLI configured with credentials that have ssm:PutParameter + kms:GenerateDataKey
set -euo pipefail

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------
ENVIRONMENT="${1:-}"
ENV_FILE="${2:-quant/.env}"

if [[ -z "$ENVIRONMENT" ]]; then
    echo "Usage: $0 <staging|prod> [path/to/.env]"
    exit 1
fi

if [[ "$ENVIRONMENT" != "staging" && "$ENVIRONMENT" != "prod" ]]; then
    echo "ERROR: environment must be 'staging' or 'prod', got '$ENVIRONMENT'"
    exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: .env file not found: $ENV_FILE"
    exit 1
fi

SSM_PATH="/trading/$ENVIRONMENT"
REGION="${AWS_REGION:-us-east-1}"
PUSHED=0
SKIPPED=0

echo "=== Pushing secrets from $ENV_FILE → $SSM_PATH (region: $REGION) ==="

# ---------------------------------------------------------------------------
# Push each KEY=VALUE from the .env file
# ---------------------------------------------------------------------------
while IFS= read -r line || [[ -n "$line" ]]; do
    # Skip blank lines and comments
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue

    # Must contain exactly one =
    [[ "$line" != *=* ]] && continue

    RAW_KEY="${line%%=*}"
    VALUE="${line#*=}"

    # Strip surrounding whitespace from key
    RAW_KEY="${RAW_KEY// /}"

    # Convert UPPER_CASE → lower_case for the SSM parameter name
    SSM_KEY=$(echo "$RAW_KEY" | tr '[:upper:]' '[:lower:]')

    # Strip wrapping quotes from value (common in .env files)
    VALUE="${VALUE#\"}" ; VALUE="${VALUE%\"}"
    VALUE="${VALUE#\'}" ; VALUE="${VALUE%\'}"

    if [[ -z "$VALUE" ]]; then
        echo "  SKIP  $SSM_PATH/$SSM_KEY  (empty value — set manually if needed)"
        (( SKIPPED++ )) || true
        continue
    fi

    echo "  PUT   $SSM_PATH/$SSM_KEY"
    aws ssm put-parameter \
        --name  "$SSM_PATH/$SSM_KEY" \
        --value "$VALUE" \
        --type  SecureString \
        --overwrite \
        --region "$REGION" \
        --no-cli-pager > /dev/null

    (( PUSHED++ )) || true

done < "$ENV_FILE"

# ---------------------------------------------------------------------------
# Optionally push a GitHub deploy key (SSH private key file)
# ---------------------------------------------------------------------------
if [[ -n "${SSH_KEY_FILE:-}" ]]; then
    SSH_KEY_FILE="${SSH_KEY_FILE/#\~/$HOME}"   # expand ~

    if [[ ! -f "$SSH_KEY_FILE" ]]; then
        echo "ERROR: SSH_KEY_FILE not found: $SSH_KEY_FILE"
        exit 1
    fi

    echo "  PUT   $SSM_PATH/github_deploy_key  (from $SSH_KEY_FILE)"
    aws ssm put-parameter \
        --name  "$SSM_PATH/github_deploy_key" \
        --value "$(cat "$SSH_KEY_FILE")" \
        --type  SecureString \
        --overwrite \
        --region "$REGION" \
        --no-cli-pager > /dev/null

    (( PUSHED++ )) || true
else
    echo "  SKIP  $SSM_PATH/github_deploy_key  (set SSH_KEY_FILE=~/.ssh/id_ed25519 to push)"
fi

echo ""
echo "Done: $PUSHED pushed, $SKIPPED skipped."
echo "Verify with: aws ssm get-parameters-by-path --path $SSM_PATH --with-decryption --region $REGION"
