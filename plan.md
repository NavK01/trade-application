# Master Quantitative & Engineering Execution Plan (`plan.md`)

**Objective:** Transform the Indian Market (NSE/BSE) Trading Application on a **₹10,000 INR capital base** into a genuinely profitable, walk-forward validated, fee-aware quantitative trading system powered by deterministic market-microstructure edges and **DeepSeek V4.1 Flash MaaS** (`deepseek-ai/deepseek-v4.1-flash-maas`).

---

## 1. Codebase Audit & Current Architecture

### 1.1 Core Trading & Execution Modules (`app/trading/`)
| File | Role in System | Current Status & Audit Findings |
| :--- | :--- | :--- |
| `app/trading/backtester.py` | Honest next-bar-open historical backtester with slippage, statutory fees, gap-through stop handling, daily locks, and AI veto hook. | **Updated & Verified:** Fixed a critical look-ahead bug in `prior_day` OHLC mapping (now maps to true $t-1$ trading day instead of current day $t$). Added percentage-anchored geometry (`stop_pct`, `target_pct`) and multi-stage next-bar **profit-lock ratchet** (`ratchet_levels`). |
| `app/trading/costs.py` | Zerodha-style NSE cash intraday transaction cost calculator (`calculate_buy_fee`, `calculate_sell_fee`). | **Verified:** Charges 0.03% brokerage (capped at ₹20), 0.025% sell STT, 0.00297% NSE exchange fee, 0.0001% SEBI fee, 0.003% buy stamp duty, and 18% GST (~0.106% round-trip before slippage). |
| `app/trading/institutional_engine.py` | Pre-market 4-Lines (`PDH`, `PDL`, `PDC`, 1H Swings), Volume Profile (`POC`, `VAH`, `VAL`), Fibonacci zones, naive GEX regime, liquidity sweeps, and statutory fee calculator. | **Updated & Verified:** Added `DELIVERY_EQUITY` statutory support (0.1% STT on both legs, ₹13.50 DP sell charge, 0.015% buy stamp duty) alongside `INTRADAY_EQUITY`, `FUTURES`, and `OPTIONS`. |
| `app/trading/fee_aware_strategies.py` | **New** standalone fee-aware intraday strategy module built from walk-forward forensics. | **Implemented & Tested:** Contains `FeeAwareOpeningPullback` (1h opening EMA-20 pullback), `LiquiditySweepLong` (5m PDL stop-hunt reclaim), `compute_features`, and multi-stage profit-lock ratchet validation. |
| `app/trading/swing_strategies.py` | **New** multi-day positional/swing strategy module and delivery-cost backtester. | **Implemented & Tested:** Contains `RSI2OversoldSwing` (2-period RSI exhaustion bounce with 2x ATR stop / 4x ATR target) and `SwingBacktestEngine` with full `DELIVERY_EQUITY` statutory fee accounting. |
| `app/trading/intraday_strategies.py` | Legacy 8-strategy intraday suite (`momentum_continuation`, `orb`, `vwap`, `ema_ribbon`, `supertrend`, `pivots`, `bollinger`, `rsi`). | **Audited:** Primary strategy (`momentum_continuation`) holds to EOD (`hold_to_close=True`), causing 80%+ of winners to give back gains before 15:20 IST and lose to statutory fees. |
| `app/trading/farom_strategy.py` | Fee-Aware Opening-Range Momentum (`FeeAwareOpeningRangeMomentum`). | **Audited:** Replaces `hold_to_close` with a fee-clearing target ($1.6\text{R}$) and minimum opening-range width gate (`min_or_width_pct >= 0.4%`). |
| `app/trading/gemini_strategy.py` | Calibrated institutional sweep & value-area bounce evaluator with lunch-hour block (`11:30–13:15 IST`). | **Audited:** Uses live clock (`datetime.now(IST)`), making it suitable for live polling but requiring timestamp injection for historical bar replay. |
| `app/trading/ai_decision.py` | `AIDecisionEngine` allowing DeepSeek to propose full trade geometry (`entry`, `stop`, `target`) within hard risk rails. | **Audited:** Long-only; enforces `min_edge_to_fee >= 2.0` and `min_rr >= 1.2`. High API call count if invoked on every bar without a deterministic pre-filter. |
| `app/trading/continuous_optimizer.py` | 3-Phase autonomous self-healing loop (Phase 1: 1Y 1h Zero-AI; Phase 2: 1M 5m DeepSeek OOS; Phase 3: Gemini 4 parameter mutation) + `TokenBudgetGuard`. | **Audited:** Cycles 1–108 plateaued because Phase 1 & Phase 2 were locked to the same baseline entry logic without multi-stage ratchets or broad-universe validation. |
| `app/trading/risk_manager.py` & `broker.py` | Live/paper execution gates, account state persistence (`SQLite`), daily loss lock (`3.0%`), daily profit lock (`1.0%`), and EOD square-off (`15:20 IST`). | **Verified:** Works cleanly across `deepseek_flash`, `opus_flash`, and `gemini_optimized` accounts. |

