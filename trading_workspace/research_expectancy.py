import sys, json, pickle, os
from typing import Any, Dict, List, Tuple
import numpy as np
import pandas as pd

sys.path.insert(0, '.')

from app.trading.backtester import backtest_engine
from app.trading.fee_aware_strategies import compute_features
from app.trading.costs import calculate_buy_fee, calculate_sell_fee

BROAD = [
    'RELIANCE','TCS','HDFCBANK','BHARTIARTL','MARUTI','SUNPHARMA','ASIANPAINT','WIPRO',
    'HCLTECH','TITAN','ULTRACEMCO','POWERGRID','ONGC','BAJFINANCE','KOTAKBANK','LT',
    'HINDUNILVR','ADANIENT','JSWSTEEL','GRASIM','TECHM','DRREDDY','CIPLA','BPCL','IOC',
    'HINDALCO','EICHERMOT','NESTLEIND','DIVISLAB','COALINDIA',
]
CACHE = '/tmp/opencode/broad_1y_1h.pkl'
SLIP = 0.0005


def load_data():
    if os.path.exists(CACHE):
        raw = pickle.load(open(CACHE, 'rb'))
        print('loaded cache', len(raw))
    else:
        raw = backtest_engine.fetch_historical_bars(BROAD, period='1y', interval='1h')
        pickle.dump(raw, open(CACHE, 'wb'))
        print('fetched', len(raw))
    return raw


def build(raw):
    frames = {}
    for sym, df in raw.items():
        df = df.sort_values('timestamp').reset_index(drop=True)
        f = compute_features(df)
        # per-day context
        day_open = df.groupby('date')['open'].transform('first')
        f['day_open'] = day_open
        first3 = df.groupby('date').cumcount() < 3
        orh = df[first3].groupby('date')['high'].max()
        orl = df[first3].groupby('date')['low'].min()
        f['or_high'] = df['date'].map(orh)
        f['or_low'] = df['date'].map(orl)
        # vwap (resetting daily)
        tp = (df['high'] + df['low'] + df['close']) / 3.0
        vol = df['volume'].replace(0, 1)
        f['vwap'] = (tp * vol).groupby(df['date']).cumsum() / vol.groupby(df['date']).cumsum()
        # prior day
        days = sorted(df['date'].unique())
        dl = {d: float(g['low'].min()) for d, g in df.groupby('date')}
        dh = {d: float(g['high'].max()) for d, g in df.groupby('date')}
        pdl = {days[i]: dl[days[i-1]] for i in range(1, len(days))}
        pdh = {days[i]: dh[days[i-1]] for i in range(1, len(days))}
        f['pdl'] = df['date'].map(pdl)
        f['pdh'] = df['date'].map(pdh)
        f['symbol'] = sym
        delta = df['close'].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        rs = gain.rolling(14, min_periods=1).mean() / loss.rolling(14, min_periods=1).mean().replace(0, 1e-9)
        f['rsi_14'] = 100 - 100 / (1 + rs)
        f['_gi'] = range(len(f))
        frames[sym] = f.reset_index(drop=True)
    return frames


def simulate(day, idx, sl, tp, ratchet=None, sl_mult=None):
    if idx + 1 >= len(day):
        return None
    ep = float(day.iloc[idx + 1]['open']) * (1 + SLIP)
    if ep <= 0:
        return None
    stop = ep * (1 - sl)
    target = ep * (1 + tp) if tp else None
    applied = 0
    rem = day.iloc[idx + 1:]
    exit_px = reason = None
    for _, b in rem.iterrows():
        o, h, l, c = float(b['open']), float(b['high']), float(b['low']), float(b['close'])
        if o <= stop:
            exit_px, reason = o, 'STOP_GAP'; break
        if l <= stop:
            exit_px, reason = stop, 'STOP'; break
        if target and h >= target:
            exit_px, reason = target, 'TARGET'; break
        if ratchet:
            gain = (h / ep - 1) * 100
            for lv in ratchet[applied:]:
                if gain >= lv[0]:
                    stop = max(stop, ep * (1 + lv[1] / 100)); applied += 1
                else:
                    break
    else:
        exit_px = float(rem.iloc[-1]['close']); reason = 'EOD'
    qty = 1
    gross = (exit_px - ep) * qty
    net = gross - calculate_buy_fee(ep * qty) - calculate_sell_fee(exit_px * qty)
    net_pct = net / (ep * qty) * 100
    return net_pct, reason


