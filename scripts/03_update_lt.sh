#!/usr/bin/env bash
set -euo pipefail
REGION="ap-south-1"; LT_ID="lt-05ad2bdcbfdd1ac91"
UD=$(base64 -w 0 bootstrap.sh)
VER=$(aws ec2 create-launch-template-version --region "$REGION" \
  --launch-template-id "$LT_ID" \
  --version-description "Momentum Platform $(date +%Y%m%d-%H%M)" \
  --launch-template-data "{\"UserData\":\"${UD}\"}" \
  --query "LaunchTemplateVersion.VersionNumber" --output text)
aws ec2 modify-launch-template --region "$REGION" \
  --launch-template-id "$LT_ID" --default-version "$VER"
echo "New default version: $VER"