---

## 2. Quantitative Forensics: Why Intraday Setups Struggled & How We Fix It

Across **Iterations 1–114** (documented in `trading_workspace/INTRADAY_STRATEGY_LEARNING_LOG.md` and `trading_workspace/iteration109–114_*.json`), our empirical backtests uncovered four root causes of unprofitability:

### 2.1 The Friction vs. Move-Size Asymmetry (The #1 Profit Killer)
* On a ₹10,000 account taking a ₹2,800–₹9,000 position, NSE intraday statutory costs (`0.106%` round-trip) plus realistic paper slippage (`5 bps` per leg = `0.10%` round-trip) create a **hard breakeven hurdle of `0.206%` of notional value per trade**.
* In the baseline 1-year/1-hour backtest (`Iteration 108`), the engine generated **+₹490.06 gross P&L** across 108 trades, but paid **₹789.51 in fees**, ending at **−₹299.45 net**.
* **Why:** 87 of 108 trades exited at `EOD_SQUARE_OFF` (15:20 IST). Intraday Indian equities mean-revert heavily in the afternoon; holding unratcheted positions to EOD converts midday +0.50% winners into +0.05% scratch trades that lose money after fees.

### 2.2 Small-Universe Selection Bias vs. Broad Generalization
* In `Iteration 109`, tuning `FeeAwareOpeningPullback` (1h) and `LiquiditySweepLong` (5m) on the 8-stock `LIQUID_NSE_EQUITIES` watchlist produced **80.0% win rate (+₹255.79, PF 4.70)** on 1y/1h and **90.0% win rate (+₹166.22, PF 3.93)** on 1mo/5m.
* However, when tested untouched in `Iteration 110` across a **broad 30-symbol Nifty 50/100 universe**, win rates dropped to **44.4% (1h)** and **56.5% (5m)** with negative net P&L.
* **Why:** Indicator thresholds alone cannot separate a stock *in play* (institutional order flow, catalyst, sector leadership) from a stock drifting in random noise.

### 2.3 Multi-Day Swing vs. Single-Session Intraday Expectancy
* In `Iterations 113–114`, we tested 5-year daily bars across the same 30 NSE large-caps split into **4 sequential walk-forward folds** (309 trading days per fold), charging full `DELIVERY_EQUITY` statutory costs (0.1% STT both legs + ₹13.50 DP fee + 0.015% stamp duty).
* **Result:** `RSI2OversoldSwing` (`rsi_2 < 10` or `< 15`, `close > close_prev`, `2x ATR` stop, `3x–4x ATR` target) was **net profitable in ALL FOUR 5-year folds** (`F1: +0.78%/trade, PF 1.32 | F2: +1.36%/trade, PF 1.67 | F3: +0.23%/trade, PF 1.08 | F4: +0.32%/trade, PF 1.13`).
* **Why:** Multi-day mean-reversion moves span `2.5%–6.0%`, making the ~`0.40%` delivery cost hurdle a small fraction of the gross edge.

---

## 3. Portfolio of Strategies: Implemented & To Implement

To make the ₹10,000 account consistently profitable across market regimes, we combine **already-implemented fee-aware modules** with **four high-conviction structural upgrades**:

