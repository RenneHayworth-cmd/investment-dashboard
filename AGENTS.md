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
- The `指数监控` page should show cached data first and must not use a periodic
  refresh timer for its summary cards or formal summary update. The
  `更新指数数据` button is their network trigger: fetch one read-only quote for
  instruments currently trading, fetch mainland, Hong Kong, Japan, and domestic
  futures lunch closes only once per page session during their respective
  breaks, and
  update formal daily data only for indexes missing their latest completed
  session. If `index_final_history` already contains the target session, do not
  request that index again. Intraday and lunch card quotes stay in session and
  process-local transient memory so browser refreshes can restore them; they
  must never overwrite or persist into append-only daily history.
  The split-column report has no user-selectable display window: retain the
  latest 120 calendar days. For every displayed trading day, show that day's
  latest MA20 state-transition date and the return accumulated from the
  transition-day close; do not repeat only the current transition metrics
  across all historical rows.
  Detail views retain their separate cache-first incremental history behavior.
- The `任务与数据` page provides manual index updates, dataset metadata, and job
  records. Its manual update only fills missing completed-session formal data
  after checking the latest 20 market trading sessions, and does not fetch
  intraday card quotes. First show a cache-only preview and require a second
  confirmation before fetching. After updating, compare the saved target-date
  close with an independent daily source where one is available; show unavailable
  or discrepant checks without replacing the formal cache. Do not add a
  background-loop toggle or auto-started updater.
- Iron ore price alerts run independently through
  `scripts/monitor_iron_ore_price.py`; do not tie them to a Streamlit page loop.
  The default threshold is 730 yuan/ton. Notify ordinary WeChat through
  ServerChan only on the first below-threshold observation, suppress repeats
  until the price returns to or above the threshold, and never persist or log
  the SendKey. The Windows scheduled task may invoke the script every minute;
  the script itself must skip non-trading sessions.
- Index MA20 updates use controlled concurrency through
  `run_index_ma20_update(..., max_workers=...)`; keep the default at 4 unless
  a data source becomes unstable. Preserve per-index raw history with
  append-only date merges: once a date has been cached, later refreshes must
  not change that row. A short upstream response must never replace the
  accumulated cache used to calculate MA20. When a cumulative index cache is
  missing or has fewer than 252 rows, bootstrap up to 1,000 calendar days so
  MA20 state-transition dates are not calculated from a short display window;
  TickFlow-backed indices should request up to 1,000 bars for the same bootstrap.
  Keep post-close confirmation rows in the append-only `index_final_history`
  cache. Use them as a calculation overlay when an old raw row contains an
  intraday value, without replacing the original `index_history` or
  `index_long_history` row.
- Index detail views persist a separate `index_long_history` dataset per index.
  Bootstrap the longest available history only once. On every detail open,
  compare the cache date with that market's latest completed session: read
  locally when current, otherwise fetch only a short missing-date window and
  append unseen dates to both accumulated and long-history caches. Existing
  dates must not be replaced, and an unfinished current session must not be
  persisted.
- Index freshness uses `services/market_calendar.py`. Its
  `STATIC_MARKET_HOLIDAYS` table contains the published 2026 cash-market
  closures for mainland China, Hong Kong, Japan, and Korea; update that table
  when exchanges publish a new annual schedule. The US fallback is generated
  by holiday rules and also handles cross-year observed New Year's Day.
  Real-time supplement rows must use the market's expected latest trade date;
  never write weekend or holiday spot values under the current calendar date.
- The `指数监控` latest summary uses dashboard-style index cards: four columns
  on desktop, fixed-height cards, index name and code on separate lines,
  A-share color convention for deltas (red up, green down), click-through
  detail views with long-history trend/drawdown summaries, and a summary table
  sorted by MA20 deviation. Keep VIX in the cards and detail view, but exclude
  it from the MA20 summary table.
- The six futures-main display names include the currently matched concrete
  contract, for example `铁矿石主连（I2609）`. Re-resolve the contract only after
  a successful manual quote update, persist the small mapping in
  `index_futures_main_contracts`, and keep canonical index names unchanged for
  links, cache keys, and calculations.
- The monitored set includes the Shanghai Composite, mainland China indices,
  EastMoney micro-cap board index, STAR 50, CSI 2000, US indices, VIX, Hang Seng Tech, Hang Seng SCHK High
  Dividend Low Volatility, Nikkei 225, Korea KOSPI, and iron ore/gold/crude
  oil/silver and CSI 500/CSI 1000 main-continuous futures. Main-continuous futures should try to
  supplement same-day spot prices so they do not remain stale during the
  trading day. Global indices may use Yahoo chart fallback when the AkShare
  Eastmoney global endpoint fails.
- `国证自由现金流` (`980092`) uses AkShare's official CNI history
  endpoint (`index_hist_cni`) so its back-calculated series reaches the
  2012-12-31 base date; generic A-share and TickFlow history are shorter.
