# Investment Dashboard

个人投资分析工作台。

## 本地运行

```powershell
cd "\\wsl.localhost\Ubuntu\home\renne\investment_dashboard"
py -m streamlit run app.py
```

## 数据缓存设计

- `data/raw/`: 原始行情 CSV
- `data/processed/`: 计算后的结果 CSV
- `output/`: 手动导出的文件
- `cache.db`: SQLite 元数据和任务记录

## 后台更新任务

任务入口统一在 `services.update_tasks.run_index_ma20_update`，Streamlit 页面和命令行后台都复用这套逻辑。

立即运行一次：

```powershell
cd "\\wsl.localhost\Ubuntu\home\renne\investment_dashboard"
py -m services.background_updater --once --days 30
```

如需忽略今日缓存重新拉取，加上 `--force-refresh`。

循环后台更新（默认每 60 分钟一次）：

```powershell
cd "\\wsl.localhost\Ubuntu\home\renne\investment_dashboard"
py -m services.background_updater --interval-minutes 60 --days 30
```

也可以在 Streamlit 左侧进入「任务与数据」页面，点击「立即运行一次」「启动后台循环」或「停止后台循环」。任务记录会写入 `cache.db` 的 `jobs` 表，后台循环 PID 会写入 `output/background_updater.pid`。

## 已网站化的桌面脚本

- 「基金分析」：场外基金输入代码后拉取东方财富累计净值；场内基金/ETF 输入代码后通过 TickFlow 拉取日收盘价；也支持上传 CSV/Excel 作为备用入口。图表包含均线、RSI、涨跌幅、滚动年化收益率和回撤分析，并计算波动率、价格百分位、最大回撤波段、修复天数和年度最大回撤。
- 「期货价差」：输入多个期货合约，在线拉取日线数据，计算“基准 - 其他”的绝对价差和百分比价差。
