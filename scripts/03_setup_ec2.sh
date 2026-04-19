#!/bin/bash
set -e

APP_DIR="/home/ec2-user/momentum-watchlist"
APP_USER="ec2-user"

echo "=== [1/5] Installing Docker (Amazon Linux) ==="

sudo yum update -y

# Install docker if not exists
if ! command -v docker &> /dev/null; then
  sudo amazon-linux-extras install docker -y 2>/dev/null || true
  sudo yum install -y docker
  sudo systemctl enable docker
  sudo systemctl start docker
  sudo usermod -aG docker $APP_USER
fi

echo "Docker: $(docker --version)"


echo ""
echo "=== [2/5] Installing Docker Compose ==="

if ! command -v docker-compose &> /dev/null; then
  sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" \
    -o /usr/local/bin/docker-compose
  sudo chmod +x /usr/local/bin/docker-compose
fi

echo "Docker Compose: $(docker-compose --version)"


echo ""
echo "=== [3/5] Installing AWS CLI (if missing) ==="

if ! command -v aws &> /dev/null; then
  curl -s "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
  unzip -q /tmp/awscliv2.zip -d /tmp/
  sudo /tmp/aws/install --update
  rm -rf /tmp/aws /tmp/awscliv2.zip
fi

echo "AWS CLI: $(aws --version)"


echo ""
echo "=== [4/5] Build Docker image ==="

cd "$APP_DIR"

sudo docker build -t momentum-watchlist:latest .


echo ""
echo "=== [5/5] Start container ==="

# Use docker compose (v2 fallback safe)
sudo docker compose up -d || sudo docker-compose up -d

sleep 5
sudo docker ps


echo ""
echo "=== [6/6] Health check ==="

for i in 1 2 3 4 5; do
  STATUS=$(curl -sf http://localhost:5000/health 2>/dev/null \
    | python3 -c "import sys,json;print(json.load(sys.stdin).get('status','fail'))" \
    2>/dev/null || echo "not ready")

  if [ "$STATUS" = "ok" ]; then
    echo "✅ Health check PASSED"
    break
  fi

  echo "Attempt $i/5 waiting..."
  sleep 4
done


PUBLIC_IP=$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4)

echo ""
echo "🚀 Container running at:"
echo "http://${PUBLIC_IP}:5000"

echo ""
echo "Useful commands:"
echo "  sudo docker compose logs -f"
echo "  sudo docker compose restart"