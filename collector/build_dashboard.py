"""Build the live dial dashboard from relay-collected data.

Runs at the end of every collector mode. Reads data/series/*, data/static/
tape_seed.csv and collector/params.json, computes 3 dial scores, writes
docs/index.html (self-contained, served by GitHub Pages) and appends
docs/history.csv.

This is a SENTIMENT DISPLAY. The page says so. Historical hit rates from the
2021-2026 walk-forward test are printed next to the session needle so nobody,
including us, mistakes tilt for edge.
"""
import csv
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"
ET = ZoneInfo("America/New_York")
NOW = dt.datetime.now(ET)
TODAY = NOW.strftime("%Y-%m-%d")

P = json.load(open(ROOT / "collector" / "params.json"))


def read_series(name):
    """Last row of a series CSV as (dict, date_str). None if missing."""
    p = DATA / "series" / name
    if not p.exists():
        return None, None
    rows = list(csv.DictReader(open(p)))
    if not rows:
        return None, None
    last = rows[-1]
    date_key = next(k for k in last if k.lower() in
                    ("date", "observation_date", "dates"))
    return last, last[date_key]


def read_tape_seed():
    p = DATA / "static" / "tape_seed.csv"
    if not p.exists():
        return {}
    return {r["signal"]: (float(r["value"]), r["as_of"])
            for r in csv.DictReader(open(p))}


def staleness(date_str, grace_days):
    if not date_str:
        return None, True
    d = None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            d = dt.datetime.strptime(date_str[:10], fmt).date()
            break
        except ValueError:
            continue
    if d is None:
        return date_str, True
    age = (NOW.date() - d).days
    return date_str, age > grace_days


def pct_score(value, qgrid):
    """Map a value onto -100..+100 via a baked percentile grid (p0..p100/5)."""
    if value is None:
        return None
    lo = 0
    for i, q in enumerate(qgrid):
        if value >= q:
            lo = i
    if lo >= len(qgrid) - 1:
        pct = 100.0
    else:
        a, b = qgrid[lo], qgrid[lo + 1]
        frac = 0.0 if b == a else (value - a) / (b - a)
        pct = (lo + frac) * 5
    return round(pct * 2 - 100, 1)


def zscore(value, prm):
    return (value - prm["mean"]) / prm["std"] * prm.get("orient", 1.0)


# ---------------- gather inputs ----------------

notes = []
seed = read_tape_seed()

# SPY daily bars: prefer stooq full refresh, else the AV append series
spy_last, spy_date = read_series("spx_daily.csv")
if spy_last is None:
    spy_last, spy_date = read_series("spy_daily_av.csv")

vix_last, vix_date = read_series("VIXCLS.csv")
vix3m_last, vix3m_date = read_series("VIX3M_History.csv")
fg_last, fg_date = read_series("fear_greed.csv")
news_spy, news_spy_date = read_series("av_sentiment_spy.csv")
news_mac, news_mac_date = read_series("av_sentiment_macro.csv")
pc_last, pc_date = read_series("equity_putcall.csv")

# today's open, if the 9:45 quote snapshot exists
quote_path = DATA / "snapshots" / TODAY / "spy_quote.json"
today_open = prior_close = None
if quote_path.exists():
    q = json.load(open(quote_path))
    g = q.get("Global Quote", {})
    try:
        today_open = float(g.get("02. open"))
        prior_close = float(g.get("08. previous close"))
    except (TypeError, ValueError):
        notes.append("9:45 quote unparseable")

# ---------------- session dial ----------------

sess_parts, sess_z = [], []

def add_sess(key, label, value, as_of, grace=4):
    date_s, stale = staleness(as_of, grace)
    if value is None:
        sess_parts.append({"label": label, "score": None, "as_of": date_s,
                           "stale": True})
        return
    z = zscore(value, P["session"][key])
    sess_z.append(z)
    sess_parts.append({"label": label, "score": round(z, 2), "as_of": date_s,
                       "stale": stale})

gap = (today_open / prior_close - 1) if today_open and prior_close else None
gap_asof = TODAY if gap is not None else None
if gap is None and spy_last:
    try:  # fall back: last session's gap, clearly dated
        o = float(spy_last.get("open") or spy_last.get("Open"))
        notes.append(f"no 9:45 quote; gap shown is from {spy_date}")
    except (TypeError, ValueError):
        o = None
add_sess("gap", "Overnight gap", gap, gap_asof, grace=0)

if spy_last:
    try:
        h, l, c = (float(spy_last.get(k) or spy_last.get(k.capitalize()))
                   for k in ("high", "low", "close"))
        add_sess("prior_close_loc", "Prior close location",
                 (c - l) / (h - l) if h > l else None, spy_date)
    except (TypeError, ValueError):
        add_sess("prior_close_loc", "Prior close location", None, spy_date)
else:
    add_sess("prior_close_loc", "Prior close location", None, None)

# prior-day VIX change needs two rows; recompute from full file
vix_chg = None
vp = DATA / "series" / "VIXCLS.csv"
if vp.exists():
    vrows = [r for r in csv.reader(open(vp))][1:]
    vals = [(r[0], float(r[1])) for r in vrows[-3:] if r[1] not in ("", ".")]
    if len(vals) >= 2:
        vix_chg = vals[-1][1] - vals[-2][1]
