import sys, json, pickle, os
from typing import Dict, List
import numpy as np
import pandas as pd

sys.path.insert(0, '.')

from app.trading.backtester import backtest_engine
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
    if os.path.exists(CACHE):
        return pickle.load(open(CACHE, 'rb'))
    import yfinance as yf
    raw = {}
    for sym in BROAD:
        try:
            d = yf.Ticker(f'{sym}.NS').history(period='5y', interval='1d')
            if d.empty:
                continue
            d = d.reset_index()
            d = d.rename(columns={'Date': 'timestamp', 'Open': 'open', 'High': 'high',
                                  'Low': 'low', 'Close': 'close', 'Volume': 'volume'})
            d['timestamp'] = pd.to_datetime(d['timestamp'], errors='coerce', utc=True).dt.tz_convert('Asia/Kolkata')
            d['date'] = d['timestamp'].dt.strftime('%Y-%m-%d')
            d['time'] = d['timestamp'].dt.strftime('%H:%M')
            d = d.dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
            if not d.empty:
                raw[sym] = d
        except Exception as exc:
            print('fetch failed', sym, exc)
    pickle.dump(raw, open(CACHE, 'wb'))
    return raw


def features(df):
    df = df.sort_values('timestamp').reset_index(drop=True)
    c = df['close']
    out = df.copy()
    out['sma20'] = c.rolling(20).mean()
    out['sma50'] = c.rolling(50).mean()
    out['sma200'] = c.rolling(200).mean()
    out['sma10'] = c.rolling(10).mean()
    hl = df['high'] - df['low']
    hc = (df['high'] - c.shift(1)).abs()
    lc = (df['low'] - c.shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    out['atr'] = tr.rolling(14).mean()
    delta = c.diff()
    gain = delta.clip(lower=0); loss = -delta.clip(upper=0)
    rs = gain.rolling(14).mean() / loss.rolling(14).mean().replace(0, 1e-9)
    out['rsi14'] = 100 - 100 / (1 + rs)
    ag = gain.rolling(2).mean(); al = loss.rolling(2).mean()
    out['rsi2'] = 100 - 100 / (1 + ag / al.replace(0, 1e-9))
    out['hh20'] = df['high'].rolling(20).max().shift(1)
    return out.reset_index(drop=True)


ENTRIES = {
    'S_trend_pullback': lambda r: r['close'] > r['sma50'] > r['sma200'] and r['low'] <= r['sma20'] and r['close'] > r['sma20'] and r['close'] > r['open'],
    'S_donchian20': lambda r: r['close'] > r['hh20'],
    'S_rsi2_oversold': lambda r: r['rsi2'] < 10 and r['close'] > r['close_prev'],
    'S_above200_pullback': lambda r: r['close'] > r['sma200'] and r['rsi2'] < 25 and r['close'] > r['open'],
    'S_golden_cross': lambda r: r['sma20'] > r['sma50'] and r['sma20_prev'] <= r['sma50_prev'],
}


def simulate(df, i, mode):
    if i + 1 >= len(df):
        return None
    nxt = df.iloc[i + 1]
    ep = float(nxt['open']) * (1 + SLIP)
    if ep <= 0 or np.isnan(ep):
        return None
    atr = float(df.iloc[i]['atr'])
    if not np.isfinite(atr) or atr <= 0:
        return None
    qty = max(1, int(CAP // ep))
    stop = ep - 2 * atr
    target = ep + 4 * atr
    hh = float(nxt['high'])
    exit_px = None; reason = None
    max_hold = {'A_atr': 10_000, 'T_time10': 10, 'M_sma20': 10_000, 'R_rsi2': 10_000}.get(mode, 10_000)
    for j in range(i + 1, min(len(df), i + 1 + max_hold)):
        b = df.iloc[j]
        o, h, l, c = float(b['open']), float(b['high']), float(b['low']), float(b['close'])
        if o <= stop:
            exit_px, reason = o, 'STOP_GAP'; break
        if l <= stop:
            exit_px, reason = stop, 'STOP'; break
        if mode in ('A_atr',) and h >= target:
            exit_px, reason = target, 'TARGET'; break
        if mode == 'M_sma20' and c < float(b['sma20']) and not np.isnan(b['sma20']):
            exit_px, reason = c, 'SMA20'; break
        if mode == 'R_rsi2' and c > float(b['sma10']) and not np.isnan(b['sma10']):
            exit_px, reason = c, 'RSI_EXIT'; break
        hh = max(hh, h)
        if mode == 'M_sma20':
            stop = max(stop, hh - 3 * atr)
        if mode == 'R_rsi2':
            stop = max(stop, ep - 2 * atr)
    if exit_px is None:
        j = min(len(df) - 1, i + max_hold)
        exit_px, reason = float(df.iloc[j]['close']), 'TIME'
    buy_val = ep * qty
    sell_val = exit_px * qty
    ch = calculate_indian_statutory_charges(buy_val, sell_val, instrument_type='DELIVERY_EQUITY', quantity=qty)
    net = ch['net_pnl']
    return net / buy_val * 100, reason


def agg(x):
    if len(x) < 5:
        return [len(x), '-', '-', '-', '-']
    a = np.array(x)
    w = a[a > 0].sum(); l = -a[a <= 0].sum()
    return [len(a), round(a.mean(), 3), round((a > 0).mean() * 100, 1), round(w / l, 2) if l > 0 else 99.0, round(a.sum(), 1)]


def run():
    raw = load()
    frames = {s: features(d) for s, d in raw.items()}
    print('symbols', len(frames))
    dates = sorted({d for f in frames.values() for d in f['date'].unique()})
    n = len(dates)
    folds = [(set(dates[:n // 4]), 'F1'), (set(dates[n // 4:n // 2]), 'F2'),
             (set(dates[n // 2:3 * n // 4]), 'F3'), (set(dates[3 * n // 4:]), 'F4')]
    print('fold sizes', [len(f[0]) for f in folds], 'total days', n)
    results = {}
    for ename, pred in ENTRIES.items():
        for mode in ('A_atr', 'T_time10', 'M_sma20', 'R_rsi2'):
            buckets = {f[1]: [] for f in folds}
            for sym, f in frames.items():
                f = f.reset_index(drop=True)
                f['close_prev'] = f['close'].shift(1)
                f['sma20_prev'] = f['sma20'].shift(1)
                f['sma50_prev'] = f['sma50'].shift(1)
                f = f.replace([np.inf, -np.inf], np.nan)
                for i in range(200, len(f) - 1):
                    r = f.iloc[i]
                    try:
                        if not pred(r):
                            continue
                    except Exception:
                        continue
                    if not all(np.isfinite([r['atr'], r['sma20'], r['sma50'], r['sma200']])):
                        continue
                    res = simulate(f, i, mode)
                    if res is None:
                        continue
                    date = r['date']
                    for ds, fname in folds:
                        if date in ds:
                            buckets[fname].append(res[0])
            results[(ename, mode)] = {fn: agg(buckets[fn]) for fn in ('F1', 'F2', 'F3', 'F4')}
    hdr = 'entry'.ljust(22) + 'exit'.ljust(12)
    for fn in ('F1', 'F2', 'F3', 'F4'):
        hdr += '| ' + fn + ' n   exp%  wr   pf   sum  '
    print(hdr)
    for key, v in results.items():
        line = key[0].ljust(22) + key[1].ljust(12)
        for fn in ('F1', 'F2', 'F3', 'F4'):
            a = v[fn]
            line += '| ' + ' '.join(str(x).rjust(5) for x in a) + ' '
        print(line)
    json.dump({f'{k[0]}|{k[1]}': v for k, v in results.items()}, open('trading_workspace/iteration113_swing.json', 'w'), indent=2)


if __name__ == '__main__':
    run()
