#!/usr/bin/env bash
# STEP 4 — Deploy frontend to S3 + invalidate CloudFront
set -euo pipefail

# Move to the root of the project
cd "$(dirname "$0")/.."

# Load config (Ensure S3_BUCKET, CF_DIST_ID, and EC2_IP are defined here)
if [ -f .deploy_config ]; then
  source .deploy_config
fi

# Variables with fallbacks
S3_BUCKET="${S3_BUCKET:-momentum-watchlist-bucket}"
CF_DIST_ID="${CF_DIST_ID:-}"
AWS_REGION="${AWS_REGION:-ap-south-1}"

# ✅ Validation
if [ -z "$S3_BUCKET" ] || [ -z "$CF_DIST_ID" ]; then
  echo "❌ Missing S3_BUCKET or CF_DIST_ID in .deploy_config"
  exit 1
fi

echo "🚀 Starting Deployment..."

# 1. Build directory setup
BUILD="./build"
rm -rf "$BUILD"
mkdir -p "$BUILD"

# Copy frontend assets (ensure the path matches your project structure)
cp -r ./frontend/* "$BUILD/"

HTML_FILE="$BUILD/index.html"

# ---------------------------------------------------------
# 🔧 THE FIX: Inject Relative Path
# ---------------------------------------------------------
echo "🔧 Setting API to relative path for CloudFront routing..."

# This replaces 'const API = "http://something";' with 'const API = "";'
# This is the "magic" that prevents Mixed Content errors.
# We use a flexible regex to catch single or double quotes.
sed -i "s|const API = [\"'].*[\"'];|const API = \"\";|g" "$HTML_FILE"

# ---------------------------------------------------------
# 2. Upload to S3
# ---------------------------------------------------------
echo "[S3] Uploading assets to $S3_BUCKET..."

# First, upload everything with a default cache
aws s3 sync "$BUILD/" "s3://$S3_BUCKET/" \
  --region "$AWS_REGION" \
  --cache-control "max-age=60" \
  --delete

# Second, set index.html to NO-CACHE so the UI updates instantly for users
aws s3 cp "$BUILD/index.html" "s3://$S3_BUCKET/index.html" \
  --region "$AWS_REGION" \
  --cache-control "no-store, no-cache, must-revalidate"

# ---------------------------------------------------------
# 3. CloudFront Invalidation
# ---------------------------------------------------------
echo "[CloudFront] Clearing cache for $CF_DIST_ID..."
# Note: CloudFront control plane is in us-east-1
INVALIDATION_ID=$(aws cloudfront create-invalidation \
  --distribution-id "$CF_DIST_ID" \
  --paths "/*" \
  --region us-east-1 \
  --query 'Invalidation.Id' \
  --output text)

echo "✅ Deployment Complete! Invalidation ID: $INVALIDATION_ID"
echo "🌐 URL: https://$(aws cloudfront get-distribution --id $CF_DIST_ID --query 'Distribution.DomainName' --output text)"