### 3.1 Already Implemented & Unit-Tested (`app/trading/`)
1. **Strategy 1 — Fee-Aware Opening EMA-20 Pullback (`FeeAwareOpeningPullback` in `app/trading/fee_aware_strategies.py`)**
   * **Timeframe:** 1h bars (`09:15 IST` opening session bar).
   * **Conditions:** `atr_pct >= 0.75%`, `ema_20 >= ema_50`, `ema50_slope >= 0.05%`, `low <= ema_20 * 1.002`, `close >= ema_20`, `body_pct in [0.30%, 2.20%]`, `lower_wick_ratio >= 0.035`, `upper_wick_ratio <= 0.15`.
   * **Exit Geometry:** Target `+0.90%`, Stop `−0.95%`, Ratchet at `+0.28% -> lock +0.16%`.
2. **Strategy 2 — Bullish Prior-Day-Low Liquidity Sweep (`LiquiditySweepLong` in `app/trading/fee_aware_strategies.py`)**
   * **Timeframe:** 5m bars (`09:30–14:00 IST`, excluding `11:30–13:15 IST` midday lull).
   * **Conditions:** Price sweeps below `PDL` (`low < pdl`) and reclaims above `PDL` (`close > pdl`), `lower_wick_ratio >= 0.42`, green body (`body_pct > 0`), `atr_pct >= 0.18%`.
   * **Exit Geometry:** Target `+0.90%`, Stop `−0.60%`, 3-stage ratchet (`+0.32% -> +0.20%`, `+0.48% -> +0.32%`, `+0.65% -> +0.48%`).
3. **Strategy 3 — RSI-2 Multi-Day Exhaustion Swing (`RSI2OversoldSwing` in `app/trading/swing_strategies.py`)**
   * **Timeframe:** 1d bars (next-day open fill, multi-day hold up to 60 days).
   * **Conditions:** `rsi_2 < 10.0` (or `15.0`), bullish turn `close > close_prev`, delivery fee gate `edge_to_fee >= 2.0x`.
   * **Exit Geometry:** Stop `Entry − 2.0 × ATR(14)`, Target `Entry + 3.0–4.0 × ATR(14)`.

### 3.2 New High-Expectancy Strategies to Implement Next
4. **Strategy 4 — Relative-Strength (RS) + RVOL "In-Play" Intraday Momentum (`RelativeStrengthLeaderBreakout`)**
   * **Why It Solves Iteration 110:** Instead of trading all 30 stocks blindly, rank the universe every morning at `09:45 IST` by:
     1. **Relative Strength vs. Nifty 50 (`RS_30m`):** Stock first-30m return minus Nifty 50 first-30m return $\ge +0.60\%$.
     2. **Relative Volume (`RVOL`):** First-30m volume $\ge 1.8\times$ the 20-day average first-30m volume.
   * **Trigger:** First pullback to VWAP or 9-EMA after breaking the 30-minute Opening Range High (`ORH-30m`), with volume contraction on the pullback and expansion on the green reclaim candle.
   * **Exit:** `1.5%` target or multi-stage ratchet (`+0.45% -> +0.22%`, `+0.80% -> +0.55%`, `+1.15% -> +0.90%`).
5. **Strategy 5 — Bearish PDH Liquidity Sweep & VWAP Fade (`LiquiditySweepShort`)**
   * **Why It Matters:** Indian NSE Cash Intraday (MIS) permits short-selling equities without F&O margin! In bear/chop regimes (like Fold 1 & Fold 2 where long-only setups struggled), fading failed breakouts above `PDH` captures rapid panic flushes.
   * **Conditions:** `high > pdh` and `close < pdh`, `upper_wick_ratio >= 0.42`, red body (`body_pct <= -0.025%`), `rsi_14 >= 55` or strong bearish displacement, `09:30–14:00 IST` (excluding lunch chop).
   * **Support Required in `backtester.py`:** Add `SELL` (short) position lifecycle support in `HistoricalBacktestEngine.run_backtest` (entry sell at `next_bar["open"] * (1 - slippage)`, stop above entry, target below entry, downward ratchet, and buy-to-cover fee accounting).
