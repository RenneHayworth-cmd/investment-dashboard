# AGENTS.md

## Project

This is a local Streamlit investment dashboard for personal market analysis.

The UI language is Chinese. Keep user-facing labels, captions, errors, and table
headers in Chinese unless the surrounding code already uses English.

## Run

Use the project virtual environment:

```bash
cd /home/renne/investment_dashboard
.venv/bin/python -m streamlit run app.py
```

Open the app from Windows or WSL at:

```text
http://localhost:8501
```

For local runs, TickFlow API Key inputs default to the `TICKFLOW_API_KEY`
environment variable when it is set. Users can still override the key in the
Streamlit sidebar or form fields.

Do not install dependencies into the system Python. Use:

```bash
.venv/bin/python -m pip install -r requirements.txt
```

## Verify

For code changes, at minimum run:

```bash
.venv/bin/python -m compileall app.py core services pages
```

For targeted edits, compile the touched modules only, then run the full command
before a larger handoff or commit.

## UI Notes

- Keep `app.py` lightweight. It should act as a status and navigation overview:
  cache count, latest cache update time, latest trade date, today-focus notes,
  and common page entry text. Avoid rebuilding it as a heavy card dashboard.
- The `指数监控` page should not block first render with a synchronous daily
  update. Show cached data immediately and let the user refresh manually.
- The `任务与数据` page provides manual index updates, dataset metadata, and job
  records. Do not add a background-loop toggle or auto-started updater.
- Index MA20 updates use controlled concurrency through
  `run_index_ma20_update(..., max_workers=...)`; keep the default at 4 unless
  a data source becomes unstable.
- The `指数监控` latest summary uses dashboard-style index cards: four columns
  on desktop, fixed-height cards, index name and code on separate lines,
  A-share color convention for deltas (red up, green down), click-through
  detail views with long-history trend/drawdown summaries, and a summary table
  sorted by MA20 deviation.
- The monitored set includes mainland China indices, EastMoney micro-cap board
  index, CSI 2000, US indices, VIX, Hang Seng Tech, Hang Seng SCHK High
  Dividend Low Volatility, Nikkei 225, Korea KOSPI, and iron ore/gold/crude
  oil/silver main-continuous futures. Main-continuous futures should try to
  supplement same-day spot prices so they do not remain stale during the
  trading day. Global indices may use Yahoo chart fallback when the AkShare
  Eastmoney global endpoint fails.
- Keep `A股分析` and `美股分析` aligned where the workflows overlap: sidebar
  settings, top summary metrics, chart tab order, and drawdown metric/chart
  style should stay consistent so users do not have to relearn the page.
- The `持仓分析` page tracks a fixed personal holding list across ETF, futures
  spread, and futures option data. It should read local cache on first render,
  fetch only after the user clicks the load button, refresh stale futures
  spread/option cache to the current trading day when a same-day quote is
  available, and use the force-refresh setting only to refetch cache that is
  already current.
- Analysis pages should follow the same control layout: keep analysis settings
  in the sidebar, and keep data input, upload, API key fields, and run/analyze
  buttons in the main page area.

## Structure

- `app.py`: Streamlit home page.
- `pages/`: Streamlit pages. Numeric prefixes control sidebar ordering.
- `core/`: shared paths, SQLite setup, cache helpers, small common utilities.
- `services/`: market data fetching and analysis logic.
- `data/raw/`: generated raw CSV data.
- `data/processed/`: generated processed CSV data.
- `output/`: generated exports and runtime files.
- `cache.db`: local SQLite cache and job metadata.

## Data And Cache

Generated data is local runtime state. Do not treat it as application source.

Avoid committing or overwriting these unless the user explicitly asks:

- `cache.db`
- `data/`
- `output/`
- `__pycache__/`

When adding a new page that fetches data, prefer using `core.cache.save_dataset`
and `core.cache.load_dataset` so the page can reuse local data and show a cache
timestamp.

