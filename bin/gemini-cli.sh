#!/usr/bin/env bash
# ==============================================================================
# Gemini CLI Wrapper for Antigravity Quant Platform
# Automatically loads Vertex AI configuration, API keys, and workspace paths.
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# Load environment variables if .env exists
if [ -f "$ROOT_DIR/.env" ]; then
    set -a
    source "$ROOT_DIR/.env"
    set +a
fi

# Ensure Vertex AI and project variables are populated
export GOOGLE_GENAI_USE_VERTEXAI="${VERTEX_AI:-true}"
export GOOGLE_CLOUD_PROJECT="${GOOGLE_CLOUD_PROJECT:-super-512620}"
export GOOGLE_CLOUD_LOCATION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
export GEMINI_API_KEY="${GEMINI_API_KEY}"
export GOOGLE_API_KEY="${GEMINI_API_KEY}"

# Default model if not specified
DEFAULT_MODEL="${GEMINI_AGENT_MODEL:-gemini-3.8-flash}"

# Check if Gemini CLI is installed
if ! command -v gemini &> /dev/null; then
    echo "⚠️  Gemini CLI not found in PATH. Please run: sudo npm install -g @google/gemini-cli"
    exit 1
fi

# If user provided arguments, pass them directly; otherwise start interactive mode with default model
if [ $# -gt 0 ]; then
    exec gemini "$@"
else
    echo "=================================================================="
    echo " 🚀 GEMINI QUANT CLI: INTERACTIVE TRADING DEVELOPMENT TERMINAL"
    echo "=================================================================="
    echo " Project:   $GOOGLE_CLOUD_PROJECT"
    echo " Location:  $GOOGLE_CLOUD_LOCATION"
    echo " Model:     $DEFAULT_MODEL"
    echo " Workspace: $ROOT_DIR"
    echo "------------------------------------------------------------------"
    echo " Type '/help' for slash commands or ask Gemini to write & test code."
    echo " To exit: type '/exit' or press Ctrl+D Ctrl+D."
    echo "=================================================================="
    exec gemini --model "$DEFAULT_MODEL"
fi

