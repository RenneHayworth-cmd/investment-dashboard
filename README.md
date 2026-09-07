# Investment Dashboard

个人投资分析工作台：本地 Streamlit 应用，覆盖指数监控、ETF 均线择时、策略回测、
组合审计、持仓与实盘记录、期货实盘与告警自动化。全部行情缓存与账本数据保存在本地，
不依赖任何云端服务。

## 文档

| 文档 | 内容 |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | 系统架构：分层、门面模式、数据流、缓存与表结构 |
| [STRATEGY.md](STRATEGY.md) | 策略与参数：MA 带状择时、逐 ETF 参数表、512890 停车、组合配置 |
| [BACKTEST.md](BACKTEST.md) | 回测体系：四台引擎、指标口径、审计与研究流程、真实回测基线 |
| [RISK.md](RISK.md) | 风险管理：数据质量闸门、执行约束、告警体系、已知风险 |
| [RESEARCH.md](RESEARCH.md) | 研究议程：冻结与样本外纪律、研究管线、未决问题 |

**[《ETF 量化择时与组合管理实战手册》（docs/handbook/，28 章）](docs/handbook/README.md)**
是上述内容的完整展开：每章含理论、数学原理、项目实现、代码分析、潜在错误、
改进方法与实际案例，所有回测数字可由
[repro_hb_backtest.py](docs/handbook/repro_hb_backtest.py) 在本地缓存上复现。

## 本地运行

```bash
cd /home/renne/investment_dashboard
.venv/bin/python -m streamlit run app.py
```

如需本地运行时自动带入 TickFlow API Key，可以先设置环境变量：

```bash
export TICKFLOW_API_KEY="你的TickFlow API Key"
.venv/bin/python -m streamlit run app.py
```

各页面的 TickFlow API Key 输入框会默认读取这个环境变量，仍可在页面里手动覆盖。

依赖安装只用项目虚拟环境：

```bash
.venv/bin/python -m pip install -r requirements.txt
```

代码改动至少执行一次编译检查：

```bash
.venv/bin/python -m compileall app.py core services pages
```

## 数据缓存设计

- `data/raw/`: 原始行情 CSV
- `data/processed/`: 计算后的结果 CSV
- `output/`: 导出文件、计划任务日志/锁/告警状态、审计与研究产物
- `cache.db`: SQLite 元数据、任务记录与实盘/期货账本

期货实盘在 `cache.db` 中保存月结单导入结果、月末账户与持仓、成交明细、逐日资金
流水、账户级手工日盈亏、期权到期事件和正式收盘/结算价。券商源结算单始终只读。

首页保持轻量，只展示缓存数据集数量、最近缓存更新时间、最近交易日、今日重点和
常用入口；具体分析从左侧页面进入。

## 代码模块边界

大型业务模块保留原导入路径作为兼容门面，调用方无需修改；具体实现按职责拆到
同名前缀的兄弟模块中：

- 基金轮动、年度 ETF 和组合审计分别位于 `services/fund_rotation_*`、
  `services/annual_etf_*`、`services/portfolio_audit_*`。
- 指数配置、行情源、历史与信号位于 `services/index_*`，更新验证、持久化和编排
  位于 `services/index_update_*`。
- 期货实盘的月结单、仓储对账、持仓到期、正式价格和盈亏位于 `services/futures_live_*`。
- 持仓分析的配置、交易时段、运行时行情、择时、正式行情和衍生品位于 `services/position_*`。
- 四个大型页面的展示与交互分别下沉到 `components/backtest/`、`components/live_record/`、
  `components/futures_live/` 和 `components/position/`；`pages/` 中的原页面继续负责
  初始化与路由。

MA 带状择时信号与绩效指标各有唯一定义：`services/ma_timing_core.py`（状态机、
触发线与参考账户模拟器）与 `core/metrics.py`（播种收益、日历日年化、夏普、回撤、
水下期等），各子系统按需包装，不再各自实现。兼容门面显式维护旧常量、类型、函数
签名和默认值。新增业务逻辑应进入对应职责模块，不应重新堆回门面或让服务层依赖
页面组件。

## 指数更新

「指数监控」页面优先展示本地缓存。Windows计划任务每天15:10和16:10各检查一次：
15:10补齐A股、日盘期货及当时已收盘市场的正式数据，16:10补齐港股并重试仍有缺口的
项目。后台任务只更新已完成交易日的正式日线；已缓存目标交易日的指数直接跳过，
周末和休市无缺口时不联网。已打开的页面每30秒只检查一次本地缓存版本，发现定时
任务完成后自动重绘上方卡片和下方MA20，该页面检查不会联网。点击「更新指数数据」
仍可手动更新：交易中的标的获取一次只读实时报价，A股、港股、日股和境内期货在各自
午休期间首次点击时获取一次上午收盘报价。盘中和午间报价只保存在当前 Streamlit
进程的临时内存中，不参与MA20计算，也不写入日线历史。指数原始历史保持只追加，另用
只追加的收盘确认日线保存已完成交易日；计算时由收盘确认日线覆盖同日盘中旧值，
因此不会改写原始历史。累计缓存不足252条时会回补最多1000天历史，MA20从累计历史
计算；页面分列报表固定保留最近120个自然日，每个交易日分别展示当时最近一次MA20
状态转变日期及从转变日收盘价起算的区间涨幅。页面下方MA20汇总表同时显示上一状态
转换时间，以及从上一转换日收盘价到当前转换日收盘价的完整上一区间涨幅。

