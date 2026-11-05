import asyncio
import contextlib
import datetime
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any
import hmac
import pandas as pd
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Response, Request, Depends, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app.config import ACCOUNT_IDS, DEFAULT_ACCOUNT_ID, settings, BASE_DIR
from app.agent.trading_agent import agent_manager
from app.agent.tools import read_file, write_file, list_files
from app.financial_advisor.advisor_service import run_financial_advisor_pipeline
from app.financial_advisor.india_market import (
    get_indian_stock_quote,
    get_indian_market_indices,
    search_indian_market_news,
)
from app.trading.execution_engine import execution_engine
from app.trading.predictor import argon_predictor
from app.trading.market_feed import market_feed
from app.trading.strategy_manager import strategy_manager
from app.trading.scanner import scanner
from app.trading.candles import get_symbol_candles
from app.trading.institutional_engine import (
    calculate_indian_statutory_charges,
    compute_premarket_4lines,
    compute_volume_profile,
    compute_fibonacci_zones,
    compute_option_chain_gex,
    detect_liquidity_sweeps_and_absorption,
    get_index_heavyweights_matrix,
    get_sector_rotation_heatmap,
    generate_ai_price_attribution,
    evaluate_risk_guardrails,
    generate_institutional_ai_signal,
)
from app.trading.skills.day_trader_adapter import day_trader
from app.trading.sentiment_analyzer import sentiment_engine
from app.trading.institutional_runner import institutional_runner
from app.system.metrics import get_system_metrics
from app.system.tokens import token_tracker
from app.system.logging_manager import (
    setup_application_logging,
    ring_buffer_handler,
    get_logging_statistics,
)
from app.storage.db import db
from app.storage.cache import cache
from app.chatbot import (
    chatbot_service,
    process_chat_message,
    ChatbotRequest,
    ChatbotResponse,
)
from app.auth import (
    LoginRequest,
    LoginResponse,
    create_access_token,
    verify_token,
    require_auth,
    get_token_from_request,
    verify_websocket_auth,
)

# Initialize production logging with strict memory and disk bounds
setup_application_logging(level=logging.INFO)
logger = logging.getLogger("app.main")

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """Start DeepSeek Institutional Runner, autonomous strategy manager, and live market feed."""
    db.prune_historical_records()
    state = db.get_account_state()
    auto_exec = bool(state.get("auto_execute", settings.AUTO_EXECUTE_DEFAULT))
    strategy_manager.auto_execute = auto_exec
    argon_predictor.auto_execute = auto_exec
    institutional_runner.auto_execute = auto_exec

    market_feed.start_feed()
    institutional_runner.start_background_loop()
    strategy_manager.start_background_loop()
    logger.info(f"⚡ Started Market Feed, DeepSeek Institutional Runner (NIFTY/BANKNIFTY/SENSEX), and Strategy Manager. Auto-execute: {auto_exec}")
    yield
    market_feed.stop_feed()
    institutional_runner.stop_background_loop()
    strategy_manager.stop_background_loop()
    logger.info("Stopped background services.")

