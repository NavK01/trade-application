"""
1-Month Real-Data Institutional Trading Audit on NSE Liquid Universe.
Period: 2026-10-01 to 2026-11-04 (23 Trading Days).
Capital: ₹10,000.00 INR.
Full Indian Statutory Taxes & Brokerage deducted on every execution.
"""

import os
import sys
import datetime
from typing import Dict, Any, List
import pandas as pd
import numpy as np

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.trading.institutional_engine import (
    calculate_indian_statutory_charges,
    detect_liquidity_sweeps_and_absorption,
)

SYMBOLS = ["SBIN", "ITC", "TATASTEEL", "INFY", "ICICIBANK", "AXISBANK", "NTPC", "COALINDIA"]

def run_1month_audit():
    workspace = os.path.dirname(os.path.abspath(__file__))
    
    # Load CSVs
    dfs = {}
    for sym in SYMBOLS:
        csv_file = os.path.join(workspace, f"real_5m_{sym}.csv")
        if not os.path.exists(csv_file):
            print(f"Error: Missing {csv_file}")
            return
        df = pd.read_csv(csv_file)
        df["dt"] = pd.to_datetime(df["Datetime"])
        df["date"] = df["dt"].dt.date
        df["time"] = df["dt"].dt.time
        dfs[sym] = df

    # 23 Trading Days from 2026-10-01 to 2026-11-04
    sbin_df = dfs["SBIN"]
    all_dates = sorted(list(sbin_df["date"].unique()))
    eval_dates = [d for d in all_dates if d >= datetime.date(2026, 10, 1)]

    print(f"Loaded {len(eval_dates)} trading days from {eval_dates[0]} to {eval_dates[-1]}")

    initial_capital = 10000.0
    available_cash = initial_capital
    all_trades = []
    daily_reports = []
    peak_equity = initial_capital
    max_drawdown_pct = 0.0

    for d_idx, today in enumerate(eval_dates):
        prev_date_idx = all_dates.index(today) - 1
        prev_date = all_dates[prev_date_idx] if prev_date_idx >= 0 else None

        day_start_equity = available_cash
        day_realized_pnl = 0.0
        day_trades = []
        open_positions = {}
        day_profit_locked = False
        day_circuit_tripped = False

        # Get today's unique times across SBIN
        today_sbin = sbin_df[sbin_df["date"] == today]
        today_times = sorted(list(today_sbin["time"].unique()))
        last_time = today_times[-1] if today_times else datetime.time(15, 15)

        # Precompute Previous Day High, Low, Close for all symbols
        prev_day_levels = {}
        for sym in SYMBOLS:
            sym_df = dfs[sym]
            if prev_date:
                p_df = sym_df[sym_df["date"] == prev_date]
                if not p_df.empty:
                    prev_day_levels[sym] = {
                        "PDH": float(p_df["High"].max()),
                        "PDL": float(p_df["Low"].min()),
                        "PDC": float(p_df["Close"].iloc[-1]),
                    }
                else:
                    prev_day_levels[sym] = {"PDH": 0.0, "PDL": 0.0, "PDC": 0.0}
            else:
                prev_day_levels[sym] = {"PDH": 0.0, "PDL": 0.0, "PDC": 0.0}

        # Iterate through today's 5m bars
        for bar_time in today_times:
            is_eod_square_off = (bar_time == last_time or bar_time >= datetime.time(15, 15))
            is_entry_window = (bar_time >= datetime.time(9, 30) and bar_time <= datetime.time(14, 45))

            # 1. Manage Existing Open Positions
            for sym in list(open_positions.keys()):
                pos = open_positions[sym]
                sym_df = dfs[sym]
                bar_match = sym_df[(sym_df["date"] == today) & (sym_df["time"] == bar_time)]
                if bar_match.empty:
                    continue
                bar = bar_match.iloc[0]
                high_p = float(bar["High"])
                low_p = float(bar["Low"])
                close_p = float(bar["Close"])

                exit_reason = None
                exit_price = close_p

                if pos["action"] == "BUY":
                    if high_p >= pos["tp"]:
                        exit_reason = "TARGET_HIT"
                        exit_price = pos["tp"]
                    elif low_p <= pos["sl"]:
                        exit_reason = "STOP_LOSS"
                        exit_price = pos["sl"]
                    elif is_eod_square_off:
                        exit_reason = "EOD_SQUARE_OFF"
                        exit_price = close_p
                else: # SELL
                    if low_p <= pos["tp"]:
                        exit_reason = "TARGET_HIT"
                        exit_price = pos["tp"]
                    elif high_p >= pos["sl"]:
                        exit_reason = "STOP_LOSS"
                        exit_price = pos["sl"]
                    elif is_eod_square_off:
                        exit_reason = "EOD_SQUARE_OFF"
                        exit_price = close_p

                if exit_reason:
                    qty = pos["qty"]
                    entry_p = pos["entry"]
                    gross_pnl = (exit_price - entry_p) * qty if pos["action"] == "BUY" else (entry_p - exit_price) * qty
                    buy_val = (entry_p if pos["action"] == "BUY" else exit_price) * qty
                    sell_val = (exit_price if pos["action"] == "BUY" else entry_p) * qty
                    charges = calculate_indian_statutory_charges(buy_val, sell_val, instrument_type="INTRADAY_EQUITY", quantity=qty)
                    fees = charges["total_charges"]
                    net_pnl = round(gross_pnl - fees, 2)

                    available_cash += (pos["cost_basis"] + net_pnl)
                    day_realized_pnl += net_pnl

                    trade_data = {
                        "trade_num": len(all_trades) + 1,
                        "date": str(today),
                        "symbol": sym,
                        "action": pos["action"],
                        "qty": qty,
                        "entry_time": str(pos["entry_time"]),
                        "entry_price": entry_p,
                        "exit_time": str(bar_time),
                        "exit_price": exit_price,
                        "gross_pnl": round(gross_pnl, 2),
                        "fees": fees,
                        "net_pnl": net_pnl,
                        "return_pct": round((net_pnl / pos["cost_basis"]) * 100, 2),
                        "exit_reason": exit_reason,
                        "status": "WIN" if net_pnl > 0 else "LOSS",
                    }
                    day_trades.append(trade_data)
                    all_trades.append(trade_data)
                    del open_positions[sym]

            # 2. Risk Evaluation
            if day_realized_pnl >= 100.0:
                day_profit_locked = True
            elif day_realized_pnl <= -300.0:
                day_circuit_tripped = True
                # Square off remaining positions immediately
                for sym in list(open_positions.keys()):
                    pos = open_positions[sym]
                    sym_df = dfs[sym]
                    bar_match = sym_df[(sym_df["date"] == today) & (sym_df["time"] == bar_time)]
                    close_p = float(bar_match.iloc[0]["Close"]) if not bar_match.empty else pos["entry"]
                    qty = pos["qty"]
                    entry_p = pos["entry"]
                    gross_pnl = (close_p - entry_p) * qty if pos["action"] == "BUY" else (entry_p - close_p) * qty
                    buy_val = (entry_p if pos["action"] == "BUY" else close_p) * qty
                    sell_val = (close_p if pos["action"] == "BUY" else entry_p) * qty
                    charges = calculate_indian_statutory_charges(buy_val, sell_val, instrument_type="INTRADAY_EQUITY", quantity=qty)
                    fees = charges["total_charges"]
                    net_pnl = round(gross_pnl - fees, 2)
                    available_cash += (pos["cost_basis"] + net_pnl)
                    day_realized_pnl += net_pnl
                    trade_data = {
                        "trade_num": len(all_trades) + 1,
                        "date": str(today),
                        "symbol": sym,
                        "action": pos["action"],
                        "qty": qty,
                        "entry_time": str(pos["entry_time"]),
                        "entry_price": entry_p,
                        "exit_time": str(bar_time),
                        "exit_price": close_p,
                        "gross_pnl": round(gross_pnl, 2),
                        "fees": fees,
                        "net_pnl": net_pnl,
                        "return_pct": round((net_pnl / pos["cost_basis"]) * 100, 2),
                        "exit_reason": "CIRCUIT_BREAKER_HALT",
                        "status": "WIN" if net_pnl > 0 else "LOSS",
                    }
                    day_trades.append(trade_data)
                    all_trades.append(trade_data)
                    del open_positions[sym]

            # 3. Search for Entry Signals
            if is_entry_window and not day_profit_locked and not day_circuit_tripped and len(open_positions) < 3:
                for sym in SYMBOLS:
                    if sym in open_positions or len(open_positions) >= 3:
                        continue
                    sym_df = dfs[sym]
                    # Direct index lookup using searchsorted or boolean
                    match_indices = sym_df[(sym_df["date"] == today) & (sym_df["time"] == bar_time)].index
                    if match_indices.empty:
                        continue
                    curr_idx = match_indices[0]
                    # Use rolling window of 60 bars
                    sub_df = sym_df.iloc[max(0, curr_idx - 60):curr_idx + 1]
                    if len(sub_df) < 15:
                        continue

                    spot = float(sub_df.iloc[-1]["Close"])
                    pd_lvl = prev_day_levels[sym]
                    pdh = pd_lvl["PDH"]
                    pdl = pd_lvl["PDL"]
                    pdc = pd_lvl["PDC"]

                    # 1H Swings (last 12 bars)
                    swing_high = float(sub_df.tail(12)["High"].max())
                    swing_low = float(sub_df.tail(12)["Low"].min())

                    # Trend Filter (EMA 20 & EMA 50)
                    ema20 = float(sub_df["Close"].ewm(span=20, adjust=False).mean().iloc[-1])
                    ema50 = float(sub_df["Close"].ewm(span=50, adjust=False).mean().iloc[-1])
                    trend_bullish = (spot >= ema20) or (ema20 >= ema50)
                    trend_bearish = (spot < ema20) and (ema20 <= ema50)

                    sweeps = detect_liquidity_sweeps_and_absorption(
                        sub_df, pdh=pdh, pdl=pdl, pdc=pdc, swing_high=swing_high, swing_low=swing_low
                    )
                    recent_sweep = sweeps[-1] if sweeps else None

                    action = None
                    sl = None
                    tp = None

                    if recent_sweep:
                        if recent_sweep["type"] == "BULLISH_LIQUIDITY_SWEEP" and trend_bullish:
                            action = "BUY"
                            sl = round(recent_sweep["wick_low"] - (spot * 0.0015), 2)
                            risk = max(spot - sl, spot * 0.006)
                            sl = round(spot - risk, 2)
                            tp = round(spot + (risk * 2.0), 2)
                        elif recent_sweep["type"] == "BEARISH_LIQUIDITY_SWEEP" and trend_bearish:
                            action = "SELL"
                            sl = round(recent_sweep["wick_high"] + (spot * 0.0015), 2)
                            risk = max(sl - spot, spot * 0.006)
                            sl = round(spot + risk, 2)
                            tp = round(spot - (risk * 2.0), 2)

                    if action and sl and tp:
                        # Sizing: Max ₹2,500 - ₹3,000 per position (<= 30% of ₹10k capital)
                        alloc = min(2800.0, available_cash)
                        qty = int(alloc // spot)
                        if qty >= 1 and (qty * spot) <= 3000.0 and (qty * spot) <= available_cash:
                            cost = qty * spot
                            available_cash -= cost
                            open_positions[sym] = {
                                "action": action,
                                "qty": qty,
                                "entry": spot,
                                "cost_basis": cost,
                                "sl": sl,
                                "tp": tp,
                                "entry_time": bar_time,
                            }

        # Clean safety check: ensure no stranded positions at end of day
        if open_positions:
            for sym in list(open_positions.keys()):
                pos = open_positions[sym]
                sym_df = dfs[sym]
                last_bar = sym_df[sym_df["date"] == today].iloc[-1]
                close_p = float(last_bar["Close"])
                qty = pos["qty"]
                entry_p = pos["entry"]
                gross_pnl = (close_p - entry_p) * qty if pos["action"] == "BUY" else (entry_p - close_p) * qty
                buy_val = (entry_p if pos["action"] == "BUY" else close_p) * qty
                sell_val = (close_p if pos["action"] == "BUY" else entry_p) * qty
                charges = calculate_indian_statutory_charges(buy_val, sell_val, instrument_type="INTRADAY_EQUITY", quantity=qty)
                fees = charges["total_charges"]
                net_pnl = round(gross_pnl - fees, 2)
                available_cash += (pos["cost_basis"] + net_pnl)
                day_realized_pnl += net_pnl
                trade_data = {
                    "trade_num": len(all_trades) + 1,
                    "date": str(today),
                    "symbol": sym,
                    "action": pos["action"],
                    "qty": qty,
                    "entry_time": str(pos["entry_time"]),
                    "entry_price": entry_p,
                    "exit_time": str(last_time),
                    "exit_price": close_p,
                    "gross_pnl": round(gross_pnl, 2),
                    "fees": fees,
                    "net_pnl": net_pnl,
                    "return_pct": round((net_pnl / pos["cost_basis"]) * 100, 2),
                    "exit_reason": "EOD_CLEAN_SQUARE_OFF",
                    "status": "WIN" if net_pnl > 0 else "LOSS",
                }
                day_trades.append(trade_data)
                all_trades.append(trade_data)
                del open_positions[sym]

        # End of day tracking
        if available_cash > peak_equity:
            peak_equity = available_cash
        dd = ((peak_equity - available_cash) / peak_equity) * 100.0
        if dd > max_drawdown_pct:
            max_drawdown_pct = dd

        wins_today = sum(1 for t in day_trades if t["status"] == "WIN")
        losses_today = sum(1 for t in day_trades if t["status"] == "LOSS")
        gross_today = sum(t["gross_pnl"] for t in day_trades)
        fees_today = sum(t["fees"] for t in day_trades)

        daily_reports.append({
            "day_num": d_idx + 1,
            "date": str(today),
            "start_equity": round(day_start_equity, 2),
            "trades_count": len(day_trades),
            "wins": wins_today,
            "losses": losses_today,
            "win_rate_pct": round((wins_today / len(day_trades) * 100), 1) if day_trades else 0.0,
            "gross_pnl": round(gross_today, 2),
            "fees": round(fees_today, 2),
            "net_pnl": round(day_realized_pnl, 2),
            "day_return_pct": round((day_realized_pnl / day_start_equity) * 100, 2),
            "end_equity": round(available_cash, 2),
        })

    # Output Results
    print("\n" + "="*95)
    print(" 📅 1-MONTH DAY-BY-DAY PERFORMANCE (OCTOBER 1, 2026 - NOVEMBER 4, 2026)")
    print("="*95)
    print(f"{'Day':<4} | {'Date':<10} | {'Trades':<6} | {'W/L':<5} | {'Win%':<6} | {'Gross PnL':<10} | {'Fees':<7} | {'Net PnL':<10} | {'Day Ret%':<8} | {'Equity':<10}")
    print("-" * 95)
    for r in daily_reports:
        print(f"{r['day_num']:<4} | {r['date']:<10} | {r['trades_count']:<6} | {r['wins']}/{r['losses']:<3} | {r['win_rate_pct']:<5.1f}% | ₹{r['gross_pnl']:<8.2f} | ₹{r['fees']:<5.2f} | ₹{r['net_pnl']:<8.2f} | {r['day_return_pct']:<7.2f}% | ₹{r['end_equity']:<8.2f}")
    print("=" * 95)

    print("\n" + "="*120)
    print(" 📋 1-MONTH EXECUTED TRADES BREAKDOWN (PER-TRADE AUDIT)")
    print("="*120)
    print(f"{'#':<3} | {'Date':<10} | {'Symbol':<9} | {'Side':<4} | {'Qty':<4} | {'Entry':<8} | {'Exit':<8} | {'Gross PnL':<10} | {'Fees':<7} | {'Net PnL':<9} | {'Return%':<7} | {'Exit Reason':<20} | {'Status'}")
    print("-" * 120)
    for t in all_trades:
        print(f"{t['trade_num']:<3} | {t['date']:<10} | {t['symbol']:<9} | {t['action']:<4} | {t['qty']:<4} | ₹{t['entry_price']:<7.2f} | ₹{t['exit_price']:<7.2f} | ₹{t['gross_pnl']:<8.2f} | ₹{t['fees']:<5.2f} | ₹{t['net_pnl']:<7.2f} | {t['return_pct']:<6.2f}% | {t['exit_reason']:<20} | {t['status']}")
    print("=" * 120)

    # Overall summary
    total_trades = len(all_trades)
    winning_trades = [t for t in all_trades if t["status"] == "WIN"]
    losing_trades = [t for t in all_trades if t["status"] == "LOSS"]
    win_count = len(winning_trades)
    loss_count = len(losing_trades)
    gross_pnl = sum(t["gross_pnl"] for t in all_trades)
    total_fees = sum(t["fees"] for t in all_trades)
    total_net_pnl = sum(t["net_pnl"] for t in all_trades)
    tot_win_pnl = sum(t["net_pnl"] for t in winning_trades)
    tot_loss_pnl = abs(sum(t["net_pnl"] for t in losing_trades))
    profit_factor = round(tot_win_pnl / max(tot_loss_pnl, 1.0), 2) if tot_loss_pnl > 0 else 99.0

    print("\n" + "="*60)
    print(" 🏆 1-MONTH COMPREHENSIVE PERFORMANCE SUMMARY")
    print("="*60)
    print(f" Initial Capital:            ₹{initial_capital:,.2f} INR")
    print(f" Ending Equity:              ₹{available_cash:,.2f} INR")
    print(f" Cumulative Net P&L:         ₹{total_net_pnl:+,.2f} INR")
    print(f" Net Return on Capital:      {round((total_net_pnl/initial_capital)*100, 2):+.2f}%")
    print(f" Total Trades Executed:      {total_trades}")
    print(f" Winning Trades:             {win_count} ({round((win_count/total_trades*100) if total_trades else 0, 1)}%)")
    print(f" Losing Trades:              {loss_count} ({round((loss_count/total_trades*100) if total_trades else 0, 1)}%)")
    print(f" Gross Profit / Loss:        ₹{gross_pnl:+,.2f} INR")
    print(f" Total Statutory Fees Paid:  ₹{total_fees:,.2f} INR")
    print(f" Profit Factor:              {profit_factor}")
    print(f" Max Drawdown:               {round(max_drawdown_pct, 2):.2f}% (Limit: 3.0%)")
    print("="*60 + "\n")

if __name__ == "__main__":
    run_1month_audit()
