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


def get(url, timeout=30, tries=3, **kw):
    """GET with retries and growing timeout. FRED in particular times out
    from Actions IPs on the first try more often than not."""
    import time
    last = None
    for attempt in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=timeout + 30 * attempt, **kw)
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            time.sleep(3 * (attempt + 1))
    raise last


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
        "https://www.cboe.com/us/options/market_statistics/daily/",
        f"https://www.cboe.com/us/options/market_statistics/daily/?dt={TODAY}",
    ]
    errs = []
    for url in candidates:
        try:
            r = get(url)
            body = r.text
            if len(body) < 50:
                continue
            ext = "html" if body.lstrip()[:1] == "<" else "json"
            save(f"snapshots/{TODAY}/cboe_putcall_raw.{ext}", body)
            if ext == "html":
                m = re.search(r"equity[^<]{0,80}put[/ ]?call[^<]{0,80}?([0-9]\.[0-9]{1,3})",
                              body, re.I | re.S)
                if m:
                    append_row("series/equity_putcall.csv",
                               ["date", "equity_pc_ratio", "source_url"],
                               [TODAY, m.group(1), url])
                return
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
        except Exception as e:
            errs.append(f"{url.split('/')[2]}{url.split('cboe.com')[-1][:40]}: {type(e).__name__}")
            continue
    log_fail("cboe:putcall", "all failed | " + " | ".join(errs))


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
    """SPX daily history. Stooq rate-limits datacenter IPs with an HTML
    interstitial; try both hosts, then fall back to AV daily SPY OHLC so the
    forward record never gaps (SPY is the accepted SPX proxy per spec)."""
    for host in ("stooq.com", "stooq.pl"):
        try:
            r = get(f"https://{host}/q/d/l/?s=%5Espx&i=d")
            if r.text.lower().startswith("date,"):
                save("series/spx_daily.csv", r.text)
                return
        except Exception:
            pass
    log_fail("stooq:spx", "both hosts refused or returned HTML; using AV fallback")
    key = os.environ.get("AV_API_KEY")
    if not key:
        log_fail("av:spy_daily", "AV_API_KEY secret not set")
        return
    try:
        r = get("https://www.alphavantage.co/query",
                params={"function": "TIME_SERIES_DAILY", "symbol": "SPY",
                        "outputsize": "compact", "apikey": key})
        j = r.json()
        ts = j.get("Time Series (Daily)")
        if not ts:
            raise ValueError(str(j)[:200])
        for day in sorted(ts)[-5:]:
            v = ts[day]
            append_row("series/spy_daily_av.csv",
                       ["date", "open", "high", "low", "close", "volume"],
                       [day, v["1. open"], v["2. high"], v["3. low"],
                        v["4. close"], v["5. volume"]])
    except Exception as e:
        log_fail("av:spy_daily", e)


def stooq_quotes():
    """Premarket quote snapshot. Caret symbols 404 on this endpoint from
    Actions IPs; plain tickers only, both hosts, accept whatever CSV we get.
    Known-fragile: Stooq blocks datacenter IPs intermittently."""
    syms = ["spy.us", "qqq.us", "aapl.us", "msft.us", "nvda.us", "amzn.us",
            "googl.us", "meta.us", "avgo.us", "tsla.us", "brk-b.us", "jpm.us"]
    for host in ("stooq.com", "stooq.pl"):
        try:
            r = get(f"https://{host}/q/l/?s={'+'.join(syms)}&f=sd2t2ohlcv&e=csv")
            if r.text.lstrip()[:1] != "<" and "," in r.text:
                save(f"snapshots/{TODAY}/premarket_quotes_{NOW.strftime('%H%M')}ET.csv", r.text)
                return
        except Exception:
            continue
    log_fail("stooq:quotes", "both hosts refused (datacenter-IP block, known-fragile)")


