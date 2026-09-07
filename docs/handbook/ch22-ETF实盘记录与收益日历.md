# 第二十二章 ETF 实盘记录与收益日历：live_trading + live_account + core.return_calendar

## 理论

模拟账户记份额，实盘账户记**钱**。`实盘记录` 页的ledger（SQLite `live_trades` + `live_cash_flows`）与回测彻底分离，核心会计口径三条：

1. **移动平均成本**：多次买入按金额加权平均成本，卖出按平均成本确认已实现盈亏；
2. **费率是百分数**：`fee_rate_pct=0.006` 表示 0.006%，买入费用计入成本，卖出费用从回款扣；
3. **估值只用正式收盘**（未复权 `FUND_ADJUST_NONE`）：盘中实时价只用于"当前估值"展示，从不进入历史估值序列。

收益日历（`core/return_calendar.py`）是这个页面的可视化招牌：日/周/月/年四档 + 金额/收益率两口径的热力网格，A 股假日渲染为带假期名的非收益格。

## 数学原理

### 持仓成本与盈亏

设买入 $(q_i, p_i, c)$，移动平均成本（含费）：

$$\bar{p}_n = \frac{\sum_{i\le n} q_i p_i (1+c)}{\sum_{i\le n} q_i}$$

卖出 $q$ 份实现盈亏 $\pi = q\,p_{\text{sell}}(1-c) - q\,\bar{p}$；剩余成本基按比例减少。未实现盈亏 $= q\,\bar{p}_{\text{now}}\cdot P_{\text{latest}} - q\,\bar{p}$（用最新正式收盘）。

### 日度收益的两种口径

- **账户口径**（`formal_account_daily`）：`daily_pnl = 当日账户盈亏 − 前一日账户盈亏`；收益率分母 = **前一日总资产 + 正净投入**（同日卖出回款先抵扣当日买入）——与期货实盘的"经济权益"分母同构（第 24 章）；
- **持仓口径**（`formal_holding_daily`）：日收益 = close-to-close 持仓收益，**不含未投资现金**；`return_pct` 按 `build_live_daily_returns` 逐日计算，净值曲线 `(1+r).cumprod()`。

### 日历聚合

- 周：ISO 周（周一至周五展示），`周收益 = Σ日收益金额`、`周收益率 = Π(1+r)−1` 复合；
- 月/年同构；**任一构成员收益率缺失则复合率记 NA**（`_period_summary:121-122` 的 `rates.isna().any()`）——不猜、不补；
- 日历网格只有周一~周五五列；A 股假日格用 `get_market_holiday_label` 渲染假期名（`_tile` 与 `render_return_calendar` 的 holiday 分支）；
- `confirmation_status` 非"正式"的日期在格子底部标注状态（如"待月结单确认"——期货实盘共用此日历组件）。

## 当前项目实现

### 服务层（services/live_trading.py 45KB + live_account.py 24KB）

- `add_live_trade(*, trade_date, trade_time=None, symbol, name, side, price, quantity, fee_rate_pct, strategy="", notes="", record_key=None)`：**只收 6 位 ETF 代码**；`record_key` 提供幂等导入（UNIQUE 冲突即跳过）；
- `build_live_positions(trades)` / `build_live_daily_pnl(trades, price_histories)` / `build_live_position_performance` / `build_live_symbol_pnl_history`（含已清仓标的）/ `append_live_symbol_pnl_total`（合计行：成本求和、其余列任一 NaN 即 NA）；
- `live_close_refresh_due(*, target_date, market_now, last_attempt, last_target_date, refresh_scope, last_refresh_scope, retry_seconds=600)`：目标日或标的范围变化立即 True，否则 10 分钟节流；
- `build_live_account_snapshot(...)`：账户级快照（总资产/现金/仓位比/净值/valuation_date/cash_warning/incomplete_realtime_symbols）——**每个估值日必须所有已持标的有正式收盘才成账**（AGENTS.md："Do not value a day until every then-held symbol has a formal close"）；
- 现金流 7 类：期初资金/资金转入/资金转出/现金分红/利息/其他收入/其他支出。

