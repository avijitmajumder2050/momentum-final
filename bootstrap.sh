#!/bin/bash
set -e
LOG_APP=/var/log/momentum-platform.log
LOG_BOOT=/var/log/momentum-platform-bootstrap.log
exec > >(tee -a "$LOG_BOOT") 2>&1

echo "=== Momentum Platform Bootstrap $(date '+%Y-%m-%d %H:%M:%S') ==="

REGION="ap-south-1"
APP_USER="ec2-user"
APP_HOME="/home/ec2-user"
S3_BUCKET="dhan-trading-data"
EIP_ALLOCATION_ID="eipalloc-098e0e7a5bcfe7bfe"
SSM_REPO_PARAM="/momentum-watchlist/github_repo"

sudo timedatectl set-timezone Asia/Kolkata
echo "[1] Timezone: $(timedatectl show -p Timezone --value)"

sudo yum update -y
sudo yum install -y docker git curl awscli
echo "[2] Packages installed"

sudo systemctl enable docker && sudo systemctl start docker
sudo usermod -aG docker "$APP_USER"
echo "[3] Docker ready"

COMPOSE_VER="2.24.5"
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -sSL "https://github.com/docker/compose/releases/download/v${COMPOSE_VER}/docker-compose-linux-x86_64" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
echo "[4] Docker Compose ready"

IMDS_TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
for i in {1..5}; do
  aws ec2 associate-address --instance-id "$INSTANCE_ID" \
    --allocation-id "$EIP_ALLOCATION_ID" --allow-reassociation --region "$REGION" && break
  sleep 5
done
echo "[5] EIP attached"
sleep 10

REPO_URL=$(aws ssm get-parameter --name "$SSM_REPO_PARAM" --region "$REGION" --query "Parameter.Value" --output text)
REPO_NAME=$(basename "$REPO_URL" .git)
APP_DIR="$APP_HOME/$REPO_NAME"
echo "[6] Repo: $REPO_URL"

cd "$APP_HOME"
if [ ! -d "$REPO_NAME" ]; then git clone "$REPO_URL"; else cd "$REPO_NAME" && git pull origin main && cd "$APP_HOME"; fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
cd "$APP_DIR"

cat > "$APP_DIR/runtime.env" << ENVEOF
USE_SSM=true
AWS_REGION=ap-south-1
S3_BUCKET=dhan-trading-data
MAX_LOSS=1000
DEPLOY_PCT=0.30
MONITOR_INTERVAL=15
TRAIL_POLL_SECS=10
BREAKEVEN_PCT=0.01
TRAIL_STEP_PCT=0.002
TIME_EXIT_MINS=10
PORT=5000
ENVEOF
chown "$APP_USER:$APP_USER" "$APP_DIR/runtime.env"
echo "[7] runtime.env written"

sudo docker build -t momentum-platform:latest .
echo "[8] Docker image built"

sudo touch "$LOG_APP"
sudo chown "$APP_USER:$APP_USER" "$LOG_APP"
sudo chmod 664 "$LOG_APP"

sudo tee /usr/local/bin/upload-platform-log.sh > /dev/null << UPLOAD
#!/bin/bash
aws s3 cp $LOG_APP s3://$S3_BUCKET/logs/momentum-platform.log --region $REGION || true
aws s3 cp $LOG_BOOT s3://$S3_BUCKET/logs/momentum-platform-bootstrap.log --region $REGION || true
UPLOAD
sudo chmod +x /usr/local/bin/upload-platform-log.sh

sudo tee /etc/systemd/system/momentum-platform.service > /dev/null << SVCEOF
[Unit]
Description=Momentum Trading Platform (Docker)
After=docker.service network-online.target
Requires=docker.service
[Service]
User=$APP_USER
Group=docker
WorkingDirectory=$APP_DIR
ExecStartPre=/bin/sleep 15
ExecStartPre=-/usr/bin/docker rm -f momentum-api
ExecStart=/usr/bin/docker run --name momentum-api --rm --env-file $APP_DIR/runtime.env -p 5000:5000 momentum-platform:latest
ExecStopPost=/usr/local/bin/upload-platform-log.sh
Restart=always
RestartSec=15
StandardOutput=append:$LOG_APP
StandardError=append:$LOG_APP
[Install]
WantedBy=multi-user.target
SVCEOF

sudo tee /etc/systemd/system/momentum-log-upload.service > /dev/null << EOF
[Unit]
Description=Upload platform logs to S3
[Service]
Type=oneshot
ExecStart=/usr/local/bin/upload-platform-log.sh
EOF

sudo tee /etc/systemd/system/momentum-log-upload.timer > /dev/null << EOF
[Unit]
Description=Upload logs every 5 minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true
[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable momentum-platform
sudo systemctl enable --now momentum-log-upload.timer
sudo systemctl restart momentum-platform

for i in $(seq 1 12); do
  STATUS=$(curl -sf http://localhost:5000/health 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
  [ "$STATUS" = "ok" ] && echo "[9] Health check PASSED" && break
  echo "[9] Attempt $i/12..." && sleep 5
done

/usr/local/bin/upload-platform-log.sh
echo "=== Bootstrap complete $(date '+%Y-%m-%d %H:%M:%S') ==="