计划任务可通过 Windows PowerShell 安装（不需要打开 Codex 或 Streamlit）：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File \\wsl.localhost\Ubuntu\home\renne\investment_dashboard\scripts\install_index_ma20_update_tasks.ps1
```

任务日志保存在 `output/logs/index_ma20_scheduled.log`；单实例锁会防止15:10和16:10
的任务重叠。计划任务会通过 WSL 登录 shell 读取非交互环境中的 `TICKFLOW_API_KEY`，
但不会复用页面输入框中的密钥；未加载密钥时会在日志中明确说明并使用免费/公开数据源，
不记录密钥内容。

「任务与数据」页面也提供"更新缺失的正式指数数据"入口；首次点击只读取本地缓存，
列出截至目标日期最近20个交易日内的缺口、预计更新范围和数据源，二次确认后才联网
更新。更新完成后会尽可能用独立日线源复核同一目标交易日的收盘价（容差 0.20%），
复核结果不覆盖正式缓存。该入口只补齐已经完成的交易日，不获取盘中卡片报价。

CSV 缓存写入使用同目录临时文件、文件锁和原子替换；SQLite 元数据写入失败时会恢复
原文件，避免并发页面或计划任务读取到截断数据。

## 复权与基金缓存

场内基金、ETF和股票统一使用明确的五种复权参数：前复权（差值）、前复权（比例）、
后复权（差值）、后复权（比例）和不复权。普通"前复权"默认指差值口径，以便与东方
财富、同花顺保持一致；比例口径仅作为复现旧结果的明确选项。新版行情缓存使用带版本
和复权方式的独立键，例如 `fund_close_v2_159545.SZ_forward_additive`，不再回退到旧
口径缓存。不复权正式收盘按日期只追加；复权缓存更新时先比较近期重叠交易日，价格
不变只追加新日期，分红等事件导致历史回溯变化时才全量重建该标的。重建失败会保留
最后可用的卡片价格，但暂停该标的择时信号、操作指引及512890承接判断。

新版缓存迁移命令默认只读预览，只有传入 `--apply` 才从环境变量读取
`TICKFLOW_API_KEY`、串行联网并写入：

```bash
.venv/bin/python scripts/migrate_fund_price_caches.py
.venv/bin/python scripts/migrate_fund_price_caches.py --apply
```

需要比较差值与比例前复权对现有参数的影响时，可运行
`.venv/bin/python scripts/audit_fund_adjustment_impact.py`；报告只做口径审计，
不自动调整均线、阈值、仓位或组合权重。

## 铁矿石价格微信提醒

`scripts/monitor_iron_ore_price.py` 独立监控铁矿石主连 `I0`。价格首次低于
730元/吨时通过Server酱推送普通微信；价格持续低于阈值时不重复推送，回到
730元及以上后重新布防。脚本只在铁矿石交易时段联网，并将运行状态写入
`output/alerts/`、日志写入 `output/logs/`。

SendKey优先读取环境变量 `SERVERCHAN_SENDKEY`，也可保存到仅当前用户可读的
`~/.config/investment_dashboard/serverchan_sendkey`。配置后可运行
`scripts/install_iron_ore_price_alert_task.ps1`，创建每分钟检查一次的Windows计划任务；
计划任务通过无窗口的 `wscript.exe` 包装器运行，非交易时段会自动跳过。

## ETF均线策略交易微信提醒

`scripts/monitor_position_timing_trades.py` 以「持仓分析」中的固定50万元ETF均线策略为
唯一持仓基准。A股交易日09:45、11:45、14:45、14:50、14:54分别读取上一正式
交易日的策略持仓，批量获取当天TickFlow实时行情，在内存中预演当天信号，并将目标
持仓相对正式持仓的净变化同时通过Server酱和Hermes微信发送；任一通道失败不会阻止
另一个通道发送，失败通道会记录到任务日志。通知按先卖后买列出ETF名称、代码、
100股整数手数量、参考价、预计金额和触发原因；同一ETF在不同策略袖套中的反向交易
会先合并为净数量。盘中行情和预演结果都不写入正式日线或策略历史。

若14:50无需操作，14:54仍会静默重新计算；仍无需操作时不重复通知，后续一旦出现
交易信号就立即发送。安装或移除Windows计划任务：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_position_timing_trade_alert_task.ps1
powershell -ExecutionPolicy Bypass -File scripts\install_position_timing_trade_alert_task.ps1 -Remove
```

任务不要求Codex、Streamlit或Hermes界面保持打开；Windows当前用户需保持登录，且
Hermes微信扫码连接需保持有效。电脑睡眠时任务设置会请求唤醒。运行状态保存在
`output/alerts/position_timing_trade_alert.json`。

## ETF组合严格审计

