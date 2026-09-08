#!/usr/bin/env python3
"""
Nova radar builder.

Reads the symbol list in universe.csv, pulls 2 years of daily bars for each
symbol from Yahoo Finance (or from a folder of cached CSVs), runs every study in
studies.py, and writes:

  radar/data.json   the computed table (one record per symbol)
  <out html>        the radar page with the data embedded (single file)

Usage:
  python build_radar.py                         # fetch from Yahoo, write radar/radar.html
  python build_radar.py --out docs/radar.html   # also copy the page somewhere else
  python build_radar.py --bars-dir data/bars    # use cached CSVs instead of Yahoo

Bars CSV format (one file per symbol, oldest first):
  day,high,low,close,volume_k     day = days since 1970-01-01, volume in thousands
"""
import argparse, csv, json, math, os, sys, time, datetime as dt
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import studies as S

YAHOO = 'https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=2y&interval=1d'
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'


# ------------------------------------------------------------------ data --

def read_universe(path):
    with open(path, newline='') as f:
        return list(csv.DictReader(f))


MIN_BARS = 260


def pack_bars(days, h, l, c, v):
    """Drop bars with a missing price and return the 5 arrays the studies use."""
    rows = [(d, hh, ll, cc, vv) for d, hh, ll, cc, vv in zip(days, h, l, c, v)
            if cc is not None and hh is not None and ll is not None
            and not (isinstance(cc, float) and math.isnan(cc))]
    if len(rows) < MIN_BARS:
        return None
    days, h, l, c, v = (np.array(x, dtype=float) for x in zip(*rows))
    return days.astype(int), np.round(h, 2), np.round(l, 2), np.round(c, 2), np.nan_to_num(v)


def fetch_yfinance(symbols, chunk=50):
    """Bulk download through the yfinance library, which handles Yahoo's
    cookie-and-crumb handshake and its rate limits (a raw request from a cloud
    machine gets throttled). Returns {symbol: bars} for what came back."""
    import yfinance as yf
    import pandas as pd
    out = {}
    for i in range(0, len(symbols), chunk):
        batch = symbols[i:i + chunk]
        ymap = {s.replace('.', '-'): s for s in batch}
        try:
            df = yf.download(list(ymap), period='2y', interval='1d', auto_adjust=False,
                             group_by='ticker', threads=True, progress=False, timeout=20)
        except Exception as e:
            print(f'  yfinance batch {i // chunk + 1} failed: {type(e).__name__}: {e}', flush=True)
            continue
        if df is None or df.empty:
            print(f'  yfinance batch {i // chunk + 1} returned nothing', flush=True)
            continue
        for ysym, sym in ymap.items():
            try:
                d = df[ysym] if isinstance(df.columns, pd.MultiIndex) else df
                d = d.dropna(subset=['Close'])
                if len(d) < MIN_BARS:
                    continue
                days = [int(t.toordinal() - dt.date(1970, 1, 1).toordinal()) for t in d.index.date]
                bars = pack_bars(days, d['High'].tolist(), d['Low'].tolist(), d['Close'].tolist(), d['Volume'].tolist())
                if bars is not None:
                    out[sym] = bars
            except Exception:
                pass
        print(f'  fetched {len(out)}/{i + len(batch)} so far', flush=True)
    return out


def fetch_yahoo(sym, session, tries=2):
    """Fallback: one symbol straight from Yahoo's chart endpoint. Short timeouts,
    two tries, no long sleeps, so a throttled runner fails fast instead of hanging."""
    ysym = sym.replace('.', '-')
    for attempt in range(tries):
        try:
            r = session.get(YAHOO.format(sym=ysym), headers={'User-Agent': UA}, timeout=10)
            if r.status_code != 200:
                time.sleep(1.0); continue
            res = r.json()['chart']['result'][0]
            q = res['indicators']['quote'][0]
            days = [int((t + 4 * 3600) // 86400) for t in res['timestamp']]   # shift so the day never straddles UTC midnight
            return pack_bars(days, q['high'], q['low'], q['close'], q['volume'])
        except Exception:
            time.sleep(1.0)
    return None


def read_bars_csv(path):
    rows = list(csv.DictReader(open(path)))
    rows = [r for r in rows if r['close']]
    days = np.array([int(r['day']) for r in rows])
    h = np.array([float(r['high']) for r in rows])
    l = np.array([float(r['low']) for r in rows])
    c = np.array([float(r['close']) for r in rows])
    v = np.array([float(r['volume_k']) * 1000 if r['volume_k'] else 0.0 for r in rows])
    return days, h, l, c, v


def day_to_iso(day):
    return (dt.date(1970, 1, 1) + dt.timedelta(days=int(day))).isoformat()


# --------------------------------------------------------------- compute --

def f2(x, nd=2):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), nd)


