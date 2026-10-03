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

- `app.py` is the live-account overview (user decision 2026-10-02) and shows only:
  combined total assets, daily P&L, total P&L since account opening (with the
  since-ETF-opening amount beside it) and cumulative return; one card each for
  ETF实盘, 微盘实盘 and 期货实盘 (broker customer equity: 盯市 minus 申报费 and other
  account fees, never deducting exercise fees twice); the combined NAV/daily-P&L
  chart; and the combined return calendar. Keep it thin: `services/home_overview.py`
  reads the three ledgers with formal closes/settlements only (no network, no
  intraday quotes) and `components/home/` renders, caching results for five minutes.
  Combine by summing each day's P&L amounts and return denominators, stop at the
  earliest latest valuation, and start at the ETF实盘 first valuation date
  (微盘实盘 capital comes out of ETF实盘). On that start day, zero only the P&L of
  accounts already valued before it. Account cards keep their own start. A ledger
  that starts after account opening may store its pre-ledger P&L in the
  `live_pre_ledger_pnl` table (never in source); ETF实盘 now starts at opening, so
  that table is empty for it.
- The `指数监控` page should show cached data first. User decision 2026-10-01:
  while this page is open, automatically refresh only S&P 500, Nasdaq Composite,
  and Nasdaq 100 card quotes every two minutes during US cash trading hours.
  Use the market calendar for NY dates, DST, holidays and shortened sessions.
  Share request reservations across browser sessions in the same process;
  failed instruments retry no sooner than ten minutes, retaining the last valid
  quote. Accept only positive prices with same-session source timestamps no
  older than three minutes. Manual successful quotes satisfy the next automatic
  slot. These quotes are transient and do not update formal summaries or history.
  VIX and all other markets remain manual. A
  cache-only fragment may poll local dataset metadata every 30 seconds and
  rerender the page after the standalone scheduled updater changes the formal
  report cache. The `更新指数数据` button is the manual network trigger: fetch one read-only quote for
  instruments currently trading, fetch mainland, Hong Kong, Japan, and domestic
  futures lunch closes only once per market trading date across browser sessions
  in the same process during their respective
  breaks, and
  update formal daily data only for indexes missing their latest completed
  session. If `index_final_history` already contains the target session, do not
  request that index again. Intraday and lunch card quotes stay in session and
  process-local transient memory so browser refreshes can restore them; they
  must never overwrite or persist into append-only daily history.
  Only valid, current lunch responses mark that instrument complete; failed,
  partial, or stale responses remain retryable. Keep the last successful quote
  after a market closes until its confirmed formal cache covers that trading date,
  using `index_final_history` (or the mapped futures contract's formal history),
  never treating an old raw intraday row as confirmation. Use local market dates,
  US daylight saving time, futures overnight trading dates, and holiday-eve night
  suspensions. Reopening reuses local data and the shared US quote cadence;
  other markets remain cache-only. The update button refreshes only
  instruments currently trading or still missing their lunch/formal data.
  The split-column report has no user-selectable display window: retain the
  latest 120 calendar days. For every displayed trading day, show that day's
  latest MA20 state-transition date and the return accumulated from the
  transition-day close; do not repeat only the current transition metrics
  across all historical rows. The bottom MA20 summary also shows the immediately
  preceding transition date and the completed return from that transition-day
  close to the current transition-day close.
  Detail views retain their separate cache-first incremental history behavior.
- Formal index cards and MA20 caches are automatically checked by the standalone
  scheduler every day at 15:10 and 16:10 Asia/Shanghai. The 15:10 run covers
  mainland China, day-session futures, and other markets already closed; the
  16:10 run adds Hong Kong and retries remaining gaps. Use
  `scripts/update_index_ma20_scheduled.py` through the Windows scheduled-task
  wrapper, keep it single-instance, and skip network work when no completed-day
  gap exists. Scheduled updates never persist intraday quotes.
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
  The default threshold is 700 yuan/ton (state and lock files are named per threshold, so a new threshold starts armed). Notify ordinary WeChat through
  Hermes WeChat as a ladder (user decision 2026-09-29): once below 700, then once
  each time a new 5-yuan level (695, 690, ...) is broken; a gap through several
  levels alerts once for the lowest. Hovering near an alerted level never
  re-alerts. Reset the ladder only when price is back at 705 (threshold + step)
  or higher. Never persist or log
  the SendKey. The Windows scheduled task may invoke the script every minute;
  the script itself must skip non-trading sessions.
- The `微盘实盘` page fetches only while it is the open page. The Windows task from
  `scripts/install_microcap_live_closes_task.ps1` (weekdays 15:10, 15:40, 17:10) runs
  `scripts/update_microcap_live_closes.py`, which calls the same append-only
  `load_microcap_histories` for every ledger code once today's session is settled; it
  makes no network request when the cache already covers today and exits non-zero
  (failure balloon) while any code is still missing.
  Same-day close sources, in order: TickFlow, EastMoney, Tencent (`qt.gtimg.cn`, only
  rows with volume today; old pre-920 Beijing codes return zero-volume rows) and Wind
  (also volume today) quotes stamped at or after 15:00, then the local BK1158 constituent
  snapshot only when it was taken that day at
  or after 15:00 and the stock is not suspended (its time is the list fetch time, not a
  trade time); daily bars fill anything else, with unadjusted Wind bars as the last
  daily candidate. Intraday quotes use the same
  TickFlow -> EastMoney -> Tencent -> Wind order before the AkShare fallbacks; Tencent
  and Wind are the vendors in that chain that do not depend on EastMoney.
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
  `STATIC_MARKET_EARLY_CLOSES` and `market_sessions_for_date` supply the published
  2026 Hong Kong and US half-day closes for quote selection and formal-close
  eligibility; update shortened sessions alongside annual holidays.
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
  links and main-continuous history cache keys. Calculate the summary MA20,
  deviation, and transition fields from a separate append-only formal daily
  cache for the currently matched concrete contract, so a rollover does not mix
  old and new contract prices. Keep detail views on the unadjusted
  main-continuous long history.
- The monitored set includes the Shanghai Composite, mainland China indices,
  EastMoney micro-cap board index, STAR 50, CSI KRX China-Korea Semiconductor
  (`931790`), ChiNext Growth (`399296`), CSI 2000, US indices, VIX, Hang Seng Tech, Hang Seng SCHK High
  Dividend Low Volatility, Nikkei 225, Korea KOSPI, and iron ore/gold/crude
  oil/silver and CSI 500/CSI 1000 main-continuous futures. Main-continuous futures should try to
  supplement same-day spot prices so they do not remain stale during the
  trading day. Global indices may use Yahoo chart fallback when the AkShare
  Eastmoney global endpoint fails.
  Hang Seng SCHK High Dividend Low Volatility may use Sina's Hong Kong index
  daily history to append missing settled dates and the Hang Seng Indexes
  official close as an independent confirmation. Its manual intraday card
  quote may use Sina realtime data. Miaoxiang may be used after Sina fails only
  when the response entity is exactly `HSHYLV.HI`; all Miaoxiang rows must be
  filtered to completed sessions and appended by unseen date only. Its realtime
  quote remains transient and must never be written to formal daily history.
  Do not use Miaoxiang `861520.EI` as a fallback for `90.BK1158`: their absolute
  levels and daily returns are different despite both resolving to a micro-cap
  label.
- `桃囍微盘` (`TXWP20`) manual index updates calculate transient intraday points
  from the 20 confirmed T-1 constituents. Normalize CSV-loaded stock codes to
  six-digit strings before quote lookup; never persist the intraday result into
  formal history. Try EastMoney then Tencent batch quotes directly before trying
  environment proxies. Require all 20 unique members and same-day timestamps for
  trading stocks; a partial or stale batch must fall back, never reduce the divisor.
  Surface quote/calculation failures while retaining valid caches.
  The v3 `calendar.csv` ends 14 days after its export; formal rebuilds extend
  sessions from `services/market_calendar.py` and fail loudly when the A-share
  holiday table does not cover the needed year.
- `国证自由现金流` (`980092`) uses AkShare's official CNI history
  endpoint (`index_hist_cni`) so its back-calculated series reaches the
  2012-12-31 base date; generic A-share and TickFlow history are shorter.
- `中韩半导体` (`931790`, after `科创50`) and `创成长` (`399296`, after `创业板指`)
  use the same unified A-share chain as the other mainland indices
  (`akshare_cn`, TickFlow first where supported; TickFlow lacks 931790). 931790
  also publishes on Korean-only sessions; those rows are dropped like other
  non-A-share dates.
- Keep `A股分析` and `美股分析` aligned where the workflows overlap: sidebar
  settings, top summary metrics, chart tab order, and drawdown metric/chart
  style should stay consistent so users do not have to relearn the page.
- The `持仓分析` page tracks a fixed personal holding list across ETF, futures
  contracts, futures spreads, and reusable futures-option data. Its default
  futures contract is `I2701`; its default spreads are `I2701 - I2705` and
  `IM2610 - IM2703`, calculated independently with the futures-spread service.
  On an A-share trading day, fetch one ETF quote batch every two minutes during
  09:30-11:30 and 13:00-15:00, and once for the lunch close (user decision
  2026-09-30, enable only where current API capability has been confirmed).
  TickFlow's registered Free plan permits 10 requests/minute and five symbols
  per request: the fixed 15-ETF list requires three requests per refresh.
  Each batch updates all ETF cards and the transient timing-table preview.
  Retry failed ETF batches no more often than every ten minutes.
  Apart from the three verified US indices above, public-source index quotas
  remain unconfirmed and index monitoring remains manual. Retain the existing
  holdings auxiliary quote cadence: every ten minutes
  09:30-10:00, every thirty minutes 10:00-11:30 and 13:00-14:50, once at lunch,
  and every two minutes 14:50-15:00. Apply that separate cadence to the holdings
  index reference, the `I2701` futures card and the two futures-spread cards, retaining
  their last successful transient values when one source fails. These previews
  remain transient and must not affect formal history or recent operation guidance.
  Automatically load holdings once on first render in each page session,
  reusing current formal caches, fetching intraday quotes, and backfilling missing
  completed sessions. Enable timed fragments after this automatic load; ordinary
  reruns must not repeat the full load. Keep the load button for explicit refreshes.
  Reopening the page must reuse same-day successful lunch quotes and derivative/index
  previews from process-local memory. Outside A-share quote hours, do not force a
  derivative realtime refresh on initial load; fetch only missing completed-session
  formal data. Once formal closes are current, reopening remains local.
  Render local cards before network work and display the ETF quote batch as soon
  as it returns, before backfilling history or refreshing derivative sources.
  Clear the early preview only when complete cards are ready; render timed card
  updates inside the fragment to avoid accumulating external-container elements.
  The save checkbox
  controls all formal ETF, futures, spread, and option cache writes; every intraday
  preview remains non-persistent regardless of that checkbox. From the A-share open through
  15:00, an ETF refresh should batch TickFlow real-time quotes and update cards;
  it may also incrementally backfill missing completed sessions, but must not
  save the current unfinished quote. During the A-share lunch break, fetch the
  lunch close once and use it as a transient timing-table preview through
  14:50. After a successful lunch fetch, do not request it again when reopening
  the page in the same process; retry failed or partial fetches no more often
  than every 10 minutes. From
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
  ETF/LOF intraday quotes (user decision 2026-10-03) fall back in order
  TickFlow → Tencent batch (`qt.gtimg.cn`, same-day rows with volume) → Sina
  batch → Wind, each step only for codes still missing; without a TickFlow key
  the chain starts at Tencent. All sources accept same-day quotes only.
  Spread and option updates
  reuse current formal caches on initial automatic loads and explicit load-button
  refreshes; force realtime previews only during A-share quote hours. Its bottom summary table contains ETFs/LOFs only and follows the index
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
  Immediately below the ETF timing table, show the fixed 500,000-yuan daily
  strategy result starting on 2026-08-05. Enter only symbols whose formal signal
  is a fresh buy on that date; an initial hold must first pass through empty and
  then a later buy, while an initial empty waits for its later buy. Start 512890
  at zero and activate each 510500/159967/159552 parking sleeve only after that
  source has first been bought and subsequently sold. Before delayed activation,
  both halves of a half-timing sleeve stay in cash; its first valid buy establishes
  the full sleeve and later sells reduce it to the long-term half. Use formal
  forward-adjusted closes, 100-share lots, 0.006% one-way fees, same-close
  execution, and no network request or result cache. Keep the start-date account
  value at 500,000 and NAV at 1 with setup fees disclosed separately; include
  later fees in daily P&L. Stop before the first incomplete formal session.
- ETF实盘 is rebuilt from broker statements since stock-account opening (user
  decision 2026-10-03): 华宝交割单 + 华宝资金明细 and 银河交割单, imported on the
  page's `交割单导入` tab with a preview and explicit confirmation
  (`services/live_statement_import.py`). Statement rows use `stmt:<broker>:` record
  keys; re-importing a broker replaces only its statement rows inside the new
  file's date range. Manual records are kept; a manual record exactly matching a
  statement row is taken over and its strategy/notes are carried over. A-share
  stocks stay in 微盘实盘 and are skipped. Repo and 金自来 principal stays cash with
  pair income on the return date; LOF 申购 rows are buys and SH-LOF 调帐转入 is not
  counted again; 资金明细 申购预扣款 rows are allocated to the later 转托转入 shares
  as buys; convertible bonds use 张, IPO payment buys the listed code at face value.
  Inter-broker custody moves are not booked. Each broker's preview must reconcile
  ledger cash to statement cash plus open repo principal minus stock cash.
  `services/live_price_history.py` loads closes only for each symbol's held window,
  uses Sina → Tencent → Wind daily bars for convertible bonds (face value before
  listing), and
  carries the previous close over suspended sessions inside the available history.
  The history section adds a category summary (ETF / LOF套利 / 可转债 / 现金管理).
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
  Below the daily-close summary, render a calendar heatmap that switches among
  daily, weekly, monthly, and yearly holding returns and between amount and
  percentage. Daily P&L is the change in cumulative total P&L. Its return base
  is the previous valuation's market value plus positive net new investment
  after offsetting same-day sale proceeds; when the day starts empty, use that
  day's buy cost as the fallback base. Sum amounts and compound daily return
  rates for calendar weeks, months, and years. Use red for gains, green for
  losses, and gray for zero or unavailable values. The daily calendar has only
  Monday-through-Friday columns. Keep weekday A-share holidays as non-return
  cells labeled with their holiday name, and show weekly periods as Monday
  through Friday. Derive these results locally
  without a new table or persisted calculation, exclude unrecorded cash, add no
  market benchmark, and retain the existing cumulative P&L curve.
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
- The `期货实盘` page is separate from ETF `实盘记录`.
  Intraday preview (user decision 2026-10-03): while the page is open, a 120s
  fragment fetches live prices for held contracts only during their futures
  sessions (CFFEX 9:30-11:30/13:00-15:00; commodities 9:00-10:15/10:30-11:30/
  13:30-15:00 plus night sessions, which belong to the next trading day and
  are suspended before holidays). Futures use Sina direct → AkShare spot →
  Wind; options use the Sina option chain → Wind (`wind_option_contract_code`,
  e.g. `I2701-P-700.DCE`, `MO2612-P-6000.CFE`). Formal option daily closes use
  Sina, then Wind when Sina fails or lags the target session. Reject quotes from another trading day;
  when a whole round fails, retry no sooner than ten minutes. Estimate the day's
  mark-to-market P&L as (live − latest formal settlement) × quantity ×
  multiplier and add it to the latest settlement equity. Keep the last quote
  after the close until that trading date's settlement is saved. Quotes stay in
  session state only and never feed caches, curves, calendars or strategy views
  (`services/futures_live_realtime.py`, `components/futures_live/realtime.py`).
  Strategy analysis (user decision 2026-10-02) shows a P&L table and selectable
  cumulative monetary P&L curves for 铁矿石滚贴水, 铁矿石跨期, IM跨期 and
  中证1000卖Put. Iron-ore short puts, assignment and subsequent directional
  rolls belong to 铁矿石滚贴水 throughout. Split shared contracts by execution
  quantity, never by whole-contract labels. Corn is excluded from strategy
  displays but remains in the account reconciliation bridge. Reuse the selected
  close/settlement basis, preserve missing-price gaps, and show unallocated
  account fees/manual adjustments separately. Do not invent strategy capital
  or returns. Read broker monthly
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
  Parse statement cash-transfer and other-funds details into a dedicated daily
  cash-flow ledger. Official rows are read-only; manual deposits and withdrawals
  must be later than the latest statement end, remain deletable, and are taken
  over only by a unique exact statement match. Daily close-based P&L is the
  change in cumulative close-based net P&L and excludes deposits/withdrawals.
  Its return denominator is previous completed-session economic equity plus
  positive same-day net deposits, where economic equity is cumulative net
  deposits plus cumulative close-based net P&L. Do not reduce the denominator
  for net withdrawals. Compound daily rates for week, month, and year views,
  and label post-statement results as `待月结单确认`.
  The account trend and return calendar must switch between `盯市` and `收盘`,
  defaulting to `盯市`. Settlement-based daily P&L is the change in cumulative
  settlement net P&L, deducts execution fees only, and uses settlement economic
  equity in its own return denominator; never mix close and settlement amounts
  or denominators. If historical settlements are incomplete, allow an
  account-level manual daily P&L from 同花顺 without creating contract prices.
  Reconcile it automatically when formal data arrives: differences within
  0.05 yuan use the formal value, larger differences require an explicit
  `采用手工` or `采用正式` choice, and unresolved differences pause formal
  cumulative confirmation. Manual daily-P&L records remain deletable.
  Backfill formal daily prices for every historically traded contract, including
  closed or expired contracts, using append-only date inserts. Exception (user
  decision 2026-10-01): an option no longer held whose whole holding period is
  covered by the latest monthly statement uses the statement as authoritative;
  do not backfill or warn about its missing exchange settlements. Iron-ore option
  expiry requires the underlying contract's official expiry-day settlement.
  Keep each strike independent; confirmed short-put assignment removes the
  option and creates the same quantity of long underlying futures at the strike.
  Pause formal valuations after an unresolved expiry, and let a later statement
  reconcile and take over confirmed manual expiry records.
- Analysis pages should follow the same control layout: keep analysis settings
  in the sidebar, and keep data input, upload, API key fields, and run/analyze
  buttons in the main page area.

## Structure

- `app.py`: Streamlit home page.
- `pages/`: Streamlit pages. Numeric prefixes control sidebar ordering. User
  decision 2026-10-02: `指数监控`, `持仓分析`, `ETF实盘`, `微盘实盘`, `期货实盘`,
  then the remaining pages in their previous order (`A股分析`, `策略回测`,
  `相关性分析`, `期货期权`, `期货价差`, `美股分析`, `微盘股`, `任务与数据`).
- `components/backtest/`: strategy-backtest mode components and annual ETF page workflow.
- `components/live_record/`: ETF live-record valuation, tables, trade forms, and history views.
- `components/futures_live/`: futures-live refresh, account, manual-entry, and history views.
- `components/position/`: holdings realtime fragment, cards/tables, detail views, and page coordination.
- `services/live_trading.py`: local live-trade ledger and position-cost calculations.
- `services/fund_rotation.py`, `services/annual_etf_portfolio.py`,
  `services/portfolio_audit.py`, and `services/portfolio_audit_analysis.py`:
  stable compatibility facades; focused implementations use the matching
  `fund_rotation_*`, `annual_etf_*`, and `portfolio_audit_*` sibling modules.
- `services/index_ma20.py` and `services/update_tasks.py`: stable compatibility
  facades; index configuration, source adapters, history/signals, validation,
  persistence, and orchestration live in `index_*` and `index_update_*` siblings.
- `services/futures_live_trading.py`: stable compatibility facade for the
  `futures_live_*` statement, repository, position, price, settlement, and P&L modules.
- `services/position_analysis.py`: stable compatibility facade for the
  `position_*` model, session, runtime, timing, market, and derivatives modules.
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

For TickFlow fund/ETF/stock daily prices, use the shared explicit adjustment
constants from `services.fund_analysis`. Plain forward adjustment means
`forward_additive`; expose `forward` only as the clearly labeled ratio-adjusted
legacy-comparison option. Always pass `none` explicitly for unadjusted prices;
legacy Python `None` is accepted only at the normalization boundary.

Fund price caches use versioned, adjustment-specific keys such as
`fund_close_v2_159545.SZ_forward_additive`. Do not fall back to the legacy
unversioned cache. Unadjusted formal closes remain append-only by date. For an
adjusted cache, fetch a recent overlap first: append unseen dates when overlap
prices are unchanged, and rebuild only that symbol when an upstream corporate
action changes historical adjusted prices. Validate the complete replacement
before the existing atomic save. If drift was detected but rebuilding fails,
keep the last card/intraday quote visible, mark formal history invalid, and
suppress timing signals, recent guidance, and 512890 parking decisions.

`scripts/migrate_fund_price_caches.py` is preview-only unless `--apply` is
provided. It reads the API key only from `TICKFLOW_API_KEY` and migrates symbols
sequentially. Keep generated cache and adjustment-audit outputs uncommitted.

## Market Data Notes

- Use `services.market_fallback.fetch_market_fallback` for ordered compatible
  history failover. Exception, empty/invalid data, and a missing requested latest
  completed date must continue to the next candidate. A successful current
  response stops further requests. When all sources lag, return the newest
  valid partial history with `market_source_warning`; callers must still report
  gaps. Never treat a partial result as a confirmed current close or replace a
  cached history with it. Source adapters must enforce exact instrument identity
  and adjustment basis; quote/chain/settlement data are separate capabilities.
- `services.akshare_sources` lists compatible unadjusted security and futures
  daily candidates. Tencent/Sina adjusted prices must not silently replace
  TickFlow additive or EastMoney additive histories. An API with no compatible
  alternative retains its explicit failure. `core.network` supplies default
  HTTP timeouts to page and scheduled fallback calls as well as Web requests.
- `services.wind_source` is the last-resort Wind (万得) backup, enabled only when
  `WIND_API_KEY` is set (never commit, log, or deploy the key; tests clear it in
  `tests/conftest.py`). It sends plain JSON-RPC `tools/call` requests to
  `mcp.wind.com.cn`, needs no cache of its own, and pauses itself for five
  minutes after a network/auth/quota failure. Wind fuzzily resolves unknown
  codes (`SA2701.CZC` returned the US stock `SA.N`), so keep only rows whose
  `Wind代码` equals the requested code; an unrecognised code is dropped from its
  batch. Wind is tried only after every other compatible source failed or
  lagged: index formal daily gaps (`INDEX_WIND_CODES`, append-only, never into
  the source-correction overlay), transient index/ETF/stock/futures quotes,
  unadjusted ETF and microcap closes, microcap post-close snapshots (stamped at
  or after 15:00), TXWP20 intraday constituents, and exact futures contracts.
  Main-continuous futures use Wind for transient card quotes only
  (`INDEX_WIND_QUOTE_ONLY`) because vendors roll on different dates. Wind's
  forward adjustment is not the additive basis, so adjusted ETF histories never
  use it. BK1158 has no Wind equivalent; `868008.WI` is a different index.
  Independent verification sources must not be replaced by Wind.

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
- The fourth strategy mode is `年度动态组合`. Keep its registry and index-family
  whitelist versioned under `config/`; the registry is a current-survivor snapshot,
  so the UI and reports must disclose survivor bias and must not claim to remove it.
  Historical proxy data may extend research history but must never make an ETF
  tradable before its listing date. Same-index older ETFs take precedence over an
  official index proxy, and an official index proxy requires its publication date
  and source URL before it can be used at an annual decision date.
- Annual dynamic selection uses only data available through the prior year-end,
  at most five calendar years, split by actual trading days 70%/30%. Enforce at
  least 504 screening days and 252 validation days after that exact split. Search
  MA10/15/20/25/30 and thresholds 0%/0.5%/1%/1.5%/2%, using the documented
  percentile composite score and the 10% validation annual-return gate.
- The annual page flow is cache-only preflight, explicit second confirmation for
  batched network completion, then a separate run action. Persist no new database
  table. Runtime market data, hash-keyed atomic checkpoints, and reports stay under
  ignored local data/output directories. A failed fetch must retain the old cache.
- Annual simulation invests once, never rebalances across directions, applies a
  same-ETF parameter change from the first close of the new year, and delays a
  changed-ETF migration until the old signal exits while always replacing the
  pending destination with the newest annual choice. Keep 512890 sleeves separate
  by role and leave their pre-2019-01-18 capital in interest-bearing cash.
  Report the same-close main result, annual immediate-switch hold benchmark,
  all-512890 benchmark, and frozen-selection next-close stress result.
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
  sells, with 100-share lot-size rounding by default. An optional base-position
  percentage (default 0) buys that share of capital at the first close and holds
  it throughout; only the remaining capital follows the MA signal, and holding
  days/win rate describe that timing sleeve.
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

## Numerical Formatting & Display Precision (数值格式化与精度规范)

**核心铁律：所有面向用户展示的数值、UI组件、图表悬浮提示（Tooltip）及导出文件，必须显式做格式化，严禁直接输出原生浮点数（Raw Floats）或未截断的长尾小数。**

1. **后端/数据层强制预格式化**：
   - 不依赖前端图表库的隐式数字转换；在 Python / 数据组装阶段预先生成格式化字符串，再传入图表或导出结构。
   - 金额、收益率、回撤、净值、价格和点数按字段语义明确保留精度。
2. **导出文件**：
   - 写入 CSV、Excel、JSON 前显式 round 浮点列，避免 IEEE 754 累加长尾误差。

## Position Web

- `web/backend/` adapts existing `services/position_*` to FastAPI; never duplicate
  MA, parking, fee, or simulation algorithms in HTTP adapters or TypeScript.
- `web/frontend/` is the Chinese mobile React UI. Keep formal and preview fields
  distinct. Existing Streamlit pages remain supported.
- Run one API worker/replica. The coordinator owns shared transient quotes and
  uses the existing runtime cadence. GET endpoints read published snapshots.
- `INVESTMENT_RUNTIME_DIR` overrides runtime paths; Compose binds external data
  to `/runtime`. Keep secrets, quote caches, DBs and build outputs out of Git/images.
- Index updates must use the stable `services.update_tasks` facade and select
  only the position reference indexes. No recent formal gap means no network.
- Validate Web changes with `tests/test_position_web.py`, existing position tests,
  frontend build and browser smoke tests. See `deploy/README.md` for operations.

## Reminder deployment

- All Fangtang/Hermes/WxPusher sends require explicit ENABLE_FANGTANG / ENABLE_WECHAT /
  ENABLE_WXPUSHER; missing flags fail closed. REMINDER_DRY_RUN blocks every send,
  including tests. WxPusher (WXPUSHER_APP_TOKEN + WXPUSHER_UIDS/WXPUSHER_TOPIC_IDS) is
  plain HTTPS, allowed on Lightsail, ETF position alerts only; enable each channel on
  one production node because delivery ledgers are per node.
- Lightsail runs only ETF reminders, with Hermes always disabled. Never deploy
  iron-ore reminders or install Hermes on Lightsail. Windows iron ore stays WeChat only.
- Keep ETF signal computation in existing position services. Event/channel
  receipts live in a cross-platform SQLite ledger under REMINDER_STATE_DIR.
  Pending/uncertain delivery must not be blindly retried; only confirmed success
  is sent. Notify once per A-share trading day at 14:50 Asia/Shanghai (user
  decision 2026-09-29, replacing the former five slots) and keep the market calendar.
- Deploy units and operations: deploy/REMINDERS.md. During migration the timer
  is dry-run; activating Fangtang requires user confirmation that Windows Fangtang stopped.

## Microcap20 ABCD

- Automatic simulation batches TickFlow close quotes in groups of at most five and stops new TickFlow requests after a rate-limit response in that run. Unadjusted daily fallback is EastMoney, Tencent, Sina, then optional Wind. Request the previous trading session alongside the target close for limit checks, and preserve original-session limit metadata during historical repairs. A formal close may repair valuation while unknown execution fields remain provisional. Remaining provisional warnings must return a data gap, not a successful complete update.

- Exclude confirmed signal-day suspensions before taking the smallest 20. Freeze the entire candidate order with each rebalance plan. On execution-day suspension, substitute using that frozen order, never execution-day capitalization. Held suspended stocks still occupy the 20-position cap; limit-up cancellations do not get replacements.
- Offline research replay lives in services/microcap_rotation_replay.py and must reconcile fills, inventories, cash, fees and all 72 display values before replacing the active research archive. User authorized replacing the old research display.

- Keep the ABCD facade in services/microcap_rotation.py; UI and CLI share it.
- New simulation accounts require explicit enablement; browsing is cache-only.
- Preserve immutable input batches, next-session execution, fixed 10000-yuan stock sizing, and 2-yuan fill fees.
- Strict v1 inputs remain fail-closed. User-authorized v2 prioritizes execution: missing prices skip only affected trades, carry valuations are marked, and unknown eligibility/events remain explicitly provisional. Never mark evidence complete merely because an event list is empty.
- v2 launches today from the previous saved close/snapshot; never create intraday fills for a close-only account.
- Historical union research stays separate from forward simulation and live_trades.
- Use tests/test_microcap_rotation.py and tests/test_microcap_rotation_ui.py; see docs/microcap_rotation.md.