app = FastAPI(
    title="Opus/DeepSeek Quant Trading Platform",
    description="Indian equity paper trading with Claude Opus 4.6 and DeepSeek 4.1 Flash",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS Middleware (allows web clients, reverse proxies, and local development)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount Static Assets
static_dir = BASE_DIR / "app" / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Request Models
class RunAgentRequest(BaseModel):
    prompt: str
    model: Optional[str] = None

class FinancialAdvisorRequest(BaseModel):
    symbol: str
    risk_attitude: Optional[str] = "moderate"
    investment_period: Optional[str] = "medium-term"
    model: Optional[str] = None

class FastOrderRequest(BaseModel):
    symbol: str
    action: str  # "BUY" or "SELL"
    quantity: int
    order_type: Optional[str] = "MARKET"
    limit_price: Optional[float] = None
    account_id: Optional[str] = DEFAULT_ACCOUNT_ID
    stop_loss: Optional[float] = None
    target_price: Optional[float] = None
    hold_to_close: Optional[bool] = False

class ClosePositionRequest(BaseModel):
    symbol: str
    account_id: Optional[str] = DEFAULT_ACCOUNT_ID

class ChatRequest(BaseModel):
    message: str
    model: Optional[str] = None
    use_search: Optional[bool] = True

class SaveFileRequest(BaseModel):
    path: str
    content: str


class InstitutionalFeeRequest(BaseModel):
    buy_value: float
    sell_value: float
    instrument_type: Optional[str] = "INTRADAY_EQUITY"
    exchange: Optional[str] = "NSE"
    quantity: Optional[float] = 1.0


class InstitutionalSignalRequest(BaseModel):
    symbol: Optional[str] = "NIFTY"
    daily_net_profit_pct: Optional[float] = 0.0
    consecutive_losses: Optional[int] = 0
    current_drawdown_pct: Optional[float] = 0.0

@app.get("/")
async def root():
    """Serve the Web Trading & Voice Dashboard."""
    index_file = static_dir / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {"message": "Opus/DeepSeek quant backend is running."}

@app.get("/api/health")
@app.get("/health")
async def health_check():
    """Health check endpoint for Nginx, VM monitoring, and system diagnostics."""
    metrics = get_system_metrics()
    return {
        "status": "healthy",
        "api_key_configured": bool(
            settings.DEEPSEEK_API_KEY and not settings.DEEPSEEK_API_KEY.startswith("your_")
        ),
        "agent_model": settings.DEEPSEEK_MODEL,
        "chat_model": settings.DEEPSEEK_MODEL,
        "opus_model": settings.OPUS_MODEL,
        "workspace_dir": str(settings.absolute_workspace_dir),
        "predictor_active": argon_predictor.is_running,
        "strategy_manager_active": strategy_manager.is_running,
        "market_feed_active": market_feed.is_running,
        "auto_execute": strategy_manager.auto_execute,
        "trading_mode": getattr(settings, "TRADING_MODE", "paper"),
        "system": metrics,
    }

@app.get("/api/system/status")
async def get_server_status(auth_user: Dict[str, Any] = Depends(require_auth)):
    """Get complete server health, RAM, CPU, cache, and database diagnostics."""
    return get_system_metrics()

@app.get("/api/system/logs")
async def get_system_logs(
    limit: int = 100,
    level: str = "INFO",
    search: Optional[str] = None,
    auth_user: Dict[str, Any] = Depends(require_auth)
):
    """
    Retrieve live application logs from the zero-leak in-memory ring buffer.
    Guaranteed memory-bounded (max 1,000 records).
    """
    safe_limit = min(max(1, limit), 500)
    logs = ring_buffer_handler.get_logs(limit=safe_limit, min_level=level, search=search)
    return {
        "success": True,
        "count": len(logs),
        "limit": safe_limit,
        "min_level": level.upper(),
        "logs": logs,
    }

@app.get("/api/system/logs/stats")
async def get_log_stats(auth_user: Dict[str, Any] = Depends(require_auth)):
    """Inspect disk log files (with max 30MB cap) and RAM buffer utilization."""
    return get_logging_statistics()

@app.post("/api/system/logs/clear")
async def clear_system_logs(auth_user: Dict[str, Any] = Depends(require_auth)):
    """Clear in-memory ring buffer (does not delete rotated disk audit files)."""
    ring_buffer_handler.clear()
    return {"success": True, "message": "In-memory log ring buffer cleared."}

# -------------------------------------------------------------
# AUTHENTICATION & ACCESS CONTROL ENDPOINTS
# -------------------------------------------------------------
@app.post("/api/auth/login", response_model=LoginResponse)
async def login(req: LoginRequest, response: Response):
    """Authenticate administrator with username and password configured in .env."""
    if not settings.AUTH_ENABLED:
        token = create_access_token(settings.ADMIN_USERNAME)
        return LoginResponse(success=True, token=token, username=settings.ADMIN_USERNAME)

    user_match = hmac.compare_digest(req.username.strip(), settings.ADMIN_USERNAME.strip())
    pass_match = hmac.compare_digest(req.password.strip(), settings.ADMIN_PASSWORD.strip())

    if not (user_match and pass_match):
        raise HTTPException(
            status_code=401,
            detail="Invalid administrator credentials. Access denied."
        )

    token = create_access_token(settings.ADMIN_USERNAME)
    response.set_cookie(
        key="auth_token",
        value=token,
        httponly=True,
        max_age=86400 * 7,
        samesite="lax",
    )
    return LoginResponse(
        success=True,
        token=token,
        username=settings.ADMIN_USERNAME,
        expires_in_seconds=86400 * 7,
    )

@app.get("/api/auth/check")
async def check_auth_status(request: Request):
    """Check if current session or token is authenticated."""
    if not settings.AUTH_ENABLED:
        return {"authenticated": True, "auth_enabled": False, "username": settings.ADMIN_USERNAME}
    token = get_token_from_request(request)
    username = verify_token(token)
    return {
        "authenticated": bool(username),
        "auth_enabled": True,
        "username": username or "",
    }

@app.post("/api/auth/logout")
async def logout(response: Response):
    """Log out administrator session."""
    response.delete_cookie(key="auth_token")
    return {"success": True, "message": "Logged out successfully."}

@app.get("/api/models")
async def list_available_models():
    """List the two supported model providers."""
    models = [
        {
            "id": settings.DEEPSEEK_MODEL,
            "name": "DeepSeek 4.1 Flash MaaS",
            "recommended": True,
            "supports_search": False,
        },
        {
            "id": settings.OPUS_MODEL,
            "name": "Claude Opus 4.6 Thinking",
            "recommended": True,
            "supports_search": False,
        },
    ]
    return {
        "chat_models": models,
        "agent_models": models,
        "live_models": [],
        "voices": [],
    }

@app.get("/api/chatbot/models")
async def list_chatbot_models():
    models_info = await list_available_models()
    return {
        "default_model": settings.DEEPSEEK_MODEL,
        "models": models_info["chat_models"],
    }

# -------------------------------------------------------------
# HIGH-SPEED EXECUTION & PORTFOLIO ENDPOINTS
# -------------------------------------------------------------
@app.post("/api/trading/order")
async def place_order(req: FastOrderRequest, auth_user: Dict[str, Any] = Depends(require_auth)):
    """Execute a stock order at sub-50ms latency on designated agent account."""
    res = await execution_engine.fast_execute_order(
        symbol=req.symbol,
        action=req.action,
        quantity=req.quantity,
        order_type=req.order_type or "MARKET",
        limit_price=req.limit_price,
        account_id=req.account_id or DEFAULT_ACCOUNT_ID,
        stop_loss=req.stop_loss,
        target_price=req.target_price,
        hold_to_close=bool(req.hold_to_close),
    )
    return res

@app.get("/api/trading/portfolio")
async def get_portfolio(account_id: str = Query(DEFAULT_ACCOUNT_ID), auth_user: Dict[str, Any] = Depends(require_auth)):
    """Get live portfolio balances, open positions, and arena metrics for designated account."""
    data = execution_engine.get_portfolio_summary(account_id=account_id)
    data["market_status"] = strategy_manager.get_market_status()
    data["arena"] = db.get_arena_comparison()
    return data

@app.get("/api/trading/market-status")
async def get_market_status():
    """Get live NSE/BSE market status, hours, and countdown to next market open."""
    return strategy_manager.get_market_status()

@app.post("/api/trading/close-position")
async def close_position(req: ClosePositionRequest, auth_user: Dict[str, Any] = Depends(require_auth)):
    """Instantly close an open position at current market price."""
    account_id = req.account_id or DEFAULT_ACCOUNT_ID
    res = await execution_engine.close_position(req.symbol, account_id=account_id)
    if not res:
        raise HTTPException(status_code=404, detail=f"No open position found for '{req.symbol}' on account '{account_id}'.")
    return res

@app.get("/api/trading/orders")
async def get_order_history(account_id: Optional[str] = Query(None), auth_user: Dict[str, Any] = Depends(require_auth)):
    """Get history of recent executed orders."""
    orders = db.get_trades(account_id=account_id, limit=50)
    return {"orders": orders}

# -------------------------------------------------------------
# CANDLESTICK CHARTS & TECHNICAL OVERLAYS (TRADINGVIEW)
# -------------------------------------------------------------
@app.get("/api/trading/candles")
async def get_candles_endpoint(
    symbol: str = Query("SBIN"),
    interval: str = Query("5m"),
    period: str = Query("5d"),
):
    """Fetch OHLCV candles, 20 EMA, VWAP, and trade execution markers for TradingView charts."""
    return get_symbol_candles(symbol=symbol, interval=interval, period=period)

# -------------------------------------------------------------
# DYNAMIC MULTI-STOCK MARKET SCANNER (HIGH-RETURN SETUPS)
# -------------------------------------------------------------
@app.get("/api/trading/scanner")
async def get_scanner_results():
    """Get latest scanned breakout & momentum setups across 30+ liquid Indian equities."""
    return scanner.get_cached_results()

@app.post("/api/trading/scanner/run")
async def trigger_scanner_run(limit: int = Query(10), auth_user: Dict[str, Any] = Depends(require_auth)):
    """Run real-time multi-stock scanner on demand."""
    results = await scanner.scan_market(limit=limit)
    return {"count": len(results), "opportunities": results}

# -------------------------------------------------------------
# REAL-TIME NEWS SENTIMENT & SECTOR INTELLIGENCE
# -------------------------------------------------------------
@app.get("/api/trading/sentiment")
async def get_stock_sentiment(symbol: str = Query("SBIN")):
    """Get real-time news sentiment and quantitative catalyst scoring via Google Search."""
    return await sentiment_engine.analyze_stock_sentiment(symbol)

@app.get("/api/trading/sectors/outlook")
async def get_sectors_outlook():
    """Get macro ranking and 5-year empirical CAGR across all 8 major NSE sectors."""
    return {"sectors": sentiment_engine.get_all_sectors_outlook()}

# -------------------------------------------------------------
# TWO-MODEL ARENA: CLAUDE OPUS VS DEEPSEEK
# -------------------------------------------------------------
@app.get("/api/trading/arena")
async def get_dual_arena(auth_user: Dict[str, Any] = Depends(require_auth)):
    return {
        "arena": db.get_arena_comparison(),
        "opus_prediction": argon_predictor.opus_last_prediction,
        "deepseek_prediction": argon_predictor.deepseek_last_prediction,
        "next_run_timestamp": argon_predictor.next_run_timestamp,
        "interval_seconds": argon_predictor.interval_seconds,
        "market_status": strategy_manager.get_market_status(),
    }

@app.post("/api/trading/arena/trigger")
async def trigger_arena_cycle_endpoint(
    symbol: Optional[str] = Query(None),
    auth_user: Dict[str, Any] = Depends(require_auth),
):
    """Trigger a DeepSeek/Opus cycle during the NSE entry window."""
    return await argon_predictor.run_arena_cycle(target_symbol=symbol)

# -------------------------------------------------------------
# 5-MINUTE ARGON AI PREDICTOR ENDPOINTS
# -------------------------------------------------------------
@app.get("/api/trading/prediction")
async def get_latest_prediction(auth_user: Dict[str, Any] = Depends(require_auth)):
    return {
        "latest_prediction": argon_predictor.last_prediction,
        "opus_prediction": argon_predictor.opus_last_prediction,
        "deepseek_prediction": argon_predictor.deepseek_last_prediction,
        "next_run_timestamp": argon_predictor.next_run_timestamp,
        "auto_execute": argon_predictor.auto_execute,
        "interval_seconds": argon_predictor.interval_seconds,
        "history": argon_predictor.prediction_history[:10],
        "market_status": strategy_manager.get_market_status(),
    }

@app.post("/api/trading/prediction/trigger")
async def trigger_prediction_now(
    symbol: Optional[str] = Query(None),
    auth_user: Dict[str, Any] = Depends(require_auth),
):
    """Trigger an immediate AI prediction without waiting for the 5-min timer."""
    pred = await argon_predictor.generate_prediction(target_symbol=symbol)
    return pred

@app.post("/api/trading/prediction/auto-toggle")
@app.post("/api/trading/auto-toggle")
async def toggle_auto_execution(
    enabled: bool = Query(...),
    account_id: Optional[str] = Query(None),
    auth_user: Dict[str, Any] = Depends(require_auth),
):
    """Enable or disable automatic execution for institutional trading."""
    strategy_manager.set_auto_execute(enabled, account_id=account_id)
    argon_predictor.auto_execute = enabled
    institutional_runner.auto_execute = enabled
    db.save_account_state(account_id or DEFAULT_ACCOUNT_ID, auto_execute=enabled)
    return {
        "auto_execute": enabled,
        "account_id": account_id or "all_accounts",
        "market_status": strategy_manager.get_market_status(),
    }

@app.post("/api/trading/reset-account")
async def reset_trading_account(
    account_id: Optional[str] = Query(None),
    capital: Optional[float] = Query(None),
    auth_user: Dict[str, Any] = Depends(require_auth),
):
    """Reset paper account(s) to specified capital (default ₹10,000)."""
    return execution_engine.reset_account(capital=capital, account_id=account_id)

@app.post("/api/trading/pre-market-scan")
async def trigger_pre_market_scan(auth_user: Dict[str, Any] = Depends(require_auth)):
    """Run morning pre-market analysis using Google Search grounding for budget stocks."""
    return await strategy_manager.run_pre_market_analysis()

@app.post("/api/trading/emergency-decision")
async def trigger_emergency_decision(
    symbol: str = Query(...),
    drop_pct: float = Query(2.0),
    current_pnl: float = Query(-50.0),
    reason: str = Query("Critical market move / adverse drop"),
    auth_user: Dict[str, Any] = Depends(require_auth),
):
    """Trigger emergency AI decision for critical market movements or adverse price drops."""
    return await argon_predictor.evaluate_emergency_decision(symbol, drop_pct, current_pnl, reason)

@app.post("/api/trading/backtest")
async def run_backtest_endpoint(
    req: Dict[str, Any] = Body(default={}),
    auth_user: Dict[str, Any] = Depends(require_auth),
):
    """Run an on-demand historical intraday backtest using DeepSeek Flash MaaS across past 5-min bars."""
    from app.trading.backtester import backtest_engine
    symbols = req.get("symbols", ["RELIANCE", "TCS", "INFY", "VEDL", "SBIN", "ICICIBANK"])
    period = req.get("period", "5d")
    use_deepseek = req.get("use_deepseek", True)
    time_filter = req.get("time_filter", True)
    return await backtest_engine.run_backtest(
        symbols=symbols,
        period=period,
        use_deepseek=use_deepseek,
        time_filter=time_filter,
    )

@app.get("/api/strategies")
async def get_all_strategies(auth_user: Dict[str, Any] = Depends(require_auth)):
    """List all registered strategies, win rates, rules, and PnL."""
    return {"strategies": db.get_strategies()}

@app.post("/api/strategies/toggle")
async def toggle_strategy_status(
    strategy_id: str = Query(...),
    is_active: bool = Query(...),
    auth_user: Dict[str, Any] = Depends(require_auth),
):
    """Toggle strategy active state."""
    ok = db.toggle_strategy(strategy_id, is_active)
    return {"strategy_id": strategy_id, "is_active": is_active, "success": ok}

@app.get("/api/learning/insights")
async def get_learning_insights(
    limit: int = Query(30),
    auth_user: Dict[str, Any] = Depends(require_auth),
):
    """Get past trade lessons and autonomous learning logs."""
    return {"insights": db.get_learning_logs(limit=limit)}

@app.get("/api/performance/daily")
async def get_daily_performance(
    account_id: Optional[str] = Query(None),
    limit: int = Query(30),
    auth_user: Dict[str, Any] = Depends(require_auth),
):
    """Get daily performance history with gross, fees, AI cost, and net."""
    return {"daily_history": db.get_daily_performance(account_id=account_id, limit=limit)}

@app.get("/api/tokens/summary")
async def get_token_accounting(
    account_id: Optional[str] = Query(None),
    auth_user: Dict[str, Any] = Depends(require_auth),
):
    """Get token usage, model breakdown, and net profit after AI cost."""
    account = account_id or DEFAULT_ACCOUNT_ID
    portfolio = execution_engine.get_portfolio_summary(account)
    return token_tracker.get_summary(
        portfolio.get("total_pnl_inr", 0.0), account_id=account
    )

@app.get("/api/trading/risk-state")
async def get_risk_state(
    account_id: str = Query(DEFAULT_ACCOUNT_ID),
    auth_user: Dict[str, Any] = Depends(require_auth),
):
    """Daily loss lock, profit target lock, trade count, and AI cost."""
    account = execution_engine.get_account(account_id)
    return strategy_manager.risk.get_state(account_id, account)


# -------------------------------------------------------------
# INSTITUTIONAL TRADING TERMINAL & SMART MONEY ENGINE
# -------------------------------------------------------------
@app.get("/api/trading/institutional/overview")
async def get_institutional_overview_endpoint(
    symbol: str = Query("NIFTY"),
):
    """
    Returns unified institutional market intelligence for NIFTY, BANK NIFTY, and SENSEX:
    - Pre-Market 4-Lines (PDH, PDL, PDC, 1H Swings)
    - Volume Profile (POC, VAH, VAL) & Fibonacci Zones
    - Option Chain GEX & Regime (Positive vs Negative Gamma)
    - Heavyweight point contributions & Sector Heatmap
    - AI Price Attribution (3-bullet explanation)
    - 0.5% Net Profit & Risk Guardrails Status
    """
    clean_sym = symbol.strip().upper()
    candles_res = get_symbol_candles(symbol=clean_sym, interval="5m", period="5d")
    spot = candles_res.get("latest_price", 0.0)
    chg_pct = candles_res.get("change_percent", 0.0)

    # GEX & Options Microstructure
    gex_data = compute_option_chain_gex(clean_sym, spot_price=spot)

    # Heavyweights & Sector Heatmap
    heavyweights = get_index_heavyweights_matrix(clean_sym, spot_price=spot)
    sectors = get_sector_rotation_heatmap()

    # Pre-Market 4-Lines, Volume Profile, Fibonacci
    four_lines = candles_res.get("four_lines", {})
    volume_profile = candles_res.get("volume_profile", {})
    fib_zones = candles_res.get("fibonacci_zones", {})

    # Recent Sweep if any
    recent_sweeps = [m for m in candles_res.get("markers", []) if "SWEEP" in m.get("text", "")]
    latest_sweep_dict = None
    if recent_sweeps:
        latest_sweep_dict = {
            "narrative": recent_sweeps[-1].get("text", ""),
            "timestamp": recent_sweeps[-1].get("time", 0)
        }

    # AI 3-Bullet Price Attribution
    attribution = generate_ai_price_attribution(
        index_symbol=clean_sym,
        spot=spot,
        change_pct=chg_pct,
        gex_data=gex_data,
        recent_sweep=latest_sweep_dict,
        heavyweights=heavyweights
    )

    # 0.5% Net Profit & Risk Guardrails
    portfolio = execution_engine.get_portfolio_summary(DEFAULT_ACCOUNT_ID)
    starting_eq = 10000.0
    current_net_pnl = portfolio.get("total_pnl_inr", 0.0)
    daily_net_profit_pct = round((current_net_pnl / starting_eq) * 100, 2)
    current_drawdown_pct = max(0.0, round((-current_net_pnl / starting_eq) * 100, 2)) if current_net_pnl < 0 else 0.0

    guardrails = evaluate_risk_guardrails(
        daily_net_profit_pct=daily_net_profit_pct,
        consecutive_losses=0,
        current_drawdown_pct=current_drawdown_pct,
        current_time=datetime.datetime.now().time(),
        trade_risk_reward=2.0
    )

    return {
        "symbol": clean_sym,
        "spot_price": spot,
        "change_percent": chg_pct,
        "gex": gex_data,
        "four_lines": four_lines,
        "volume_profile": volume_profile,
        "fibonacci_zones": fib_zones,
        "heavyweights": heavyweights,
        "sectors": sectors,
        "attribution": attribution,
        "guardrails": guardrails,
        "daily_net_profit_pct": daily_net_profit_pct,
        "daily_goal_pct": 0.50,
        "daily_goal_reached": daily_net_profit_pct >= 0.50,
    }


@app.get("/api/trading/institutional/gex")
async def get_institutional_gex_endpoint(
    symbol: str = Query("NIFTY"),
):
    """Returns strike-by-strike GEX table, open interest, and gamma flip zone."""
    candles_res = get_symbol_candles(symbol=symbol, interval="5m", period="1d")
    spot = candles_res.get("latest_price", 23000.0)
    return compute_option_chain_gex(symbol, spot_price=spot)


@app.post("/api/trading/institutional/calculate-fees")
async def calculate_fees_endpoint(req: InstitutionalFeeRequest):
    """Calculates all Indian statutory charges, taxes, GST, and net breakeven points."""
    return calculate_indian_statutory_charges(
        buy_value=req.buy_value,
        sell_value=req.sell_value,
        instrument_type=req.instrument_type or "INTRADAY_EQUITY",
        exchange=req.exchange or "NSE",
        quantity=req.quantity or 1.0
    )


@app.post("/api/trading/institutional/ai-signal")
async def generate_ai_signal_endpoint(req: InstitutionalSignalRequest):
    """Generates 4-step institutional AI signal with 1:2 R:R and Indian statutory fee deductions."""
    clean_sym = (req.symbol or "NIFTY").strip().upper()
    candles_res = get_symbol_candles(symbol=clean_sym, interval="5m", period="5d")
    spot = candles_res.get("latest_price", 0.0)
    raw_candles = candles_res.get("candles", [])
    df = pd.DataFrame(raw_candles)
    if not df.empty and "open" in df.columns:
        df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})

    return generate_institutional_ai_signal(
        symbol=clean_sym,
        spot_price=spot,
        df=df,
        daily_net_profit_pct=req.daily_net_profit_pct or 0.0,
        consecutive_losses=req.consecutive_losses or 0,
        current_drawdown_pct=req.current_drawdown_pct or 0.0
    )

