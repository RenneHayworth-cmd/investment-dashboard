# 微盘20 ABCD：运行与核验

## 当前启用版本：v2 执行优先（2026-09-21）

按用户最新要求，当前实际运行口径如下，本节覆盖下方原v1的阻断说明：

- 四个22万元账户已启用。首日使用2026-09-18已保存的名单和收盘信号，在2026-09-21收盘模拟执行；A从首日收盘归一化，不计启动前收益。
- 首日计划：A指数基准、B微盘20、C现金、D买入512890。计划不保证成交，尚未收盘时页面显示待执行。
- TickFlow自动获取未复权日线、同日证券元数据；日线缺失时补查15点后同日行情快照，再由AkShare补价。
- 缺少某只价格先跳过该笔买入；待卖保留。定时补跑会检查最新日结中的缺价，取到同日正式价格后重放原计划，归档旧日结并事务重算；这属于补录同日收盘模拟，不是次日追单。已有持仓用前值参考估值并标记，不伪造今日收盘。
- 已知停牌或涨跌停仍跳过。资格字段缺失时允许按模拟可交易假设执行，逐笔写入未核验说明。
- 权益事件资料不完整不再阻断全部账户；目前自动版仍未完整计入分红送转，因此收益标为待核验参考。手工核验文件是可选补充，不再要求用户每天制作。
- 如果缺当日成分，仍执行已冻结的前日计划；暂不生成新的周度名单，不因空名单清仓。必要指数正式行情仍须可用。
- 原v1严格引擎与历史档案核验保留。v2是独立版本，不篡改历史研究或真实交易记录。
- 定时任务仍为工作日15:20、16:20，网页关闭不影响。当前用户需登录、电脑及WSL需运行。零成交也会说明原因，不宣称全部已成交。
- 新专项测试：tests/test_microcap_rotation_operational.py。未收盘时不允许用盘中报价提前写入成交。


入口为「微盘股 → ABCD策略」。浏览只读缓存，首次点击“启用每日模拟”才创建四个22万元账户。历史研究已独立导入，不能作为新模拟的起始持仓或收益。

## 固定口径

A为BK1158无摩擦收盘比例基准。B按周首个交易日执行前日名单。C使用MA15±2.5%滞回，关闭时现金；D关闭时用可用现金买512890。每只股票新仓约1万元，最近整百股，留存股票不重配，盈利不放大股票头寸；ETF按成交金额万分之0.6计佣后向下取整百份；股票每笔固定2元，未计税费和滑点。股票市值同值按代码排序。

S0无成交且净值为1。至少120个S0之前的连续正式指数交易日用于预热，未知状态保持现金。之后信号严格次日执行；风险退出优先。涨停买入取消，不追单、不补排名；卖出受阻占用20只名额，后续每日按最新目标重核。停牌估值仅允许有明确证据的前值。A无实际现金持仓；其指数名义资产单独计算。

金额逐笔使用Decimal、四舍五入到分；整手目标使用ROUND_HALF_UP。内部仓位0—1，展示百分比。最大回撤计入22万元起始峰值；每日模拟年化统计排除S0初始化行。历史统计沿用已核对174日年化分母和2%/252无风险/MAR。周度胜率使用周五末值，排除初始无前值周。

## 日结与恢复

services/microcap_rotation.py为唯一服务入口。SQLite使用microcap_rotation_前缀独立表，不接触live_trades。冻结批次包含完整指数预热、快照、资格、行情及权益证据，带SHA256。账户版本、策略、日期及事件编号唯一；四账户同日一次事务提交，失败全部回滚。自动及手动共享文件锁。缺口之后不跳日，补全证据后重跑可恢复；已完成日期不联网、不重新成交。

配置改变必须创建新版本；v1不提供页面修改固定口径。当前为WSL部署，进程锁使用fcntl。

## 当前数据门禁与证据文件

既有BK1158快照没有保留全部逐行来源日期，亦不提供完整的最低申报量、涨跌停、资格及分红送转证据。因此不能仅依据名称/涨跌幅推断所有字段通过。正式模拟必须接入核验记录，目前通过页面上传或CLI导入。**未提供核验记录时自动任务会明确失败并保留上次日结，不会产生每日模拟业绩。** 这不是已完成自动权益数据供应的声明。

