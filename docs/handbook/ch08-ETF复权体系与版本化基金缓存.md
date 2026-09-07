# 第八章 ETF 复权体系与版本化基金缓存：fund_analysis + position_market 数据层

## 理论

ETF 会分红、拆分、份额折算。拿"未复权价"算 MA20，分红除息日会出现一根 -3% 的假阴线，直接触发一次错误卖出。复权不是可选项，但**选哪种复权**是策略问题：

- **前复权（差值/加法）**：把历史价格整体平移，使最新价 = 现价。优点：绝对价格保持"元"，整数手与费用计算直观；缺点：每次新分红都会改写全部历史（回溯变化）。
- **前复权（比例）**：等比缩放，收益率与未复权一致；缺点：历史价格不再等于真实成交价。
- **后复权**：以上市日为锚向前放大，收益率不回溯，但价格远离现价。

本项目把 `forward_additive`（前复权-差值）定为**择时默认**（`FUND_ADJUST_FORWARD_ADDITIVE`，`fund_analysis.py:18`），把 `none`（不复权）定为**实盘估值默认**（`live_record` 一律 `FUND_ADJUST_NONE`），并把"ratio 口径"明确标注为"清晰标记的比例复权对照选项"。这个选择的含义：**信号看收益率（用前加复权），成交与估值看真钱（用未复权）**。

## 数学原理

设分红除息日 $t$ 派息 $d$（每股），除息前价 $P_{t-1}$，除息参考价 $P_t^- = P_t + d$。加法（差值）前复权对 $t$ 之前所有价格加常数：

$$\tilde P_i = P_i - d \quad (i < t), \qquad \tilde P_i = P_i \quad (i \ge t)$$

比例前复权则是 $\tilde P_i = P_i \cdot P_t / P_t^-$。**两者在除息日的"收益率"等价**（都是 $P_t/P_t^- - 1$），但价格水平不同：加法复权保持"价格差"的量纲，比例复权保持"收益率"的量纲。连续多次分红时，加法复权是逐次平移的叠加，比例复权是逐次相乘——这也是"差值口径"与"比例口径"在长历史上会出现可观测差异的原因（`scripts/audit_fund_adjustment_impact.py` 就是专门审计这个差异对 MA 择时回测影响的脚本，逐代码间隔 6.2 秒限频抓两套口径并对比）。

份额折算（如 510500 的 0.28032483 折算）在加法复权里表现为**比例**跳变，审计脚本与回测里必须用显式台账（`known_share_splits`）处理，而不是从复权因子反推——因为三位小数舍入会把 1.145 辨认成 1.14。

## 当前项目实现

### 复权常量与缓存键（fund_analysis.py）

```python
FUND_ADJUST_FORWARD_ADDITIVE = "forward_additive"   # 择时默认
FUND_ADJUST_FORWARD_RATIO = "forward"               # 明确标注的比例口径
FUND_ADJUST_BACKWARD_* / FUND_ADJUST_NONE = "none"  # 实盘估值
FUND_CACHE_SCHEMA_VERSION = 2
def build_fund_cache_symbol(prefix, symbol, adjust):
    return f"{prefix}_v{FUND_CACHE_SCHEMA_VERSION}_{symbol}_{normalize_fund_adjustment(adjust)}"
```

缓存键形如 `fund_close_v2_159545.SZ_forward_additive`，period 单独存（`5000_1d`）。**禁止回退到旧版无版本键**（AGENTS.md 明文）。`normalize_fund_adjustment` 是规范化边界：显式传 `"none"`，遗留 `None` 也在此归一。

### 数据行打戳（stamp_fund_history_metadata）

每行带 `_adjust_mode` 与 `_cache_schema_version` 两列——读回时校验一致性，防止不同口径数据混进同一缓存。

### 增量与重建（services/position_market.py，944 行）

`load_or_fetch_etf` 的状态机（摘关键分支）：

1. 缓存键 `build_fund_cache_symbol("fund_close", symbol, adjust)`，source 依 161128 为 `akshare` 否则 `tickflow`；
2. `minimum_rows = strategy[0]`（有择时策略时至少要有 MA 周期长度历史）；
3. 增量条数 `incremental_count = min(max(120, count//20), count)`（5000 时为 250）；
4. `_adjusted_history_has_overlap_changes`：重叠日期的收盘/开盘价用 `np.isclose(rtol=1e-9, atol=1e-9)` 对比——**前加复权的历史会因新分红而回溯变化**，检测到即全量重建该标的（状态"已重建"），重建失败回退旧缓存并置 `formal_history_valid=False`（状态"缓存待校验"），此时**抑制一切择时信号与 512890 停车决策**；
5. `_prepare_fetched_etf_history`：非未完成会话时 `filter_final_etf_rows` 后强制 `_final_close_confirmed=True`——写进缓存的每行都声明"我来自已完成收盘"。

### 161128 的特例链

`标普信息科技LOF易方达 (161128)`：正式日线优先东财/AkShare（`ETF_AKSHARE_HISTORY_CODES={"161128"}`）；新浪兜底必须先过 `_ensure_sina_adjustment_is_identity`——拉 `{qfq|hfq}.js` 复权因子，发现任何非恒等因子就拒绝（"新浪备用源存在复权事件"）；15:05 后新浪收盘快照 `_fetch_sina_exchange_fund_final_close` 校验"日期==目标日且时间 ≥ 最后一节收盘"才允许追加当日行；TickFlow 缺 161128 实时行情时用新浪快照做**仅盘中**兜底。

