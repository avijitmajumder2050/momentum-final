#!/bin/bash
set -e

LOGSCANNER=/var/log/trading-app-mtf-mstock.log
LOG=/var/log/trading-app-mtf-mstock-bootstrap.log
exec > >(tee -a $LOG) 2>&1

echo "🚀 Bootstrapping Trading + Momentum EC2"

REGION="ap-south-1"
SSM_REPO_PARAM="/trading-app-mtf/github_repo"
SSM_MOMENTUM_REPO="/momentum-watchlist/github_repo"

APP_USER="ec2-user"
APP_HOME="/home/ec2-user"

RUN_DOCKER_APP=true

# -----------------------------
# System setup
# -----------------------------
sudo yum update -y
sudo timedatectl set-timezone Asia/Kolkata

sudo yum install -y git python3.11 python3.11-pip python3.11-devel awscli docker
sudo systemctl enable docker
sudo systemctl start docker
sudo usermod -aG docker $APP_USER

echo "✅ Installed Python + Docker"

# -----------------------------
# Attach Elastic IP
# -----------------------------
EIP_ALLOCATION_ID="eipalloc-098e0e7a5bcfe7bfe"

IMDS_TOKEN=$(curl -s -X PUT \
  "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")

INSTANCE_ID=$(curl -s \
  -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" \
  http://169.254.169.254/latest/meta-data/instance-id)

aws ec2 associate-address \
  --instance-id $INSTANCE_ID \
  --allocation-id $EIP_ALLOCATION_ID \
  --allow-reassociation \
  --region $REGION || true

echo "✅ Elastic IP ready"

# -----------------------------
# Python aliases
# -----------------------------
BASHRC="$APP_HOME/.bashrc"
grep -q "alias python=python3.11" "$BASHRC" || echo "alias python=python3.11" >> "$BASHRC"
grep -q "alias pip=pip3.11" "$BASHRC" || echo "alias pip=pip3.11" >> "$BASHRC"

cd "$APP_HOME"

# ============================================================
# 🔵 PART 1: TRADING APP
# ============================================================

echo "=== Setup Trading App ==="

REPO_URL=$(aws ssm get-parameter \
  --name "$SSM_REPO_PARAM" \
  --region "$REGION" \
  --query "Parameter.Value" \
  --output text)

REPO_NAME=$(basename "$REPO_URL" .git)

[ ! -d "$REPO_NAME" ] && git clone "$REPO_URL"

cd "$REPO_NAME"

[ ! -d "venv" ] && /usr/bin/python3.11 -m venv venv

source venv/bin/activate
pip install --upgrade pip
[ -f requirements.txt ] && pip install -r requirements.txt

mkdir -p logs outputs

sudo touch $LOGSCANNER
sudo chown $APP_USER:$APP_USER $LOGSCANNER
sudo chmod 664 $LOGSCANNER

# systemd service
sudo tee /etc/systemd/system/trading-app-mtf.service > /dev/null <<EOF
[Unit]
Description=Trading app scanner Service
After=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_HOME/$REPO_NAME
ExecStart=$APP_HOME/$REPO_NAME/venv/bin/python app/main.py
Restart=always
StandardOutput=append:$LOGSCANNER
StandardError=append:$LOGSCANNER

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable trading-app-mtf
sudo systemctl restart trading-app-mtf

echo "✅ Trading app started"

# ============================================================
# 🟢 PART 2: MOMENTUM DOCKER APP
# ============================================================

if [ "$RUN_DOCKER_APP" = true ]; then

  echo "=== Setup Momentum Docker App ==="

  cd "$APP_HOME"

  MOMENTUM_REPO=$(aws ssm get-parameter \
    --name "$SSM_MOMENTUM_REPO" \
    --region "$REGION" \
    --query "Parameter.Value" \
    --output text)

  MOMENTUM_NAME=$(basename "$MOMENTUM_REPO" .git)

  [ ! -d "$MOMENTUM_NAME" ] && git clone "$MOMENTUM_REPO"

  cd "$MOMENTUM_NAME"

  echo "🔨 Building Docker image..."
  sudo docker build -t momentum-watchlist:latest .

  # -----------------------------
  # Install docker-compose if missing
  # -----------------------------
  if ! command -v docker-compose &> /dev/null && ! sudo docker compose version &> /dev/null; then
    echo "⚠️ Installing docker-compose..."
    sudo curl -L https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
      -o /usr/local/bin/docker-compose
    sudo chmod +x /usr/local/bin/docker-compose
  fi

  # -----------------------------
  # Move to docker folder
  # -----------------------------
  cd docker

  # -----------------------------
  # Ensure runtime.env
  # -----------------------------
  if [ ! -f runtime.env ]; then
    echo "⚠️ runtime.env not found → creating default"
    cat <<EOF > runtime.env
PORT=5000
ENV=prod
EOF
  fi

  # -----------------------------
  # Cleanup old container
  # -----------------------------
  echo "🧹 Cleaning old container..."
  sudo docker rm -f momentum-watchlist 2>/dev/null || true

  # -----------------------------
  # Start container
  # -----------------------------
  echo "🚀 Starting container..."

  if sudo docker compose version &> /dev/null; then
    sudo docker compose up -d
  elif command -v docker-compose &> /dev/null; then
    sudo docker-compose up -d
  else
    echo "⚠️ Compose not available → fallback"
    sudo docker run -d \
      --restart unless-stopped \
      -p 5000:5000 \
      --name momentum-watchlist \
      momentum-watchlist:latest
  fi

  sleep 5
  sudo docker ps

  # -----------------------------
  # Health check
  # -----------------------------
  echo "🔍 Health check..."

  for i in {1..6}; do
    STATUS=$(curl -sf http://localhost:5000/health || echo "fail")
    if echo "$STATUS" | grep -q "ok"; then
      echo "✅ Momentum app healthy"
      break
    fi
    echo "Retry $i..."
    sleep 5
  done

  echo "📜 Container logs:"
  sudo docker logs momentum-watchlist --tail 20 || true

fi

# ============================================================
# FINAL OUTPUT
# ============================================================

PUBLIC_IP=$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4)

echo ""
echo "======================================"
echo "🚀 SYSTEM READY"
echo "Trading App  : running (systemd)"
echo "Momentum App : http://${PUBLIC_IP}:5000"
echo "======================================"