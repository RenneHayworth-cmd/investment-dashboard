# 《ETF 量化择时与组合管理实战手册》

> 一本基于真实运行系统的工程手册。全部 28 章结合当前项目（`investment-dashboard`）的真实代码、真实策略参数与真实回测结果写成；所有回测数字可由 [repro_hb_backtest.py](repro_hb_backtest.py) 在项目本地缓存上一键复现，原始输出见 `output/handbook_backtest_results.json`。

## 这本书怎么读

每章固定七节：**理论 → 数学原理 → 当前项目实现 → 代码分析 → 潜在错误 → 改进方法 → 实际案例**。第一部分（1–2 章）是全景；数据工程（3–8 章）与择时引擎（9–13 章）是理解一切的地基；回测体系（14–20 章）是全书的重心；实盘与运维（21–28 章）把理论钉到每天的操作上。

时间紧张的读者建议路径：**1 → 9 → 11 → 12 → 14 → 15 → 21 → 28**（全景 + 择时数学 + 停车机制 + 两台回测引擎 + 50 万模拟 + 缺陷清单）。

## 目录

### 第一部分 全景

| 章 | 标题 | 覆盖模块 |
|---|---|---|
| [01](ch01-导论与项目全景.md) | 导论：从 MA20 择时到组合管理 | `app.py`、页面清单、五条铁律 |
| [02](ch02-系统架构与代码组织.md) | 系统架构与代码组织：门面模式与模块拆分 | 全部 facade、`components/` 分层 |

### 第二部分 数据工程

| 章 | 标题 | 覆盖模块 |
|---|---|---|
| [03](ch03-运行时与缓存内核.md) | 运行时与缓存内核 | `core/paths.py`、`core/db.py`、`core/cache.py`、`core/indicators.py` |
| [04](ch04-交易日历与三档时间语义.md) | 交易日历与三档时间语义 | `services/market_calendar.py` |
| [05](ch05-指数数据源与容错路由.md) | 指数数据源与容错路由 | `index_config.py`、`index_source_router.py`、`index_sources_*`（6 个） |
| [06](ch06-只追加历史与更新编排.md) | 只追加历史与更新编排 | `index_update_*`（5 个）、`update_tasks.py`、定时脚本 |
| [07](ch07-盘中行情与正式数据隔离.md) | 盘中行情与正式数据隔离 | `index_realtime.py` |
| [08](ch08-ETF复权体系与版本化基金缓存.md) | ETF 复权体系与版本化基金缓存 | `fund_analysis.py`、`position_market.py` 数据层、迁移/审计脚本 |

### 第三部分 择时引擎

| 章 | 标题 | 覆盖模块 |
|---|---|---|
| [09](ch09-MA带状择时的理论与数学.md) | MA 带状择时的理论与数学 | 四份状态机实现的对照 |
| [10](ch10-指数MA20引擎.md) | 指数 MA20 引擎 | `index_signals.py`、`index_frames.py`、`index_history.py`、指数监控页 |
| [11](ch11-ETF择时快照引擎.md) | ETF 择时快照引擎 | `position_timing.py`、参数表、`position_models.py` |
| [12](ch12-512890停车ETF.md) | 512890 停车 ETF：聚合状态机 | `calculate_512890_parking_snapshot`、袖套承接 |
| [13](ch13-实时刷新调度与收盘确认链.md) | 实时刷新调度与收盘确认链 | `position_sessions/runtime`、`components/position/realtime.py`、交易提醒脚本 |

### 第四部分 回测体系