使用命令生成待核验模板：
```bash
.venv/bin/python scripts/update_microcap_rotation.py --evidence-template 2026-09-21
```
模板由本地实际快照生成并带snapshot_hash，不把缺失字段填为通过。保存到output后，补入可追溯来源及对应事实，再导入。可由可信数据供应程序直接每日生成同结构文件，存入data/raw/microcap_rotation_evidence/YYYY-MM-DD.json。文件与已冻结日结分离，不能用新文件悄悄改写已结算结果。

结构：
- date/source：日期、核验记录来源。
- snapshot_hash/snapshot_source：程序生成的原始快照哈希及成分、行情日期来源。
- constituent_eligibility：以六位代码为键，每项含is_st布尔值、asof当日日期、source。
- quotes：当日实际持仓、待买股票及需配置512890的行情。每项包含date、close、adjustment="none"、formal=true、source、eligible、eligibility_source、halted、limit_up、limit_down、min_buy；四个状态字段必须是明确布尔值。科创板最小买入不得低于200股。停牌必须另有halt_source；确认停牌可仅对已有持仓用前值估值。
- events_complete/events_source：明确当日相关标的权益事件已完成核验及来源。无事件也要有核验来源，不能因列表为空默认通过。
- events：事件id、code、date入账日、record_date登记日、source、kind。cash事件含cash_per_share（实际净到账每股金额）；shares事件含new_shares_per_share（每股新增股数）。登记日持仓从已结账本读取。现金在实际到账日入账，送转在新增股份入账日入账。无法确认到账日、税额、零碎股、配股等复杂事件时停止推进，不能硬填零。

指数使用现有正式缓存及收盘确认覆盖层，必要时仅更新微盘指数。成分优先复用15:05快照，只可补采当日，不回填历史。资格已核验而价格缺失时允许通过TickFlow未复权日线补价；网络不替代资格及权益证明。依赖TickFlow环境密钥，未配置则保持明确失败。

## 常用命令

```bash
.venv/bin/python scripts/update_microcap_rotation.py --preflight
.venv/bin/python scripts/update_microcap_rotation.py --refresh
.venv/bin/python scripts/update_microcap_rotation.py --target 2026-09-21
.venv/bin/python scripts/update_microcap_rotation.py --evidence /path/to/2026-09-21.json
.venv/bin/python scripts/update_microcap_rotation.py --import-research /path/to/ABCD策略全景审计交接包
```

Windows运行scripts/install_microcap_rotation_task.ps1注册工作日15:20、16:20任务。封装隐藏运行WSL，不发外部通知。要求Windows北京时间、电脑开机且当前用户已登录、WSL和必要数据源可用；错过时间可补跑。日志在output/logs/microcap_rotation_task.log，业务状态同时进入现有“任务与数据”。未启用账户时任务跳过，绝不自动开户。

## 历史研究证据

导入器仅读取明确列出的13个原始输入，不扫描06审计输出、不读取旧finalize_report结果。当前版本保留C原表0.55%的已知差异，展示复算55.22%；其他未知总表差异直接失败。严格核验模式连已知差异也判失败。保留源净值、交易、持仓、审计说明、转换记录和文件哈希。新净值从原权益重算，不沿用旧净值舍入值。原包只读。

历史仓位按明确列定义转换（C除以1、D除以100）并对照金额；期末持仓日期仅去除明确的“(期末快照)”后缀。逐笔库存、资金、持仓价格乘数量、日末现金及资产必须闭合。确认D旧样本278180.50元、较C差5580.50元仅说明原假设下算术一致，不表示严格PIT或可实现收益。

## 验证

```bash
MICROCAP_RESEARCH_PACKAGE=/path/to/ABCD策略全景审计交接包 .venv/bin/python -m unittest tests.test_microcap_rotation tests.test_microcap_rotation_ui
.venv/bin/python -m compileall app.py core services pages
git diff --check
```

测试使用临时账本和明确标为test的合成行情，覆盖完整日流程、暂停恢复、成交约束、权益事件及事务失败。真实缓存预检另行记录，不能把合成演练称为真实市场交易。原微盘成分及固定候选池历史入口保留。没有修改现有ETF50万元策略，也不连接券商。

2026-09-21修复：原首轮B成交8只、D未成交，原因为收盘价暂未取到且16:20误跳过已结日。补价及重算后B成交20只、D成交1笔，未成交均为0；旧日结保存在microcap_rotation_revisions。新增tests/test_microcap_rotation_repair.py覆盖归档、重复运行、事务失败回滚和单标的收盘快照回退。
