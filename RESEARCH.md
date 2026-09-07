# RESEARCH.md — 研究议程

> 配套文档：[README](README.md) · [ARCHITECTURE](ARCHITECTURE.md) · [STRATEGY](STRATEGY.md) · [BACKTEST](BACKTEST.md) · [RISK](RISK.md) · [实战手册（28 章）](docs/handbook/README.md)

本仓库的研究哲学一句话：**先证明参数不是孤峰，再冻结，然后用未来的每一天检验它——
研究不碰正式参数，正式参数必须由冻结流程产生。**

## 1. 研究基础设施现状

| 设施 | 位置 | 状态 |
|---|---|---|
| 组合严格审计 | `scripts/run_etf_portfolio_audit.py` + `portfolio_audit_*` | 已产出（`output/etf_portfolio_audit_20260727/`：19 CSV + 13 图 + 4 报告） |
| 参数冻结与样本外 | `portfolio_audit_tracking.py` | 冻结于 2026-07-28，样本外持续累积 |
| 动态阈值两阶段研究 | `dynamic_threshold_research.py` + `scripts/run_dynamic_threshold_research.py` | 已产出（`output/dynamic_threshold_research_20260815/`，14 只逐 ETF 13 CSV） |
| 年度动态组合 | `annual_etf_*` + `scripts/run_annual_etf_backtest.py` | 可运行（checkpoint 断点续跑） |
| 策略注册表 | `scripts/research/strategy_registry.py` | 工作区新增：从权威常量生成机器可读策略清单 |
| 向量化网格引擎 | `scripts/research/grid_engine.py` | 工作区新增：MA5-120 × 阈值 0-4%，T+1 保守变体，统一评估起点 |
| 验证管线 | `scripts/research/run_strategy_validation.py` | 工作区新增：三口径回测 + 70/30 样本外 + 滚动窗口 + 邻域 + 全网格平台/尖峰 + Bootstrap 1000 + 漏单蒙特卡洛 200 路径 + 成本/滑点压力 + 牛熊分段 + 相关矩阵 |
| 指标统一库 | `core/metrics.py` + `ma_timing_core.py` | 工作区新增：重构后全部回测数字逐字节一致 |
| 复权口径审计 | `scripts/audit_fund_adjustment_impact.py` | 可运行（差值 vs 比例前复权对照） |
| 缓存迁移 | `scripts/migrate_fund_price_caches.py` | 已完成使命（159545 金标准验收内置） |

## 2. 方法论纪律（已内置于代码）

1. **无未来函数**：σ/ATR 用 shift(1)；信号先在全区间算好再切片；MA 预热用区间前数据；
2. **评分防偏科**：窗口内 pct-rank 复合（水下期 0.45 权重），最差子窗优先排序；
3. **对照组强制**：动态阈值必须同时压过 current 与 joint_fixed（固定阈值获得同等的
   MA 搜索权）——排除"MA 换参"的假阳性；
4. **代理隔离**：短历史 ETF 用指数代理长历史研究参数，再用 `replace_signal_with_proxy`
   分离信号源影响；代理不得让 ETF 在上市前可交易；
5. **压力测试前置**：成交率/滑点/漏单/删最佳交易都在上线评审硬条件里；
6. **确定性**：随机种子固定并按袖套偏移（`+allocation_index*17`），蒙特卡洛可复现；
7. **声明式披露**：生存者偏差、执行假设、窗口实际起止全部进报告。

## 3. 未决问题（按优先级）

### R1 需要数据积累

- **样本外验证不足**：冻结参数（2026-07-28）样本外历史 <2 个月。任何"上线/扩仓"
  决策至少等一个季度样本外，且期间不回调参数（回调即重新冻结、样本外清零重计）；
- **动态阈值证据不足**：两阶段研究当前产出以 B/D 级为主（动态族训练期无稳定优势）。
  保持每季度重跑，等待族稳定性出现；
- **微盘股拥挤度与 MA20 偏离的关系**：快照数据（第 200 名市值/微盘 20 均值）刚开始
  积累，暂无统计可用。

### R2 可以立即做（对应手册第 28 章路线图）

- 修 P0 五项（call 结算价、同月双结单、承接口径、乘数缺失费用、Yahoo tz），
  每项配回归测试；
- `_find_column` 与当日收益分母的统一（消除最后两处口径分裂）；
- 单标的回测的分红现金流参数（与年度引擎对齐）；
- `scripts/research/run_strategy_validation.py` 接入 `任务与数据` 页的定期审查视图。

### R3 值得研究的新方向

- **参数的"年龄"**：给冻结参数加"样本外日数/样本外夏普"自动统计页，
  让"该重新研究了没有"变成一个可观察指标；
- **评分权重敏感性**：年度组合与两阶段研究共用 0.45/0.25/0.15/0.10/0.05 权重——
  对权重做 ±0.1 扰动看年度选择/候选排序是否翻转（元过拟合检查）；
- **`SCORE_WEIGHTS` 配置化 + 版本化**：权重变更时旧 checkpoint 自动失效；
- **卖出侧的分级退出**：当前是 0/1 二态（半仓策略除外），可研究按偏离深度分批减仓
  的变体并用 `simulate_timing_account` 的三口径对照评估；
- **期货价差信号化**：跨期价差（如 I2701-I2705）目前只监控不交易，
  可研究展期窗口的系统性规则；
- **512890 停车收益率归因**：量化"空仓期停进 512890 vs 纯现金"的历史差异，
  为停车机制本身做一次审计级验证。

### R4 工程侧研究

- `file_path` 相对化 + SQLite WAL + Windows 真锁（msvcrt）；
- 守护脚本的 `fcntl` 守卫与 Windows 事件日志告警；
- `update_tasks` 门面签名自动生成（`inspect.signature` CI 校验）；
- 相关性结果表去重与 `alert_state` 表接线（跨脚本共享告警状态）。

## 4. 复现与研究工作流

```bash
# 全部手册回测数字（零网络）
.venv/bin/python docs/handbook/repro_hb_backtest.py
# → output/handbook_backtest_results.json

# 组合严格审计（--quick 跳过 Walk-Forward）
.venv/bin/python scripts/run_etf_portfolio_audit.py

# 动态阈值两阶段研究
.venv/bin/python scripts/run_dynamic_threshold_research.py

# 年度动态组合（先 prepare 再 run）
.venv/bin/python scripts/prepare_annual_etf_backtest.py          # 默认只读预览
.venv/bin/python scripts/prepare_annual_etf_backtest.py --apply  # 联网补缓存
.venv/bin/python scripts/run_annual_etf_backtest.py

# 策略注册表 + 验证管线（工作区新增）
.venv/bin/python scripts/research/strategy_registry.py
.venv/bin/python scripts/research/run_strategy_validation.py --fast

# 样本外跟踪（每次运行只追加冻结日后日期）
.venv/bin/python -c "from services.portfolio_audit_tracking import run_out_of_sample_tracking; ..."
```

研究产出全部落在 `output/`（gitignore）；`run_manifest.json`（参数 + 输入 sha256）
保证每个结果可追溯到数据状态。
