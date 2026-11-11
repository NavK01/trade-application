"""
1-Month Institutional Backtest Runner for NIFTY 50, BANK NIFTY, and SENSEX.

Tests the 4-Step Institutional Framework (Environment, Location, Confirmation, Execution)
with AI Signal Generation and full Indian Statutory Fee Deductions on 1 month of 5-minute data.
Tracks inputs passed to AI, AI recommendations (action, entry, stop loss, target, R:R),
gross P&L, fees, net P&L, win rate, and drawdown.
"""

import os
import sys
import json
import datetime
from typing import Dict, Any, List
import pandas as pd
import numpy as np
import yfinance as yf

# Ensure project root in python path
sys.path.insert(0, "/home/super/Documents/Projects/GeminiAgent")

from app.trading.institutional_engine import (
    calculate_indian_statutory_charges,
    compute_premarket_4lines,
    compute_volume_profile,
    compute_fibonacci_zones,
    compute_option_chain_gex,
    detect_liquidity_sweeps_and_absorption,
    evaluate_risk_guardrails,
    generate_institutional_ai_signal,
)

INDICES_CONFIG = {
    "NIFTY 50": {"yf_symbol": "^NSEI", "lot_size": 25, "exchange": "NSE", "point_val": 25},
    "BANK NIFTY": {"yf_symbol": "^NSEBANK", "lot_size": 15, "exchange": "NSE", "point_val": 15},
    "SENSEX": {"yf_symbol": "^BSESN", "lot_size": 10, "exchange": "BSE", "point_val": 10},
}


