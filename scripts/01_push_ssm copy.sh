#!/usr/bin/env bash
set -euo pipefail
REGION="ap-south-1"
[ -f backend/.env ] || { echo "ERROR: backend/.env missing"; exit 1; }
source backend/.env
put_sec(){ aws ssm put-parameter --region $REGION --name "$1" --value "$2" --type SecureString --overwrite --description "Momentum/$3" && echo "  ok $1"; }
put_str(){ aws ssm put-parameter --region $REGION --name "$1" --value "$2" --type String       --overwrite --description "Momentum/$3" && echo "  ok $1"; }
put_sec "/momentum-watchlist/apiKey"          "${apiKey:?}"    "apiKey"
put_sec "/momentum-watchlist/totpKey"         "${totpKey:?}"   "totpKey"
put_sec "/momentum-watchlist/userid"          "${userid:?}"    "userid"
put_sec "/momentum-watchlist/pin"             "${pin:?}"       "pin"
put_str "/momentum-watchlist/S3_BUCKET"       "dhan-trading-data" "S3_BUCKET"
put_str "/momentum-watchlist/github_repo"     "${GITHUB_REPO:?}"  "github_repo"
put_str "/momentum-watchlist/trading-capital" "${TRADING_CAPITAL:-100000}" "capital"
put_str "/momentum-watchlist/max-loss"        "${MAX_LOSS:-1000}" "max-loss"
echo "Done."
