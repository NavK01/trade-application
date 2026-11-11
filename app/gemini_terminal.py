"""
Gemini 4 Optimized Live Trading Terminal Application
Runs on Port 7001. Provides full interactive live market trading terminal
driven by the Gemini 4 Calibrated Strategy (1.25% target, 2.0x volume, 1.05R BE, 0.65% SL, lunch block).
Dedicated Account: 'gemini_optimized' (₹10,000 Initial Capital).
"""

import os
import asyncio
import datetime
import logging
from pathlib import Path
from typing import Dict, Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.config import settings
from app.financial_advisor.india_market import (
    get_indian_market_indices,
    get_indian_stock_quote,
)
from app.trading.candles import get_symbol_candles
from app.storage.db import db
from app.storage.cache import cache
from app.trading.execution_engine import execution_engine
from app.trading.gemini_runner import gemini_runner, ACCOUNT_ID
from app.trading.gemini_strategy import (
    generate_gemini_optimized_signal,
    MIN_TARGET_PCT,
    MIN_SL_PCT,
    MIN_SWEEP_VOL_MULTIPLIER,
    BREAKEVEN_TRIGGER_R,
)
from app.trading.market_feed import market_feed
from app.trading import market_hours
from app.trading.strategy_manager import strategy_manager

app = FastAPI(title="Gemini 4 Quant Terminal (Optimized)", version="2.0.0")

logger = logging.getLogger("app.gemini_terminal")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).parent


class OrderRequest(BaseModel):
    symbol: str
    action: str
    quantity: int
    order_type: str = "MARKET"
    stop_loss: Optional[float] = None
    target_price: Optional[float] = None
    strategy_name: Optional[str] = "gemini_calibrated_manual"
    account_id: Optional[str] = ACCOUNT_ID


class ClosePositionRequest(BaseModel):
    symbol: str
    account_id: Optional[str] = ACCOUNT_ID


class SignalRequest(BaseModel):
    symbol: str


@app.on_event("startup")
async def startup_event():
    # Ensure account state seeded
    db.init_db()

    # The Gemini-4 intraday churner was measured net-negative after statutory
    # fees (see trading_workspace/FROZEN_BASELINE.json). It is OFF by default
    # and only runs when legacy intraday engines are explicitly re-enabled with
    # fresh walk-forward evidence. This prevents the live server from buying at
    # the open and force-squaring-off every position at the 15:20 EOD bell.
    legacy = bool(getattr(settings, "ENABLE_LEGACY_INTRADAY_ENGINES", False))
    acc = execution_engine.get_account(ACCOUNT_ID)
    if acc:
        acc.auto_execute = legacy
    db.save_account_state(ACCOUNT_ID, auto_execute=legacy)

    market_feed.start_feed()
    if legacy:
        gemini_runner.start_background_loop()
        logger.warning("Legacy intraday churner ENABLED (gemini_runner auto-execute).")
    else:
        logger.info(
            "Legacy intraday churner disabled. Set ENABLE_LEGACY_INTRADAY_ENGINES=true "
            "only with new walk-forward evidence."
        )


@app.on_event("shutdown")
async def shutdown_event():
    gemini_runner.stop_background_loop()
    market_feed.stop_feed()


# -----------------------------------------------------------------------------
# REST APIS FOR LIVE TERMINAL
# -----------------------------------------------------------------------------

@app.get("/api/health")
async def health_check():
    import psutil
    vm = psutil.virtual_memory()
    return {
        "status": "healthy",
        "model": "google-vertex/gemini-4-argon",
        "account_id": ACCOUNT_ID,
        "strategy": "Gemini 4 Calibrated Institutional Strategy",
        "parameters": {
            "min_target_pct": MIN_TARGET_PCT,
            "min_sweep_vol_multiplier": MIN_SWEEP_VOL_MULTIPLIER,
            "breakeven_trigger_r": BREAKEVEN_TRIGGER_R,
            "min_sl_pct": MIN_SL_PCT,
            "time_filter_lunch_block": True,
        },
        "system": {
            "ram": {"percent": vm.percent},
            "cpu": {"percent": psutil.cpu_percent(interval=None)},
        }
    }


@app.get("/api/trading/portfolio")
async def get_portfolio(account_id: str = Query(ACCOUNT_ID)):
    summary = execution_engine.get_portfolio_summary(account_id or ACCOUNT_ID)
    summary["market_status"] = strategy_manager.get_market_status()
    return summary


@app.get("/api/trading/orders")
async def get_orders(account_id: str = Query(ACCOUNT_ID)):
    acc = execution_engine.get_account(account_id or ACCOUNT_ID)
    orders = acc.order_history if acc else []
    return {"orders": orders, "count": len(orders)}


@app.get("/api/trading/market-status")
async def get_market_status():
    return strategy_manager.get_market_status()


@app.get("/api/financial-advisor/indices")
async def get_indices():
    return get_indian_market_indices()


@app.get("/api/trading/candles")
async def get_candles(
    symbol: str = Query(..., description="Stock symbol (e.g., SBIN)"),
    interval: str = Query("15m", description="Candle interval"),
    period: str = Query("5d", description="Time period")
):
    return get_symbol_candles(symbol=symbol, interval=interval, period=period)


@app.post("/api/trading/order")
async def execute_order(req: OrderRequest):
    order = await execution_engine.fast_execute_order(
        symbol=req.symbol.upper(),
        action=req.action.upper(),
        quantity=req.quantity,
        order_type=req.order_type.upper(),
        strategy_name=req.strategy_name or "gemini_manual",
        account_id=ACCOUNT_ID,
        stop_loss=req.stop_loss,
        target_price=req.target_price,
    )
    return {
        "status": "success" if order.get("status") in ("FILLED", "OPEN") else "failed",
        "order": order,
        "portfolio": execution_engine.get_portfolio_summary(ACCOUNT_ID),
    }


