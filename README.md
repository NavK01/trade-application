# Gemini Quant Trading Agent & Fast Trading Platform

A complete production-ready backend and web console powered by the latest **Google GenAI SDK**, supporting:
1. **Gemini Live Multimodal Voice-to-Voice (`gemini-3.8-flash`)**: Real-time bidirectional voice communication with Gemini using low-latency PCM audio streaming over WebSockets.
2. **Autonomous 5-Minute AI Predictor (`gemini-4-argon`)**: Background analyzer that generates BUY/SELL/HOLD trading signals with targets, stop-losses, and auto-execution.
3. **Gemini Chatbot with Built-in Google Search (`app/chatbot/`)**: Conversational trading assistant that integrates real-time Google Search grounding, multi-model selection (`gemini-2.5-flash`, `gemini-2.5-pro`, `gemini-3.8-flash`, etc.), live portfolio/market awareness, and sub-50ms conversational order execution.
4. **Indian Stock Market (NSE / BSE) Live Intelligence**: Real-time live quotes in Indian Rupees (INR / ₹), 52-week ranges, market cap in ₹ Crores, live benchmark indices (NIFTY 50, SENSEX, BANK NIFTY, INDIA VIX), and real-time news search.
5. **Sub-50ms Fast Order Router**: Instant market and limit order execution with live latency tracking, positions manager, and real-time P&L calculations.
6. **Google ADK Financial Advisor Integration (`app/financial-advisor/`)**: Specialized multi-agent system (Data Analyst, Trading Analyst, Execution Analyst, Risk Analyst) adapted specifically for Indian market equities.
7. **Autonomous Code Engineering Agent**: An intelligent quant developer loop running directly on your Linux VM that inspects, edits, executes, and self-heals trading strategies until all code runs without errors.

---

## 🌐 VM Server Domain Information

- **Azure VM Domain:** `crimson.southindia.cloudapp.azure.com`
- **Dashboard URL (via Nginx Port 80):** `http://crimson.southindia.cloudapp.azure.com`
- **Direct FastAPI Port:** `http://crimson.southindia.cloudapp.azure.com:7000`

---

## 📁 Project Structure

```
GeminiAgent/
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI application, REST endpoints & WebSockets
│   ├── config.py               # Environment & workspace configuration
│   ├── chatbot/                # Gemini Chatbot with built-in Google Search grounding
│   │   ├── __init__.py
│   │   ├── chatbot.py          # Core chatbot service & Google search integration
│   │   ├── models.py           # Chatbot Pydantic request & response schemas
│   │   └── prompts.py          # Quant trading & market system prompts
│   ├── live/
│   │   ├── __init__.py
│   │   └── live_session.py     # Gemini Multimodal Live API bridge & audio handler
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── trading_agent.py    # Autonomous task manager & multi-turn loop
│   │   ├── tools.py            # VM tools: execute_bash, read_file, write_file, etc.
│   │   └── prompt.py           # Specialized quant trading & debugging system prompts
│   ├── trading/
│   │   ├── __init__.py
│   │   ├── execution_engine.py # Sub-50ms fast order router & positions manager
│   │   ├── predictor.py        # 5-minute autonomous Gemini 4 Argon predictor
│   │   ├── market_feed.py      # Real-time WebSocket price streamer
│   │   └── chat_agent.py       # Live-aware interactive trading chat agent
│   ├── financial_advisor/      # Google ADK Financial Advisor for Indian Market
│   │   ├── india_market.py     # Live NSE/BSE quotes & indices in INR
│   │   └── advisor_service.py  # Multi-agent financial report coordinator
│   └── static/                 # Front-end trading console & voice interface
│       ├── index.html
│       ├── style.css
│       └── app.js
├── trading_workspace/          # Where your trading strategies and scripts live
│   └── strategies/
│       ├── nifty_intraday.py   # Indian market Nifty 50 intraday strategy
│       └── ema_crossover.py    # Starter EMA crossover quant strategy
├── nginx/
│   └── gemini-agent.conf       # Production Nginx reverse-proxy config
├── systemd/
│   └── gemini-agent.service    # Background service file for 24/7 VM execution
├── setup_vm.sh                 # One-click automated setup script for Azure VM
├── run.sh                      # Quick runner script
├── test_setup.py               # Local verification test script (6/6 checks)
├── .env.example                # Environment variable template
├── .env                        # Local environment file
├── requirements.txt            # Python dependencies
└── README.md
```

---

## 🚀 One-Command Automated Deployment to Azure VM

