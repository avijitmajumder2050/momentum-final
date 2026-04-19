#!/usr/bin/env bash
# STEP 4 — Deploy frontend to S3 + invalidate CloudFront
set -euo pipefail

cd "$(dirname "$0")/.."

# Load config
if [ -f .deploy_config ]; then
  source .deploy_config
fi

S3_BUCKET="${S3_BUCKET:-momentum-watchlist-bucket}"
CF_DIST_ID="${CF_DIST_ID:-}"
AWS_REGION="${AWS_REGION:-ap-south-1}"

# ✅ Validation
if [ -z "$S3_BUCKET" ] || [ -z "$CF_DIST_ID" ]; then
  echo "❌ Missing S3_BUCKET or CF_DIST_ID in .deploy_config"
  exit 1
fi

echo "🚀 Starting Deployment..."

# Build directory setup
BUILD="./build"
rm -rf "$BUILD"
mkdir -p "$BUILD"
cp -r ./frontend/* "$BUILD/"

HTML_FILE="$BUILD/index.html"

# ---------------------------------------------------------
# 🔧 THE FIX: Inject Relative Path (HTTPS Safe)
# ---------------------------------------------------------
echo "🔧 Stripping insecure IP and setting relative API path..."

# This regex finds 'const API = "any-url-here";' and changes it to 'const API = "";'
# This forces the browser to call https://yourdomain.com/api/...
sed -i 's|const API = ".*";|const API = "";|g' "$HTML_FILE"

# ---------------------------------------------------------
# Upload to S3
# ---------------------------------------------------------
echo "[S3] Uploading to $S3_BUCKET..."
aws s3 sync "$BUILD/" "s3://$S3_BUCKET/" \
  --region "$AWS_REGION" \
  --cache-control "max-age=60" \
  --delete

# ---------------------------------------------------------
# CloudFront Invalidation
# ---------------------------------------------------------
echo "[CloudFront] Clearing cache for $CF_DIST_ID..."
aws cloudfront create-invalidation \
  --distribution-id "$CF_DIST_ID" \
  --paths "/*" \
  --region us-east-1 > /dev/null

echo "✅ Deployment Complete!"