- Keep `A股分析` and `美股分析` aligned where the workflows overlap: sidebar
  settings, top summary metrics, chart tab order, and drawdown metric/chart
  style should stay consistent so users do not have to relearn the page.
- The `持仓分析` page tracks a fixed personal holding list across ETF, futures
  spread, and futures option data. Its default spreads are `I2609 - I2705` and
  `IM2609 - IM2703`, calculated independently with the futures-spread service.
  On an A-share trading day, fetch one ETF quote batch every 10 minutes from
  09:30 through 10:00, every 30 minutes from 10:00 through 11:30, once for the
  lunch close, every 30 minutes from 13:00 through 14:50, and every two minutes
  from 14:50 through 15:00. Each batch updates all ETF cards and the transient
  timing-table preview for configured timing symbols. Use the same schedule to
  refresh the two futures-spread cards and four futures-option cards, retaining
  their last successful transient values when one source fails. These previews
  remain transient and must not affect formal history or recent operation guidance.
  It should read local cache on first render
  and fetch after the user clicks the load button. Its timed fragments remain
  network-disabled until that click within the page session. The save checkbox
  controls all formal ETF, spread, and option cache writes; every intraday
  preview remains non-persistent regardless of that checkbox. From the A-share open through
  15:05, an ETF refresh should batch TickFlow real-time quotes and update cards;
  it may also incrementally backfill missing completed sessions, but must not
  save the current unfinished quote. During the A-share lunch break, fetch the
  lunch close once and use it as a transient timing-table preview through
  14:50. After a successful lunch fetch, do not request it again in the same
  page session; retry a failed fetch no more often than every 10 minutes. From
  14:50 through 15:00 on an A-share
  trading day, the two-minute page fragment should batch real-time quotes every
  two minutes and use them as a transient timing-table preview. Reruns inside
  the two-minute interval reuse the latest result. At 15:00 stop polling but
  retain the last successful same-day quote in cards and the timing preview.
  At 15:05 switch to the formal daily close only after it has been confirmed;
  if formal data is still missing, keep the transient quote instead of falling
  back to the previous close. The preview must not be cached or included in
  recent operation guidance. The lunch preview follows the same no-persistence
  and no-operation-guidance rules. Before the
  open, on non-trading days, and from 15:05 onward, use the daily-history path. While
  the page is open, a local two-minute fragment check should fetch each missing
  formal ETF close once after 15:05, mark the current row as close-confirmed,
  append it to cache, and then update the timing table. Earlier cached dates
  remain unchanged; an unconfirmed same-day row left by an older version must
  not be treated as the formal close. For 161128, formal daily history prefers
  EastMoney/AkShare. When EastMoney is unavailable, Sina exchange history may
  be used only after its adjustment metadata confirms that unadjusted prices
  are identical to the requested adjustment basis. If both long-history paths
  still lag after 15:05, a Sina close snapshot may append the current row only
  when its trade date matches the latest completed session and its timestamp is
  at or after the market close. Apply the same incremental cache rules;
  intraday card quotes remain in the TickFlow batch when available. If TickFlow
  omits 161128, use its same-day Sina snapshot as an intraday-only fallback.
  Spread and option updates
  remain tied to the load button. Its bottom summary table contains ETFs/LOFs only and follows the index
  MA summary style. Beside the current interval return, show the preceding
  transition date and the completed return between that transition and the
  current transition. Use MA20/1% for 513260, 159915, and 588000; MA15/1% for 510500;
  MA20/0.5% for 159201; MA25/2% for 159655, 159501, and 159967; MA25/1.5% for
  161128; MA10/1% for 159545; MA30/1.5% for 518850; MA10/2.5% for 159552;
  MA15/0.5% for 513310; and MA10/2% for 513880. Preserve the
  previous position while price remains inside the threshold band. Treat
  159655 and 159501 as half long-term holding and half MA timing: display a
  bearish timing state as half position rather than empty. Treat 159201,
  159545, 518850, 513260, 588000, 159915, 510500, 159967, 161128, 159552,
  513310, and 513880 as pure timing.
  Treat 512890 as a parking ETF without its own MA signal. Its active transfer
  sources are 510500, 159967, and 159552, each contributing 10% when its timing
  state is empty. Include 512890 in the bottom table with its full fund name,
  code, latest price, daily change, dynamic 0%-30% weight, and aggregate state:
  hold when any source is empty, otherwise empty. Aggregate state-transition
  dates change only on 0% to nonzero or nonzero to 0%; intermediate transfers
  change weight without starting a new interval. Calculate current and previous
  interval returns from 512890 prices at those aggregate transitions. When an
  actively weighted parking-source ETF exits on a formal close, include the
  corresponding 512890 buy in recent operation guidance. Use the complete fund
  names stored in the fixed ETF display-name mapping, and show ETF codes as six
  digits without `.SH` or `.SZ` in cards, tables, and detail captions. At the
  very bottom, show every position transition reconstructed from formal daily
  closes in the latest seven calendar days; intraday quotes must not affect it.
