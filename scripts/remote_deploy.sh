#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." >/dev/null 2>&1 && pwd)"
cd "$DIR"

VM_HOST="crimson.southindia.cloudapp.azure.com"
VM_USER="crimson"
SSH_KEY="$DIR/CrimsonAgent_key.pem"
REMOTE_DIR="/home/$VM_USER/GeminiAgent"

echo "=========================================================="
echo " 🚀 DEPLOYING PRODUCTION QUANT TERMINAL TO AZURE VM"
echo " Host: $VM_USER@$VM_HOST"
echo " Target Directory: $REMOTE_DIR"
echo "=========================================================="

if [ ! -f "$SSH_KEY" ]; then
    echo "❌ Error: SSH key '$SSH_KEY' not found!"
    exit 1
fi
chmod 400 "$SSH_KEY"

# 1. Clean up old services and obsolete tools (agy, opencode, deepseek-code) on the remote server
echo "[1/5] Removing obsolete tools (agy, opencode, deepseek-code) from server..."
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$VM_USER@$VM_HOST" bash -s << 'EOF'
set -e
echo "Stopping gemini-agent service before sync..."
sudo systemctl stop gemini-agent 2>/dev/null || true
sudo pkill -9 -f uvicorn 2>/dev/null || true

echo "Killing legacy opencode and deepseek bridge processes..."
sudo pkill -9 -f opencode 2>/dev/null || true
sudo pkill -9 -f "bridge.server" 2>/dev/null || true
sudo pkill -9 -f "DeepSeekCode" 2>/dev/null || true
sudo pkill -9 -f "autonomous_quant_supervisor" 2>/dev/null || true

echo "Removing legacy systemd units (supervisor, optimizer)..."
sudo systemctl stop gemini-supervisor 2>/dev/null || true
sudo systemctl disable gemini-supervisor 2>/dev/null || true
sudo rm -f /etc/systemd/system/gemini-supervisor.service

sudo systemctl stop gemini-optimizer 2>/dev/null || true
sudo systemctl disable gemini-optimizer 2>/dev/null || true
sudo rm -f /etc/systemd/system/gemini-optimizer.service

echo "Purging agy (~/.gemini), opencode, and DeepSeekCode files..."
rm -rf ~/.gemini
rm -rf ~/.opencode ~/.config/opencode ~/.local/share/opencode ~/.local/state/opencode ~/.cache/opencode
rm -rf ~/DeepSeekCode ~/.deepseek_code

sudo systemctl daemon-reload
sudo systemctl reset-failed
echo "✅ Server cleanup complete. Only the trading application environment remains."
EOF

# 2. Sync codebase
echo "[2/5] Syncing updated institutional trading terminal to $REMOTE_DIR..."
rsync -avz -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" \
    --exclude='venv/' \
    --exclude='.git/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='CrimsonAgent_key.pem' \
    --exclude='.pytest_cache/' \
    --exclude='*.log' \
    --exclude='*.db' \
    --exclude='*.db-shm' \
    --exclude='*.db-wal' \
    "$DIR/" "$VM_USER@$VM_HOST:$REMOTE_DIR/"

# Ensure .env is copied with AUTO_EXECUTE_DEFAULT=true
echo "Syncing production .env configuration..."
scp -i "$SSH_KEY" -o StrictHostKeyChecking=no "$DIR/.env" "$VM_USER@$VM_HOST:$REMOTE_DIR/.env"

# 3. Configure systemd service for automatic execution and restart
echo "[3/5] Configuring systemd service for 100% automated execution..."
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$VM_USER@$VM_HOST" bash -s << 'EOF'
set -e
SERVICE_FILE="/etc/systemd/system/gemini-agent.service"
sudo bash -c "cat << 'UNIT' > $SERVICE_FILE
[Unit]
Description=Gemini Quant Agent & Automated Fast Trading Platform
After=network.target

[Service]
Type=simple
User=crimson
Group=crimson
WorkingDirectory=/home/crimson/GeminiAgent
Environment=\"PATH=/home/crimson/GeminiAgent/venv/bin:/usr/local/bin:/usr/bin:/bin\"
Environment=\"PYTHONUNBUFFERED=1\"
ExecStart=/home/crimson/GeminiAgent/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 7000 --workers 1
Restart=always
RestartSec=5
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
UNIT"

sudo systemctl daemon-reload
sudo systemctl enable gemini-agent
sudo systemctl restart gemini-agent
echo "✅ gemini-agent.service restarted and enabled for automatic boot start."
EOF

# 4. Verify Nginx reverse proxy
echo "[4/5] Checking Nginx reverse proxy..."
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$VM_USER@$VM_HOST" bash -s << 'EOF'
set -e
sudo cp /home/crimson/GeminiAgent/nginx/gemini-agent.conf /etc/nginx/sites-available/gemini-agent
sudo ln -sf /etc/nginx/sites-available/gemini-agent /etc/nginx/sites-enabled/gemini-agent
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl restart nginx
sudo systemctl enable nginx
echo "✅ Nginx reverse proxy operational."
EOF

# 5. Health Check & Final Verification
echo "[5/5] Verifying application endpoints on remote server..."
sleep 3
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$VM_USER@$VM_HOST" bash -s << 'EOF'
set -e
echo "Checking service status..."
sudo systemctl is-active gemini-agent

echo "Testing Local API Health (Port 7000)..."
curl -s http://127.0.0.1:7000/api/health | grep -q "ok" && echo "✅ Local API Port 7000 Healthy"

echo "Testing Nginx Proxy (Port 80)..."
curl -s http://127.0.0.1/api/health | grep -q "ok" && echo "✅ Nginx Proxy Port 80 Healthy"

echo "Testing Institutional Engine Endpoint..."
curl -s http://127.0.0.1:7000/api/trading/institutional/overview | head -c 120
echo ""

echo "Verifying running processes (NO opencode, NO deepseek bridge, NO agy):"
ps aux | grep -E 'python|uvicorn|opencode|bridge' | grep -v 'grep'
EOF

echo ""
echo "=========================================================="
echo " 🎉 DEPLOYMENT SUCCESSFUL!"
echo " Application is running automatically in the background."
echo " Domain: http://$VM_HOST"
echo " Direct Port: http://$VM_HOST:7000"
echo "=========================================================="

