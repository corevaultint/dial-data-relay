# Nova Radar

A self-updating version of the Simpler Trading radar screen, built from the
study formulas decoded from the scanner. Runs on GitHub Actions, publishes a
single HTML page.

## What is in this folder

| File | What it does |
|---|---|
| `universe.csv` | The symbol list, one row per symbol: symbol, name, group, sector, industry group, sector ETF. Edit this to change what the radar covers. Groups appear in the order they first occur. |
| `studies.py` | The study library. Plain Python + numpy. Squeeze Pro, RAF Fisher, Moxie, Divergent Bars, HOLB/LOHB, ATR stop, 8/21 cross and volume surge, Stacked EMA, ranges, RS score. Every function has a comment saying what it computes and where the formula came from. |
| `build_radar.py` | Pulls 2 years of daily bars per symbol from Yahoo Finance, runs the studies, writes `data.json` and the page. |
| `template.html` | The page. Filters, presets, sorting, sparklines. The builder pastes the data into it. |
| `radar.yml` | The GitHub Actions workflow. Copy it to `.github/workflows/radar.yml`. |
| `radar.html` | The last built page (committed by the workflow). |

## Install (5 steps)

1. Copy the `radar/` folder into the root of the `dial-data-relay` repo.
2. Copy `radar/radar.yml` to `.github/workflows/radar.yml`.
3. In that workflow, set `RADAR_OUT` to wherever the Pages site serves from. If the dial dashboard's `index.html` is in `docs/`, leave it as `docs/radar.html`. If it is at the repo root, use `radar.html`.
4. Commit and push.
5. Actions tab, pick "radar", "Run workflow". After 2 to 3 minutes the page is at
   `https://corevaultint.github.io/dial-data-relay/radar.html` (adjust if `RADAR_OUT` differs).

It then rebuilds every weekday at 21:45 UTC (after the close) on its own.

## Run it on your Mac

```
pip install numpy requests
python radar/build_radar.py
open radar/radar.html
```

## Changing things

Add or remove symbols: edit `universe.csv`. Change a threshold (the 1.2 RAF
level, the 1.75 ATR multiple, the 1.5x volume surge): the defaults are the
function arguments at the top of each study in `studies.py`. Add a filter: the
filter state lives in `DEFAULT` in `template.html`, the test in `passes()`, the
control in `drawerHTML()`.

## Data notes

Yahoo's volume runs 2 to 5% off the scanner's feed, so volume-based flags can
differ at the margin. A few Yahoo histories carry bad prints (FDX, HON were
wrong on 2026-09-04); everything downstream of a bad bar is wrong for that
symbol until Yahoo fixes it. The page footer lists symbols that failed to load.

RS rank is a percentile within this list, not across the scanner's 11,000
symbols, so the number differs from the scanner even though the ordering matches.
