#!/usr/bin/env bash
set -euo pipefail

BUCKET="momentum-watchlist-bucket"
REGION="ap-south-1"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo "=== [1/4] Creating S3 bucket: ${BUCKET} ==="
aws s3 mb "s3://${BUCKET}" --region "${REGION}" 2>/dev/null || echo "(already exists)"

echo "=== Configuring public access block ==="
aws s3api put-public-access-block \
  --bucket "${BUCKET}" \
  --public-access-block-configuration \
  BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false

echo "=== Enabling static website hosting ==="
aws s3api put-bucket-website \
  --bucket "${BUCKET}" \
  --website-configuration '{
    "IndexDocument": {
      "Suffix": "index.html"
    }
  }'

echo "=== Creating bucket policy file (local) ==="

POLICY_FILE="./s3_policy.json"

cat > "$POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PublicReadGetObject",
      "Effect": "Allow",
      "Principal": "*",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::${BUCKET}/*"
    }
  ]
}
EOF

echo "=== Applying bucket policy ==="

aws s3api put-bucket-policy \
  --bucket "${BUCKET}" \
  --policy file://s3_policy.json

echo ""
echo "S3 Website URL:"
echo "http://${BUCKET}.s3-website.${REGION}.amazonaws.com"


echo ""
echo "=== [2/4] IAM Role Setup ==="

sed "s/REPLACE_BUCKET_NAME/${BUCKET}/g" infra/iam_policy.json > ./iam_policy_final.json

aws iam create-role \
  --role-name MomentumWatchlistRole \
  --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[
      {
        "Effect":"Allow",
        "Principal":{"Service":"ec2.amazonaws.com"},
        "Action":"sts:AssumeRole"
      }
    ]
  }' 2>/dev/null || echo "(role exists)"

aws iam put-role-policy \
  --role-name MomentumWatchlistRole \
  --policy-name MomentumWatchlistPolicy \
  --policy-document file://iam_policy_final.json

aws iam create-instance-profile \
  --instance-profile-name MomentumWatchlistProfile 2>/dev/null || echo "(profile exists)"

aws iam add-role-to-instance-profile \
  --instance-profile-name MomentumWatchlistProfile \
  --role-name MomentumWatchlistRole 2>/dev/null || echo "(already attached)"

echo "IAM Role ready"


echo ""
echo "=== [3/4] CloudFront Distribution ==="

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

echo "CloudFront ID: ${CF_ID}"
echo "CloudFront URL: https://${CF_DOMAIN}"


echo ""
echo "=== [4/4] Saving config ==="

cat > .deploy_config <<EOF
CF_DIST_ID=${CF_ID}
CF_DOMAIN=${CF_DOMAIN}
S3_BUCKET=${BUCKET}
AWS_REGION=${REGION}
EOF

echo "Deployment completed successfully"

echo ""
echo "CLEANUP TEMP FILES"
rm -f s3_policy.json iam_policy_final.json