def av_news_sentiment():
    key = os.environ.get("AV_API_KEY")
    if not key:
        log_fail("alphavantage", "AV_API_KEY secret not set")
        return
    import time
    for i, (label, params) in enumerate([
        ("spy", {"tickers": "SPY", "limit": "200"}),
        ("macro", {"topics": "economy_macro,financial_markets", "limit": "200"}),
    ]):
        try:
            if i:
                time.sleep(20)   # AV free tier throttles bursts
            j = None
            for attempt in range(2):
                r = get("https://www.alphavantage.co/query",
                        params={"function": "NEWS_SENTIMENT", "apikey": key,
                                "sort": "LATEST", **params})
                j = r.json()
                if "feed" in j:
                    break
                if "Information" in j or "Note" in j:
                    time.sleep(25)   # throttle message: wait and retry once
                    continue
                break
            if not j or "feed" not in j:
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


def av_spy_quote():
    """9:45 run: SPY GLOBAL_QUOTE gives today's open for the gap signal."""
    key = os.environ.get("AV_API_KEY")
    if not key:
        log_fail("av:spy_quote", "AV_API_KEY secret not set")
        return
    try:
        r = get("https://www.alphavantage.co/query",
                params={"function": "GLOBAL_QUOTE", "symbol": "SPY", "apikey": key})
        j = r.json()
        if not j.get("Global Quote", {}).get("02. open"):
            raise ValueError(str(j)[:200])
        save(f"snapshots/{TODAY}/spy_quote.json", json.dumps(j))
    except Exception as e:
        log_fail("av:spy_quote", e)


def aaii_survey():
    """Weekly AAII numbers from the public survey page. Best effort."""
    try:
        r = get("https://www.aaii.com/sentimentsurvey")
        text = r.text
        pcts = re.findall(r"(?:Bullish|Neutral|Bearish)[^0-9]{0,60}([0-9]{1,2}\.[0-9])\s*%", text)
        if len(pcts) < 3:
            raise ValueError("could not find 3 percentages on page")
        bull, neut, bear = (float(x) / 100 for x in pcts[:3])
        append_row("series/aaii.csv", ["date", "bullish", "neutral", "bearish", "spread"],
                   [TODAY, bull, neut, bear, round(bull - bear, 4)])
    except Exception as e:
        log_fail("aaii", e)


def naaim():
    try:
        r = get("https://naaim.org/programs/naaim-exposure-index/")
        text = r.text
        m = (re.search(r"this week[^0-9-]{0,40}(-?\d{1,3}\.?\d{0,2})", text, re.I)
             or re.search(r"exposure\s+index[^0-9-]{0,120}(-?\d{1,3}\.\d{1,2})", text, re.I | re.S)
             or re.search(r'"(?:value|y)"\s*:\s*(-?\d{1,3}\.\d{1,2})(?![\s\S]{0,200}"(?:value|y)")', text))
        if not m:
            save(f"snapshots/{TODAY}/naaim_page.html", text)
            raise ValueError("number not found; raw page snapshotted for inspection")
        append_row("series/naaim.csv", ["date", "exposure_index"], [TODAY, m.group(1)])
    except Exception as e:
        log_fail("naaim", e)


# ---------------- main ----------------

def main():
    mode = os.environ.get("MODE") or (
        "premarket" if NOW.hour < 9 else
        "open945" if NOW.hour < 12 else "close")
    print(f"run {NOW.isoformat(timespec='seconds')} mode={mode}")
    if NOW.weekday() >= 5 and not os.environ.get("FORCE"):
        print("weekend, skipping")
        return
    if mode == "premarket":
        cnn_fear_greed()
        stooq_quotes()
        av_news_sentiment()
    elif mode == "open945":
        av_spy_quote()
        cnn_fear_greed()
    else:
        fred_series()
        cboe_vix3m()
        cboe_putcall()
        cnn_fear_greed()
        stooq_spx()
        aaii_survey()
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
    try:
        import subprocess
        subprocess.run([sys.executable, str(ROOT / "collector" / "build_dashboard.py")],
                       check=True)
    except Exception as e:
        print(f"dashboard build failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
