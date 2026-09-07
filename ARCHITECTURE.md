# ARCHITECTURE.md — 系统架构

> 配套文档：[README](README.md) · [STRATEGY](STRATEGY.md) · [BACKTEST](BACKTEST.md) · [RISK](RISK.md) · [RESEARCH](RESEARCH.md) · [实战手册（28 章）](docs/handbook/README.md)

## 1. 总览

本地 Streamlit 多页应用 + SQLite/CSV 持久层，运行于 WSL（Windows 侧只做计划任务闹钟）。

```
Windows 计划任务 (ps1)  ──wsl.exe──▶  守护脚本 (scripts/*.py, fcntl 单实例锁)
Streamlit (app.py → pages/ → components/ → services/)  ──▶  core/ (cache/db/paths)
                                                          ──▶  data/raw/*.csv + cache.db
网络源: TickFlow / AkShare / EastMoney / Sina / Yahoo / CBOE / 妙想 / 恒生官网 / 大商所 / 中金所
```

依赖方向单向：`pages/components → services(门面) → services(实现) → core → pandas/sqlite`；服务层不得反向依赖页面组件。

## 2. 目录与模块地图

```
app.py                  首页：缓存统计 + 导航（刻意轻量，79 行）
pages/                  12 个页面壳（数字前缀=侧边栏顺序）
components/
  backtest/             策略回测四模式（ma_timing / portfolio_timing / fund_rotation / annual_*）
  position/             持仓页（coordinator / realtime / cards_tables / details / performance / formatting）
  live_record/          ETF 实盘（dashboard / valuation / tables / trades / cash / history / account）
  futures_live/         期货实盘（refresh / account / manual / history / formatting）
  position_table.py     持仓类 HTML 表格共享样式
services/
  ma_timing_core.py     ★ MA 带状择时唯一定义（状态机/触发线/参考账户模拟器）
  core 指标 → core/metrics.py ★ 绩效指标唯一定义（播种/年化/夏普/回撤/水下期）
  index_* (config/sources/router/realtime/signals/frames/history/ma20门面
           + update_frames/models/orchestration/persistence/validation, update_tasks门面)
  fund_rotation(门面) + fund_rotation_(data/models/metrics/momentum/summary/timing)
  annual_etf_(models/market/selection/simulation/checkpoint/report/portfolio门面)
  portfolio_audit(门面) + portfolio_audit_(models/data/execution/engine/metrics
                  + analysis[_core/_full_history/_missed_orders]/tracking)
  position_analysis(门面) + position_(models/sessions/runtime/market/timing/performance/derivatives)
  live_trading / live_account
  futures_live_trading(门面) + futures_live_(calendar/models/statement_parser/repository
                            /positions/prices/settlements/pnl/daily_pnl)
  futures_spread / futures_options_analysis / correlation_analysis / us_stock_analysis
  microcap / price_alerts / notify/ (models/channels/engine/server)
core/                   paths / db / cache / indicators / metrics / return_calendar / ui
config/                 frozen_strategy_20260728.json / annual_etf_registry_v1.csv /
                        annual_etf_index_families.json / etf_portfolio_audit.json /
                        dynamic_threshold_research.json
scripts/                计划任务与迁移脚本；scripts/research/（注册表/网格引擎/验证管线）
tests/                  23 个测试文件（~600KB）
docs/handbook/          实战手册 28 章 + 复现脚本
```

## 3. 兼容门面（facade）机制

三个手法并存：

1. **`_call()` 依赖注入**（`position_analysis` / `futures_live_trading`）：调用前把被测试 patch 过的门面符号临时 setattr 到实现模块，调用后恢复；
2. **globals 回写**（`update_tasks`）：每次调用前把门面 globals 同步进子模块 `__dict__`（"Keep legacy patch targets effective"）；
3. **namespace 注入**（`components/annual_etf_dynamic.py`）：把门面 `globals()` 整个传给实现函数。

规则：新增业务逻辑进职责模块；门面只做转发与旧签名维护。

## 4. 数据流：从网络到展示

### 4.1 指数正式日线（append-only 四层）

```
fetch_index_report(每指数):
  读 index_history / index_final_history / index_source_correction_history
  effective = history ←overlay← finalized ←overlay← correction   (内存叠加)
  缺口: <252行→bootstrap 1000天; 否则增量30天; 20交易日缺口→(target-min).days+14
  TickFlow 主路径 → fetch_one_index (TickFlow→路由器10源) → filter_completed_market_dates
  append_cached_index_rows → 三缓存 + sync_index_long_history     (行数不增不写)
run_index_ma20_update:
  今日缓存短路 → 并发4 → merge/enrich/sanitize/trim(120)
  → save_dataset("index_ma20_latest")  ← 唯一整文件覆盖写（展示层）
  → verify_updated_index_data (独立源, ±0.20%, 只读)
```

时间语义三档（`market_calendar`）：`expected_latest_trade_date`（页面预期）、
`latest_completed_trade_date`（可入正式缓存的判定）、`latest_settled_trade_date(+10min)`
（唯一允许写今天的判定）。期货夜盘开盘中目标日回退前一交易日。

### 4.2 盘中报价（永不落盘）

