#!/usr/bin/env bash
# Quick local runner for Gemini Quant Agent & Voice Assistant

set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$DIR"

if [ ! -d "venv" ]; then
    echo "Virtual environment 'venv' not found. Creating..."
    python3 -m venv venv || {
        python3 -m venv --without-pip venv
        curl -sS https://bootstrap.pypa.io/get-pip.py | venv/bin/python
    }
    venv/bin/pip install --upgrade pip
    venv/bin/pip install -r requirements.txt
fi

echo "=========================================================="
echo " Starting Gemini Agent locally..."
echo " - Local Dashboard: http://localhost:7000"
echo " - Network Access:  http://0.0.0.0:7000"
echo " - Agent Model:     gemini-4-argon"
echo " - Live Voice:      gemini-3.8-flash"
echo "=========================================================="
echo ""

source venv/bin/activate
exec uvicorn app.main:app --host 0.0.0.0 --port 7000 --reload
