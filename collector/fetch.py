"""Daily market data collector for the direction dial project.

Runs on GitHub Actions (unrestricted network). Two modes, picked by ET hour
or forced with MODE env var:
  premarket  (~8:00 ET): quote snapshots, CNN Fear & Greed, AV news sentiment
  close      (after 16:15 ET): full-series refreshes, CBOE put/call, NAAIM

Every source is wrapped: one failure never kills the run. Whatever succeeded
gets committed. Failures land in data/log/failures.csv so gaps are visible,
never papered over.
"""
import csv
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
ET = ZoneInfo("America/New_York")
NOW = dt.datetime.now(ET)
TODAY = NOW.strftime("%Y-%m-%d")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) direction-dial-relay/1.0"}

failures = []


def log_fail(source, err):
    failures.append((NOW.isoformat(timespec="seconds"), source, str(err)[:300]))
    print(f"FAIL {source}: {err}", file=sys.stderr)


def save(relpath, content, binary=False):
    p = DATA / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content) if binary else p.write_text(content)
    print(f"wrote {relpath} ({len(content)} bytes)")


def get(url, timeout=30, **kw):
    r = requests.get(url, headers=UA, timeout=timeout, **kw)
    r.raise_for_status()
    return r


def append_row(relpath, header, row):
    """Append one row to a series CSV, creating it with header if new.
    Skips if a row for the same first-column value (date) already exists."""
    p = DATA / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        with open(p) as f:
            if any(line.split(",")[0] == str(row[0]) for line in f):
                return
        with open(p, "a", newline="") as f:
            csv.writer(f).writerow(row)
    else:
        with open(p, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerow(row)


# ---------------- sources ----------------

def fred_series():
    for sid, cosd in [("VIXCLS", "1990-01-01"),
                      ("BAMLH0A0HYM2", "1996-12-31"),
                      ("SP500", "2000-01-01")]:
        try:
            r = get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={cosd}")
            if len(r.text) < 200 or "observation_date" not in r.text.splitlines()[0]:
                raise ValueError(f"unexpected payload ({len(r.text)} bytes)")
            save(f"series/{sid}.csv", r.text)
        except Exception as e:
            log_fail(f"fred:{sid}", e)


def cboe_vix3m():
    try:
        r = get("https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX3M_History.csv")
        save("series/VIX3M_History.csv", r.text)
    except Exception as e:
        log_fail("cboe:vix3m", e)


def cboe_putcall():
    """CBOE daily ratios. Endpoint has moved before; try candidates, save raw."""
    candidates = [
        f"https://cdn.cboe.com/data/us/options/market_statistics/daily/{TODAY}_daily_options",
        f"https://cdn.cboe.com/api/global/us_options/market_statistics/daily/?dt={TODAY}",
        "https://cdn.cboe.com/api/global/us_options/market_statistics/ratios/",
    ]
    for url in candidates:
        try:
            r = get(url)
            body = r.text
            if len(body) < 50:
                continue
            save(f"snapshots/{TODAY}/cboe_putcall_raw.json", body)
            # best-effort parse of equity P/C into the series
            try:
                j = json.loads(body)
                flat = json.dumps(j).lower()
                m = re.search(r'"equity[^"]*put[^"]*call[^"]*ratio[^"]*"\s*:\s*"?([0-9.]+)', flat)
                if m:
                    append_row("series/equity_putcall.csv",
                               ["date", "equity_pc_ratio", "source_url"],
                               [TODAY, m.group(1), url])
            except Exception:
                pass  # raw snapshot is saved either way
            return
        except Exception:
            continue
    log_fail("cboe:putcall", "all candidate endpoints failed")


def cnn_fear_greed():
    try:
        r = get("https://production.dataviz.cnn.io/index/fearandgreed/graphdata")
        j = r.json()
        save(f"snapshots/{TODAY}/fear_greed.json", json.dumps(j))
        score = j.get("fear_and_greed", {}).get("score")
        rating = j.get("fear_and_greed", {}).get("rating")
        if score is not None:
            append_row("series/fear_greed.csv",
                       ["date", "score", "rating"], [TODAY, round(score, 2), rating])
    except Exception as e:
        log_fail("cnn:fear_greed", e)


def stooq_spx():
    try:
        r = get("https://stooq.com/q/d/l/?s=%5Espx&i=d")
        if r.text.lower().startswith("date,"):
            save("series/spx_daily.csv", r.text)
        else:
            raise ValueError(f"payload not CSV: {r.text[:60]!r}")
    except Exception as e:
        log_fail("stooq:spx", e)


def stooq_quotes():
    syms = ["spy.us", "qqq.us", "^vix", "^spx", "aapl.us", "msft.us", "nvda.us",
            "amzn.us", "googl.us", "meta.us", "avgo.us", "tsla.us", "brk-b.us", "jpm.us"]
    try:
        r = get(f"https://stooq.com/q/l/?s={'+'.join(syms)}&f=sd2t2ohlcv&e=csv")
        save(f"snapshots/{TODAY}/premarket_quotes_{NOW.strftime('%H%M')}ET.csv", r.text)
    except Exception as e:
        log_fail("stooq:quotes", e)


def av_news_sentiment():
    key = os.environ.get("AV_API_KEY")
    if not key:
        log_fail("alphavantage", "AV_API_KEY secret not set")
        return
    for label, params in [
        ("spy", {"tickers": "SPY", "limit": "200"}),
        ("macro", {"topics": "economy_macro,financial_markets", "limit": "200"}),
    ]:
        try:
            r = get("https://www.alphavantage.co/query",
                    params={"function": "NEWS_SENTIMENT", "apikey": key,
                            "sort": "LATEST", **params})
            j = r.json()
            if "feed" not in j:
                raise ValueError(str(j)[:200])
            save(f"snapshots/{TODAY}/av_news_{label}.json", json.dumps(j))
            scores = [float(a["overall_sentiment_score"]) for a in j["feed"]
                      if a.get("overall_sentiment_score") is not None]
            if scores:
                append_row(f"series/av_sentiment_{label}.csv",
                           ["date", "mean_score", "n_articles"],
                           [TODAY, round(sum(scores) / len(scores), 4), len(scores)])
        except Exception as e:
            log_fail(f"alphavantage:{label}", e)


def naaim():
    try:
        r = get("https://naaim.org/programs/naaim-exposure-index/")
        m = re.search(r"this week[^0-9-]{0,40}(-?\d{1,3}\.?\d{0,2})", r.text, re.I)
        if not m:
            raise ValueError("number not found on page (layout may have changed)")
        append_row("series/naaim.csv", ["date", "exposure_index"], [TODAY, m.group(1)])
    except Exception as e:
        log_fail("naaim", e)


# ---------------- main ----------------

def main():
    mode = os.environ.get("MODE") or ("premarket" if NOW.hour < 12 else "close")
    print(f"run {NOW.isoformat(timespec='seconds')} mode={mode}")
    if NOW.weekday() >= 5 and not os.environ.get("FORCE"):
        print("weekend, skipping")
        return
    if mode == "premarket":
        cnn_fear_greed()
        stooq_quotes()
        av_news_sentiment()
    else:
        fred_series()
        cboe_vix3m()
        cboe_putcall()
        cnn_fear_greed()
        stooq_spx()
        naaim()
    if failures:
        p = DATA / "log" / "failures.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        new = not p.exists()
        with open(p, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["timestamp", "source", "error"])
            w.writerows(failures)
    print(f"done: {len(failures)} failures logged")


if __name__ == "__main__":
    main()
