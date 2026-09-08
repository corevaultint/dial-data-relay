"""
Study library for the Nova radar.

Every function takes plain numpy arrays of daily bars (high, low, close, volume)
and returns arrays of the same length, oldest bar first. The formulas are the
ones reverse-engineered from the Simpler Trading scanner (see the Nova project
docs: raf-voodoo-derivation.md and scanner-indicators-derivation.md).

Nothing here needs a chart platform. It is plain Python + numpy so it runs in
GitHub Actions, on a laptop, or anywhere else.
"""
import numpy as np

NaN = float('nan')


# ---------------------------------------------------------------- basics --

def sma(x, n):
    """Simple moving average over the last n bars (NaN until n bars exist)."""
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), NaN)
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def ema(x, n):
    """Exponential moving average, alpha = 2/(n+1), seeded with the first value.
    This is how ThinkorSwim's ExpAverage seeds; after a few hundred bars the
    seed choice makes no visible difference."""
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), NaN)
    if len(x) == 0:
        return out
    a = 2.0 / (n + 1)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def wilders(x, n):
    """Wilder's smoothing (used by ATR and ADX): seeded with the SMA of the first
    n values, then prev + (x - prev)/n."""
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), NaN)
    if len(x) < n:
        return out
    out[n - 1] = np.mean(x[:n])
    for i in range(n, len(x)):
        out[i] = out[i - 1] + (x[i] - out[i - 1]) / n
    return out


def rolling_max(x, n):
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), NaN)
    for i in range(n - 1, len(x)):
        out[i] = np.max(x[i - n + 1:i + 1])
    return out


def rolling_min(x, n):
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), NaN)
    for i in range(n - 1, len(x)):
        out[i] = np.min(x[i - n + 1:i + 1])
    return out


def stdev_pop(x, n):
    """Population standard deviation over n bars (the scanner's StDev is population)."""
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), NaN)
    for i in range(n - 1, len(x)):
        out[i] = np.std(x[i - n + 1:i + 1])
    return out


def true_range(high, low, close):
    high, low, close = (np.asarray(a, dtype=float) for a in (high, low, close))
    prev = np.roll(close, 1)
    prev[0] = close[0]
    return np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))


def linreg_endpoint(x, n):
    """Value of the least-squares line fitted to the last n bars, evaluated at
    the last bar. ThinkorSwim calls this Inertia."""
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), NaN)
    t = np.arange(n, dtype=float)
    tm = t.mean()
    denom = np.sum((t - tm) ** 2)
    for i in range(n - 1, len(x)):
        w = x[i - n + 1:i + 1]
        if np.any(np.isnan(w)):
            continue
        slope = np.sum((t - tm) * (w - w.mean())) / denom
        out[i] = w.mean() + slope * (n - 1 - tm)
    return out


# ---------------------------------------------------------- Squeeze Pro --

