#!/usr/bin/env bash
# STEP 4 — Deploy frontend to S3 + invalidate CloudFront
# Run from laptop after getting the EC2 public IP
# Usage: EC2_API_URL=http://1.2.3.4:5000 ./scripts/04_deploy_frontend.sh
set -euo pipefail

cd "$(dirname "$0")/.."

# Load saved config or use env vars
if [ -f .deploy_config ]; then
  source .deploy_config
fi

S3_BUCKET="${S3_BUCKET:-your-momentum-watchlist-bucket}"
CF_DIST_ID="${CF_DIST_ID:-EXXXXXXXXXXXXX}"
EC2_API_URL="${EC2_API_URL:-http://YOUR-EC2-IP:5000}"
AWS_REGION="${AWS_REGION:-ap-south-1}"

echo "Deploying frontend..."
echo "  Bucket   : ${S3_BUCKET}"
echo "  CF Dist  : ${CF_DIST_ID}"
echo "  API URL  : ${EC2_API_URL}"
echo ""

BUILD="./build"
rm -rf "${BUILD}" && mkdir -p "${BUILD}"
cp -r ./frontend/. "${BUILD}/"

# Inject EC2 URL into the HTML
sed -i.bak "s|http://YOUR-EC2-IP:5000|${EC2_API_URL}|g" "${BUILD}/index.html"
rm -f "${BUILD}/index.html.bak"

echo "[s3] Uploading..."
aws s3 sync "${BUILD}/" "s3://${S3_BUCKET}/" \
  --region "${AWS_REGION}" \
  --cache-control "max-age=60" \
  --delete \
  --exclude "data/*"

echo "[cloudfront] Invalidating cache..."
aws cloudfront create-invalidation \
  --distribution-id "${CF_DIST_ID}" \
  --paths "/*" \
  --region us-east-1 \
  --query "Invalidation.Status" \
  --output text

CF_URL=$(aws cloudfront get-distribution \
  --id "${CF_DIST_ID}" \
  --query "Distribution.DomainName" \
  --output text \
  --region us-east-1 2>/dev/null || echo "${CF_DOMAIN:-unknown}")

echo ""
echo "Frontend live at: https://${CF_URL}"