- The `实盘记录` page stores actual executions separately from simulated
  backtests in the local `live_trades` SQLite table. Treat `fee_rate_pct` as a
  percentage value, calculate position cost with fees included, and use moving
  average cost for realized P&L. Do not write these personal records into source
  files or mix them into strategy backtest trades. Rebuild its daily close P&L
  curve from `live_trades` and unadjusted formal close histories: show market
  value, remaining cost, realized/unrealized/total P&L, and total P&L divided by
  cumulative buy cost. Whenever the page is open, backfill a missing latest
  completed session, including before the open and on weekends or market
  holidays; only the current trading day's close remains unavailable until
  15:05. Check every two minutes and retry failed network updates no more often
  than every ten minutes. Recheck immediately when the completed-session target
  changes. Do not value a day until every then-held symbol has a formal close
  covering that date; derived daily P&L rows need not be persisted.
  Show close-fetch failures prominently above the current-position table,
  including the affected symbol and source error, while retaining the local
  cache and the ten-minute retry behavior.
  The current-position table also shows the latest formal close, market value,
  and each symbol's daily and cumulative P&L as compact amount/rate cells from
  the same formal-close histories. Show each open symbol's market-value weight
  immediately after cumulative P&L; calculate it against the total displayed
  open-position market value, excluding unrecorded cash. Daily P&L is the change
  in cumulative P&L since the previous valuation; include same-day added buy
  cost in the daily-return denominator.
  At the page bottom, show a per-symbol historical P&L table covering both open
  and fully closed symbols. Use buy cash amounts including buy fees, sell cash
  proceeds after sell fees, moving-average cost for realized P&L, and the latest
  formal close for unrealized P&L on open positions. A closed symbol's total P&L
  remains its final realized P&L and must not disappear from this table. Append
  a total row for monetary fields and recalculate the total return against total
  cumulative buy cost; do not sum share quantities across different ETFs. Keep
  the total row's trade-date and valuation-date cells blank, center the table,
  and style the total row like the current-position total row.
- The `期货实盘` page is separate from ETF `实盘记录`. Read broker monthly
  statements from `FUTURES_STATEMENT_DIR` or the default local OneDrive
  directory without modifying them. Match only broker statement filenames and
  support both `.xls` and `.xlsx`; derived statistics workbooks are not input.
  The latest successful statement supplies official account data and month-end
  futures/options positions, while every statement supplies the immutable
  execution history. Keep statement imports, account snapshots, positions,
  executions, and formal daily close/settlement prices in dedicated SQLite tables rather than
  `live_trades`.
  Show official quantity, post-month-end manual change, and estimated quantity
  separately. Manual futures/options trades must be later than the latest
  statement end, use explicit buy/sell and open/close fields, reject closing
  more than the available long or short position, and remain the only deletable
  execution source. When a later statement arrives, reconcile a manual trade by
  broker execution ID first; otherwise take it over only on a unique exact
  date/contract/side/price/quantity/time match. Leave ambiguous matches marked
  `待核对` and exclude only confirmed takeovers from calculations. Broker option
  details do not contain reliable open/close or close-P&L fields; retain
  `未提供`, and derive option realized/unrealized totals from signed premium cash
  flow plus the official remaining-position basis instead of inventing an
  open/close flag.
  Render cached data first, then once per page session backfill current contracts
  through the latest completed futures trading day; retain old formal data and
  surface each failed contract/source. Never persist an unfinished current-day
  quote. Produce a daily account valuation only when every active contract has
  a formal close for the same date. Keep close-based cumulative net P&L and the
  broker-style `全部盈亏（盯市）` separate: the latter requires same-date settlement
  prices for every active contract, deducts execution fees, and excludes account-level
  declaration fees. The current-position and historical-P&L
  tables must cover futures and options separately or together, retain fully
  closed contracts, and avoid summing hand counts across unlike contracts.
  Account cumulative fees use the official monthly fee totals plus unmatched
  manual fees. Parse explicitly labeled declaration fees from the statement's
  other-funds detail, include them in the official fee total, and show them as
  a fee component rather than an unexplained reconciliation difference. Keep
  any remaining unexplained difference visible instead of assigning it to a
  contract.
- Analysis pages should follow the same control layout: keep analysis settings
  in the sidebar, and keep data input, upload, API key fields, and run/analyze
  buttons in the main page area.

## Structure

