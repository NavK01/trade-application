#!/usr/bin/env bash
# ==============================================================================
# One-Click VM Setup Script for Gemini Quant Agent & Fast Trading Platform
# Target Server: crimson.southindia.cloudapp.azure.com (Ubuntu 24.04 LTS)
# ==============================================================================

set -e

echo "=========================================================="
echo " 🚀 Setting up Gemini Agent on Azure VM..."
echo " Host: crimson.southindia.cloudapp.azure.com"
echo "=========================================================="

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$APP_DIR"

ACTUAL_USER="${SUDO_USER:-$(whoami)}"
ACTUAL_GROUP="$(id -gn "$ACTUAL_USER" 2>/dev/null || echo "$ACTUAL_USER")"
echo "Running setup for user: $ACTUAL_USER (Group: $ACTUAL_GROUP) in $APP_DIR"

# 1. Install System Dependencies
echo "[1/7] Installing required Ubuntu packages..."
sudo apt update -y
sudo apt install -y python3-venv python3-pip python3-dev build-essential nginx curl git redis-server
sudo systemctl enable redis-server || sudo systemctl enable redis || true
sudo systemctl restart redis-server || sudo systemctl restart redis || true

# 2. Setup Python Virtual Environment
echo "[2/7] Configuring Python virtual environment..."
if [ ! -d "venv" ]; then
    python3 -m venv venv || {
        python3 -m venv --without-pip venv
        if [ -f "$APP_DIR/get-pip.py" ]; then
            echo "Using local get-pip.py bootstrap..."
            venv/bin/python "$APP_DIR/get-pip.py"
        else
            echo "Downloading get-pip.py bootstrap from PyPA..."
            curl -sS https://bootstrap.pypa.io/get-pip.py | venv/bin/python
        fi
    }
fi

# 3. Install Python Dependencies
echo "[3/7] Installing and upgrading project requirements to latest versions..."
venv/bin/pip install --upgrade pip setuptools wheel
venv/bin/pip install --upgrade -r requirements.txt

# 4. Check .env configuration
echo "[4/7] Checking .env configuration..."
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "⚠️ Created default .env from .env.example. Make sure to add your GEMINI_API_KEY in .env!"
fi

# Fix ownership so running user owns the virtualenv and app files
if [ "$(whoami)" = "root" ] && [ -n "$SUDO_USER" ]; then
    chown -R "$ACTUAL_USER:$ACTUAL_GROUP" "$APP_DIR"
fi

# 5. Configure Nginx Reverse Proxy
echo "[5/7] Setting up Nginx with WebSocket support..."
sudo cp "$APP_DIR/nginx/gemini-agent.conf" /etc/nginx/sites-available/gemini-agent
sudo ln -sf /etc/nginx/sites-available/gemini-agent /etc/nginx/sites-enabled/gemini-agent
# Remove default nginx site if active
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl restart nginx
sudo systemctl enable nginx

# 6. Configure Systemd Service
echo "[6/7] Installing systemd background service..."
SERVICE_FILE="/etc/systemd/system/gemini-agent.service"
sudo bash -c "cat <<EOF > $SERVICE_FILE
[Unit]
Description=Gemini Quant Agent & Fast Trading Platform
After=network.target

[Service]
Type=simple
User=$ACTUAL_USER
Group=$ACTUAL_GROUP
WorkingDirectory=$APP_DIR
Environment=\"PATH=$APP_DIR/venv/bin:/usr/local/bin:/usr/bin:/bin\"
Environment=\"PYTHONUNBUFFERED=1\"
ExecStart=$APP_DIR/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 7000 --workers 1
Restart=always
RestartSec=5
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF"

sudo systemctl daemon-reload
sudo systemctl enable gemini-agent
sudo pkill -9 -f uvicorn || true
sudo systemctl restart gemini-agent

# 7. Configure Systemd Journal Retention & Size Limits (Never Bloats Memory or Disk)
echo "[7/8] Configuring systemd journal size limits (max 100MB disk, 50MB RAM)..."
sudo mkdir -p /etc/systemd/journald.conf.d
sudo bash -c "cat <<EOF > /etc/systemd/journald.conf.d/gemini-limits.conf
[Journal]
SystemMaxUse=100M
RuntimeMaxUse=50M
MaxRetentionSec=7day
EOF"
sudo systemctl restart systemd-journald || true

# 8. Configure Firewall (UFW) if enabled
echo "[8/8] Checking firewall rules..."
if command -v ufw >/dev/null 2>&1; then
    if sudo ufw status | grep -q "Status: active"; then
        echo "UFW is active, allowing ports 80, 443, and 7000..."
        sudo ufw allow 80/tcp
        sudo ufw allow 443/tcp
        sudo ufw allow 7000/tcp
    fi
fi

echo ""
echo "=========================================================="
echo " ✅ SETUP COMPLETE & RUNNING!"
echo " Status: sudo systemctl status gemini-agent"
echo " Access Dashboard via Domain:"
echo " 👉 http://crimson.southindia.cloudapp.azure.com"
echo " 👉 http://crimson.southindia.cloudapp.azure.com:7000 (direct port)"
echo "=========================================================="
