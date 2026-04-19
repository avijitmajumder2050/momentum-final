#!/usr/bin/env bash
# STEP 4 — Deploy frontend to S3 + invalidate CloudFront

set -euo pipefail

cd "$(dirname "$0")/.."

echo "=== Loading config ==="

# Load config
if [ -f .deploy_config ]; then
  source .deploy_config
fi

# 🔴 FIX: removed trailing space
S3_BUCKET="${S3_BUCKET:-momentum-watchlist-bucket}"
CF_DIST_ID="${CF_DIST_ID:-}"
EC2_API_URL="${EC2_API_URL:-}"
AWS_REGION="${AWS_REGION:-ap-south-1}"

# ✅ Validation
if [ -z "$S3_BUCKET" ] || [ -z "$CF_DIST_ID" ] || [ -z "$EC2_API_URL" ]; then
  echo "❌ Missing required variables"
  echo "S3_BUCKET=$S3_BUCKET"
  echo "CF_DIST_ID=$CF_DIST_ID"
  echo "EC2_API_URL=$EC2_API_URL"
  exit 1
fi

echo "Deploying frontend..."
echo "  Bucket   : $S3_BUCKET"
echo "  CF Dist  : $CF_DIST_ID"
echo "  API URL  : $EC2_API_URL"
echo ""

# -----------------------------
# Build
# -----------------------------
BUILD="./build"
rm -rf "$BUILD"
mkdir -p "$BUILD"

cp -r ./frontend/* "$BUILD/"

HTML_FILE="$BUILD/index.html"

if [ ! -f "$HTML_FILE" ]; then
  echo "❌ index.html missing"
  exit 1
fi

# -----------------------------
# Inject API URL (SAFE)
# -----------------------------
echo "🔧 Injecting API URL..."

# Replace placeholder (RECOMMENDED)
sed "s|http://YOUR-EC2-IP:5000|$EC2_API_URL|g" "$HTML_FILE" > "$HTML_FILE.tmp"
mv "$HTML_FILE.tmp" "$HTML_FILE"

# -----------------------------
# Upload to S3
# -----------------------------
echo "[S3] Uploading..."

aws s3 sync "$BUILD/" "s3://$S3_BUCKET/" \
  --region "$AWS_REGION" \
  --cache-control "max-age=60" \
  --delete \
  --exclude "data/*"

echo "✅ Upload complete"

# -----------------------------
# CloudFront Invalidation
# -----------------------------
echo "[CloudFront] Invalidating..."

INVALIDATION_ID=$(aws cloudfront create-invalidation \
  --distribution-id "$CF_DIST_ID" \
  --paths "/*" \
  --region us-east-1 \
  --query "Invalidation.Id" \
  --output text)

echo "Invalidation ID: $INVALIDATION_ID"

# -----------------------------
# Fetch URL
# -----------------------------
CF_URL=$(aws cloudfront get-distribution \
  --id "$CF_DIST_ID" \
  --query "Distribution.DomainName" \
  --output text \
  --region us-east-1)

echo ""
echo "===================================="
echo "🚀 Frontend deployed"
echo "🌐 https://$CF_URL"
echo "===================================="