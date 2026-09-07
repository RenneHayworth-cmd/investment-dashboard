# 第九章 MA 带状择时的理论与数学

## 理论

移动平均择时是最古老也最持久的策略族：价格站上均线做多、跌下均线离场，本质是对"趋势存在性"下注。它的学术证据链从 Brock, Lakonishok & LeBaron (1992) 对 DJIA 的经典检验开始，到 Zakamulin (2017) 对"MA 择时是否只是动量"的分解，结论稳定为三点：

1. MA 择时主要收益来源是**尾部风险规避**——它在大熊市把仓位切到零，把 -60% 级别的回撤砍半（本项目数据：588000 成立来持有 -59.64% vs 策略 -26.62%）；
2. 它在**均值回归的市场**（低波动红利类资产）持续跑输持有，因为每次小幅穿越都是假信号；
3. 简单穿越的信号频率太高，**阈值带（band）**是把 whipsaw 降到可承受次数的标准手段。

阈值带的另一个名字是"死区"（dead band）或"滞回"（hysteresis）——控制论里消除抖动的经典工具。本项目全部择时逻辑（指数、ETF、回测、审计、研究）共用这一个状态机。

## 数学原理

### 状态机

设第 $t$ 日收盘 $P_t$，均线 $M_t = \frac{1}{n}\sum_{i=0}^{n-1} P_{t-i}$（`rolling(n).mean()`，min_periods=n，不足 n 日为 NaN）。带阈值 $\theta$（百分数）的目标仓位：

$$w_t^* = \begin{cases}
1, & P_t > M_t(1+\theta) \\
0, & P_t < M_t(1-\theta) \\
w_{t-1}^*, & |P_t - M_t| \le M_t\theta \ \text{（或 } M_t=\text{NaN）}
\end{cases}$$

**状态只在越带时翻转，带内冻结**——这就是滞回。信号日 $t$ 的收盘价即成交价（`after_close` 假设），所以第 $t$ 日净值增长按 $w_t^*$ 对第 $t+1$ 日起的收益生效（同日收盘信号、同日收盘成交、次日起承担方向风险）。

### whipsaw 的量化

无带策略（$\theta=0$）的翻仓条件是 $P_t$ 与 $M_t$ 的大小关系翻转。设 $P_t - M_t$ 近似为均值为 0 的平稳序列，日标准差 $\sigma_d$，则穿越频率近似 Ornstein-Uhlenbeck 首达时间问题，$\theta$ 越大穿越频率指数级下降；工程上更直接的经验是本项目的真实参数表（第 11 章）：高波动成长 ETF 用宽带（159552 MA10/2.5%），低波动资产用窄带或干脆不给信号（512890 无自身 MA 参数）。

### 半仓策略的等价形式

`half_timing` = 恒持 $\frac{1}{2}$ + 择时 $\frac{1}{2}$：

$$V_t^{half} = \tfrac12 V_t^{hold} + \tfrac12 V_t^{timing} \iff w_t^{half} = \tfrac12 + \tfrac12 w_t$$

收益波动减半（若两部分不完全相关则更少），但给定了**下限暴露**——永不空仓的一半，适合"怕踏空"的资产（纳指、标普）。

### 滞后性与成本

MA 的滞后近似 $n/2$ 日（趋势转折点到确认点），阈值带额外增加确认延迟 $E[\tau] \approx \theta\sqrt{n}/\sigma_{\text{daily}}$ 量级。单边成本 $c$、年均翻仓 $k$ 次时的年成本拖累 $2ck$（本项目 $c=0.00006$、$k \le 20$ 时成本可忽略；159915 成立来 210 次交易的成本也很小，主要代价是 whipsaw 本身）。

## 当前项目实现

### 统一定义：services/ma_timing_core.py（373 行）

