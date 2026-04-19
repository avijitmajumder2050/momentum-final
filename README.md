# Momentum Terminal — Complete Deployment Guide
## Docker on EC2 + S3/CloudFront Frontend + AWS SSM

### Architecture
```
Browser
  └─► CloudFront (HTTPS CDN)
        └─► S3 Bucket (index.html)
              │
              │  API calls (CORS)
              ▼
          EC2 Instance
            └─► Docker Container (Gunicorn + Flask)
                  ├─► AWS SSM Parameter Store  ← apiKey, totpKey, userid, pin
                  ├─► S3 Bucket /data/momentum_watchlist.csv
                  └─► Angel One SmartAPI  ← LTP, orders, GTT, P&L
```

---

## File Structure
```
momentum-final/
├── Dockerfile                       ← Multi-stage, non-root, healthcheck
├── docker-compose.yml               ← Service definition, IAM via instance role
├── .dockerignore
├── backend/
│   ├── app.py                       ← Flask API, all routes
│   ├── angel_broker.py              ← SmartAPI: LTP, MARGIN LIMIT, GTT
│   ├── breakout_monitor.py          ← Background thread auto-buy
│   ├── position_sizing.py           ← balance×30%×mtf, MAX_LOSS cap
│   ├── ssm_config.py                ← SSM push/load, bootstrap()
│   ├── watchlist_s3.py              ← S3 CSV CRUD
│   ├── requirements.txt
│   └── .env.example
├── frontend/
│   └── index.html                   ← Complete SPA, no framework
├── infra/
│   └── iam_policy.json              ← Least-privilege SSM + S3
└── scripts/
    ├── 01_push_ssm.sh               ← Push .env → SSM (laptop, once)
    ├── 02_setup_aws.sh              ← Create S3, IAM, CloudFront (laptop, once)
    ├── 03_setup_ec2.sh              ← Install Docker, build image (EC2, once)
    ├── 04_deploy_frontend.sh        ← Upload HTML to S3, invalidate CF (laptop)
    └── 05_deploy_backend_update.sh  ← Rsync + docker rebuild (laptop, on changes)
```

---

## Step-by-Step Deployment

### Prerequisites (laptop)
```bash
# Install
brew install awscli          # or: pip install awscli
pip install boto3 python-dotenv smartapi-python pyotp

# Configure AWS
aws configure
# Region: ap-south-1
# Format: json
```

---

### Step 1 — Fill credentials and push to SSM
```bash
cd momentum-final/backend
cp .env.example .env
# Edit .env: set apiKey, totpKey, userid, pin, S3_BUCKET, TRADING_CAPITAL

cd ..
./scripts/01_push_ssm.sh

# Verify:
aws ssm get-parameters \
  --names /momentum-watchlist/apiKey /momentum-watchlist/userid \
  --with-decryption --region ap-south-1 \
  --query "Parameters[*].{Name:Name,Type:Type}"
```

---

### Step 2 — Create S3, IAM Role, CloudFront
```bash
# Edit scripts/02_setup_aws.sh: set BUCKET name
./scripts/02_setup_aws.sh

# This creates:
#   S3 bucket          (static hosting + CSV storage)
#   IAM Role           MomentumWatchlistRole (SSM read + S3 data/* read/write)
#   Instance Profile   MomentumWatchlistProfile
#   CloudFront dist    pointing to S3 bucket
#
# Saves CF_DIST_ID and S3_BUCKET to .deploy_config
```

---

### Step 3 — Launch EC2 instance

In AWS Console → EC2 → Launch Instance:
| Setting | Value |
|---------|-------|
| AMI | Ubuntu Server 22.04 LTS |
| Type | t3.small |
| Key pair | your .pem key |
| IAM Profile | MomentumWatchlistProfile |
| Security Group | Port 22 (SSH) + Port 5000 (API) |
| Storage | 20 GB gp3 |

```bash
# Copy project to EC2
scp -i ~/.ssh/your-key.pem -r ./momentum-final \
    ubuntu@<EC2-IP>:~/momentum-watchlist

# SSH in and run setup
ssh -i ~/.ssh/your-key.pem ubuntu@<EC2-IP>
cd ~/momentum-watchlist
bash scripts/03_setup_ec2.sh

# Should end with:
# Health check PASSED
# Container running on http://<IP>:5000
```

---

### Step 4 — Deploy frontend
```bash
# Back on your laptop:
EC2_API_URL=http://<EC2-PUBLIC-IP>:5000 ./scripts/04_deploy_frontend.sh

# Opens: https://<your-cloudfront-domain>
```

---

## Everyday Operations

### Push code changes
```bash
./scripts/05_deploy_backend_update.sh
```

### Rotate Angel One credentials
```bash
# Edit backend/.env with new credentials
PUSH_SECRETS=true ./scripts/05_deploy_backend_update.sh
```

### View live logs
```bash
ssh -i key.pem ubuntu@<EC2-IP>
sudo docker compose logs -f api
```

### Restart container
```bash
sudo docker compose restart api
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health + monitor status |
| GET | `/api/watchlist` | All rows |
| POST | `/api/watchlist` | Add stock (auto-resolves Angel token) |
| DELETE | `/api/watchlist/<i>` | Delete row |
| PATCH | `/api/watchlist/<i>` | Update field |
| GET | `/api/watchlist/refresh` | Fetch LTP + update S3 CSV |
| GET | `/api/auto_buy_status` | Auto-bought symbols |
| POST | `/api/auto_buy_toggle` | `{"enabled": true/false}` |
| POST | `/api/position_sizing` | Calculate qty (balance×30%×mtf) |
| POST | `/api/place_order` | MARGIN LIMIT → GTT on success |
| GET | `/api/pnl` | Today P&L + balance |
| GET | `/api/search_symbol?q=X` | Angel One searchScrip |

---

## Order Flow
```
User clicks BUY (or breakout_monitor detects LTP > entry)
  ↓
GET /api/position_sizing
  balance = broker.get_funds()         ← live from Angel One rmsLimit
  deployable = balance × 30%
  risk = min(MAX_LOSS=1000, deployable × 2%)
  qty  = floor(risk / sl_distance × mtf_leverage)
  ↓
POST /api/place_order
  1. place_margin_limit_order (LIMIT, INTRADAY)
  2. if success → place_gtt_order (target + SL legs)
  3. mark_auto_buy in S3 CSV (Action=AUTO_BUYED, Qty, GTT_Order_ID)
```

---

## Security Notes
- No credentials stored on EC2 — SSM only
- Docker container runs as non-root (appuser)
- S3 `data/` prefix excluded from public bucket policy
- CloudFront HTTPS terminates before reaching EC2
- Tighten CORS after testing: update `origins` in `app.py`
