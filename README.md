# Investment Dashboard

个人投资分析工作台。

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

## 数据缓存设计

- `data/raw/`: 原始行情 CSV
- `data/processed/`: 计算后的结果 CSV
- `output/`: 手动导出的文件
- `cache.db`: SQLite 元数据和任务记录

## 后台更新任务

任务入口统一在 `services.update_tasks.run_index_ma20_update`，Streamlit 页面和命令行后台都复用这套逻辑。

立即运行一次：

```bash
cd /home/renne/investment_dashboard
.venv/bin/python -m services.background_updater --once --days 30
```

如需忽略今日缓存重新拉取，加上 `--force-refresh`。

循环后台更新（默认每 60 分钟一次）：

```bash
cd /home/renne/investment_dashboard
.venv/bin/python -m services.background_updater --interval-minutes 60 --days 30
```

也可以在 Streamlit 左侧进入「任务与数据」页面，点击「立即运行一次」「启动后台循环」或「停止后台循环」。任务记录会写入 `cache.db` 的 `jobs` 表，后台循环 PID 会写入 `output/background_updater.pid`。

## 已网站化的桌面脚本

- 「A股分析」：场外基金输入代码后拉取东方财富累计净值；场内基金/股票输入代码后通过 TickFlow 拉取日收盘价；也支持上传 CSV/Excel 作为备用入口。图表包含均线、RSI、涨跌幅、滚动年化收益率和回撤分析，并计算波动率、价格百分位、最大回撤波段、修复天数和年度最大回撤。
- 「基金轮动」：支持上传多个基金/ETF 文件，或输入场内基金/ETF 代码通过 TickFlow 获取前复权/后复权日线，也支持输入场外基金代码通过东方财富获取累计净值。TickFlow 获取的数据会保存到本地缓存，后续默认复用缓存，只有勾选「联网更新数据」才重新拉取。策略按 22 个交易日动量做满仓轮动，输出轮动策略与单独持有对比的净值走势、回撤分析、交易明细、每日持仓金额和摘要。场内 ETF 按开盘价成交并计入买入 +0.05%、卖出 -0.05% 滑点，按 100 份整数交易；场外基金按累计净值近似成交，不套用整手限制。
- 「相关性分析」：支持上传 CSV/Excel，或通过 TickFlow 获取 A 股 ETF、美股日线，并支持期货主连数据。A 股 ETF、美股、期货主连和上传文件可以混合输入，统一放入同一个矩阵两两计算。不同标的可按全区间、最近 1/3/5 年或自定义日期过滤，再按共同日期对齐后计算收盘价或日收益率 Pearson 相关系数 r，输出相关矩阵、两两相关表；右侧会持久化展示历史分析结果，例如 `159915 / 512890 0.24 较弱`，并支持删除。
- 「期货价差」：输入多个期货合约，优先通过 TickFlow 拉取日线数据，计算“基准 - 其他”的绝对价差。
- 「美股分析」：输入 AAPL、MSFT、COWZ 等美股代码，通过 TickFlow 拉取日线收盘价，复用均线、RSI、滚动年化收益率和回撤分析，并支持本地缓存与增量更新。
