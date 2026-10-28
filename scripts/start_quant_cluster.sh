#!/usr/bin/env bash
# ==============================================================================
# Dual-Engine Quant Cluster Launcher for Azure VM (16 GB RAM)
# Manages parallel workflows in a persistent Tmux session:
#   Window 0: Live Server Logs & Health
#   Window 1: Interactive Gemini CLI Terminal
#   Window 2: Autonomous Strategy Optimizer
#   Window 3: System RAM & Resource Monitor (htop)
# ==============================================================================

SESSION="quant-cluster"
APP_DIR="/home/crimson/GeminiAgent"

# Check if tmux is installed
if ! command -v tmux &> /dev/null; then
    echo "Installing tmux..."
    sudo apt-get update && sudo apt-get install -y tmux
fi

# Attach if session already exists
tmux has-session -t "$SESSION" 2>/dev/null
if [ $? -eq 0 ]; then
    echo "Attaching to existing '$SESSION' tmux session..."
    exec tmux attach -t "$SESSION"
fi

echo "Creating new '$SESSION' multi-application cluster..."

# Window 0: Live Trading Service Monitor
tmux new-session -d -s "$SESSION" -n "Live-Server" -c "$APP_DIR"
tmux send-keys -t "$SESSION:Live-Server" "journalctl -u gemini-agent.service -f" C-m

# Window 1: Interactive Gemini CLI
tmux new-window -t "$SESSION" -n "Gemini-CLI" -c "$APP_DIR"
tmux send-keys -t "$SESSION:Gemini-CLI" "./bin/gemini-cli.sh" C-m

# Window 2: Autonomous Strategy Optimizer
tmux new-window -t "$SESSION" -n "Optimizer-Daemon" -c "$APP_DIR"
tmux send-keys -t "$SESSION:Optimizer-Daemon" "PYTHONPATH=. venv/bin/python3 app/trading/continuous_optimizer.py" C-m

# Window 3: System RAM & Resource Monitor
tmux new-window -t "$SESSION" -n "System-Monitor" -c "$APP_DIR"
tmux send-keys -t "$SESSION:System-Monitor" "htop" C-m

# Select Gemini CLI window by default
tmux select-window -t "$SESSION:Gemini-CLI"

echo "Cluster started successfully!"
echo "To attach, run: tmux attach -t $SESSION"
echo "To switch windows: Ctrl+B then window number (0: Server, 1: Gemini CLI, 2: Optimizer, 3: Monitor)"
echo "To detach without killing: Ctrl+B then D"

if [ -t 0 ]; then
    tmux attach -t "$SESSION"
fi