# -------------------------------------------------------------
# INTERACTIVE CHATBOT (DEEPSEEK / CLAUDE OPUS)
# -------------------------------------------------------------
@app.post("/api/chatbot", response_model=ChatbotResponse)
@app.post("/api/chatbot/chat", response_model=ChatbotResponse)
async def chatbot_endpoint(
    req: ChatbotRequest,
    auth_user: Dict[str, Any] = Depends(require_auth),
):
    """
    Chatbot endpoint powered by DeepSeek 4.1 Flash or Claude Opus 4.6.
    Features live NSE/BSE market visibility and fast paper-order execution.
    """
    return await chatbot_service.chat(
        message=req.message,
        model=req.model,
        use_search=req.use_search if req.use_search is not None else True,
        history=req.history,
    )

@app.post("/api/chat", response_model=ChatbotResponse)
async def chat_with_agent(
    req: ChatRequest,
    auth_user: Dict[str, Any] = Depends(require_auth),
):
    """Interactive chat with live market and portfolio awareness."""
    return await chatbot_service.chat(
        message=req.message,
        model=req.model,
        use_search=req.use_search if req.use_search is not None else True,
    )

# -------------------------------------------------------------
# INDIAN FINANCIAL ADVISOR ENDPOINTS
# -------------------------------------------------------------
@app.post("/api/financial-advisor/analyze")
async def analyze_indian_stock(
    req: FinancialAdvisorRequest,
    auth_user: Dict[str, Any] = Depends(require_auth),
):
    """Execute the multi-agent financial advisor pipeline for an Indian stock (NSE/BSE)."""
    if not req.symbol.strip():
        raise HTTPException(status_code=400, detail="Stock symbol must not be empty.")
    result = await run_financial_advisor_pipeline(
        symbol=req.symbol.strip(),
        risk_attitude=req.risk_attitude or "moderate",
        investment_period=req.investment_period or "medium-term",
        model=req.model
    )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Analysis failed."))
    return result

