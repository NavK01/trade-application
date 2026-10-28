#!/usr/bin/env bash
# ==============================================================================
# Antigravity Autonomous Quant Supervisor Bot Control Script
# Usage: ./scripts/run_autonomous_bot.sh {start|stop|restart|status|logs}
# ==============================================================================

ACTION="${1:-status}"

case "$ACTION" in
    start)
        echo "Starting gemini-supervisor.service..."
        sudo systemctl start gemini-supervisor.service
        sudo systemctl status gemini-supervisor.service --no-pager | head -n 15
        ;;
    stop)
        echo "Stopping gemini-supervisor.service..."
        sudo systemctl stop gemini-supervisor.service
        ;;
    restart)
        echo "Restarting gemini-supervisor.service..."
        sudo systemctl restart gemini-supervisor.service
        sudo systemctl status gemini-supervisor.service --no-pager | head -n 15
        ;;
    status)
        sudo systemctl status gemini-supervisor.service --no-pager
        ;;
    logs)
        journalctl -u gemini-supervisor.service -f
        ;;
    foreground)
        echo "Running Supervisor in foreground mode..."
        cd /home/crimson/GeminiAgent && PYTHONPATH=. venv/bin/python3 app/system/autonomous_quant_supervisor.py
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs|foreground}"
        exit 1
        ;;
esac