def squeeze_pro(high, low, close, length=20, bb_mult=2.0, kc_mults=(2.0, 1.5, 1.0)):
    """Squeeze Pro, decoded exactly from the scanner.

    Bollinger Bands (20, 2, population stdev) inside a Keltner channel built on a
    20 SMA with a 20 SMA of true range. Three compression levels: Low (Keltner
    x2.0), Mid (x1.5), High (x1.0). Momentum is Inertia(20) of close minus the
    average of the 20-bar Donchian midline and the 20 EMA.

    Returns a dict with:
      momentum        float array
      in_sqz[level]   bool arrays for 'low', 'mid', 'high'
      state[level]    string arrays: 'in', 'fired_long', 'fired_short', 'none'
      bars[level]     int arrays: negative = bars in squeeze (today = -1),
                      positive = bars since the release (release bar = 1), 0 = none
    """
    high, low, close = (np.asarray(a, dtype=float) for a in (high, low, close))
    n = len(close)
    basis = sma(close, length)
    dev = stdev_pop(close, length)
    bb_up, bb_dn = basis + bb_mult * dev, basis - bb_mult * dev
    atr_s = sma(true_range(high, low, close), length)

    donch_mid = (rolling_max(high, length) + rolling_min(low, length)) / 2
    delta = close - (donch_mid + ema(close, length)) / 2
    mom = linreg_endpoint(delta, length)

    out = {'momentum': mom, 'in_sqz': {}, 'state': {}, 'bars': {}}
    for name, mult in zip(('low', 'mid', 'high'), kc_mults):
        kc_up, kc_dn = basis + mult * atr_s, basis - mult * atr_s
        insq = (bb_up < kc_up) & (bb_dn > kc_dn)
        insq = np.where(np.isnan(bb_up) | np.isnan(kc_up), False, insq)
        state = np.array(['none'] * n, dtype=object)
        bars = np.zeros(n, dtype=int)
        for i in range(n):
            if insq[i]:
                state[i] = 'in'
                bars[i] = (bars[i - 1] - 1) if (i > 0 and insq[i - 1]) else -1
            elif i > 0 and insq[i - 1]:
                # release bar: direction is the momentum sign on this bar
                state[i] = 'fired_long' if mom[i] > 0 else 'fired_short'
                bars[i] = 1
            elif i > 0 and state[i - 1] in ('fired_long', 'fired_short'):
                extending = (mom[i] > mom[i - 1]) if state[i - 1] == 'fired_long' else (mom[i] < mom[i - 1])
                if extending:
                    state[i] = state[i - 1]
                    bars[i] = bars[i - 1] + 1
        out['in_sqz'][name] = insq
        out['state'][name] = state
        out['bars'][name] = bars
    return out


# ------------------------------------------------------------ RAF Fisher --

def raf_fisher(high, low, close, length=17):
    """The ReadyAimFire oscillator (the magenta line), decoded exactly:
    Ehlers Fisher Transform, length 17, close normalised inside the 17-bar
    high/low range, smoothing 1/3 in and 1/2 out."""
    high, low, close = (np.asarray(a, dtype=float) for a in (high, low, close))
    n = len(close)
    hh, ll = rolling_max(high, length), rolling_min(low, length)
    raf = np.full(n, NaN)
    v = 0.0
    prev = 0.0
    for i in range(n):
        if np.isnan(hh[i]):
            continue
        rng = hh[i] - ll[i]
        raw = 2 * ((close[i] - ll[i]) / rng - 0.5) if rng > 0 else 0.0
        v = raw / 3 + (2.0 / 3) * v
        vc = min(max(v, -0.999), 0.999)
        prev = 0.5 * np.log((1 + vc) / (1 - vc)) + 0.5 * prev
        raf[i] = prev
    return raf


def raf_signals(raf, threshold=1.2):
    """Turn and the +/-1.2 signals. Returns string arrays:
    turn: 'up' | 'down' | 'none'; buy (oversold buy) and sell (overbought sell) bool arrays."""
    n = len(raf)
    turn = np.array(['none'] * n, dtype=object)
    buy = np.zeros(n, dtype=bool)
    sell = np.zeros(n, dtype=bool)
    for i in range(2, n):
        if np.isnan(raf[i - 2]):
            continue
        if raf[i] > raf[i - 1] and raf[i - 1] < raf[i - 2]:
            turn[i] = 'up'
            buy[i] = raf[i - 1] < -threshold
        elif raf[i] < raf[i - 1] and raf[i - 1] > raf[i - 2]:
            turn[i] = 'down'
            sell[i] = raf[i - 1] > threshold
    return turn, buy, sell


# ----------------------------------------------------------------- Moxie --