@app.get("/api/financial-advisor/quote")
async def get_stock_quote(symbol: str = Query(...)):
    """Fetch live quote in INR for an Indian stock or index."""
    return get_indian_stock_quote(symbol)

@app.get("/api/financial-advisor/indices")
async def get_market_indices():
    """Fetch live Nifty 50, Sensex, Bank Nifty, and India VIX."""
    return get_indian_market_indices()

@app.get("/api/financial-advisor/news")
async def get_market_news(query: str = Query(..., description="Search query")):
    """Live web search for Indian market news and corporate earnings."""
    return search_indian_market_news(query)

# -------------------------------------------------------------
# AUTONOMOUS AGENT REST & WEBSOCKET ENDPOINTS
# -------------------------------------------------------------
@app.post("/api/agent/run")
async def run_agent(
    req: RunAgentRequest,
    auth_user: Dict[str, Any] = Depends(require_auth),
):
    """Launch an autonomous agent task to work on trading code."""
    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt must not be empty.")
    task = await agent_manager.start_task(req.prompt, req.model)
    return {"task_id": task.task_id, "status": task.status}

@app.get("/api/agent/tasks")
async def list_tasks(auth_user: Dict[str, Any] = Depends(require_auth)):
    """List all agent tasks in memory."""
    return {"tasks": agent_manager.list_tasks()}