@app.get("/api/auth/check")
async def auth_check():
    return {"authenticated": True, "user": {"username": "gemini_quant"}}


@app.post("/api/trading/position/close")
@app.post("/api/trading/close-position")
async def close_single_position(req: ClosePositionRequest):
    result = await execution_engine.close_position(
        symbol=req.symbol.upper(),
        account_id=ACCOUNT_ID,
        reason="manual_close"
    )
    return {
        "status": "success",
        "close_trade": result,
        "portfolio": execution_engine.get_portfolio_summary(ACCOUNT_ID),
    }


@app.post("/api/trading/flatten")
async def flatten_all_positions():
    results = await execution_engine.flatten_portfolio(account_id=ACCOUNT_ID)
    return {
        "status": "success",
        "closed_count": len(results),
        "results": results,
        "portfolio": execution_engine.get_portfolio_summary(ACCOUNT_ID),
    }


@app.post("/api/trading/account/reset")
@app.post("/api/trading/reset-account")
async def reset_dummy_account():
    acc = execution_engine.get_account(ACCOUNT_ID)
    if acc:
        acc.initial_capital = 10000.0
        acc.available_cash = 10000.0
        acc.realized_pnl = 0.0
        acc.positions.clear()
        acc.order_history.clear()
        db.save_account_state(ACCOUNT_ID, initial_capital=10000.0, available_cash=10000.0, realized_pnl=0.0)
    return {
        "status": "success",
        "message": "Reset ₹10,000 dummy capital for Gemini 4 Optimized Account.",
        "portfolio": execution_engine.get_portfolio_summary(ACCOUNT_ID),
    }


@app.post("/api/trading/institutional/ai-signal")
async def get_gemini_signal(req: SignalRequest):
    sym = req.symbol.strip().upper()
    candles_data = get_symbol_candles(symbol=sym, interval="5m", period="5d")
    spot = candles_data.get("latest_price", 0.0) or cache.get_price(sym) or 0.0
    import pandas as pd
    df = pd.DataFrame(candles_data.get("candles", []))
    if not df.empty and "open" in df.columns:
        df = df.rename(columns={
            "open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"
        })
    sig = generate_gemini_optimized_signal(symbol=sym, spot_price=spot, df=df)
    return sig


@app.get("/api/trading/institutional/overview")
async def get_overview(symbol: str = Query("SBIN")):
    sym = symbol.strip().upper()
    candles_data = get_symbol_candles(symbol=sym, interval="5m", period="5d")
    spot = candles_data.get("latest_price", 0.0) or cache.get_price(sym) or 0.0
    import pandas as pd
    df = pd.DataFrame(candles_data.get("candles", []))
    if not df.empty and "open" in df.columns:
        df = df.rename(columns={
            "open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"
        })
    sig = generate_gemini_optimized_signal(symbol=sym, spot_price=spot, df=df)
    return {
        "symbol": sym,
        "latest_price": spot,
        "ai_signal": sig,
        "calibrated_parameters": {
            "min_target_pct": MIN_TARGET_PCT,
            "min_sweep_vol_multiplier": MIN_SWEEP_VOL_MULTIPLIER,
            "breakeven_trigger_r": BREAKEVEN_TRIGGER_R,
            "min_sl_pct": MIN_SL_PCT,
            "lunch_block": "11:30 - 13:15 IST",
        },
        "portfolio": execution_engine.get_portfolio_summary(ACCOUNT_ID),
    }


# -----------------------------------------------------------------------------
# WEBSOCKETS
# -----------------------------------------------------------------------------

@app.websocket("/ws/market")
async def ws_market_stream(websocket: WebSocket):
    await websocket.accept()
    q = market_feed.subscribe()
    try:
        await websocket.send_json({
            "type": "market_tick",
            "watchlist": list(market_feed.cached_quotes.values()),
            "indices": get_indian_market_indices().get("indices", {}),
            "portfolio": execution_engine.get_portfolio_summary(ACCOUNT_ID),
            "market_status": strategy_manager.get_market_status(),
        })
        while True:
            data = await q.get()
            data["portfolio"] = execution_engine.get_portfolio_summary(ACCOUNT_ID)
            await websocket.send_json(data)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        market_feed.unsubscribe(q)


@app.websocket("/ws/predictor")
async def ws_predictor_stream(websocket: WebSocket):
    await websocket.accept()
    q = gemini_runner.subscribe()
    try:
        await websocket.send_json({
            "type": "gemini_institutional_snapshot",
            "signals": gemini_runner.latest_signals,
            "portfolio": execution_engine.get_portfolio_summary(ACCOUNT_ID),
        })
        while True:
            data = await q.get()
            await websocket.send_json(data)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        gemini_runner.unsubscribe(q)


# -----------------------------------------------------------------------------
# HTML UI ROUTING
# -----------------------------------------------------------------------------

STATIC_DIR = BASE_DIR / "static"


@app.get("/", response_class=HTMLResponse)
@app.get("/gemini/", response_class=HTMLResponse)
async def serve_gemini_terminal():
    ui_path = STATIC_DIR / "gemini_index.html"
    if ui_path.exists():
        with open(ui_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Gemini 4 Terminal UI loading...</h1>")


# Mount static assets
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/gemini/static", StaticFiles(directory=str(STATIC_DIR)), name="gemini_static")