To deploy and start the application on your Azure VM (`crimson.southindia.cloudapp.azure.com`):

```bash
# Run the automated deployment script (uses CrimsonAgent_key.pem)
./deploy_to_vm.sh
```

The script automatically:
1. Verifies SSH connectivity to `crimson@crimson.southindia.cloudapp.azure.com`.
2. Syncs the entire codebase and `.env` configuration to `/home/crimson/GeminiAgent` on the VM (excluding caches & venv).
3. Installs all system dependencies (`python3-venv`, `nginx`, `git`, `curl`, `redis-server`).
4. Creates the Python virtual environment and installs all dependencies (`requirements.txt`).
5. Configures Nginx with WebSocket reverse-proxying.
6. Installs and starts the `gemini-agent` systemd daemon for 24/7 background operation.
7. Verifies that the server is active on `http://crimson.southindia.cloudapp.azure.com`.

---

## 🧠 Low Token Budget & Multi-Tiered AI Architecture

- **Strict 100K Token/Day Budget Cap:** The system enforces a strict daily token quota of 100,000 tokens. If the quota is approached, non-emergency AI routines gracefully transition to deterministic quantitative models (zero token cost) so your credits are never overrun.
- **5-Minute Scalp Loop:** Powered by **Gemini 3.8 Flash** (~250 tokens per run, ultra-fast low-latency intraday predictions).
- **30-Minute Strategic Cycle:** Powered by **Gemini 4 Argon** (runs every 30 minutes for macro trend, Nifty 50 breadth, and sector rotation analysis).
- **Emergency Decision Engine:** Triggered when an active position drops $\ge 2\%$ or suffers sudden volatility shocks. Uses top-tier Gemini reasoning to evaluate immediate actions (`EMERGENCY_EXIT`, `TIGHTEN_STOP`, or `HOLD_NOISE`).
- **Market Schedule Synchronization:**
  - `09:00 - 09:15 IST`: Pre-market Google Search screening.
  - `09:15 IST`: Automatic market open start.
  - `15:20 IST`: Automated EOD square-off to eliminate overnight risk.
  - `15:30 IST`: Daily performance reporting and SQLite snapshot.

---

## 💻 Local Testing & Development

Before deploying to the server, you can run and test everything locally:

```bash
# Run local verification
venv/bin/python test_setup.py

# Start local server
./run.sh
```
Then open: `http://localhost:7000`

---

## 📡 REST & WebSocket API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Health check & config status |
| `GET` | `/api/models` | List of supported reasoning & live models |
| `POST` | `/api/trading/order` | Execute fast stock order (`symbol`, `action`, `quantity`, `order_type`) |
| `GET` | `/api/trading/portfolio` | Get live account balances, open positions, and P&L in INR |
| `POST` | `/api/trading/close-position`| Instantly close an open position |
| `GET` | `/api/trading/prediction` | Get latest Gemini 4 Argon 5-min prediction & schedule |
| `POST` | `/api/trading/prediction/trigger` | Trigger immediate Argon prediction on demand |
| `POST` | `/api/trading/prediction/auto-toggle` | Enable/disable auto-trade execution |
| `POST` | `/api/chatbot` | Primary Chatbot endpoint with Google Search grounding & model choice |
| `GET` | `/api/chatbot/models` | List available Gemini models for Chatbot with search capabilities |
| `POST` | `/api/chat` | Interactive chat with live portfolio awareness & trade commands |
| `POST` | `/api/financial-advisor/analyze` | Run Indian Market Financial Advisory report on any NSE/BSE stock |
| `GET` | `/api/financial-advisor/quote?symbol=...` | Live quote in INR for Indian stocks (e.g., RELIANCE, TCS, TATAMOTORS) |
| `GET` | `/api/financial-advisor/indices` | Live benchmark levels for Nifty 50, Sensex, Bank Nifty, India VIX |
| `GET` | `/api/financial-advisor/news?query=...` | Live web search for Indian market news and SEBI corporate filings |
| `POST` | `/api/agent/run` | Start autonomous code task (`{"prompt": "...", "model": "..."}`) |
| `WS` | `/ws/market` | Live streaming market ticks and portfolio updates (every 2s) |
| `WS` | `/ws/predictor` | Real-time Argon 5-minute prediction alerts |
| `WS` | `/ws/live` | Bidirectional Gemini Live Audio stream (Gemini 3.8 Flash) |
| `WS` | `/ws/agent/{task_id}` | Live agent thinking & terminal events stream |