def compute_record(meta, days, h, l, c, v, spark_bars=60):
    rec = dict(meta)
    close = float(c[-1]); prev = float(c[-2])
    rec.update({
        'close': f2(close), 'prev_close': f2(prev),
        'chg_pct': f2((close / prev - 1) * 100),
        'asof': day_to_iso(days[-1]),
        'spark': [f2(x) for x in c[-spark_bars:]],
    })

    sq = S.squeeze_pro(h, l, c)
    mom = sq['momentum']
    rec['sqz'] = {lvl: {'state': str(sq['state'][lvl][-1]), 'bars': int(sq['bars'][lvl][-1])} for lvl in ('low', 'mid', 'high')}
    rec['num_squeezes'] = int(sum(bool(sq['in_sqz'][k][-1]) for k in ('low', 'mid', 'high')))
    rec['momentum'] = f2(mom[-1], 3)
    rec['momentum_prev'] = f2(mom[-2], 3)
    rec['mom_dir'] = 'up' if mom[-1] > mom[-2] else 'down'
    # bars since the most recent release on any level (for "fired within N bars")
    fired_bars = [rec['sqz'][k]['bars'] for k in ('low', 'mid', 'high') if rec['sqz'][k]['state'].startswith('fired')]
    rec['fired_dir'] = None
    if fired_bars:
        lvl = min((k for k in ('low', 'mid', 'high') if rec['sqz'][k]['state'].startswith('fired')), key=lambda k: rec['sqz'][k]['bars'])
        rec['fired_dir'] = 'long' if rec['sqz'][lvl]['state'] == 'fired_long' else 'short'
        rec['fired_bars'] = min(fired_bars)
    else:
        rec['fired_bars'] = None

    label, emas = S.ema_stack(c)
    rec['ema_stack'] = label
    rec['ema'] = {str(k): f2(val) for k, val in emas.items()}

    pct52, from52 = S.range_pct(h, l, c, 252)
    pct21, from21 = S.range_pct(h, l, c, 21)
    rec.update({'pct_52w': f2(pct52, 1), 'from_52w_high': f2(from52, 1), 'pct_21d': f2(pct21, 1), 'from_21d_high': f2(from21, 1)})

    raf = S.raf_fisher(h, l, c)
    turn, buy, sell = S.raf_signals(raf)
    rec.update({'raf': f2(raf[-1], 3), 'raf_prev': f2(raf[-2], 3), 'raf_turn': str(turn[-1]),
                'raf_buy': bool(buy[-1]), 'raf_sell': bool(sell[-1])})

    mx, mxp = S.moxie(days, c)
    rec.update({'moxie': f2(mx, 3), 'moxie_prior': f2(mxp, 3)})
    rec['moxie_cross'] = 'bull' if (mxp <= 0 < mx) else 'bear' if (mxp >= 0 > mx) else 'none'

    trend, stop, atr = S.atr_stop(h, l, c)
    rec.update({'atr_trend': int(trend[-1]), 'atr_stop': f2(stop[-1]), 'atr': f2(atr[-1], 3)})
    # bars since the trend flipped
    flip = int(np.argmax(trend[::-1] != trend[-1])) if np.any(trend != trend[-1]) else len(trend)
    rec['atr_trend_bars'] = flip

    bull, bear = S.divergent_bars(h, l, c)
    rec['div_bull_bars'] = int(S.bars_since(bull)[-1])
    rec['div_bear_bars'] = int(S.bars_since(bear)[-1])

    holb, lohb = S.holb_lohb(h, l, c)
    rec['holb_bars'] = int(S.bars_since(holb)[-1])
    rec['lohb_bars'] = int(S.bars_since(lohb)[-1])

    cross, surge, ef, es = S.phoenix(c, v)
    last_cross = 'none'; cross_bars = -1
    for i in range(len(cross) - 1, -1, -1):
        if cross[i] != 'none':
            last_cross, cross_bars = str(cross[i]), len(cross) - 1 - i
            break
    rec.update({'px_cross': last_cross, 'px_cross_bars': cross_bars, 'vol_surge': bool(surge[-1])})
    avg_v = S.sma(v, 20)[-1]
    rec['vol_ratio'] = f2(v[-1] / avg_v, 2) if avg_v and not math.isnan(avg_v) else None
    rec['avg_dollar_vol_m'] = f2(avg_v * close / 1e6, 1) if avg_v and not math.isnan(avg_v) else None
    rec['volume'] = int(v[-1])

    rec['rs_score'] = f2(S.rs_score(c), 4)
    return rec