6. **Strategy 6 — Volatility Contraction & NR7 Multi-Day Swing Breakout (`NR7SwingBreakout`)**
   * **Why It Complements Strategy 3:** While `RSI2OversoldSwing` buys dips in range/mean-reverting markets, `NR7SwingBreakout` captures explosive multi-day trend continuations.
   * **Conditions:** Stock `close > sma_50 > sma_200`, today's range (`high - low`) is the narrowest of the last 7 sessions (`NR7`), and volume dries up (`volume < 0.75 * vol_sma_20`).
   * **Trigger:** Next day stop-limit buy above the NR7 high (`high * 1.001`), stop below NR7 low (`max(nr7_low, entry - 1.5 * atr_14)`), target `+3.5 * atr_14` with trailing stop at `sma_10`.
7. **Strategy 7 — Regime-Gated Capital Allocator & AI Meta-Filter (`RegimePortfolioRouter`)**
   * **How It Works:**
     * Evaluates Nifty 50 daily & intraday regime (`close vs. sma_50`, `India VIX / ATR%`, and `Breadth`).
     * Allocates ₹10,000 across two buckets:
       * **Swing Bucket (up to ₹6,000–₹9,000 when overnight):** Takes A+ `RSI2OversoldSwing` and `NR7SwingBreakout` setups that clear delivery charges by $\ge 2.5\times$.
       * **Intraday MIS Bucket (up to 5x leverage on free cash during 09:30–14:30 IST):** Trades only Top-2 `RVOL + RS` stocks using `LiquiditySweepLong`, `LiquiditySweepShort`, and `FeeAwareOpeningPullback`.
     * **DeepSeek V4.1 Flash Role:** Runs strictly *after* a deterministic candidate passes all mathematical gates (`edge_to_fee >= 2.2`, `fee_to_risk <= 20%`). DeepSeek checks for hard invalidations (violent counter-bar, adverse news/earnings shock, or exhausted multi-day extension) keeping token spend under `< ₹0.20/day`.

---

## 4. What We Must Do to Make the System Consistently Profitable

### Pillar 1: Enforce the "2.2x Fee Hurdle + Ratchet" Law on Every Trade
* **Never hold unratcheted intraday trades to 15:20 IST.** Every intraday position must carry `ratchet_levels` where Level 1 triggers at `+0.30% to +0.35%` and locks at least `+0.18% to +0.20%` (above the `0.146%` fee + slippage floor).
* **Raise `MIN_EDGE_TO_FEE_RATIO` from `1.0` to `2.2` and `MAX_FEE_TO_RISK_PCT` from `25.0%` to `18.0%`:** Rejects low-volatility or undersized trades where statutory fees eat more than 18% of the 1R risk budget.

### Pillar 2: Leverage NSE Intraday MIS (Up to 3x–4x Effective Notional on ₹10,000) & DP-Fee-Aware Swing Sizing
* **Intraday MIS Sizing:** On ₹10,000 cash, Zerodha/Angel One MIS provides 5x intraday margin (₹50,000 buying power). Using a conservative **1.0% account risk per trade (₹100)** and a `0.60%` stop loss yields a **₹16,666 notional position (1.66x leverage)**. At ₹16,666 notional, brokerage is still `0.03%` (₹5.00), so percentage friction remains flat while rupee profit per winning trade scales **6x higher** than a ₹2,800 position!
* **Swing Delivery Sizing:** Because delivery charges include a **flat ₹13.50 DP charge per sell scrip**, taking tiny ₹2,000 delivery trades costs `0.675%` in DP charges alone. Therefore, swing trades on a ₹10,000 account must deploy **₹7,500–₹9,500 in a single best candidate** (`1 konzentrierte Position`), reducing the flat ₹13.50 DP fee to just `0.15%` of position value.

### Pillar 3: Add Short-Selling Support to the Backtester & Live Engine
* Supporting `SELL` (short) signals in `HistoricalBacktestEngine.run_backtest` (`app/trading/backtester.py`) unlocks `LiquiditySweepShort` (PDH stop-hunt fades) and bearish ORB breakdowns, turning red market days from losing/idle days into high-velocity profit days.

