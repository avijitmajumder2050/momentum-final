#!/usr/bin/env bash
set -euo pipefail
REGION="ap-south-1"; LT_ID="lt-05ad2bdcbfdd1ac91"
INSTANCE_ID=$(aws ec2 run-instances --region "$REGION" \
  --launch-template "LaunchTemplateId=${LT_ID},Version=\$Default" \
  --iam-instance-profile Name=EC2-AI-Agent-Role \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=momentum-platform}]' \
  --query "Instances[0].InstanceId" --output text)
echo "Instance: $INSTANCE_ID"
aws ec2 wait instance-running --instance-ids "$INSTANCE_ID" --region "$REGION"
IP=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" --region "$REGION" \
  --query "Reservations[0].Instances[0].PublicIpAddress" --output text)
echo "IP: $IP"
echo "Monitor: watch -n30 'aws s3 cp s3://dhan-trading-data/logs/momentum-platform-bootstrap.log - | tail -20'"
