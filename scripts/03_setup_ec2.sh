#!/usr/bin/env bash
# STEP 3 — Run ONCE on fresh Ubuntu 22.04 EC2 instance
# scp this project to EC2 first, then run this script
set -euo pipefail

APP_DIR="/home/ubuntu/momentum-watchlist"
SERVICE="momentum-watchlist"

echo "=== [1/5] Installing Docker ==="
sudo apt-get update -y
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update -y
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker ubuntu
echo "Docker installed: $(docker --version)"


echo ""
echo "=== [2/5] Installing AWS CLI ==="
curl -sf "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
unzip -q /tmp/awscliv2.zip -d /tmp/
sudo /tmp/aws/install --update
rm -rf /tmp/awscliv2.zip /tmp/aws
echo "AWS CLI: $(aws --version)"


echo ""
echo "=== [3/5] Building Docker image ==="
cd "${APP_DIR}"
sudo docker build -t momentum-watchlist:latest .
echo "Image built"


echo ""
echo "=== [4/5] Starting container ==="
sudo docker compose up -d
sleep 5
sudo docker compose ps


echo ""
echo "=== [5/5] Health check ==="
for i in 1 2 3 4 5; do
  STATUS=$(curl -sf http://localhost:5000/health 2>/dev/null | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('status','fail'))" 2>/dev/null || echo "not ready")
  if [ "$STATUS" = "ok" ]; then
    echo "Health check PASSED"
    break
  fi
  echo "  Attempt $i/5 — waiting..."
  sleep 4
done

PUBLIC_IP=$(curl -sf http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo "unknown")
echo ""
echo "Container running on http://${PUBLIC_IP}:5000"
echo "  curl http://${PUBLIC_IP}:5000/health"
echo ""
echo "Useful commands:"
echo "  sudo docker compose logs -f api"
echo "  sudo docker compose restart api"