def weekly_closes(days, close):
    """Collapse daily bars into weekly bars (Monday to Friday). `days` are day
    numbers since 1970-01-01 (day 0 was a Thursday, so (day + 3) // 7 groups
    Monday..Sunday together). Returns (week_ids, weekly_close, index_of_last_daily_bar_in_week)."""
    days = np.asarray(days, dtype=int)
    close = np.asarray(close, dtype=float)
    wk = (days + 3) // 7
    ids, last_idx = [], []
    for i in range(len(days)):
        if i == 0 or wk[i] != wk[i - 1]:
            ids.append(wk[i]); last_idx.append(i)
        else:
            last_idx[-1] = i
    return np.array(ids), close[np.array(last_idx)], np.array(last_idx)


def moxie(days, close, fast=12, slow=26, signal=9):
    """Moxie = weekly MACD(12, 26, 9) histogram, decoded exactly.
    Returns (current, prior): the histogram for the current week (partial week
    if today is not Friday) and for the previous completed week."""
    _, wc, _ = weekly_closes(days, close)
    if len(wc) < slow + signal:
        return NaN, NaN
    macd = ema(wc, fast) - ema(wc, slow)
    hist = macd - ema(macd, signal)
    return float(hist[-1]), float(hist[-2])


# --------------------------------------------------------- Divergent bars --

def divergent_bars(high, low, close):
    """Bullish divergent bar: lower low than the prior bar and a close above the
    bar's midpoint. Bearish: higher high and a close below the midpoint.
    Returns (bull, bear) bool arrays."""
    high, low, close = (np.asarray(a, dtype=float) for a in (high, low, close))
    mid = (high + low) / 2
    bull = np.zeros(len(close), dtype=bool)
    bear = np.zeros(len(close), dtype=bool)
    bull[1:] = (low[1:] < low[:-1]) & (close[1:] > mid[1:])
    bear[1:] = (high[1:] > high[:-1]) & (close[1:] < mid[1:])
    return bull, bear


def bars_since(flags, today_is=0):
    """Bars since the last True flag. today_is=0 means a flag on the current bar
    reads 0 (the scanner's convention for Divergent Bars). Returns -1 if never."""
    flags = np.asarray(flags, dtype=bool)
    last = -1
    out = np.full(len(flags), -1, dtype=int)
    for i in range(len(flags)):
        if flags[i]:
            last = i
        out[i] = (i - last + today_is) if last >= 0 else -1
    return out


# ------------------------------------------------------------- HOLB / LOHB --

def holb_lohb(high, low, close, window=21):
    """HOLB = the close crosses above the High Of the Lowest Bar in the window
    (the bar with the lowest low over the last 21 bars, including today):
    yesterday's close was at or below that high, today's is above it.
    LOHB = mirror: the close crosses below the Low Of the Highest Bar.
    A cross, not a first-time break, so a symbol can flag more than once.
    Returns (holb, lohb) bool arrays flagging the cross bar only."""
    high, low, close = (np.asarray(a, dtype=float) for a in (high, low, close))
    n = len(close)
    holb = np.zeros(n, dtype=bool)
    lohb = np.zeros(n, dtype=bool)
    for i in range(window - 1, n):
        lo_w = low[i - window + 1:i + 1]
        j = i - window + 1 + int(np.argmin(lo_w))          # lowest-low bar
        if j < i and close[i] > high[j] and close[i - 1] <= high[j]:
            holb[i] = True
        hi_w = high[i - window + 1:i + 1]
        k = i - window + 1 + int(np.argmax(hi_w))          # highest-high bar
        if k < i and close[i] < low[k] and close[i - 1] >= low[k]:
            lohb[i] = True
    return holb, lohb


# ---------------------------------------------------------------- ATR stop --