### Pillar 4: Relative-Strength & Volume (`RVOL`) Pre-Screening
* Add a cross-sectional daily ranker in `app/trading/fee_aware_strategies.py` that computes each stock's rolling 20-day time-of-day volume profile and relative return against the cross-sectional median of the universe. Only allow intraday long entries on stocks in the **top 25% of cross-sectional relative strength** (and short entries in the **bottom 25%**).

---

## 5. Phased Implementation Roadmap & Verification Protocol

### Phase 1: Backtester Enhancements (Short-Selling + MIS Risk-Based Leverage)
* **Files to Modify:** `app/trading/backtester.py`, `app/config.py`
* **Tasks:**
  1. Add bidirectional (`BUY` and `SELL`) execution support in `HistoricalBacktestEngine.run_backtest`, including short stop-loss (`bar_high >= stop_loss`), short target (`bar_low <= target_1`), and downward profit-lock ratchets.
  2. Add configurable `intraday_leverage_mult` (default `1.0` for backward compatibility, `2.5` for MIS mode) so risk-based sizing `qty = int(min(max_risk / risk_per_share, (day_start_equity * intraday_leverage_mult) / entry))` can utilize realistic NSE MIS margin while keeping hard rupee risk capped at `RISK_PER_TRADE_PCT` (`0.75%–1.0%` = ₹75–₹100).
* **Verification:** Add unit tests in `tests/test_fee_aware_strategies.py` verifying short trade PnL, short gap-through stops, and short ratchets.

### Phase 2: Implement `LiquiditySweepShort`, `RelativeStrengthLeaderBreakout`, and `NR7SwingBreakout`
* **Files to Modify:** `app/trading/fee_aware_strategies.py`, `app/trading/swing_strategies.py`
* **Tasks:**
  1. Implement `LiquiditySweepShort` (PDH sweep & rejection) in `app/trading/fee_aware_strategies.py`.
  2. Implement `RelativeStrengthLeaderBreakout` (cross-sectional RS + RVOL + VWAP/EMA-9 pullback) in `app/trading/fee_aware_strategies.py`.
  3. Implement `NR7SwingBreakout` in `app/trading/swing_strategies.py` and integrate portfolio-level multi-symbol simulation in `SwingBacktestEngine.run_portfolio_backtest` (enforcing max 1–2 concurrent swing positions on ₹10,000 capital).
* **Verification:** Unit tests covering happy path, boundary conditions, and fee-gate rejections.

### Phase 3: Multi-Fold Walk-Forward Validation (1Y No-AI + 1M DeepSeek AI)
* **Files to Create/Update:** `trading_workspace/run_walkforward_profitability_suite.py`, `app/trading/continuous_optimizer.py`
* **Tasks:**
  1. Run 30-symbol walk-forward validation on:
     * **Arm 1 (Intraday Long + Short + RS Filter, 1Y 1h & 1M 5m, Zero AI)**
     * **Arm 2 (Intraday Long + Short + RS Filter, 1M 5m, with DeepSeek V4.1 Flash Veto)**
     * **Arm 3 (Multi-Day Swing Portfolio `RSI2OversoldSwing` + `NR7SwingBreakout`, 5Y 4-Fold & 1Y Daily)**
  2. Wire the winning combined evaluator into `ContinuousOptimizer` (`app/trading/continuous_optimizer.py`) and `StrategyManager` (`app/trading/strategy_manager.py`).
  3. Log all results to `trading_workspace/INTRADAY_STRATEGY_LEARNING_LOG.md` and `trading_workspace/iteration115_results.json`.

### Phase 4: Definition of Done & Promotion Gates
Before any strategy configuration is promoted to default live/paper execution, it must pass:
- [~] **Gate 1 (Broad-Universe Walk-Forward):** Positive net expectancy after full statutory fees and 5 bps/side slippage across $\ge 3$ sequential time folds on the 30-symbol NSE universe.
  - **PARTIAL:** Arm 3 multi-day swing passes (`3/4` 5-year folds net-positive). The intraday arms (1 and 1b) fail — see measured results below.
- [ ] **Gate 2 (Profit Factor & Drawdown):** Net Profit Factor $\ge 1.35$ and Maximum Portfolio Drawdown $\le 8.0\%$ on ₹10,000 capital.
  - **FAIL (Arm 3):** avg swing fold PF `1.18` (< 1.35) and max swing fold DD `23.06%` (> 8.0%).
