#!/usr/bin/env bash
# Setup script for Oracle Cloud Ubuntu VM (or any Debian-based host)
# Run on a fresh VM as the deploy user.
set -euo pipefail

echo "=== Link Intelligence Platform — Setup ==="

# 1. System packages
echo "[1/6] Installing system packages..."
sudo apt-get update -y
sudo apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip python3-dev \
    build-essential libffi-dev libssl-dev libxml2-dev libxslt1-dev \
    redis-server nginx \
    sqlite3

# 2. Redis (no Docker)
echo "[2/6] Enabling Redis..."
sudo systemctl enable redis-server
sudo systemctl start redis-server
redis-cli ping || { echo "Redis not responding"; exit 1; }

# 3. Python virtualenv
echo "[3/6] Creating Python virtualenv..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 4. Generate keys if missing
echo "[4/6] Generating crypto keys..."
if [ ! -f .env ]; then
    cp .env.example .env
    AES_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
    HMAC_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
    SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")
    sed -i "s|^AES_ENCRYPTION_KEY=.*|AES_ENCRYPTION_KEY=${AES_KEY}|" .env
    sed -i "s|^HMAC_KEY=.*|HMAC_KEY=${HMAC_KEY}|" .env
    sed -i "s|^APP_SECRET_KEY=.*|APP_SECRET_KEY=${SECRET}|" .env
    echo "✅ Keys generated and saved to .env"
    echo "⚠️  Edit .env to add your Telegram + LLM credentials."
else
    echo ".env already exists, skipping."
fi

# 5. Init database
echo "[5/6] Initializing database..."
mkdir -p data logs
python scripts/init_db.py

# 6. Install systemd services
echo "[6/6] Installing systemd services..."
sudo cp deploy/link-intel-web.service /etc/systemd/system/
sudo cp deploy/link-intel-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable link-intel-web link-intel-worker

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env and fill in Telegram + LLM credentials"
echo "  2. Start services:"
echo "     sudo systemctl start link-intel-web link-intel-worker"
echo "  3. Check status:"
echo "     sudo systemctl status link-intel-web link-intel-worker"
echo "  4. Visit: http://localhost:8000"
echo ""
echo "To view logs:"
echo "  journalctl -u link-intel-web -f"
echo "  journalctl -u link-intel-worker -f"