@app.get("/api/agent/task/{task_id}")
async def get_task_status(
    task_id: str,
    auth_user: Dict[str, Any] = Depends(require_auth),
):
    """Get status and history of a specific agent task."""
    task = agent_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    return {
        **task.to_dict(),
        "events": [e.to_dict() for e in task.events],
    }

@app.post("/api/agent/cancel/{task_id}")
async def cancel_task(
    task_id: str,
    auth_user: Dict[str, Any] = Depends(require_auth),
):
    """Cancel a running agent task."""
    task = agent_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    task.cancel()
    return {"status": "cancelled", "task_id": task_id}

@app.websocket("/ws/agent/{task_id}")
async def ws_agent_stream(websocket: WebSocket, task_id: str):
    """Real-time event stream for a running agent task."""
    if not await verify_websocket_auth(websocket):
        return

    task = agent_manager.get_task(task_id)
    if not task:
        await websocket.close(code=1008, reason="Task not found")
        return

    await websocket.accept()
    q = task.subscribe()
    try:
        for evt in task.events:
            await websocket.send_json(evt.to_dict())
        while True:
            if task.status in ["completed", "failed", "cancelled"] and q.empty():
                break
            try:
                data = await asyncio.wait_for(q.get(), timeout=1.0)
                await websocket.send_json(data)
            except asyncio.TimeoutError:
                if task.status in ["completed", "failed", "cancelled"] and q.empty():
                    break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"Error in ws_agent_stream: {e}")
    finally:
        task.unsubscribe(q)
        try:
            await websocket.close()
        except Exception:
            pass

