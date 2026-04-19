#!/usr/bin/env bash
# setup_ec2.sh — Run ONCE on fresh Ubuntu 22.04 EC2
# Installs Docker, pulls image, starts container via docker-compose
set -euo pipefail

APP_DIR="/home/ubuntu/momentum-watchlist"

echo "════════════════════════════════════════"
echo "  Momentum Watchlist — EC2 Docker Setup"
echo "════════════════════════════════════════"

# ── 1. System update ──────────────────────────────────────────────────────
echo "[1/5] System packages..."
sudo apt-get update -y
sudo apt-get install -y curl git ca-certificates gnupg lsb-release unzip

# ── 2. Install Docker ─────────────────────────────────────────────────────
echo "[2/5] Installing Docker..."
if ! command -v docker &> /dev/null; then
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] \
    https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
  sudo apt-get update -y
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  sudo usermod -aG docker ubuntu
  echo "  ✅ Docker installed"
else
  echo "  ✅ Docker already installed"
fi

# Install docker-compose v2 standalone (for compatibility)
if ! command -v docker-compose &> /dev/null; then
  sudo curl -SL "https://github.com/docker/compose/releases/download/v2.24.0/docker-compose-linux-$(uname -m)" \
    -o /usr/local/bin/docker-compose
  sudo chmod +x /usr/local/bin/docker-compose
fi

# ── 3. Install AWS CLI ────────────────────────────────────────────────────
echo "[3/5] Installing AWS CLI..."
if ! command -v aws &> /dev/null; then
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
  unzip -q /tmp/awscliv2.zip -d /tmp
  sudo /tmp/aws/install
  rm -rf /tmp/aws /tmp/awscliv2.zip
fi
echo "  ✅ AWS CLI $(aws --version)"

# ── 4. Setup app directory ────────────────────────────────────────────────
echo "[4/5] Setting up app directory..."
mkdir -p "${APP_DIR}"
chown -R ubuntu:ubuntu "${APP_DIR}"

# ── 5. Start container ────────────────────────────────────────────────────
echo "[5/5] Starting container..."
cd "${APP_DIR}"

if [ -f "docker/docker-compose.yml" ]; then
  docker-compose -f docker/docker-compose.yml up -d --build
  echo ""
  echo "  Waiting for container to be healthy..."
  sleep 10
  docker-compose -f docker/docker-compose.yml ps
else
  echo "  ⚠️  docker-compose.yml not found. Run deploy_backend.sh first."
fi

PUBLIC_IP=$(curl -sf http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo "unknown")
echo ""
echo "✅  Setup complete!"
echo "    Health: curl http://${PUBLIC_IP}:5000/health"
echo ""
echo "⚠️  Security Group: ensure port 5000 is open for API access"
echo "⚠️  IAM Role: ensure MomentumWatchlistRole is attached to this EC2 instance"