add_sess("prior_vix_chg", "Prior VIX change", vix_chg, vix_date)

for key, label in [("prior_breadth", "Breadth (seed)"),
                   ("prior_nhnl", "Highs-lows (seed)")]:
    seeded = seed.get(f"{key.replace('prior_', 'prior_')}_for_session") \
        or seed.get(key.replace("prior_", ""))
    if seeded:
        add_sess(key, label, seeded[0], seeded[1], grace=9)
    else:
        add_sess(key, label, None, None)

session_score = None
if sess_z:
    comp = sum(sess_z) / len(sess_z)
    session_score = pct_score(comp, P["session_composite_q"])

# ---------------- mood dial ----------------

mood_parts, mood_scores = [], []

def add_mood(label, score, as_of, grace=4):
    date_s, stale = staleness(as_of, grace)
    if score is not None:
        score = round(max(-100, min(100, score)), 1)
        mood_scores.append(score)
    mood_parts.append({"label": label, "score": score, "as_of": date_s,
                       "stale": stale})

add_mood("Fear & Greed",
         (float(fg_last["score"]) - 50) * 2 if fg_last else None, fg_date, 4)
ns = []
if news_spy:
    ns.append(float(news_spy["mean_score"]))
if news_mac:
    ns.append(float(news_mac["mean_score"]))
add_mood("News sentiment",
         (sum(ns) / len(ns)) / P["mood"]["news_sent_scale"] * 100 if ns else None,
         news_spy_date or news_mac_date, 4)
vr = None
if vix_last and vix3m_last:
    try:
        v = float(vix_last["VIXCLS"])
        v3 = float(vix3m_last.get("CLOSE") or vix3m_last.get("close"))
        vr = v / v3
    except (KeyError, TypeError, ValueError):
        pass
# high ratio = stress; mood needle treats stress as negative, so invert
add_mood("VIX term structure",
         -pct_score(vr, P["mood"]["vix_ratio"]["q"]) if vr else None,
         min(d for d in (vix_date, vix3m_date) if d) if vix_date and vix3m_date
         else vix_date or vix3m_date, 6)
add_mood("Equity put/call",
         None if pc_last is None else
         -pct_score(float(pc_last["equity_pc_ratio"]),
                    [0.4, 0.45, 0.5, 0.52, 0.54, 0.56, 0.58, 0.6, 0.62, 0.64,
                     0.66, 0.68, 0.7, 0.72, 0.75, 0.78, 0.82, 0.86, 0.92, 1.0,
                     1.2]), pc_date, 4)

mood_score = round(sum(mood_scores) / len(mood_scores), 1) if mood_scores else None

# ---------------- tape dial ----------------

tape_parts, tape_scores = [], []
for key, label in [("breadth_ma", "Breadth vs 20/50 MA"),
                   ("nhnl", "52wk highs-lows"),
                   ("aaii_spread", "AAII bull-bear (contrarian)")]:
    seeded = seed.get(key)
    if seeded:
        date_s, stale = staleness(seeded[1], 9)
        s = pct_score(seeded[0], P["tape"][key]["q"])
        if key == "aaii_spread":
            s = -s  # contrarian, matches its tested orientation
        tape_scores.append(s)
        tape_parts.append({"label": label, "score": s, "as_of": date_s,
                           "stale": stale})
    else:
        tape_parts.append({"label": label, "score": None, "as_of": None,
                           "stale": True})
tape_score = round(sum(tape_scores) / len(tape_scores), 1) if tape_scores else None

# ---------------- history + page ----------------

DOCS.mkdir(exist_ok=True)
hist_p = DOCS / "history.csv"
new = not hist_p.exists()
rows = list(csv.reader(open(hist_p))) if not new else []
if not any(r and r[0] == TODAY and r[1] == NOW.strftime("%H%M") for r in rows):
    with open(hist_p, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "time_et", "session", "mood", "tape"])
        w.writerow([TODAY, NOW.strftime("%H%M"), session_score, mood_score,
                    tape_score])

history = []
if hist_p.exists():
    for r in list(csv.DictReader(open(hist_p)))[-90:]:
        history.append({k: r[k] for k in ("date", "time_et", "session",
                                          "mood", "tape")})

payload = {
    "generated": NOW.strftime("%Y-%m-%d %H:%M ET"),
    "dials": {
        "session": {"score": session_score, "parts": sess_parts,
                    "hitrates": P["session_hitrates"]},
        "mood": {"score": mood_score, "parts": mood_parts},
        "tape": {"score": tape_score, "parts": tape_parts},
    },
    "notes": notes,
    "history": history,
}
json.dump(payload, open(DOCS / "data.json", "w"), indent=1)

template = open(ROOT / "collector" / "dashboard_template.html").read()
html = template.replace("/*__PAYLOAD__*/null", json.dumps(payload))
open(DOCS / "index.html", "w").write(html)
print(f"dashboard built: session={session_score} mood={mood_score} "
      f"tape={tape_score} notes={notes}")
