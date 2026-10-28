#!/usr/bin/env bash
# ==============================================================================
# One-Step Antigravity Pro Login for Azure Server
# Authorizes the CLI using your Antigravity Pro Google Account.
# All operations will consume included Antigravity credits (Zero Cloud Billing).
# ==============================================================================

echo "=================================================================="
echo " 🔑 ANTIGRAVITY PRO PLAN AUTHENTICATION (ZERO CLOUD CREDITS)"
echo "=================================================================="
echo " Connecting to Antigravity using your Pro Plan account..."
echo " Models unlocked: Gemini 3.8 Flash (High/Med/Low), Claude Sonnet 4.6, Opus 4.6"
echo " Press Enter to begin sign-in (or Ctrl+C to abort)..."
read -r

exec /usr/local/bin/agy