Display cached timestamps as:

```text
YYYY-MM-DD HH:MM:SS
```

not ISO strings containing `T`.

## Market Data Notes

The project currently uses:

- TickFlow for futures, funds/ETFs, US stocks, and some index data.
- EastMoney for off-exchange mutual fund cumulative NAV data.
- AkShare as fallback or as the primary source for some China market and options
  data.
- SQLite and CSV files for local cache.

Network data sources can fail. Keep user-facing errors clear and include which
source failed when possible.

For futures:

- Specific contracts use raw contract prices.
- Main continuous contracts use AkShare/Sina codes such as `IM0`, `I0`, `AU0`.
- Main continuous series are not front-adjusted or back-adjusted by this app.
- Futures drawdown analysis is useful, but futures options should usually focus
  on走势、波动、成交量、持仓量 rather than standard drawdown tables.

For fund rotation:

- The app now has a dedicated `基金轮动` page after `A股分析`.
- Data sources include uploaded files, TickFlow exchange-traded funds/ETFs, and
  EastMoney off-exchange mutual funds.
- TickFlow fund rotation data is cached with `core.cache`; default behavior is
  to reuse local cache and fetch fresh data only when the page's refresh option
  is enabled.
- Single-asset MA20 timing on the `基金轮动` page uses the same day's close for
  both signal and execution. Its configurable trigger threshold defaults to 1%:
  close above MA20 by the threshold buys, close below MA20 by the threshold
  sells, with 100-share lot-size rounding by default.
- Rotation signals use the previous trading day's close/NAV and require a full
  lookback window before the first rebalance date.
- Exchange-traded ETFs are modeled with rebalance execution at the trading day's
  open, buy slippage `+0.05%`, sell slippage `-0.05%`, and 100-share lot-size
  rounding with residual cash retained.
- Off-exchange mutual funds are modeled with cumulative NAV as the execution
  proxy and do not use 100-share lot rounding.

For correlation analysis:

- The `相关性分析` page sits after `基金轮动`.
- It computes Pearson correlation coefficients `r` on close prices or daily
  returns after inner joining all selected symbols by common dates.
- It does not expose manual date-window choices; inputs are inner-joined by
  common dates, so the effective start is the latest first available date among
  the selected symbols.
- Supported sources are uploaded CSV/Excel files, TickFlow A-share ETFs,
  TickFlow US stocks, and futures main-continuous data.
- The sources can be mixed in one run; any non-empty A-share ETF, US stock,
  futures, or uploaded-file inputs should be combined into one matrix.
- The page persists pairwise analysis results in SQLite table
  `correlation_results`; saved results are rendered as bottom matrices across
  app sessions, grouped by asset category. A-share
  ETFs/stocks, US stocks, futures main-continuous contracts, uploads, and
  cross-asset pairs should be separate matrices. Within each matrix, keep only
  the newest value for each asset pair. After a new calculation, render the
  merged saved matrix instead of a separate current-only matrix; deleting a
  matrix removes all rows in that category group. If a category matrix has
  missing pairs, auto-fill them from local cached correlation datasets only; do
  not trigger network fetches from the history renderer.

## Development Style

- Follow existing Streamlit patterns in the app.
- Keep edits scoped to the requested page/service.
- Put reusable market fetching and calculations in `services/`, not directly in
  page files, when the logic is non-trivial.
- Keep page files focused on UI, inputs, charts, and table display.
- Prefer `pandas` operations over ad hoc string parsing when handling tabular
  market data.
- Use `plotly` for charts, matching existing chart style and tab layout.
- Do not add large visual redesigns unless explicitly requested.
- For functional changes that alter page structure, user-visible capabilities,
  data sources, or trading/backtest rules, also check whether `app.py`,
  `README.md`, and `AGENTS.md` need corresponding updates in the same change.

## Git

Before committing, check:

```bash
git status --short
git diff --stat
```

Commit only relevant source changes. Do not revert unrelated user changes.