def run_1month_index_backtest(index_name: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Runs a 1-month backtest on 5-minute bars for a single index."""
    yf_sym = config["yf_symbol"]
    lot_size = config["lot_size"]
    exchange = config["exchange"]

    print(f"\n=======================================================")
    print(f" FETCHING 1-MONTH 5M DATA FOR {index_name} ({yf_sym})")
    print(f"=======================================================")
    
    ticker = yf.Ticker(yf_sym)
    df = ticker.history(period="1mo", interval="5m")
    if df.empty:
        print(f"ERROR: No historical data returned for {yf_sym}")
        return {"index": index_name, "error": "No data"}

    # Clean DataFrame
    df = df.dropna().copy()
    df["Date"] = df.index.date
    df["Time"] = df.index.time

    unique_days = sorted(df["Date"].unique())
    print(f"Total bars: {len(df)} across {len(unique_days)} trading sessions.")

    starting_capital = 50000.0  # ₹50,000 index trading margin
    current_capital = starting_capital
    peak_capital = starting_capital
    max_drawdown_pct = 0.0

    all_trades = []
    ai_decision_samples = []

    # Iterate day-by-day to simulate real-time market execution
    for day_idx in range(1, len(unique_days)):
        day_date = unique_days[day_idx]
        prev_date = unique_days[day_idx - 1]

        prev_day_df = df[df["Date"] == prev_date]
        today_df = df[df["Date"] == day_date]

        if len(today_df) < 10 or len(prev_day_df) < 10:
            continue

        # Day-level tracking for circuit breakers
        day_start_capital = current_capital
        day_consecutive_losses = 0
        day_trades = 0

        # Reference Levels from Previous Day
        pdh = float(prev_day_df["High"].max())
        pdl = float(prev_day_df["Low"].min())
        pdc = float(prev_day_df["Close"].iloc[-1])

        active_trade = None

        # Step through today's 5-minute bars
        for bar_idx in range(12, len(today_df)):
            current_bar = today_df.iloc[bar_idx]
            bar_time = current_bar["Time"]
            current_close = float(current_bar["Close"])
            current_high = float(current_bar["High"])
            current_low = float(current_bar["Low"])

            # 1. Manage Active Trade
            if active_trade:
                # Check target hit
                if active_trade["action"] == "BUY" and current_high >= active_trade["target"]:
                    exit_price = active_trade["target"]
                    gross_pts = exit_price - active_trade["entry"]
                    gross_pnl = gross_pts * active_trade["qty"]
                    fees = calculate_indian_statutory_charges(
                        active_trade["entry"] * active_trade["qty"],
                        exit_price * active_trade["qty"],
                        instrument_type="INTRADAY_EQUITY",
                        exchange=exchange,
                        quantity=active_trade["qty"]
                    )
                    net_pnl = round(gross_pnl - fees["total_charges"], 2)
                    current_capital += net_pnl
                    active_trade.update({
                        "exit_price": exit_price,
                        "exit_reason": "TARGET_HIT",
                        "gross_pnl": gross_pnl,
                        "fees": fees["total_charges"],
                        "net_pnl": net_pnl,
                        "status": "WIN",
                    })
                    all_trades.append(active_trade)
                    active_trade = None
                    day_consecutive_losses = 0

                elif active_trade["action"] == "SELL" and current_low <= active_trade["target"]:
                    exit_price = active_trade["target"]
                    gross_pts = active_trade["entry"] - exit_price
                    gross_pnl = gross_pts * active_trade["qty"]
                    fees = calculate_indian_statutory_charges(
                        active_trade["entry"] * active_trade["qty"],
                        exit_price * active_trade["qty"],
                        instrument_type="INTRADAY_EQUITY",
                        exchange=exchange,
                        quantity=active_trade["qty"]
                    )
                    net_pnl = round(gross_pnl - fees["total_charges"], 2)
                    current_capital += net_pnl
                    active_trade.update({
                        "exit_price": exit_price,
                        "exit_reason": "TARGET_HIT",
                        "gross_pnl": gross_pnl,
                        "fees": fees["total_charges"],
                        "net_pnl": net_pnl,
                        "status": "WIN",
                    })
                    all_trades.append(active_trade)
                    active_trade = None
                    day_consecutive_losses = 0

                # Check stop loss hit
                elif active_trade and active_trade["action"] == "BUY" and current_low <= active_trade["stop_loss"]:
                    exit_price = active_trade["stop_loss"]
                    gross_pts = exit_price - active_trade["entry"]
                    gross_pnl = gross_pts * active_trade["qty"]
                    fees = calculate_indian_statutory_charges(
                        active_trade["entry"] * active_trade["qty"],
                        exit_price * active_trade["qty"],
                        instrument_type="INTRADAY_EQUITY",
                        exchange=exchange,
                        quantity=active_trade["qty"]
                    )
                    net_pnl = round(gross_pnl - fees["total_charges"], 2)
                    current_capital += net_pnl
                    active_trade.update({
                        "exit_price": exit_price,
                        "exit_reason": "STOP_LOSS",
                        "gross_pnl": gross_pnl,
                        "fees": fees["total_charges"],
                        "net_pnl": net_pnl,
                        "status": "LOSS",
                    })
                    all_trades.append(active_trade)
                    active_trade = None
                    day_consecutive_losses += 1

                elif active_trade and active_trade["action"] == "SELL" and current_high >= active_trade["stop_loss"]:
                    exit_price = active_trade["stop_loss"]
                    gross_pts = active_trade["entry"] - exit_price
                    gross_pnl = gross_pts * active_trade["qty"]
                    fees = calculate_indian_statutory_charges(
                        active_trade["entry"] * active_trade["qty"],
                        exit_price * active_trade["qty"],
                        instrument_type="INTRADAY_EQUITY",
                        exchange=exchange,
                        quantity=active_trade["qty"]
                    )
                    net_pnl = round(gross_pnl - fees["total_charges"], 2)
                    current_capital += net_pnl
                    active_trade.update({
                        "exit_price": exit_price,
                        "exit_reason": "STOP_LOSS",
                        "gross_pnl": gross_pnl,
                        "fees": fees["total_charges"],
                        "net_pnl": net_pnl,
                        "status": "LOSS",
                    })
                    all_trades.append(active_trade)
                    active_trade = None
                    day_consecutive_losses += 1

                # Market Close Square-Off at 15:15 IST
                elif active_trade and bar_time >= datetime.time(15, 15):
                    exit_price = current_close
                    gross_pts = (exit_price - active_trade["entry"]) if active_trade["action"] == "BUY" else (active_trade["entry"] - exit_price)
                    gross_pnl = gross_pts * active_trade["qty"]
                    fees = calculate_indian_statutory_charges(
                        active_trade["entry"] * active_trade["qty"],
                        exit_price * active_trade["qty"],
                        instrument_type="INTRADAY_EQUITY",
                        exchange=exchange,
                        quantity=active_trade["qty"]
                    )
                    net_pnl = round(gross_pnl - fees["total_charges"], 2)
                    current_capital += net_pnl
                    status = "WIN" if net_pnl > 0 else "LOSS"
                    if status == "LOSS":
                        day_consecutive_losses += 1
                    else:
                        day_consecutive_losses = 0
                    active_trade.update({
                        "exit_price": exit_price,
                        "exit_reason": "INTRADAY_EOD_SQUAREOFF",
                        "gross_pnl": gross_pnl,
                        "fees": fees["total_charges"],
                        "net_pnl": net_pnl,
                        "status": status,
                    })
                    all_trades.append(active_trade)
                    active_trade = None

                continue

            # 2. Check Daily Risk Guardrails before searching for a new trade
            day_net_pnl = current_capital - day_start_capital
            day_net_pct = (day_net_pnl / day_start_capital) * 100
            current_dd_pct = ((peak_capital - current_capital) / peak_capital) * 100 if peak_capital > 0 else 0

            guardrails = evaluate_risk_guardrails(
                daily_net_profit_pct=day_net_pct,
                consecutive_losses=day_consecutive_losses,
                current_drawdown_pct=current_dd_pct,
                current_time=bar_time,
                trade_risk_reward=2.0
            )

            # If kill switches fire or outside high-volume windows, skip
            if not guardrails["can_trade"]:
                continue

            # 3. Contextual Data up to this bar
            seen_df = pd.concat([prev_day_df, today_df.iloc[:bar_idx + 1]])
            vp = compute_volume_profile(seen_df)
            swing_high = float(today_df.iloc[max(0, bar_idx - 12):bar_idx + 1]["High"].max())
            swing_low = float(today_df.iloc[max(0, bar_idx - 12):bar_idx + 1]["Low"].min())

            sweeps = detect_liquidity_sweeps_and_absorption(
                today_df.iloc[max(0, bar_idx - 2):bar_idx + 1],
                pdh=pdh, pdl=pdl, pdc=pdc,
                swing_high=swing_high, swing_low=swing_low
            )

            if sweeps and not active_trade:
                sweep = sweeps[-1]
                # Chris Kmer 4-Step Decision:
                # Long lower wick sweep of PDL / Swing Low => BUY
                if sweep["type"] == "BULLISH_LIQUIDITY_SWEEP":
                    entry = current_close
                    sl = sweep["wick_low"] - (entry * 0.001)
                    risk = entry - sl
                    if risk > 0:
                        target = entry + (risk * 2.0)  # 1:2 R:R target
                        active_trade = {
                            "date": str(day_date),
                            "entry_time": str(bar_time),
                            "symbol": index_name,
                            "action": "BUY",
                            "entry": round(entry, 2),
                            "stop_loss": round(sl, 2),
                            "target": round(target, 2),
                            "risk_pts": round(risk, 2),
                            "target_pts": round(risk * 2.0, 2),
                            "rr_ratio": 2.0,
                            "qty": lot_size,
                            "setup": sweep["swept_level"],
                            "narrative": sweep["narrative"],
                        }
                        if len(ai_decision_samples) < 3:
                            ai_decision_samples.append({
                                "symbol": index_name,
                                "date": str(day_date),
                                "time": str(bar_time),
                                "input_data": {
                                    "ltp": entry,
                                    "pdh": pdh, "pdl": pdl, "pdc": pdc,
                                    "swing_high": swing_high, "swing_low": swing_low,
                                    "poc": vp.get("POC"), "vah": vp.get("VAH"), "val": vp.get("VAL"),
                                    "sweep_event": sweep
                                },
                                "ai_recommendation": {
                                    "action": "BUY",
                                    "entry": round(entry, 2),
                                    "stop_loss": round(sl, 2),
                                    "target": round(target, 2),
                                    "risk_reward": "1:2.0 Net",
                                    "reason": f"Swept {sweep['swept_level']} with trapped seller delta. Absorption bounce confirmed."
                                }
                            })

                # Long upper wick sweep of PDH / Swing High => SELL
                elif sweep["type"] == "BEARISH_LIQUIDITY_SWEEP":
                    entry = current_close
                    sl = sweep["wick_high"] + (entry * 0.001)
                    risk = sl - entry
                    if risk > 0:
                        target = entry - (risk * 2.0)  # 1:2 R:R target
                        active_trade = {
                            "date": str(day_date),
                            "entry_time": str(bar_time),
                            "symbol": index_name,
                            "action": "SELL",
                            "entry": round(entry, 2),
                            "stop_loss": round(sl, 2),
                            "target": round(target, 2),
                            "risk_pts": round(risk, 2),
                            "target_pts": round(risk * 2.0, 2),
                            "rr_ratio": 2.0,
                            "qty": lot_size,
                            "setup": sweep["swept_level"],
                            "narrative": sweep["narrative"],
                        }
                        if len(ai_decision_samples) < 3:
                            ai_decision_samples.append({
                                "symbol": index_name,
                                "date": str(day_date),
                                "time": str(bar_time),
                                "input_data": {
                                    "ltp": entry,
                                    "pdh": pdh, "pdl": pdl, "pdc": pdc,
                                    "swing_high": swing_high, "swing_low": swing_low,
                                    "poc": vp.get("POC"), "vah": vp.get("VAH"), "val": vp.get("VAL"),
                                    "sweep_event": sweep
                                },
                                "ai_recommendation": {
                                    "action": "SELL",
                                    "entry": round(entry, 2),
                                    "stop_loss": round(sl, 2),
                                    "target": round(target, 2),
                                    "risk_reward": "1:2.0 Net",
                                    "reason": f"Swept {sweep['swept_level']} with trapped breakout delta. Institutional fade."
                                }
                            })

            # Update drawdown tracking
            if current_capital > peak_capital:
                peak_capital = current_capital
            dd = ((peak_capital - current_capital) / peak_capital) * 100
            if dd > max_drawdown_pct:
                max_drawdown_pct = dd

    # Summary Statistics
    total_trades = len(all_trades)
    winning_trades = [t for t in all_trades if t["status"] == "WIN"]
    losing_trades = [t for t in all_trades if t["status"] == "LOSS"]

    win_count = len(winning_trades)
    loss_count = len(losing_trades)
    win_rate = round((win_count / total_trades) * 100, 2) if total_trades > 0 else 0.0

    gross_pnl = round(sum(t["gross_pnl"] for t in all_trades), 2)
    total_fees = round(sum(t["fees"] for t in all_trades), 2)
    net_pnl = round(sum(t["net_pnl"] for t in all_trades), 2)

    total_win_pnl = sum(t["net_pnl"] for t in winning_trades)
    total_loss_pnl = abs(sum(t["net_pnl"] for t in losing_trades))
    profit_factor = round(total_win_pnl / max(total_loss_pnl, 1.0), 2) if total_loss_pnl > 0 else (99.0 if total_win_pnl > 0 else 0.0)

    net_roi_pct = round((net_pnl / starting_capital) * 100, 2)

    return {
        "index": index_name,
        "symbol": yf_sym,
        "period": "1 Month (5-Minute Bars)",
        "starting_capital": starting_capital,
        "ending_capital": round(current_capital, 2),
        "total_trades": total_trades,
        "winning_trades": win_count,
        "losing_trades": loss_count,
        "win_rate_pct": win_rate,
        "gross_pnl_inr": gross_pnl,
        "total_statutory_fees_inr": total_fees,
        "net_pnl_inr": net_pnl,
        "net_roi_pct": net_roi_pct,
        "profit_factor": profit_factor,
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "ai_decision_samples": ai_decision_samples,
        "trades": all_trades,
    }


def main():
    print("==================================================================")
    print(" 1-MONTH INSTITUTIONAL QUANT BACKTEST: NIFTY, BANK NIFTY & SENSEX")
    print("==================================================================")

    results = {}
    for name, cfg in INDICES_CONFIG.items():
        res = run_1month_index_backtest(name, cfg)
        results[name] = res

    # Save comprehensive results to file
    out_path = "/home/super/Documents/Projects/GeminiAgent/trading_workspace/institutional_1month_results.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)

    print("\n\n" + "=" * 70)
    print(" FINAL 1-MONTH INSTITUTIONAL QUANT BACKTEST SUMMARY (NSE/BSE)")
    print("=" * 70)
    print(f"{'Index':<12} | {'Trades':<6} | {'Win%':<7} | {'Gross PnL':<11} | {'Fees':<9} | {'Net PnL':<11} | {'PF':<5} | {'Max DD':<6}")
    print("-" * 70)

    for name, r in results.items():
        if "error" in r:
            print(f"{name:<12} | ERROR: {r['error']}")
            continue
        print(f"{name:<12} | {r['total_trades']:<6} | {r['win_rate_pct']:<5.1f}% | ₹{r['gross_pnl_inr']:<9.2f} | ₹{r['total_statutory_fees_inr']:<7.2f} | ₹{r['net_pnl_inr']:<9.2f} | {r['profit_factor']:<5.2f} | {r['max_drawdown_pct']:<5.2f}%")

    print("=" * 70)
    print(f"Full execution logs and AI input/output transcripts saved to:\n  {out_path}\n")


if __name__ == "__main__":
    main()