- [x] **Gate 3 (AI Cost Efficiency):** DeepSeek V4.1 Flash token spend $< 10\%$ of gross trading profit, with all `TokenBudgetGuard` limits respected.
  - **PASS:** Arm 2 spent **₹0.16** on 15 veto calls vs ₹112.77 gross (+0.14%), well under 10%. **However the veto changed nothing** — all 15 calls left the trade set byte-identical to the zero-AI arm (every veto was EXECUTE or downgraded by the `hard_invalidation` guard). The AI is cost-efficient but currently value-neutral on this window.
- [x] **Gate 4 (Test Suite Integrity):** 100% pass rate across `tests/test_fee_aware_strategies.py`, `tests/test_institutional_engine.py`, `tests/test_institutional_runner.py`, and `tests/test_continuous_optimizer.py`.
  - **PASS:** 94 tests green (`test_fee_aware_strategies`, `test_institutional_engine`, `test_institutional_runner`, `test_dashboard_profit_contract`).

#### Phase 4 Measured Results — Iteration 115 Walk-Forward Suite (`trading_workspace/iteration115_results.json`)

| Arm | Universe / Interval | Folds | Trades | Win % | Gross ₹ | Fees ₹ | Net ₹ | PF | MaxDD | Verdict |
| :--- | :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :--- |
| **1** | 30 symbols, 1Y 1h, zero-AI | 1 | 175 | 41.1 | −937.64 | 1425.12 | **−2362.76** | 0.45 | 23.67% | ❌ FAIL |
| **1b** | 30 symbols, 1mo 5m, zero-AI | 1 | 52 | 55.8 | +112.77 | 497.55 | **−384.78** | 0.71 | 6.61% | ❌ FAIL |
| **2** | 30 symbols, 1mo 5m, + DeepSeek veto | 1 | 52 | 55.8 | +112.77 | 497.55 | **−384.94** | 0.71 | 6.61% | ⚠️ AI no-op (−₹0.16 cost) |
| **3** | 30 symbols, 5Y daily swing | 4 | 36/29/22/28 | 30.6/51.7/36.4/35.7 | — | 1023/768/598/766 | **+817/+1832/−937/+505** | 1.18 avg | 23.06% | ⚠️ 3/4 folds |

**Interpretation.** The intraday fee-aware suite (Arms 1, 1b) is **net-negative after statutory costs** on real data, most severely at 1Y/1h where fees (₹1,425) exceed a negative gross. The 1mo/5m arm has a *positive gross* (+₹113) that fees (₹498) convert to a −₹385 net — confirming the friction-vs-move-size asymmetry is the binding constraint, not signal quality. The multi-day swing portfolio (`RSI2OversoldSwing` + `NR7SwingBreakout`) is the only durable positive-expectancy candidate, but its PF (1.18) and drawdown (23.06%) do not yet clear Gate 2. **No intraday configuration is promoted to live default.**

### Phase 5: Live Paper-Trading of the Frozen Swing Edge (Measured Truth)

Strategy work is **frozen** (`trading_workspace/FROZEN_BASELINE.json`, `rsi2_swing_v1`). The only remaining engineering goal is to measure whether the one durable edge holds up on live data, not to keep tuning it.