# -------------------------------------------------------------
# REAL-TIME MARKET TICK STREAM & PREDICTOR WEBSOCKETS
# -------------------------------------------------------------
@app.websocket("/ws/market")
async def ws_market_stream(websocket: WebSocket):
    """Stream live market prices, indices, and portfolio values every 2s."""
    if not await verify_websocket_auth(websocket):
        return

    await websocket.accept()
    q = market_feed.subscribe()
    try:
        # Initial snapshot
        await websocket.send_json({
            "type": "market_tick",
            "watchlist": list(market_feed.cached_quotes.values()),
            "indices": get_indian_market_indices().get("indices", {}),
            "portfolio": execution_engine.get_portfolio_summary(),
            "market_status": strategy_manager.get_market_status(),
        })
        while True:
            data = await q.get()
            await websocket.send_json(data)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"Error in ws_market_stream: {e}")
    finally:
        market_feed.unsubscribe(q)

@app.websocket("/ws/predictor")
async def ws_predictor_stream(websocket: WebSocket):
    """Stream DeepSeek Institutional AI prediction alerts and schedule updates."""
    if not await verify_websocket_auth(websocket):
        return

    await websocket.accept()
    q = institutional_runner.subscribe()
    try:
        # Send initial snapshot of latest institutional signals
        await websocket.send_json({
            "type": "institutional_update",
            "cycle": institutional_runner.cycle_count,
            "timestamp": institutional_runner.last_run_timestamp or datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "auto_execute": institutional_runner.auto_execute,
            "market_open": market_hours.is_market_open(),
            "signals": institutional_runner.latest_signals,
            "portfolio": execution_engine.get_portfolio_summary(),
        })
        while True:
            data = await q.get()
            await websocket.send_json(data)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"Error in ws_predictor_stream: {e}")
    finally:
        institutional_runner.unsubscribe(q)

