"""
EMA Crossover Backtest Strategy with Risk Analytics.
Can be executed directly or optimized/debugged by the Gemini Agent.
"""
import sys
import numpy as np
import pandas as pd

def generate_synthetic_market_data(days: int = 180, initial_price: float = 65000.0, seed: int = 42) -> pd.DataFrame:
    """Generate realistic BTC/USDT price data using Geometric Brownian Motion."""
    np.random.seed(seed)
    # 1 hour intervals
    periods = days * 24
    dt = 1 / 365 / 24
    mu = 0.05  # Drift
    sigma = 0.65  # 65% annualized volatility
    
    returns = np.random.normal(loc=(mu - 0.5 * sigma**2) * dt, scale=sigma * np.sqrt(dt), size=periods)
    price_series = initial_price * np.exp(np.cumsum(returns))
    
    dates = pd.date_range(end=pd.Timestamp.now(), periods=periods, freq="1h")
    
    df = pd.DataFrame({
        "timestamp": dates,
        "close": price_series,
        "open": price_series * (1 + np.random.normal(0, 0.001, periods)),
        "high": price_series * (1 + np.abs(np.random.normal(0, 0.003, periods))),
        "low": price_series * (1 - np.abs(np.random.normal(0, 0.003, periods))),
        "volume": np.random.uniform(50, 500, periods)
    })
    return df

def run_backtest(df: pd.DataFrame, fast_period: int = 9, slow_period: int = 21, fee_rate: float = 0.0006) -> dict:
    """Run an Exponential Moving Average (EMA) Crossover Backtest."""
    df = df.copy()
    df["fast_ema"] = df["close"].ewm(span=fast_period, adjust=False).mean()
    df["slow_ema"] = df["close"].ewm(span=slow_period, adjust=False).mean()

    # Position: 1 for long, 0 for flat
    df["signal"] = 0
    df.loc[df["fast_ema"] > df["slow_ema"], "signal"] = 1
    # Trade trigger: 1 when entering long, -1 when exiting
    df["position"] = df["signal"].shift(1).fillna(0)
    df["trades"] = df["position"].diff().abs()

    # Log returns
    df["market_return"] = df["close"].pct_change().fillna(0)
    # Strategy return minus fee on position changes
    df["strategy_return"] = (df["position"] * df["market_return"]) - (df["trades"] * fee_rate)

    # Cumulative return
    df["equity_curve"] = (1 + df["strategy_return"]).cumprod()
    
    total_return = (df["equity_curve"].iloc[-1] - 1) * 100
    
    # Calculate Max Drawdown
    running_max = df["equity_curve"].cummax()
    drawdown = (df["equity_curve"] - running_max) / running_max
    max_drawdown = drawdown.min() * 100
    
    # Calculate Sharpe Ratio (annualized for hourly data)
    strat_ret = df["strategy_return"]
    if strat_ret.std() > 0:
        sharpe_ratio = (strat_ret.mean() / strat_ret.std()) * np.sqrt(24 * 365)
    else:
        sharpe_ratio = 0.0

    # Win rate
    trade_returns = []
    in_pos = False
    entry_price = 0.0
    for idx, row in df.iterrows():
        if row["position"] == 1 and not in_pos:
            in_pos = True
            entry_price = row["close"]
        elif row["position"] == 0 and in_pos:
            in_pos = False
            ret = (row["close"] - entry_price) / entry_price - (2 * fee_rate)
            trade_returns.append(ret)

    win_count = sum(1 for r in trade_returns if r > 0)
    total_trades = len(trade_returns)
    win_rate = (win_count / total_trades * 100) if total_trades > 0 else 0.0

    return {
        "fast_period": fast_period,
        "slow_period": slow_period,
        "total_candles": len(df),
        "total_trades": total_trades,
        "win_rate_pct": round(win_rate, 2),
        "total_return_pct": round(total_return, 2),
        "max_drawdown_pct": round(max_drawdown, 2),
        "sharpe_ratio": round(sharpe_ratio, 2),
    }

if __name__ == "__main__":
    print("=== Running EMA Crossover Quantitative Backtest ===")
    df = generate_synthetic_market_data(days=90)
    results = run_backtest(df, fast_period=9, slow_period=21)
    
    print("-" * 50)
    for k, v in results.items():
        print(f"{k:20s}: {v}")
    print("-" * 50)
    print("Backtest finished with exit code 0.")
    sys.exit(0)