`index_realtime.fetch_realtime_quotes`（并发8，仅开市标的）→ `_RUNTIME_QUOTE_CACHE`
（进程内存，锁保护）→ `apply_realtime_quotes_to_summary`（副本上覆盖四列+来源两列，
拒绝早于缓存日期的报价）。ETF 侧同构：`position_runtime` 三层节流
（fragment 120s → band TTL → 进程级 band TTL+午间单次闸），预览信号 =
正式历史 + 当日瞬时价（临时行），`initial_state=0` 冷启动。

### 4.3 ETF 基金缓存（版本化）

键 `fund_close_v2_{symbol}_{adjust}` + period（`5000_1d`）；行级元数据
`_adjust_mode/_cache_schema_version/_final_close_confirmed`；前加复权重叠变化→全量重建，
失败→`formal_history_valid=False` 抑制全部下游信号。择时默认 `forward_additive`，
实盘估值 `none`，161128 走东财/AkShare 特例链（新浪兜底需复权因子恒等校验）。

### 4.4 期货实盘

月结单（`^\d+_(\d{4}-\d{2}).xls(x)`）→ sha256 幂等导入（SAVEPOINT）→
月末账户/持仓/成交/流水四表 → 手工成交/流水按 broker_trade_id 或精确五元组
（唯一候选→已接管；多候选→待核对；0→保持手工且参与计算）→ 双口径日账本
（盯市=结算价+扣成交/行权费；收盘=收盘价+扣全部费+月度残差挂账）→
同花顺日盈亏 0.05 元容差对账接管。

## 5. 存储结构

### 5.1 文件（core.cache，事务性）

`save_dataset`：临时文件 → fsync → flock（Windows 占位）→ backup → `os.replace` →
SQLite UPSERT；失败回滚。`datasets` 表唯一键 `(symbol, source, data_type, period)`。
`cache.db` 路径按平台：Windows→`~/investment_dashboard_data`，WSL→仓库根。

### 5.2 SQLite 表（core/db.py，12 张）

`datasets` / `jobs` / `correlation_results` / `live_trades`（record_key 幂等）/
`live_cash_flows` / `futures_statement_imports` / `futures_account_monthly` /
`futures_month_end_positions` / `futures_live_trades`（部分唯一索引 broker id）/
`futures_daily_closes`（close+settlement 双价，settlement 只填 NULL）/ `futures_cash_flows`
（source_key UNIQUE）/ `futures_option_expiry_events` / `futures_daily_pnl_overrides`。

### 5.3 缓存层命名（指数）

| source | 语义 | 写入 |
|---|---|---|
| `index_history` | 累计原始 | append-only |
| `index_final_history` | 收盘确认 | append-only |
| `index_source_correction_history` | 源校正 | append-only |
| `index_long_history` | 长历史研究 | append-only（空不初始化） |
| `index_ma20_latest` | 展示报告 | 整文件覆盖 |
| `index_futures_main_contracts` | 主连合约映射 | merged 覆盖 |
| `index_futures_contract_{X}` | 当前具体合约日线 | append-only |

## 6. 统一定义库（防口径漂移）

- `services/ma_timing_core.py`：`ma_threshold_states`（信号语义，带内维持信号状态）与
  `threshold_desired_position`（账户自愈语义，带内维持实际持仓）；`simulate_timing_account`
  支持 after_close / next_open / next_close 与滑点。
- `core/metrics.py`：播种收益、日历日年化（≤-100% 返回 -1）、√252 波动、
  `sharpe_ratio(risk_free_annual)`、initial_capital 播种回撤、自然日水下期、Calmar、胜率。
- 消费方：`fund_rotation_*`、`position_timing`、`annual_etf_selection`（部分）、
  `portfolio_audit_*`、`dynamic_threshold_research`、`scripts/research/*`。
- 重构验证：全部回测数字在重构前后重放**逐字节一致**（见 BACKTEST.md §6）。

## 7. 调度与运维矩阵

| 任务 | 触发 | 幂等机制 | 退出/状态 |
|---|---|---|---|
| 指数正式数据 | 15:10 / 16:10 | pending 判定 + use_fresh_cache=False | exit 0/1/2 |
| 铁矿石告警 | 每分钟 | 边沿触发状态机 + 临时目录测试隔离 | output/alerts/*.json |
| 交易提醒 | 9:45/11:45/14:45/14:50/14:54 | slot 去重 + 先卖后买净变化 | 双通道推送 |
| 微盘股快照 | 计划任务 | (日期,代码) 去重 keep last | 共享缓存 |

Windows 计划任务 → `wsl.exe -d Ubuntu -- ...` 桥接；`fcntl.flock` 单实例锁。
手动兜底：`任务与数据` 页两段式更新（本地预览 → 行级签名防 TOCTOU → 确认执行+复核）。

## 8. 已知架构约束

- 守护脚本顶层 `import fcntl`（仅 WSL 可跑）；`core.cache` 已加 Windows 占位锁守卫但无跨进程保护；
- `datasets.file_path` 存绝对路径，跨机迁移需重建元数据或直接读 CSV；
- SQLite 无 WAL，多进程并发写偶发 `database is locked`（timeout=30 缓解）；
- 进程级行情缓存多 worker 部署时各进程独立（午间单次闸失效）；
- 静态假期表仅覆盖 2026 年（2027 年起依赖 exchange_calendars 或人工更新表）。

细节与逐行分析见实战手册第 1–8、26–28 章。