历史状态：状态机曾有 4 份并行实现（回测页单标的、组合择时信号帧、持仓页快照、年度组合选参），指标计算另有 8 处重复——`ma_timing_core.py` 的模块 docstring 开宗明义记录了这一点。**当前工作区已把信号定义与账户模拟收敛为唯一定义**：

- `ma_threshold_states(close, ma_period, threshold_pct, initial_state=0)`：**唯一的信号状态机**（numpy 循环：`close > buy_line → 1`、`close < sell_line → 0`、带内或 MA 为 NaN → 维持原状态），返回与 close 同索引的 0/1 序列；
- `ma_threshold_series`：均线与买/卖触发线的唯一定义（`buy_line = ma×(1+θ)`、`sell_line = ma×(1−θ)`）；
- `threshold_desired_position(close, buy_line, sell_line, current_position)`：**账户自愈语义**的阈值决策——带内维持*实际持仓*而非信号状态。docstring 解释了差异：信号日买入失败（现金不足以买一手）时，账户不应在带内反复重试，而应等下一次有效穿带再试。单标的回测 `run_ma20_timing_backtest` 用本语义；
- `ma_threshold_transitions`：从状态序列推导转换表（首个有效状态与初始不同也记一次转换）；
- `simulate_timing_account(close, states, config, open_)`：**参考账户模拟器**，显式支持 `after_close`（当日收盘信号/当日收盘成交）、`next_open`（前收盘信号/次日开盘成交，含滑点）、`next_close`（前收盘信号/次日收盘成交）三种执行口径，停牌日不成交也不重估（净值沿用最近有效收盘）；`TimingAccountResult.metrics()` 直接产出总收益/年化/波动/夏普/回撤/最长水下期/Calmar 指标卡。

docstring 同时声明口径纪律（与 AGENTS.md 一致）：`after_close` 依赖 2026-07-06 起的盘后固定价机制，更早历史属于"当前规则模拟"；研究评估必须同时报告 `next_open`/`next_close` 保守口径。

### 消费方（重构后）

| 消费方 | 用法 |
|---|---|
| `fund_rotation_timing._build_timing_signal_frame` | `ma_threshold_states` + `previous_state` 跟踪标注 买入/卖出/持有/空仓/等待均线 |
| `fund_rotation_timing.run_ma20_timing_backtest` | `ma_series/buy_line_series/sell_line_series`（全历史预计算再裁区间）+ 循环内 `threshold_desired_position` |
| `position_timing._timing_states_and_ma` | 快照引擎的状态/均线来源（`ma_threshold_series + ma_threshold_states`） |
| `annual_etf_selection._desired_states` | 年度组合选参的信号（保持本地实现，语义对齐） |
| `scripts/research/grid_engine.py` | 向量化网格引擎，"与 ma_timing_core / core.metrics 对齐"，标题级结果以 `simulate_timing_account` 为准 |

带状判定的统一写法（`ma_timing_core.ma_threshold_states`）：

```python
if close_values[index] > buy_line:            # 严格大于
    state = 1
elif np.isfinite(sell_values[index]) and close_values[index] < sell_values[index]:  # 严格小于
    state = 0
# 带内 / MA 不足：维持原状态
```

**严格不等号**是回测与信号帧的口径；指数展示版（`index_signals.calculate_ma20_transition_history`，第 10 章）仍用 `close >= ma` 判方向（无带、含等号），因为它只做展示不做成交。

## 代码分析

重构后最值得精读的是**两种"维持"语义的区分**（`ma_threshold_states` vs `threshold_desired_position` 的 docstring 对照）：

- 信号语义：带内维持**上一次信号状态**——两次独立调用（如实时快照）结果确定；
- 账户语义：带内维持**实际持仓** `int(current_position)`——若上一日想买但现金不足一手没成交，带内不会持续重试，避免"每根带内 K 线都尝试买入"的账面噪声。

这两者在"信号=持仓"的顺利路径上等价，只在成交失败时分歧——把分歧显式化为两个函数，比一个函数靠调用方约定语义要稳得多。

