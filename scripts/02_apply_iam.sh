#!/usr/bin/env bash
set -euo pipefail
ROLE="EC2-AI-Agent-Role"
aws iam get-role --role-name "$ROLE" --query "Role.RoleName" --output text >/dev/null
aws iam put-role-policy --role-name "$ROLE" \
  --policy-name "MomentumPlatformPolicy" \
  --policy-document file://infra/iam_policy.json
echo "Policy applied to $ROLE"
aws iam list-role-policies --role-name "$ROLE" --query "PolicyNames" --output table