| 章 | 标题 | 覆盖模块 |
|---|---|---|
| [14](ch14-单标的MA择时回测引擎.md) | 单标的 MA 择时回测引擎（逐段精读） | `fund_rotation_timing.run_ma20_timing_backtest` |
| [15](ch15-组合择时与半仓袖套.md) | 组合择时、半仓袖套与空仓承接 | `run_portfolio_timing_backtest`、`_run_delayed_timing_sleeve` |
| [16](ch16-回测指标体系与分期报告.md) | 回测指标体系与分期报告 | `fund_rotation_metrics.py`、`fund_rotation_summary.py` |
| [17](ch17-多基金动量轮动.md) | 多基金动量轮动 | `fund_rotation_momentum.py`、`fund_rotation_data.py` |
| [18](ch18-年度动态组合.md) | 年度动态组合 | `annual_etf_*`（7 个）、注册表/白名单配置 |
| [19](ch19-组合严格审计.md) | 组合严格审计 | `portfolio_audit_*`（11 个）、`run_etf_portfolio_audit.py` |
| [20](ch20-动态阈值两阶段研究.md) | 动态阈值两阶段研究 | `dynamic_threshold_research.py`、研究编排脚本 |

### 第五部分 组合与实盘

| 章 | 标题 | 覆盖模块 |
|---|---|---|
| [21](ch21-持仓分析页与50万模拟策略.md) | 持仓分析页与 50 万模拟策略 | `position_performance.py`、`components/position/` |
| [22](ch22-ETF实盘记录与收益日历.md) | ETF 实盘记录与收益日历 | `live_trading.py`、`live_account.py`、`components/live_record/`、`core/return_calendar.py` |
| [23](ch23-期货实盘-结单解析与对账.md) | 期货实盘（上）：结单解析与对账 | `futures_live_models/parser/repository/calendar` |
| [24](ch24-期货实盘-盈亏口径与结算.md) | 期货实盘（下）：双口径盈亏与结算 | `futures_live_positions/prices/settlements/pnl/daily_pnl`、`components/futures_live/` |
| [25](ch25-多品种工具箱.md) | 多品种工具箱：价差/期权/相关性/美股/微盘股 | `futures_spread.py`、`futures_options_analysis.py`、`correlation_analysis.py`、`us_stock_analysis.py`、`microcap.py` |
| [26](ch26-风险管理与通知体系.md) | 风险管理、通知体系与告警脚本 | `price_alerts.py`、`notify/`、风控分层 |
| [27](ch27-自动化调度与运维.md) | 自动化调度与运维 | 全部计划任务脚本、`pages/9_任务与数据.py` |
| [28](ch28-缺陷清单与研究议程.md) | 缺陷清单、改进路线图与研究议程 | 全项目问题分级与修复路线 |

## 真实回测数据的来源

- **数据**：项目本地缓存（Windows 运行时目录 `~/investment_dashboard_data`），ETF 前加复权日线截至 **2026-09-02**（14 只，共 69 个缓存文件），指数收盘确认历史截至 **2026-08-14**（21 个监控对象）；
- **引擎**：项目自身代码（`run_ma20_timing_backtest`、`run_portfolio_timing_backtest`、`build_position_timing_performance`、`calculate_512890_parking_snapshot`），在 WSL 的项目 `.venv`（Python 3.12.3 / pandas 3.0.5）中运行，全程零网络请求；全部数字在"统一状态机重构"（`services/ma_timing_core.py` + `core/metrics.py`，见第 9、28 章）前后的引擎上各重放一次，结果逐字节一致；
- **关键数字**：冻结十 ETF 组合 2025-02-27→2026-09-02 总收益 **+36.27%**（基准 +25.60%）、最大回撤 **-4.87%**（基准 -12.17%）、夏普 **2.02**；50 万模拟策略 2026-08-05→09-02 累计 **-0.80%**（净值 0.9920，13 笔交易）；各 ETF 成立来/近一年分期明细见第 9、14、16、21 章案例。

## 配套文档

仓库根目录另有一组项目级文档：[README](../../README.md)（运行与维护）、[ARCHITECTURE](../../ARCHITECTURE.md)、[STRATEGY](../../STRATEGY.md)、[BACKTEST](../../BACKTEST.md)、[RISK](../../RISK.md)、[RESEARCH](../../RESEARCH.md)。

## 勘误与维护

本手册的行号引用基于写作时的代码状态（2026-09-03），后续重构可能漂移；函数名、常量名与口径描述以代码为准。发现不一致时，优先按第 28 章的方法论处理：先写测试钉住现状，再决定哪一侧是标准。