def atr_stop(high, low, close, length=14, mult=1.75):
    """Ratchet trailing stop on closes: 1.75 x Wilder's ATR(14). Trend is +1 while
    the close stays above the long stop, -1 while it stays below the short stop.
    Returns (trend, stop_line, atr)."""
    high, low, close = (np.asarray(a, dtype=float) for a in (high, low, close))
    n = len(close)
    atr = wilders(true_range(high, low, close), length)
    trend = np.zeros(n, dtype=int)
    stop = np.full(n, NaN)
    started = False
    for i in range(n):
        if np.isnan(atr[i]):
            continue
        long_stop = close[i] - mult * atr[i]
        short_stop = close[i] + mult * atr[i]
        if not started:
            trend[i] = 1 if close[i] >= close[i - 1] else -1
            stop[i] = long_stop if trend[i] == 1 else short_stop
            started = True
            continue
        if trend[i - 1] == 1:
            if close[i] < stop[i - 1]:
                trend[i], stop[i] = -1, short_stop
            else:
                trend[i], stop[i] = 1, max(stop[i - 1], long_stop)
        else:
            if close[i] > stop[i - 1]:
                trend[i], stop[i] = 1, long_stop
            else:
                trend[i], stop[i] = -1, min(stop[i - 1], short_stop)
    return trend, stop, atr


# ------------------------------------------------------------ Phoenix lite --

def phoenix(close, volume, fast=8, slow=21, vol_len=20, surge_mult=1.5):
    """8/21 EMA cross flags and the 1.5x volume surge. Returns
    (cross: 'bull'|'bear'|'none' array, surge bool array, ema_fast, ema_slow)."""
    close, volume = np.asarray(close, dtype=float), np.asarray(volume, dtype=float)
    ef, es = ema(close, fast), ema(close, slow)
    n = len(close)
    cross = np.array(['none'] * n, dtype=object)
    for i in range(1, n):
        if ef[i] > es[i] and ef[i - 1] <= es[i - 1]:
            cross[i] = 'bull'
        elif ef[i] < es[i] and ef[i - 1] >= es[i - 1]:
            cross[i] = 'bear'
    avg_v = sma(volume, vol_len)
    surge = np.where(np.isnan(avg_v), False, volume > surge_mult * avg_v)
    return cross, surge, ef, es


# ------------------------------------------------------------- EMA stack --

def ema_stack(close, lengths=(8, 21, 34, 55)):
    """The radar's Stacked EMA: POS when 8 > 21 > 34 > 55, NEG when fully
    inverted (8 < 21 < 34 < 55), MIX otherwise. Fitted 194/194 against the
    scanner's radar screen on 2026-09-04 (the 89 EMA is not part of it).
    Returns the label for the last bar and the dict of EMA values."""
    vals = {n: ema(close, n)[-1] for n in lengths}
    seq = [vals[n] for n in lengths]
    if all(a > b for a, b in zip(seq, seq[1:])):
        label = 'POS'
    elif all(a < b for a, b in zip(seq, seq[1:])):
        label = 'NEG'
    else:
        label = 'MIX'
    return label, vals


# ------------------------------------------------- relative strength --

def rs_score(close):
    """IBD-style relative strength score: 2 x the 63-day return, plus the 126,
    189 and 252-day returns. Ranking this across a list tracks the scanner's
    Relative Strength percentile (Spearman 0.997 on 2026-09-04). The percentile
    itself is taken across whatever list you feed the builder, so it is a rank
    within the radar universe, not across the scanner's 11,000 symbols."""
    close = np.asarray(close, dtype=float)
    def ret(n):
        return close[-1] / close[-1 - n] - 1 if len(close) > n else NaN
    return 2 * ret(63) + ret(126) + ret(189) + ret(252)


# ------------------------------------------------------- ranges and misc --

def range_pct(high, low, close, window):
    """Where the close sits inside the window's high/low range, 0 to 100.
    Window includes today. Also returns % from the window high."""
    hi = rolling_max(high, window)[-1]
    lo = rolling_min(low, window)[-1]
    c = float(close[-1])
    pct = (c - lo) / (hi - lo) * 100 if hi > lo else NaN
    from_high = (c / hi - 1) * 100 if hi > 0 else NaN
    return pct, from_high
