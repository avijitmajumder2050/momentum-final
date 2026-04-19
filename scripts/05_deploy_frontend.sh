#!/usr/bin/env bash
set -euo pipefail
[ -f .deploy_config ] && source .deploy_config
S3_BUCKET="${S3_BUCKET:-dhan-trading-data}"
CF_DIST_ID="${CF_DIST_ID:-EXXXXXXXXXXXXX}"
EC2_API_URL="${EC2_API_URL:-http://YOUR-EIP:5000}"
BUILD="./build"; rm -rf "$BUILD" && mkdir -p "$BUILD"
cp -r ./frontend/. "$BUILD/"
sed -i.bak "s|http://YOUR-EC2-IP:5000|${EC2_API_URL}|g" "$BUILD/index.html"
rm -f "$BUILD/index.html.bak"
aws s3 sync "$BUILD/" "s3://${S3_BUCKET}/frontend/" \
  --cache-control "max-age=60" --delete
aws cloudfront create-invalidation --distribution-id "$CF_DIST_ID" \
  --paths "/*" --region us-east-1 --output text
echo "Frontend deployed"
