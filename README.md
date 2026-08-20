# Direction Dial Data Relay

Collects free market data twice each trading day via GitHub Actions and commits
it to this repo, because the Claude session sandbox cannot reach market data
sites directly but can reach GitHub.

Runs weekdays at ~8:00am ET (pre-market snapshot: Fear & Greed, quote snapshot,
Alpha Vantage news sentiment) and ~4:30-5:30pm ET (post-close: FRED series
refresh, CBOE VIX3M and put/call, SPX history, NAAIM).

## Layout

- `data/series/` - append-forward or full-refresh CSV series (the useful stuff)
- `data/snapshots/YYYY-MM-DD/` - raw per-day payloads
- `data/log/failures.csv` - every fetch that failed, timestamped. Gaps are
  logged, never silently skipped.

## Setup (once, ~10 minutes)

1. Create a GitHub account if needed, then a **private** repo named
   `dial-data-relay` (Private matters: some sources' terms do not allow public
   redistribution).
2. Upload `README.md`, `collector/fetch.py` and `.github/workflows/collect.yml`
   with the same folder structure. Easiest reliable path:
   - "Add file" > "Create new file", type the path
     `.github/workflows/collect.yml`, paste that file's contents, commit.
   - Same for `collector/fetch.py` and `README.md`.
3. Add the Alpha Vantage key: repo **Settings > Secrets and variables >
   Actions > New repository secret**, name `AV_API_KEY`, value = the key.
4. Go to the **Actions** tab, enable workflows if prompted, open
   "collect-market-data", press **Run workflow** (leave mode blank).
5. After a minute or two the run should show a green check and a `data/`
   folder appears in the repo. Failures per source are normal on day one
   (endpoints get adjusted); they will be listed in `data/log/failures.csv`.

## Notes

- The workflow needs no personal token; it commits with the built-in Actions
  token. A read-only fine-grained PAT is only needed later, when a Claude
  session should pull the collected data.
- Expect the CBOE put/call and NAAIM fetchers to need one round of adjustment
  after the first real run; both endpoints are best-guess and fail loudly.
- Alpha Vantage free tier is 25 requests/day; the collector uses 2.