### 申购费率约定

`fee_rate_pct` 是百分数值（0.006 表示 0.006%），实盘与回测统一单边 0.006%（=0.00006 比例）。

## 代码分析

复权重建的失败路径是本章最重要的代码（`position_market.py` 中段）：

```python
if adjusted_history_has_overlap_changes:
    rebuilt = fetch_full(...)          # 全量重建
    if rebuilt ok: 状态="已重建"; save_dataset(...)
    else: 状态="缓存待校验"; formal_history_valid=False
```

`formal_history_valid=False` 的下游效应（消费方全都在）：`calculate_etf_timing_snapshot` 不再产出信号、512890 停车快照返回占位、50 万模拟策略直接报错停止、操作指引剔除该标的。**一条复权回溯变化能让全系统静默降级到"只展示不决策"**——这是设计而非缺陷，但它要求用户理解"缓存待校验"四个字的分量。

新浪收盘快照的时间校验（`_fetch_sina_exchange_fund_final_close`）逐条列出了拒绝条件：非当天、时间早于收盘、字段不足 32、价格 ≤0——任何一个不满足都返回 None 而不是"拿近似值凑数"。

### 分析指标层：analyze_fund_nav（fund_analysis.py:467 起）

`A股分析`/`美股分析` 页的统一计算入口（`normalize_nav_dataframe` 先把任意来源规范成 `date/price` 两列）：日涨跌、20/60 日涨幅、20 日波动率与"动量/波动比"（`return_20d / volatility_20d`）、RSI（**SMA 口径**，`gain/loss` 各自滚动均值，非 Wilder 平滑）、历史全窗价格百分位（`expanding().rank(pct=True)`）、任意 MA 组合与偏离、YTD、基准日涨幅（默认 2024-09-24，`A%股分析` 页默认 2024-09-24、美股页 2025-04-07）、`cummax` 回撤、滚动年化（≥3 年用 756 日窗口，键名随窗口变化：`三年滚动年化收益率(%)`）、最大回撤五要素（`calculate_max_drawdown_info`：峰/谷/修复日/天数/是否修复）、分段回撤表（`extract_drawdown_periods`，只保留深度 ≥ 最大回撤 1/4 的段）、年度回撤表（`calculate_yearly_drawdowns`）。`calculate_current_drawdown_info` 单独回答"当前回撤处于什么状态"。这些函数同时被 `pages/2_A股分析.py`、`pages/7_美股分析.py` 与持仓详情视图消费——是"两页同构"承诺的计算层基础。

## 潜在错误

1. **比例口径与东财无等价源**：`ETF_AKSHARE_HISTORY_CODES` 里的 161128 若被请求 `forward`（比例）口径，东财路径直接抛错——UI 上这是隐藏的失败模式，只有读过代码才知道；
2. `_ensure_sina_adjustment_is_identity` 对**前复权**也要求零复权事件，等于新浪日线在前复权口径下几乎只适用于无分红标的——保守到近乎不可用，但换来的正确性值得；
3. `fetch_tickflow_fund_close` 的 `fetch_tickflow_instrument_name` 失败静默返回代码本身——缓存的 `name` 列可能因此退化为代码，下游 `ETF_DISPLAY_NAMES` 映射能救回来，但 CSV 里的 `name` 已不真；
4. `_merge_current_day_refresh`（衍生品模块用）用 `pd.Timestamp.now(tz="Asia/Shanghai")` 而非入参 `market_now`，测试注入时口径不一致。

## 改进方法

- 把 `_final_close_confirmed` 的判定从"列存在且为真"升级为与 `latest_settled_trade_date` 联动校验（防老版本缓存留下未确认行）；
- 复权重建失败时在 UI 卡片上给出"上次成功重建时间与失败原因"，当前只有状态文案；
- 161128 的比例口径失败信息里直接说明"东财无比例复权等价源"，减少用户困惑；
- `migrate_fund_price_caches.py` 里对 159545 的硬编码验收断言（MA10≈1.3069、偏离≈-1.6757 等，数据须覆盖 2026-08-12）是很好的"金标准"做法，值得推广到其余 13 只 ETF 的迁移验收。

## 实际案例

本地缓存目录就是复权体系的实物证据（`C:\Users\Renne\investment_dashboard_data\data\raw\`）：

- `tickflow/fund_close_v2_512890.SH_forward_additive_5000_1d.csv`（2019-01-18 → 2026-09-02，1848 行）——**新版前加复权**，供择时；
- `tickflow/fund_close_512890.SH_forward_5000_1d.csv`（1835 行）——**旧版** `forward` 键的遗留缓存，v2 体系下已不可达（无版本键禁用）；
- `akshare/fund_close_v2_161128.SZ_forward_additive_5000_1d.csv`（2017-01-03 → 2026-09-02，2327 行）——东财路径的历史比 TickFlow 更长，正是 161128 特例链存在的理由。

同一只 512890 两个键并存、新键可用旧键作废，这就是"版本化、口径明确、禁回退"三个词的落地形态。
