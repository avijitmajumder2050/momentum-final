#!/usr/bin/env bash
set -euo pipefail
EC2_HOST="ec2-user@YOUR-EIP"
SSH_KEY="~/.ssh/your-key.pem"
APP_DIR="/home/ec2-user/momentum-platform"
[[ "${PUSH_SECRETS:-false}" == "true" ]] && ./scripts/01_push_ssm.sh
rsync -avz --progress --exclude "__pycache__/" --exclude "*.pyc" \
  --exclude ".env" --exclude ".git/" --exclude "frontend/" --exclude "build/" \
  -e "ssh -i ${SSH_KEY} -o StrictHostKeyChecking=no" \
  ./ "${EC2_HOST}:${APP_DIR}/"
ssh -i "$SSH_KEY" "$EC2_HOST" \
  "cd ${APP_DIR} && sudo docker build -t momentum-platform:latest . && sudo systemctl restart momentum-platform"
sleep 5
ssh -i "$SSH_KEY" "$EC2_HOST" "curl -sf http://localhost:5000/health | python3 -c \"import sys,json;d=json.load(sys.stdin);print('OK' if d.get('status')=='ok' else 'FAIL')\"" || echo FAIL
echo "Backend updated"
