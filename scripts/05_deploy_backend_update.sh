#!/usr/bin/env bash
# STEP 5 — Push backend code updates to EC2 and rebuild Docker image
# Usage: ./scripts/05_deploy_backend_update.sh
set -euo pipefail

# ─── EDIT THESE ──────────────────────────────────────────────────────────────
EC2_HOST="ec2-YOUR-IP.ap-south-1.compute.amazonaws.com"
EC2_USER="ubuntu"
SSH_KEY="~/.ssh/your-key.pem"
APP_DIR="/home/ubuntu/momentum-watchlist"
# ─────────────────────────────────────────────────────────────────────────────

if [[ "${PUSH_SECRETS:-false}" == "true" ]]; then
  echo "[ssm] Pushing credentials to SSM..."
  python3 backend/ssm_config.py
fi

echo "[rsync] Syncing files..."
rsync -avz --progress \
  --exclude "__pycache__/" \
  --exclude "*.pyc" \
  --exclude ".env" \
  --exclude "frontend/" \
  --exclude ".git/" \
  -e "ssh -i ${SSH_KEY} -o StrictHostKeyChecking=no" \
  ./ "${EC2_USER}@${EC2_HOST}:${APP_DIR}/"

echo "[docker] Rebuilding and restarting..."
ssh -i "${SSH_KEY}" "${EC2_USER}@${EC2_HOST}" \
  "cd ${APP_DIR} && sudo docker build -t momentum-watchlist:latest . && sudo docker compose up -d"

echo "[health] Checking..."
sleep 5
ssh -i "${SSH_KEY}" "${EC2_USER}@${EC2_HOST}" \
  "curl -sf http://localhost:5000/health | python3 -c \"import sys,json;d=json.load(sys.stdin);print('OK' if d.get('status')=='ok' else 'FAIL')\" || echo FAIL"

echo "Done. Container updated."