需要核对复权、真实成交、分红、基准公平性、参数稳健性和样本外表现时，运行独立的
增强审计入口：

```bash
.venv/bin/python scripts/run_etf_portfolio_audit.py
```

集中配置位于 `config/etf_portfolio_audit.json`。审计使用前复权价格生成信号，
使用未复权开盘/收盘价成交和估值，并从公开基金分红表单独计入现金分红。
结果写入 `output/etf_portfolio_audit_20260727/`；`--quick` 跳过完整 Walk-Forward，
`--refresh-data` 重新联网。参数冻结后由 `services/portfolio_audit_tracking.py`
维护真正样本外跟踪（`out_of_sample_daily.csv` 只追加冻结日之后的日期）。

## 真实回测基线（2026-09-02 数据，本地缓存重放）

- 冻结十 ETF 组合（`config/frozen_strategy_20260728.json`，各 10%）：
  2025-02-27 → 2026-09-02 总收益 **+36.27%** / 年化 22.71% / 最大回撤 **-4.87%** /
  夏普 2.02；一直持有基准 +25.60% / 回撤 -12.17%。
- 固定 50 万模拟策略（2026-08-05 起）：截至 2026-09-02 净值 0.9920（-0.80%），
  13 笔交易，初始建仓 159501/510500/513310/518850。
- 复现方式见 [docs/handbook/repro_hb_backtest.py](docs/handbook/repro_hb_backtest.py)。

## 页面速览

- **指数监控**：上证、主要宽基（含科创50、中证2000）、微盘股、恒生科技、恒生港股通
  高息低波、纳指100、VIX、日经225、KOSPI 及中证500/1000、铁矿石、沪金、原油、沪银
  期货主连的摘要卡片；MA20 偏离率排序表（不含 VIX）。恒生港股通高息低波在东财不可用
  时依次用新浪/妙想 `HSHYLV.HI` 补缺，以恒生官网收盘复核；妙想 `861520.EI` 不作为
  微盘股 `90.BK1158` 的备用源。期货主连名称附带当前实际合约，汇总用当前具体合约的
  独立正式日线；原油主连实时与 2026-07-10 起日线统一东财 `142.scm` 口径，来源校正
  独立缓存。
- **A股分析 / 美股分析**：场外基金（东财累计净值）、场内基金/股票（TickFlow，默认
  前复权差值）、美股日线；均线、RSI、涨跌幅、滚动年化、回撤分析；两页控制布局一致。
- **策略回测**：单标的 MA20 择时、多 ETF 配置择时、年度动态组合、22 日动量轮动。
  择时默认 1% 阈值、当日收盘信号/成交；"一直持有收益"固定按区间首尾实际交易日；
  策略回撤/波动/夏普从初始资金起算。轮动默认盘后固定价模式（2026-07-06 起可用，
  此前历史属当前规则模拟），可切次日开盘模式（场内 ETF ±0.05% 滑点）。
- **策略回测·年度动态组合**：2019 年起 50 万一次性投入，上一年末前最多五年数据、
  实际交易日 70/30 拆分（≥504/≥252 天）、MA10-30 × 0-2% 网格、复合评分与 10% 验证
  年化门槛；注册表披露生存者偏差；旧 ETF 等退出信号后迁移，对比一直持有、全 512890
  与次日收盘压力四条曲线。
- **相关性分析**：上传/TickFlow/期货主连混合矩阵，共同日期对齐，Pearson r 持久化
  分组展示，缺失 pair 用本地缓存自动补算。
- **持仓分析**：固定持仓清单（15 只 ETF + I2701 + 两组价差），缓存优先；A股交易日
  9:30-10:00 每 10 分钟、10:00-11:30 每 30 分钟、午休一次、13:00-14:50 每 30 分钟、
  14:50-15:00 每 2 分钟的临时行情带；15:05 后自动确认正式收盘。逐 ETF 冻结参数的
  择时状态表、512890 停车承接（权重随 510500/159967/159552 空仓数 0%-30%）、
  近 7 日操作指引与固定 50 万模拟区块。
- **实盘记录**：`live_trades` 台账、移动平均成本、不复权正式收盘估值、账户/持仓双
  口径、日周月年收益日历（节假日带名称）、逐符号历史盈亏（含已清仓）。
- **期货实盘**：券商月结单驱动；官方/手工/预计持仓；盯市与收盘双口径盈亏与收益日历；
  同花顺日盈亏补录与 0.05 元容差自动对账；月末后手工成交/出入金与后续结单自动接管；
  铁矿石期权到期作废/履约确认。
- **期货价差**：多合约"基准−其他"绝对价差；铁矿石合约按个人投资者截止日截断。
- **微盘股**：东财 BK1158 成分按总市值升序；真实成分快照跟踪第 200 名市值与微盘 20
  均值（剔除停牌）。
- **任务与数据**：正式指数数据的预览-确认两段式手动更新、独立源复核、数据集与任务
  记录浏览。

## Git

提交前检查：

```bash
git status --short
git diff --stat
```

只提交相关源码改动；不回退无关的用户修改。`cache.db`、`data/`、`output/`、
`__pycache__/` 为运行时状态，不要提交。
