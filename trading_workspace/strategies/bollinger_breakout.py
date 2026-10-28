"""
Bollinger Bands & Volume Breakout Strategy for Indian Equities.
Demonstrates building on BaseStrategy with sub-millisecond execution.
"""

import pandas as pd
from trading_workspace.strategies.base_strategy import BaseStrategy


class BollingerBreakoutStrategy(BaseStrategy):
    """
    Bollinger Bands Volatility Breakout:
    - Long entry: Close breaks above Upper Band with Volume > 1.2 * 20-period Average Volume.
    - Short/Exit entry: Close breaks below Middle Band (SMA 20) or Lower Band.
    """

    def __init__(self, symbol: str = "RELIANCE", window: int = 20, num_std: float = 2.0):
        super().__init__(name="BollingerBreakout", symbol=symbol, timeframe="15m")
        self.window = window
        self.num_std = num_std

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        # Bollinger Bands calculation
        df["sma"] = df["close"].rolling(window=self.window).mean()
        df["std"] = df["close"].rolling(window=self.window).std()
        df["upper"] = df["sma"] + (df["std"] * self.num_std)
        df["lower"] = df["sma"] - (df["std"] * self.num_std)
        df["vol_sma"] = df["volume"].rolling(window=self.window).mean()

        df["signal"] = 0
        long_condition = (df["close"] > df["upper"]) & (df["volume"] > df["vol_sma"] * 1.1)
        exit_condition = df["close"] < df["sma"]

        df.loc[long_condition, "signal"] = 1
        df.loc[exit_condition, "signal"] = 0

        # Forward fill active positions
        df["signal"] = df["signal"].replace(0, float("nan")).ffill().fillna(0)
        return df


if __name__ == "__main__":
    strategy = BollingerBreakoutStrategy(symbol="RELIANCE")
    data = strategy.fetch_data(period="30d")
    results = strategy.backtest(data)
    print("=== Bollinger Breakout Backtest Results ===")
    for k, v in results.items():
        print(f"  {k}: {v}")

