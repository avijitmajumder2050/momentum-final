#!/usr/bin/env bash
# STEP 2 — Create S3 bucket, IAM role, CloudFront distribution
# Run ONCE from your laptop after filling in variables below
set -euo pipefail

# ─── EDIT THESE ──────────────────────────────────────────────────────────────
BUCKET="your-momentum-watchlist-bucket"
REGION="ap-south-1"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
# ─────────────────────────────────────────────────────────────────────────────

echo "=== [1/4] Creating S3 bucket: ${BUCKET} ==="
aws s3 mb "s3://${BUCKET}" --region "${REGION}" 2>/dev/null || echo "(already exists)"

aws s3api put-public-access-block \
  --bucket "${BUCKET}" \
  --public-access-block-configuration \
    BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false

aws s3api put-bucket-website \
  --bucket "${BUCKET}" \
  --website-configuration '{"IndexDocument":{"Suffix":"index.html"}}'

aws s3api put-bucket-policy \
  --bucket "${BUCKET}" \
  --policy "{
    \"Version\":\"2012-10-17\",
    \"Statement\":[{
      \"Effect\":\"Allow\",\"Principal\":\"*\",
      \"Action\":\"s3:GetObject\",
      \"Resource\":\"arn:aws:s3:::${BUCKET}/*\",
      \"Condition\":{\"StringNotLike\":{\"s3:prefix\":[\"data/*\"]}}
    }]
  }"
echo "S3 bucket ready: http://${BUCKET}.s3-website.${REGION}.amazonaws.com"


echo ""
echo "=== [2/4] Creating IAM role ==="
# Update bucket name in policy
sed "s/REPLACE_BUCKET_NAME/${BUCKET}/g" infra/iam_policy.json > /tmp/iam_policy_final.json

aws iam create-role \
  --role-name MomentumWatchlistRole \
  --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]
  }' 2>/dev/null || echo "(role already exists)"

aws iam put-role-policy \
  --role-name MomentumWatchlistRole \
  --policy-name MomentumWatchlistPolicy \
  --policy-document file:///tmp/iam_policy_final.json

aws iam create-instance-profile \
  --instance-profile-name MomentumWatchlistProfile 2>/dev/null || echo "(profile exists)"

aws iam add-role-to-instance-profile \
  --instance-profile-name MomentumWatchlistProfile \
  --role-name MomentumWatchlistRole 2>/dev/null || echo "(already added)"

echo "IAM role MomentumWatchlistRole created"


echo ""
echo "=== [3/4] Creating CloudFront distribution ==="
CF_ORIGIN="${BUCKET}.s3-website.${REGION}.amazonaws.com"

CF_ID=$(aws cloudfront create-distribution \
  --origin-domain-name "${CF_ORIGIN}" \
  --default-root-object index.html \
  --query "Distribution.Id" \
  --output text \
  --region us-east-1)

CF_DOMAIN=$(aws cloudfront get-distribution \
  --id "${CF_ID}" \
  --query "Distribution.DomainName" \
  --output text \
  --region us-east-1)

echo "CloudFront distribution: ${CF_ID}"
echo "CloudFront URL: https://${CF_DOMAIN}"

# Save for later scripts
echo "CF_DIST_ID=${CF_ID}" > .deploy_config
echo "CF_DOMAIN=${CF_DOMAIN}" >> .deploy_config
echo "S3_BUCKET=${BUCKET}" >> .deploy_config
echo "AWS_REGION=${REGION}" >> .deploy_config

echo ""
echo "=== [4/4] Summary ==="
echo "  S3 Bucket  : ${BUCKET}"
echo "  CF Dist ID : ${CF_ID}"
echo "  CF URL     : https://${CF_DOMAIN}"
echo ""
echo "Config saved to .deploy_config — used by 04_deploy_frontend.sh"
echo ""
echo "Next: launch EC2 instance and run scripts/03_setup_ec2.sh on it"
