import sys, json, pickle, os
from typing import Any, Dict, List
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
CACHE = '/tmp/opencode/broad_2y_1h.pkl'
SLIP = 0.0005


def load():
    if os.path.exists(CACHE):
        return pickle.load(open(CACHE, 'rb'))
    raw = backtest_engine.fetch_historical_bars(BROAD, period='2y', interval='1h')
    pickle.dump(raw, open(CACHE, 'wb'))
    return raw


def build(raw):
    out = {}
    for sym, df in raw.items():
        df = df.sort_values('timestamp').reset_index(drop=True)
        f = compute_features(df)
        f['day_open'] = df.groupby('date')['open'].transform('first')
        out[sym] = f.reset_index(drop=True)
    return out


ENTRIES = {
    'C0_base': dict(atr=0.75, slope=0.05, body=(0.3, 2.2), lwick=0.035, uwick=0.15),
    'C1_lowatr': dict(atr=0.50, slope=0.05, body=(0.3, 2.2), lwick=0.035, uwick=0.15),
    'C2_noslope': dict(atr=0.75, slope=0.0, body=(0.3, 2.2), lwick=0.035, uwick=0.15),
    'C3_nowicks': dict(atr=0.75, slope=0.05, body=(0.0, 3.0), lwick=0.0, uwick=1.0),
    'C4_aboveopen': dict(atr=0.75, slope=0.05, body=(0.3, 2.2), lwick=0.035, uwick=0.15, above_open=True),
}

EXITS = {
    'H_sl0.8_r': dict(sl=0.008, target=None, ratchet=[[0.8, 0.2], [1.6, 0.8]]),
    'H_sl1.0_r': dict(sl=0.010, target=None, ratchet=[[0.8, 0.2], [1.6, 0.8]]),
    'H_sl1.0_r2': dict(sl=0.010, target=None, ratchet=[[1.0, 0.5], [2.0, 1.0]]),
    'H_sl1.2_nr': dict(sl=0.012, target=None, ratchet=None),
    'T_1.5_sl0.8': dict(sl=0.008, target=0.015, ratchet=[[0.7, 0.5]]),
}


def passes(r, p):
    if str(r['time']) != '09:15':
        return False
    if not (r['atr_pct'] >= p['atr']):
        return False
    if not (r['ema_20'] >= r['ema_50']):
        return False
    if not (r['ema50_slope'] >= p['slope']):
        return False
    if not (r['low'] <= r['ema_20'] * 1.002 and r['close'] >= r['ema_20']):
        return False
    if not (p['body'][0] <= r['body_pct'] <= p['body'][1]):
        return False
    if not (r['lower_wick_ratio'] >= p['lwick'] and r['upper_wick_ratio'] <= p['uwick']):
        return False
    if p.get('above_open') and not (r['close'] > r['day_open']):
        return False
    return True


def simulate(day, i, sl, target, ratchet):
    if i + 1 >= len(day):
        return None
    ep = float(day.iloc[i + 1]['open']) * (1 + SLIP)
    if ep <= 0:
        return None
    stop = ep * (1 - sl)
    tgt = ep * (1 + target) if target else None
    applied = 0
    rem = day.iloc[i + 1:]
    exit_px = reason = None
    for _, b in rem.iterrows():
        o, h, l = float(b['open']), float(b['high']), float(b['low'])
        if o <= stop:
            exit_px, reason = o, 'STOP_GAP'; break
        if l <= stop:
            exit_px, reason = stop, 'STOP'; break
        if tgt and h >= tgt:
            exit_px, reason = tgt, 'TARGET'; break
        if ratchet:
            gain = (h / ep - 1) * 100
            for lv in ratchet[applied:]:
                if gain >= lv[0]:
                    stop = max(stop, ep * (1 + lv[1] / 100)); applied += 1
                else:
                    break
    else:
        exit_px = float(rem.iloc[-1]['close']); reason = 'EOD'
    net = (exit_px - ep) - calculate_buy_fee(ep) - calculate_sell_fee(exit_px)
    return net / ep * 100, reason


def agg(x):
    if len(x) < 5:
        return [len(x)] + ['-'] * 4
    a = np.array(x)
    w = a[a > 0].sum(); l = -a[a <= 0].sum()
    return [len(a), round(a.mean(), 3), round((a > 0).mean() * 100, 1),
            round(w / l, 2) if l > 0 else 99.0, round(a.sum(), 1)]


def run():
    frames = build(load())
    print('symbols', len(frames))
    all_dates = sorted({d for f in frames.values() for d in f['date'].unique()})
    n = len(all_dates)
    folds = [(all_dates[:n // 3], 'F1'), (all_dates[n // 3: 2 * n // 3], 'F2'), (all_dates[2 * n // 3:], 'F3')]
    print('folds:', len(all_dates[:n // 3]), len(all_dates[n // 3:2 * n // 3]), len(all_dates[2 * n // 3:]))
    res = {}
    for ename, ep in ENTRIES.items():
        for xname, ex in EXITS.items():
            buckets = {f[1]: [] for f in folds}
            for sym, f in frames.items():
                f = f.reset_index(drop=True)
                gi = {d: g.index[0] for d, g in f.groupby('date')}
                for date, day in f.groupby('date'):
                    day = day.reset_index(drop=True)
                    # opening bar is first row
                    if len(day) < 2:
                        continue
                    if int(gi[date]) < 55:
                        continue
                    row = f.iloc[int(gi[date])]
                    if not passes(row, ep):
                        continue
                    r = simulate(day, 0, ex['sl'], ex['target'], ex['ratchet'])
                    if r is None:
                        continue
                    for dates, fname in folds:
                        if date in dates:
                            buckets[fname].append(r[0])
            key = (ename, xname)
            res[key] = {fn: agg(buckets[fn]) for fn in ('F1', 'F2', 'F3')}
    # Print compact table
    print(f"{'entry':14} {'exit':12} | {'n':>4} {'exp%':>6} {'wr':>5} {'pf':>5} {'sum':>6} | {'n':>4} {'exp%':>6} {'wr':>5} {'pf':>5} {'sum':>6} | {'n':>4} {'exp%':>6} {'wr':>5} {'pf':>5} {'sum':>6}")
    for key, v in res.items():
        cells = []
        for fn in ('F1', 'F2', 'F3'):
            a = v[fn]
            cells.append(' '.join(str(x).rjust(6) for x in a))
        print(f"{key[0]:14} {key[1]:12} | " + ' | '.join(cells))
    json.dump({f"{k[0]}|{k[1]}": v for k, v in res.items()}, open('trading_workspace/iteration112_pullback_2y.json', 'w'), indent=2)


if __name__ == '__main__':
    run()
