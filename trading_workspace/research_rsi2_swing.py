import sys, json, pickle, os
from typing import Dict, List
import numpy as np
import pandas as pd

sys.path.insert(0, '.')

from app.trading.institutional_engine import calculate_indian_statutory_charges

BROAD = [
    'RELIANCE','TCS','HDFCBANK','BHARTIARTL','MARUTI','SUNPHARMA','ASIANPAINT','WIPRO',
    'HCLTECH','TITAN','ULTRACEMCO','POWERGRID','ONGC','BAJFINANCE','KOTAKBANK','LT',
    'HINDUNILVR','ADANIENT','JSWSTEEL','GRASIM','TECHM','DRREDDY','CIPLA','BPCL','IOC',
    'HINDALCO','EICHERMOT','NESTLEIND','DIVISLAB','COALINDIA',
]
CACHE = '/tmp/opencode/broad_5y_1d.pkl'
SLIP = 0.0005
CAP = 9000.0


def load():
    return pickle.load(open(CACHE, 'rb'))


def features(df):
    df = df.sort_values('timestamp').reset_index(drop=True)
    c = df['close']
    out = df.copy()
    out['sma200'] = c.rolling(200).mean()
    out['sma10'] = c.rolling(10).mean()
    hl = df['high'] - df['low']
    hc = (df['high'] - c.shift(1)).abs()
    lc = (df['low'] - c.shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    out['atr'] = tr.rolling(14).mean()
    delta = c.diff()
    gain = delta.clip(lower=0); loss = -delta.clip(upper=0)
    ag = gain.rolling(2).mean(); al = loss.rolling(2).mean()
    out['rsi2'] = 100 - 100 / (1 + ag / al.replace(0, 1e-9))
    return out.reset_index(drop=True)


ENTRIES = {
    'R0_rsi2_10': lambda r: r['rsi2'] < 10 and r['close'] > r['close_prev'],
    'R1_rsi2_5': lambda r: r['rsi2'] < 5 and r['close'] > r['close_prev'],
    'R2_rsi2_15': lambda r: r['rsi2'] < 15 and r['close'] > r['close_prev'],
    'R3_rsi2_10_above200': lambda r: r['rsi2'] < 10 and r['close'] > r['close_prev'] and r['close'] > r['sma200'],
    'R4_rsi2_10_below200': lambda r: r['rsi2'] < 10 and r['close'] > r['close_prev'] and r['close'] < r['sma200'],
}
EXITS = {
    'sl2_tp4': (2.0, 4.0),
    'sl1.5_tp3': (1.5, 3.0),
    'sl1.5_tp4': (1.5, 4.0),
    'sl2_tp3': (2.0, 3.0),
    'sl2.5_tp5': (2.5, 5.0),
    'sl2_tp6': (2.0, 6.0),
}


def simulate(df, i, slm, tpm):
    if i + 1 >= len(df):
        return None
    nxt = df.iloc[i + 1]
    ep = float(nxt['open']) * (1 + SLIP)
    atr = float(df.iloc[i]['atr'])
    if ep <= 0 or not np.isfinite(atr) or atr <= 0:
        return None
    qty = max(1, int(CAP // ep))
    stop = ep - slm * atr
    target = ep + tpm * atr
    exit_px = None; reason = None
    for j in range(i + 1, len(df)):
        b = df.iloc[j]
        o, h, l = float(b['open']), float(b['high']), float(b['low'])
        if o <= stop:
            exit_px, reason = o, 'GAP'; break
        if l <= stop:
            exit_px, reason = stop, 'STOP'; break
        if h >= target:
            exit_px, reason = target, 'TARGET'; break
        if j - i >= 60:
            exit_px, reason = float(b['close']), 'TIME'; break
    if exit_px is None:
        exit_px, reason = float(df.iloc[-1]['close']), 'TIME'
    buy_val = ep * qty
    sell_val = exit_px * qty
    ch = calculate_indian_statutory_charges(buy_val, sell_val, instrument_type='DELIVERY_EQUITY', quantity=qty)
    return ch['net_pnl'] / buy_val * 100, reason


def agg(x):
    if len(x) < 5:
        return [len(x), '-', '-', '-', '-']
    a = np.array(x)
    w = a[a > 0].sum(); l = -a[a <= 0].sum()
    return [len(a), round(a.mean(), 3), round((a > 0).mean() * 100, 1), round(w / l, 2) if l > 0 else 99.0, round(a.sum(), 1)]


def run():
    raw = load()
    frames = {s: features(d) for s, d in raw.items()}
    dates = sorted({d for f in frames.values() for d in f['date'].unique()})
    n = len(dates)
    folds = [(set(dates[:n // 4]), 'F1'), (set(dates[n // 4:n // 2]), 'F2'),
             (set(dates[n // 2:3 * n // 4]), 'F3'), (set(dates[3 * n // 4:]), 'F4')]
    results = {}
    for ename, pred in ENTRIES.items():
        for xname, (slm, tpm) in EXITS.items():
            buckets = {f[1]: [] for f in folds}
            for sym, f in frames.items():
                f = f.reset_index(drop=True)
                f['close_prev'] = f['close'].shift(1)
                for i in range(200, len(f) - 1):
                    r = f.iloc[i]
                    if not np.isfinite(r['atr']) or not np.isfinite(r['sma200']):
                        continue
                    try:
                        if not pred(r):
                            continue
                    except Exception:
                        continue
                    res = simulate(f, i, slm, tpm)
                    if res is None:
                        continue
                    for ds, fname in folds:
                        if r['date'] in ds:
                            buckets[fname].append(res[0])
            results[(ename, xname)] = {fn: agg(buckets[fn]) for fn in ('F1', 'F2', 'F3', 'F4')}
    print('entry'.ljust(24) + 'exit'.ljust(12) + ''.join(f'| {fn} n    exp%   wr    pf    sum   ' for fn in ('F1', 'F2', 'F3', 'F4')))
    allpos = []
    for key, v in results.items():
        line = key[0].ljust(24) + key[1].ljust(12)
        positive_all = True
        for fn in ('F1', 'F2', 'F3', 'F4'):
            a = v[fn]
            line += '| ' + ' '.join(str(x).rjust(6) for x in a) + ' '
            if a[1] == '-' or a[1] <= 0:
                positive_all = False
        if positive_all:
            allpos.append(key)
        print(line)
    print('\nPositive in ALL folds:', allpos)
    json.dump({f'{k[0]}|{k[1]}': v for k, v in results.items()}, open('trading_workspace/iteration114_rsi2_swing.json', 'w'), indent=2)


if __name__ == '__main__':
    run()