另一处细节：`run_ma20_timing_backtest` 重构后把 `ma_series/buy_line_series/sell_line_series` 在**全量历史**上预计算、再按区间切片取值（`ma_series.get(trade_date)`）——第 14 章将引用的"MA 预热不丢"语义由此保证。

**重构正确性的实证**：本手册的全部回测数字（14 只 ETF 分期回测、冻结组合、50 万模拟）在重构前后的引擎上各重放一次，结果**逐字节一致**（唯一差异是生成时间戳）——语义保持的声明被数据证实。

## 潜在错误

1. **参数是逐 ETF 手工选的**。当前参数表（MA10~30 × 0.5~2.5%）来自人工研究，`frozen_strategy_20260728.json` 冻结了它，审计脚本做了邻域稳健性检验——但没有人能保证它不是"恰好好看"的一组。潜在错误不是代码错误，而是**流程性错误：把幸存参数当真理**。审计报告对 159967 的定制参数归因、对统一 MA20/1% 的对照，正是为此设计的纠偏。
2. 指数展示版 `is_above = close >= ma` 在 $P_t = M_t$ 的整数价位（如均线 3000.00、收盘 3000.00）会把状态翻上去，而统一状态机用严格不等号不翻——**展示与回测在零偏离点上可能给出相反状态**，极端但真实存在（第 28 章保留此条）。
3. 年度组合的 `_desired_states` 仍是本地实现（语义对齐但代码独立）——统一尚未覆盖到它，未来改动状态机时需同步检查。
4. `ma_timing_core.simulate_timing_account` 在停牌日用 `last_valid_close` 沿用估值——净值连续但**当日收益为零**的口径需要在解读网格结果时知晓（引擎 docstring 已声明）。

## 改进方法

- ~~把四份实现收敛到一个纯函数库~~——**已在当前工作区实施**（`ma_timing_core.py` + `core/metrics.py`），剩余收尾：`annual_etf_selection._desired_states` 与 `index_signals` 展示版迁移/对齐；
- 零偏离约定统一：展示版改为严格不等号（与 `ma_threshold_states` 一致）；
- `simulate_timing_account` 支持分红现金流参数（当前参考模拟器不含分红，年度组合引擎有）；
- 为 `threshold_desired_position` 与 `ma_threshold_states` 的分歧路径补一个"现金不足一手"的单元测试，把账户自愈语义钉死。

## 实际案例

真实缓存上的三个对照（全部由 `run_ma20_timing_backtest` 在 2026-09-02 收盘数据上重放，初始 10 万、单边 0.006%、整手 100）：

| ETF | 参数 | 策略成立来 | 持有成立来 | 策略MDD | 持有MDD | 交易次数 | 胜率 |
|---|---|---|---|---|---|---|---|
| 588000 科创50 | MA20/1% | **+129.65%** | +16.59% | **-26.62%** | -59.64% | 74 | 43.24% |
| 159915 创业板 | MA20/1% | **+508.31%** | +318.44% | **-44.69%** | -69.58% | 210 | 33.33% |
| 512890 红利低波 | MA20/1%（示意） | +55.66% | **+139.48%** | -17.14% | -16.53% | 87 | 48.84% |

同一套状态机：高波动成长资产上大胜（回撤减半、收益翻倍），低波动红利资产上惨败（收益不到持有的一半，MDD 还更大）。512890 的年度分解更刺眼：2022 年 -5.16%、2025 年 -0.28%、2026 年 -1.2%，反复被 whipsaw 消耗。**这就是为什么 512890 在本项目里被设计为"停车 ETF"——它不生成信号，只承接别人的空仓资金**（第 12 章）。参数表里宽带的 159552（MA10/2.5%）成立来 +124.19% vs 持有 +127.81%、回撤 -13.12% vs -21.39%——宽带换来的不是更高收益而是更浅回撤，说明阈值带的第一功能是风控，其次才是增强。