- `app.py`: Streamlit home page.
- `pages/`: Streamlit pages. Numeric prefixes control sidebar ordering.
- `services/live_trading.py`: local live-trade ledger and position-cost calculations.
- `services/futures_live_trading.py`: read-only broker statement import, futures/options manual trades, position projection, and P&L calculations.
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
- Main continuous contracts generally use AkShare/Sina codes such as `IM0`, `I0`, `AU0`. `原油主连` uses EastMoney `142.scm` for realtime and daily data so it matches the EastMoney futures page; keep `SC0` only as its futures-session symbol. Apply EastMoney rows from 2026-07-10 through the separate append-only `index_source_correction_history` overlay, without replacing accumulated raw history.
- Main continuous series are not front-adjusted or back-adjusted by this app.
- Futures drawdown analysis is useful, but futures options should usually focus
  on走势、波动、成交量、持仓量 rather than standard drawdown tables.

For fund rotation:

- The app now has a dedicated `策略回测` page after `A股分析`.
- The page also provides a multi-ETF allocation timing mode. Each row configures
  an ETF or cash weight and chooses always-hold, pure MA timing, or half
  always-hold plus half MA timing. Weights must total 100%. Use the latest
  common available trading date range, execute MA signals at that day's close,
  retain the existing transaction-cost and lot-size rules, and compare against
  the same ETF weights held continuously. Cash rows remain cash in both paths;
  zero-weight rows stay editable but are excluded from the run, and any total
  below 100% is automatically assigned to cash. Reject totals above 100%.
- Data sources include uploaded files, TickFlow exchange-traded funds/ETFs, and
  EastMoney off-exchange mutual funds.
- TickFlow data is cached with `core.cache`. Single-asset MA timing fetches
  fresh data whenever the user runs it and falls back to local cache only when
  that fetch fails. Multi-fund rotation reuses local cache by default and
  fetches fresh data only when its refresh option is enabled.
- Strategy backtests use a user-selected start and end date rather than a
  user-selected daily-bar count. TickFlow requests up to 10,000 bars in the
  background and reuses legacy 5,000/10,000-bar caches when available. MA and
  momentum warm-up data before the selected start date must remain available.
- Both MA20 timing and fund rotation show separate results for 近一年、今年来、
  近三年、近五年、成立来, anchored to the selected interval's actual final
  trading date. Show each period's actual start/end so shorter-lived assets are
  not presented as having a full five-year history.
- Backtest summaries and period tables include transaction win rate. Count only
  closed positions: net sell proceeds after the sell fee versus original buy
  amount plus the buy fee. Open positions are excluded. Sell rows in the trade
  detail include realized P&L amount and percentage; rotation rows aggregate all
  positions sold in that rebalance and also show per-symbol P&L in sell details.
- Single-asset MA20 timing on the `策略回测` page uses the same day's close for
  both signal and execution. Its configurable trigger threshold defaults to 1%:
  close above MA20 by the threshold buys, close below MA20 by the threshold
  sells, with 100-share lot-size rounding by default.
- The MA timing benchmark is labeled `一直持有收益` and always uses the first
  and last actual trading dates in the selected interval. Its start date must
  not shift with the configured MA period; before the MA is available, the
  strategy remains in cash while the benchmark still runs from interval start.
  Backtest metrics label strategy drawdown as `策略最大回撤`; MA timing also
  reports `一直持有最大回撤` over that same selected interval.
- Strategy drawdown, daily volatility, and Sharpe calculations must seed the
  series with initial capital so first-day transaction costs and slippage are
  included instead of treating the first post-trade NAV as the starting peak.
- Rotation defaults to `after_close`: use the scheduled rebalance day's close
  to calculate momentum after the close, then model the trade at that same
  close through the post-close fixed-price session. Do not apply exchange
  slippage in this mode, but keep transaction fees and clearly state that the
  backtest assumes full execution despite time-priority matching. The current
  post-close mechanism expanded to all A-shares and ETFs on 2026-07-06, so
  earlier history under this mode is a current-rule simulation rather than a
  claim that the execution method was historically available.
- Keep `next_open` as a comparison mode: use the previous trading day's
  close/NAV signal and execute at the next eligible open.
- For multi-position rotation, retain symbols that remain in the selected set;
  sell only exiting symbols and use the released cash to buy only entering
  symbols. Do not rebalance weights when the selected set is unchanged. A
  missing open price should delay execution only when that symbol must actually
  be bought or sold, not merely because it is present in the candidate universe.
- In `next_open` mode, exchange-traded ETFs use buy slippage `+0.05%`, sell
  slippage `-0.05%`, and 100-share lot-size rounding with residual cash retained.
- Off-exchange mutual funds are modeled with cumulative NAV as the execution
  proxy and do not use exchange slippage or 100-share lot rounding. Uploaded or
  fetched data without an open-price column uses close as the execution proxy
  and also does not apply exchange slippage.

For correlation analysis:

- The `相关性分析` page sits after `策略回测`.
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