### 组件层（components/live_record/，9 文件）

- `dashboard.py`（447 行）：主入口 `render_live_account_dashboard`——加载正式历史（`load_or_fetch_etf(adjust=none)`，10 分钟节流）→ 共享持仓实时行情（与持仓页同一套 `_RUNTIME_ETF_QUOTE_CACHE`）→ snapshot → tabs（持仓情况/标的详情/交易明细）→ 历史区（账户口径/持仓口径 radio + 日历 + 双轴图）；
- `valuation.py`（267 行）：旧版路径保留（依赖注入），其 session 键 `live_pnl_close_*` 与 dashboard 的 `live_record_formal_*` **各自独立**；
- `tables.py`：持仓表 12 列 + 合计行（`weight_pct = market_value / total_assets`）；`history.py`：逐符号历史 HTML 表（红涨绿跌、合计行加粗）；
- `trades.py` / `cash.py`：表单与明细（删除需确认 checkbox，key 带 id 防串）。

## 代码分析

`render_return_calendar`（core/return_calendar.py，260 行）的三层结构值得学习：

1. **数据契约**：必需列 `date/pnl_amount/return_pct`，可选 `confirmation_status`——期货实盘与 ETF 实盘两个消费者用同一契约；
2. **周期导航**：日视图按月网格（`build_live_return_month_grid` + 上/下月按钮，session 记忆月份）；周/月/年视图按年导航；`_navigate` 的禁用边界（current ≤ minimum 等）防越界；
3. **着色语义**：正红 `rgba(239,68,68, α)`、负绿 `rgba(34,197,94, α)`，α 随 |值|/max|值| 从 0.14 到 0.42 线性加深——**热度由组内相对值决定**，跨组不可比（日历 caption 不解释这一点，读者需知）。

`_load_live_formal_histories` 的刷新节流三元组（target_date/scope/600s）：**标的集合变化立即刷新**（新增持仓当天就要估值）而不仅仅看时间——比单纯的时间节流更正确。

## 潜在错误

1. **两套 session 键并行**（valuation vs dashboard）：理论上同一 target 会触发两次独立联网（各自 600s 节流），浪费且可能拿到不同时刻的数据；
2. 持仓表 `weight_pct` 分母：传入 `total_assets` 时含现金（仓位语义），否则只含市值（权重语义）——两种语义一个函数，调用方必须自知；
3. `complete_sum` 的 NaN 传染：合计行任一成员 NaN → 整列 NA——保守正确，但一个标的缺收盘会遮蔽全部合计；
4. 收益日历的复合率在"当月有新增投入"时仍按纯复合计算——分母不含新增资金（账户口径的日收益已按资金加权，复合可行；但大额入金当月的"收益率"会被稀释成接近零，解读需小心）；
5. 删除成交不重算历史估值（derived rows 不持久化——设计如此，但要理解"删旧交易会重放全部下游"的成本）。

## 改进方法

- 合并两套刷新 session 键（同一 prefix 供 dashboard/valuation 共用）；
- 持仓表的 weight 语义拆成两个键（`仓位占比` vs `组合权重`）；
- 日历对"当月净投入 >10% 期初资产"的月份加脚标提示；
- 合计行的 NaN 策略改为"金额列求和（NaN 视 0）+ 比率列 NA"并在 UI 注明。

## 实际案例

页面底部的逐符号历史盈亏表（`build_live_symbol_pnl_history`）定义了"闭环"：一个标的从首笔买入到全部卖出，`status=已平仓`，`total_pnl = 最终已实现盈亏`，**永不从表里消失**（AGENTS.md："A closed symbol's total P&L remains its final realized P&L and must not disappear"）。合计行的总收益率 = 总盈亏 / 总累计买入成本——分母是"累计投入弹药"而非平均占用，这是**资金收益率**而非时间加权收益，适合回答"这台机器每投一块钱赚了几分"。收益日历的收益格 tooltip 同时给金额与收益率（`_tile` 的 title），点击月导航回看历史月份——设计上它是给"复盘每月哪几天在亏钱"用的，而不是给"晒收益"用的。