def entries(r, ready):
    t = str(r['time'])
    e = {}
    e['A_momentum_breakout'] = ready and t >= '10:15' and r['close'] > r['or_high'] and r['close'] > r['vwap'] and r['ema_20'] > r['ema_50'] and r['close'] > r['day_open']
    e['B_vwap_bounce'] = ready and r['low'] <= r['vwap'] * 1.002 and r['close'] > r['vwap'] and r['ema_20'] > r['ema_50'] and r['close'] > r['open']
    e['C_opening_pullback'] = ready and t == '09:15' and r['atr_pct'] >= 0.75 and r['ema_20'] >= r['ema_50'] and r['ema50_slope'] >= 0.05 and r['low'] <= r['ema_20'] * 1.002 and r['close'] >= r['ema_20'] and 0.3 <= r['body_pct'] <= 2.2 and r['lower_wick_ratio'] >= 0.035 and r['upper_wick_ratio'] <= 0.15
    e['D_rsi_reversion'] = ready and r['rsi_14'] < 32 and r['close'] > r['open']
    e['E_ema_pullback'] = ready and r['low'] <= r['ema_20'] * 1.002 and r['close'] > r['ema_20'] and r['ema_20'] > r['ema_50'] and r['close'] > r['open']
    e['F_pdl_sweep'] = ready and not np.isnan(r.get('pdl', np.nan)) and r['low'] < r['pdl'] and r['close'] > r['pdl'] and r['lower_wick_ratio'] >= 0.25 and r['atr_pct'] >= 0.4
    e['G_pdh_breakout'] = ready and not np.isnan(r.get('pdh', np.nan)) and r['close'] > r['pdh'] and r['close'] > r['open'] and r['ema_20'] > r['ema_50']
    return {k: bool(v) for k, v in e.items()}


EXITS = {
    'X1_1.0_0.5': dict(sl=0.005, tp=0.010, ratchet=[[0.5, 0.3]]),
    'X2_1.5_0.75': dict(sl=0.0075, tp=0.015, ratchet=[[0.7, 0.4]]),
    'X3_2.5_1.0': dict(sl=0.010, tp=0.025, ratchet=[[1.0, 0.3]]),
    'X4_hold_1.0': dict(sl=0.010, tp=None, ratchet=[[0.8, 0.2], [1.6, 0.8]]),
    'X5_trail_0.8': dict(sl=0.008, tp=None, ratchet=[[0.5, 0.1], [1.0, 0.5], [1.8, 1.0]]),
}


def run():
    raw = load_data()
    frames = build(raw)
    all_dates = sorted({d for f in frames.values() for d in f['date'].unique()})
    cut = all_dates[int(len(all_dates) * 0.6)]
    print('train <', cut, 'test >=', cut, 'days', len(all_dates))
    stats = {}
    for sym, f in frames.items():
        f = f.reset_index(drop=True)
        for date, day in f.groupby('date'):
            day = day.reset_index(drop=True)
            for i in range(len(day) - 1):
                gidx = int(day['_gi'].iloc[i])
                row = f.iloc[gidx]
                ready = gidx >= 55
                sig = entries(row, ready)
                if not any(sig.values()):
                    continue
                for name, ok in sig.items():
                    if not ok:
                        continue
                    for exname, ex in EXITS.items():
                        r = simulate(day, i, ex['sl'], ex['tp'], ex['ratchet'])
                        if r is None:
                            continue
                        net_pct, reason = r
                        key = (name, exname)
                        d = stats.setdefault(key, {'train': [], 'test': [], 'reasons': {}})
                        (d['train'] if date < cut else d['test']).append(net_pct)
                        d['reasons'][reason] = d['reasons'].get(reason, 0) + 1
    rows = []
    for (name, exname), d in stats.items():
        tr, te = d['train'], d['test']
        if len(tr) < 20 or len(te) < 10:
            continue
        def agg(x):
            a = np.array(x)
            wins = a[a > 0].sum(); losses = -a[a <= 0].sum()
            return len(a), round(a.mean(), 4), round((a > 0).mean() * 100, 1), round(wins / losses, 2) if losses > 0 else 99.0, round(a.sum(), 2)
        rows.append((name, exname, *agg(tr), *agg(te)))
    rows.sort(key=lambda r: r[4], reverse=True)  # by train total
    print(f"{'entry':22} {'exit':14} {'nTr':>4} {'exTr%':>7} {'wrTr':>5} {'pfTr':>5} {'sumTr':>8} {'nTe':>4} {'exTe%':>7} {'wrTe':>5} {'pfTe':>5} {'sumTe':>8}")
    for r in rows:
        print(f"{r[0]:22} {r[1]:14} {r[2]:>4} {r[3]:>7} {r[4]:>5} {r[5]:>5} {r[6]:>8} {r[7]:>4} {r[8]:>7} {r[9]:>5} {r[10]:>5} {r[11]:>8}")
    json.dump([{'entry': r[0], 'exit': r[1], 'train': r[2:7], 'test': r[7:12]} for r in rows],
              open('trading_workspace/iteration111_expectancy_grid.json', 'w'), indent=2)


if __name__ == '__main__':
    run()
