#!/usr/bin/env bash
# ==============================================================================
# One-Command Remote Deployment Script for Gemini Quant Trading Platform
# Syncs codebase to Azure VM and initiates automated server configuration.
# Target: crimson@crimson.southindia.cloudapp.azure.com
# ==============================================================================

set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$DIR"

VM_HOST="crimson.southindia.cloudapp.azure.com"
VM_USER="crimson"
SSH_KEY="$DIR/CrimsonAgent_key.pem"
REMOTE_DIR="/home/$VM_USER/GeminiAgent"

echo "=========================================================="
echo " 🚀 Deploying Gemini Agent to Azure VM"
echo " Target: $VM_USER@$VM_HOST"
echo " Destination: $REMOTE_DIR"
echo "=========================================================="

# 1. Verify SSH Private Key
if [ ! -f "$SSH_KEY" ]; then
    echo "❌ Error: SSH key '$SSH_KEY' not found!"
    exit 1
fi
chmod 400 "$SSH_KEY"

# 2. Test Connection
echo "[1/4] Verifying SSH connectivity..."
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$VM_USER@$VM_HOST" "echo '✅ SSH Connection Verified! Host: \$(hostname)'"

# 3. Create Remote Directory
echo "[2/4] Preparing remote deployment directory..."
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$VM_USER@$VM_HOST" "mkdir -p $REMOTE_DIR"

# 4. Transfer Codebase
echo "[3/4] Transferring codebase to remote VM (excluding venv & caches)..."
if command -v rsync >/dev/null 2>&1; then
    rsync -avz -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" \
        --exclude='venv/' \
        --exclude='.git/' \
        --exclude='__pycache__/' \
        --exclude='*.pyc' \
        --exclude='CrimsonAgent_key.pem' \
        --exclude='.pytest_cache/' \
        --exclude='*.log' \
        "$DIR/" "$VM_USER@$VM_HOST:$REMOTE_DIR/"
else
    echo "Notice: rsync not found locally, streaming via tar over ssh..."
    tar --exclude='./venv' \
        --exclude='./.git' \
        --exclude='./__pycache__' \
        --exclude='./*.pyc' \
        --exclude='./CrimsonAgent_key.pem' \
        -czf - . | ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$VM_USER@$VM_HOST" "tar -xzf - -C $REMOTE_DIR"
fi

# Ensure .env is present remotely
if [ -f "$DIR/.env" ]; then
    echo "Syncing .env configuration to remote server..."
    scp -i "$SSH_KEY" -o StrictHostKeyChecking=no "$DIR/.env" "$VM_USER@$VM_HOST:$REMOTE_DIR/.env"
fi

# 5. Execute Setup and Service Installation on Remote VM
echo "[4/4] Executing remote setup_vm.sh on VM..."
ssh -i "$SSH_KEY" -t -o StrictHostKeyChecking=no "$VM_USER@$VM_HOST" "cd $REMOTE_DIR && chmod +x setup_vm.sh run.sh && sudo ./setup_vm.sh"

echo ""
echo "=========================================================="
echo " 🎉 DEPLOYMENT COMPLETE & APPLICATION RUNNING ON AZURE VM!"
echo " Access Web Dashboard via Domain:"
echo " 👉 http://$VM_HOST"
echo " 👉 http://$VM_HOST:7000 (direct port)"
echo " Status Check: ssh -i CrimsonAgent_key.pem $VM_USER@$VM_HOST 'sudo systemctl status gemini-agent'"
echo "=========================================================="