* **New module `app/trading/swing_live_runner.py` (`SwingLiveRunner`):**
  1. Reads strategy parameters **from the frozen baseline**, never from class defaults (`rsi2_max=10`, `stop=2.0×ATR`, `target=3.0×ATR`, `max_hold=60`, `min_edge_to_fee=2.2`, `fee_to_risk≤18%`, `max_concurrent=2`).
  2. Evaluates `RSI2OversoldSwing` on **completed daily bars only** (today's forming bar is dropped), matching the backtester's signal-on-completed-bar / fill-next-open convention. One scan per session, inside the 09:15–15:20 IST entry window.
  3. Opens risk-sized **delivery** positions (`delivery_hold=True`) on each auto-execute account, capped at 2 concurrent.
  4. Owns the only time-based exit (`max_hold_days` in business days); stop/target are enforced by the shared protective-exit loop and the broker.
* **Delivery-hold plumbing:** a new `delivery_hold` marker on `open_positions` (schema + migration) threads through `db.py` → `broker.py` → `execution_engine.py` → `/api/trading/order`. Both EOD square-off loops (`strategy_manager`, `institutional_runner`) and the daily-loss flatten now **skip** delivery holds, so a multi-day swing is never flattened at 15:20 IST.
* **Legacy engine isolation:** `ENABLE_LEGACY_INTRADAY_ENGINES` (default `false`) gates `argon_predictor` + `institutional_runner` entry. Enabling auto-execute now runs **only** the validated swing edge; the measured net-negative index/intraday systems stay dormant.
* **Truth measurement:** `truth` verdict comes from `app/trading/paper_truth.py` vs the frozen expectation (≥30 closed trades before it judges).
* **Verification:** `tests/test_swing_live_runner.py` (entry geometry, concurrency cap, same-day idempotency, auto-execute/no-op, entry-window, dropped-today-bar, time exit, EOD square-off skip, `delivery_hold` persistence) + a legacy-gating assertion in `tests/test_auto_execute_toggle.py`. Suite: **113 tests OK**.

The swing wiring is intentionally the *only* new live path; success is judged by the Truth tab over the coming months, not by further parameter search.

### Phase 6: Market-Intelligence Agent (Scored, Analysis-Only Forecasts)

The user asked for an agent that "analyses the market over time and predicts direction." The honest, valuable version of that is a **measurement instrument**: every forecast is logged with a reference price and horizon, then automatically scored against the price that actually printed. It never trades, so it cannot contaminate the frozen swing edge.

* **New module `app/trading/market_intel.py` (`MarketIntelAgent`):**
  1. Universe = the swing universe of ~30 NSE large-caps **plus** `^NSEI` (NIFTY) and `^NSEBANK` (BANKNIFTY).
  2. Per symbol it builds a deterministic evidence pack from **completed daily bars only** (today's forming bar is dropped): trend/momentum, 2-day RSI mean reversion, ATR-scaled volatility, 52-week position, and real news headlines via Google-Search grounding.
  3. One DeepSeek call per symbol returns strict JSON `{direction UP|DOWN|FLAT, expected_move_pct, confidence_pct, catalysts, reasoning}`; malformed answers are **discarded, never stored**.
  4. Each forecast is persisted with `reference_price` and `horizon_date` (5 business days out). A resolution pass later finds the first completed bar on/after the horizon and scores direction hit, absolute move error, a Brier score on stated confidence, and the **net-of-delivery-fee** P&L the view would have earned.
* **Storage:** new `forecasts` table + indexes in `app/storage/db.py` (`record_forecast`, `has_forecast_for`, `get_open_forecasts`, `get_forecasts`, `resolve_forecast`).
* **Config:** `MARKET_INTEL_*` keys in `app/config.py` and `.env.example`. AI spend is bounded by `MARKET_INTEL_MAX_AI_CALLS=40` (~₹0.9/day) and charged to the `analysis` token account.
* **Safety:** analysis-only. `run_cycle`/`resolve_due_forecasts` are idempotent per day and skip everything when no API key is configured. Verdict is `insufficient_sample` until ≥30 forecasts resolve, then `on_track`/`underperforming`.
* **Endpoints:** `GET /api/analysis/market-intel` (report) and `POST /api/analysis/market-intel/run` (manual cycle, `force`/`resolve` options); `market_intel_active` in `/api/health`; background loop starts after `MARKET_INTEL_RUN_HOUR_IST` (16:00 IST) on trading days.
* **Verification:** `tests/test_market_intel.py` (16 tests: business-day math, flat-band classification, delivery-cost model, forecast persistence/reference price/horizon, dropped forming bar, idempotency, symbol cap, invalid-output rejection, no-key skip, insufficient-bars skip, due/incorrect/immature resolution, and report verdicts). Full targeted suite: **130 tests OK**. A live smoke cycle against the real DeepSeek endpoint persisted a valid forecast row for RELIANCE (reference ₹1313.4, horizon 2026-11-12).

The agent's value is decided by the resolved forecasts over time; the report stays `insufficient_sample` until the sample is real.
