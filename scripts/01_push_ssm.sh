#!/usr/bin/env bash
set -euo pipefail

REGION="ap-south-1"
ENV_FILE="backend/.env"

if [ ! -f "$ENV_FILE" ]; then
  echo "❌ backend/.env not found"
  exit 1
fi

echo "=== Loading .env ==="

# ✅ SAFE LOADER (FIXED)
set -a
source "$ENV_FILE"
set +a

echo "=== DEBUG VARIABLES ==="
echo "apiKey=$apiKey"
echo "totpKey=$totpKey"
echo "userid=$userid"
echo "pin=$pin"
echo "TRADING_CAPITAL=$TRADING_CAPITAL"
echo "MAX_LOSS=$MAX_LOSS"
echo "GITHUB_REPO=$GITHUB_REPO"

echo "=== PUSHING SSM ==="

put_secure() {
  aws ssm put-parameter \
    --region "$REGION" \
    --name "$1" \
    --value "$2" \
    --type "SecureString" \
    --overwrite >/dev/null

  echo "✔ Secure: $1"
}

put_string() {
  aws ssm put-parameter \
    --region "$REGION" \
    --name "$1" \
    --value "$2" \
    --type "String" \
    --overwrite >/dev/null

  echo "✔ String: $1"
}

put_secure "/momentum-watchlist/apiKey" "$apiKey"
put_secure "/momentum-watchlist/totpKey" "$totpKey"
put_secure "/momentum-watchlist/userid" "$userid"
put_secure "/momentum-watchlist/pin" "$pin"

put_string "/momentum-watchlist/S3_BUCKET" "momentum-watchlist-bucket"
put_string "/momentum-watchlist/trading-capital" "${TRADING_CAPITAL:-100000}"
put_string "/momentum-watchlist/max-loss" "${MAX_LOSS:-1000}"
put_string "/momentum-watchlist/github_repo" "$GITHUB_REPO"

echo "=== DONE ==="