# -------------------------------------------------------------
# WORKSPACE CODE & FILE MANAGEMENT ENDPOINTS
# -------------------------------------------------------------
@app.get("/api/workspace/files")
async def get_workspace_files(auth_user: Dict[str, Any] = Depends(require_auth)):
    """List all strategy and code files in the trading workspace recursively."""
    ws = settings.absolute_workspace_dir
    files = []
    for p in sorted(ws.rglob("*")):
        if any(part.startswith(".") for part in p.parts):
            continue
        if "__pycache__" in p.parts or "node_modules" in p.parts:
            continue
        rel = str(p.relative_to(ws))
        files.append({
            "name": p.name,
            "rel_path": rel,
            "is_dir": p.is_dir(),
            "size": p.stat().st_size if p.is_file() else 0,
        })
    return {"workspace": str(ws), "files": files}

@app.get("/api/workspace/file")
async def get_file_content(
    path: str = Query(...),
    auth_user: Dict[str, Any] = Depends(require_auth),
):
    """Read file content from workspace."""
    return await read_file(path)

@app.post("/api/workspace/file")
async def save_file_content(
    req: SaveFileRequest,
    auth_user: Dict[str, Any] = Depends(require_auth),
):
    """Save code or edits directly to workspace."""
    return await write_file(req.path, req.content)