def add_rs_rank(records):
    scored = [r for r in records if r.get('rs_score') is not None]
    scored.sort(key=lambda r: r['rs_score'])
    n = len(scored)
    for i, r in enumerate(scored):
        r['rs_rank'] = round(100 * (i + 1) / n, 1)
    for r in records:
        r.setdefault('rs_rank', None)


# ------------------------------------------------------------------ main --

MAX_FAIL_SHARE = 0.15   # above this the run is declared broken and nothing is written


def load_all_bars(universe, bars_dir=None):
    """{symbol: bars} for the whole universe. Cached CSVs if bars_dir is given,
    otherwise a bulk yfinance download with a per-symbol fallback."""
    symbols = [m['symbol'] for m in universe]
    bars = {}
    if bars_dir:
        for sym in symbols:
            p = os.path.join(bars_dir, sym + '.csv')
            if os.path.exists(p):
                bars[sym] = read_bars_csv(p)
        return bars
    t0 = time.time()
    print(f'fetching {len(symbols)} symbols through yfinance', flush=True)
    try:
        bars = fetch_yfinance(symbols)
    except ImportError:
        print('  yfinance is not installed (pip install yfinance); using the raw endpoint only', flush=True)
    missing = [s for s in symbols if s not in bars]
    if missing:
        print(f'  {len(missing)} missing after yfinance, trying the raw endpoint: {missing[:20]}{" ..." if len(missing) > 20 else ""}', flush=True)
        import requests
        session = requests.Session()
        for sym in missing:
            b = fetch_yahoo(sym, session)
            if b is not None:
                bars[sym] = b
            time.sleep(0.2)
    print(f'  bars for {len(bars)}/{len(symbols)} symbols in {time.time() - t0:.0f}s', flush=True)
    return bars


def build(universe_path, out_paths, bars_dir=None, data_path=None, template_path=None):
    universe = read_universe(universe_path)
    records, failures = [], []
    bars = load_all_bars(universe, bars_dir)
    for i, meta in enumerate(universe):
        sym = meta['symbol']
        if sym not in bars:
            failures.append(sym); continue
        try:
            records.append(compute_record(meta, *bars[sym]))
        except Exception as e:  # keep going, report at the end
            failures.append(f'{sym} ({type(e).__name__}: {e})')
        if (i + 1) % 50 == 0:
            print(f'  {i + 1}/{len(universe)} computed', flush=True)
    if len(failures) > MAX_FAIL_SHARE * len(universe):
        print(f'ABORT: {len(failures)} of {len(universe)} symbols failed, the last good page is left in place. '
              f'Failed: {failures}', flush=True)
        sys.exit(2)
    add_rs_rank(records)
    asof = max((r['asof'] for r in records), default=None)
    payload = {
        'asof': asof,
        'built_at': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
        'count': len(records),
        'failures': failures,
        'groups': list(dict.fromkeys(m['group'] for m in universe)),
        'symbols': records,
    }
    data_path = data_path or os.path.join(HERE, 'data.json')
    with open(data_path, 'w') as f:
        json.dump(payload, f, separators=(',', ':'))
    template_path = template_path or os.path.join(HERE, 'template.html')
    html = open(template_path, encoding='utf-8').read()
    html = html.replace('/*__RADAR_DATA__*/null', json.dumps(payload, separators=(',', ':')))
    for p in out_paths:
        os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            f.write(html)
        print('wrote', p)
    print(f'{len(records)} symbols, {len(failures)} failures: {failures}')
    return payload


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--universe', default=os.path.join(HERE, 'universe.csv'))
    ap.add_argument('--bars-dir', default=None, help='folder of cached bar CSVs instead of Yahoo')
    ap.add_argument('--out', action='append', default=None, help='where to write the page (repeatable)')
    ap.add_argument('--data', default=None, help='where to write data.json')
    args = ap.parse_args()
    outs = args.out or [os.path.join(HERE, 'radar.html')]
    build(args.universe, outs, bars_dir=args.bars_dir, data_path=args.data)
