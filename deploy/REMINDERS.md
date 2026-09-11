# ETF 提醒部署与维护

本次部署验证（2026-09-11）：257 项相关测试及 4 项子测试通过，Python 编译通过。
已实际安装并反复启动 ETF service；非通知时点正确跳过，强制 dry-run 取得
11 项有效正式历史和 11 份报价，收盘后由原业务逻辑拒绝生成盘中建议。
盘中有效输入的完整计算/正文/事件键一致性由无真实发送的测试验证；不将收盘检查冒充盘中实测。
缓存路径修复部署后，公网首页、health、dashboard 均返回 200，15 个 ETF 正式数据
无缺口、策略无错误。timer 已 enabled，两个发送渠道关闭且 REMINDER_DRY_RUN=true。
真实方糖/微信测试均未执行；Windows 原生机器的完整验证留给后续本地切换。

Lightsail 仅承担 ETF 方糖提醒；Windows 的 ETF 与铁矿石提醒使用 Hermes 微信。
不在 Lightsail 安装 Hermes 或部署铁矿石任务。业务计算仍来自
`services.position_analysis` / `services.position_performance`，没有另写信号算法。

## 渠道策略

`ENABLE_FANGTANG` 和 `ENABLE_WECHAT` 缺省、空值或 false 都表示关闭。
true/1/yes/on 表示开启；无效值报错并停止，不会默默打开。
`REMINDER_DRY_RUN=true` 强制关闭两渠道，包括测试通知入口和底层发送函数。
`REMINDER_NODE=lightsail` 禁止微信；Linux ETF 专用入口还强制覆盖微信开关为 false。
这些变量要进入脚本进程环境；普通 Windows/WSL 脚本不自动读取仓库 `.env`。

迁移期：服务器 `ENABLE_FANGTANG=false`、`ENABLE_WECHAT=false`、
`REMINDER_DRY_RUN=true`。正式切换必须等 Windows ETF 已停止方糖，且用户确认。
届时服务器仅开启 Fangtang 并关闭 dry-run；Windows 明确设置 Fangtang=false、
WeChat=true。升级旧代码时缺少开关将停止发送，务必同步设置 Windows 任务环境。
铁矿石脚本永远只选择微信，不会因 Fangtang=true 改为双发；禁用微信时不改变其
阈值状态，重新启用后继续原有首次跌破/恢复后重布防规则。

## 私有配置与持久化

- `/srv/investment-dashboard/reminder.env`：0600，生产私有 EnvironmentFile。
  使用 `SERVERCHAN_SENDKEY`，真实值只在服务器安全编辑，不能发到对话或写进 Git。
  dry-run 不需要 SendKey。样板为 `deploy/reminder.env.example`。
- 已有仓库 `.env` 仅作为 TickFlow 等既有环境来源。第二个 EnvironmentFile 的同名值优先。
- `/srv/investment-dashboard/reminder-state/`：0700，账本、锁、兼容状态及轮转日志。
- `INVESTMENT_RUNTIME_DIR=/srv/investment-dashboard` 复用 Web 正式行情缓存。
  `core.cache.load_dataset` 对不存在的旧绝对路径回退到当前数据目录下相同
  source/symbol/period 的 CSV，兼容 Docker `/runtime` 与宿主机 `/srv`；不改写行情或元数据。
  内存中的盘中报价不持久化。状态不在 Docker 临时层，重建镜像不会删除它。
- 默认 Windows 状态使用 `core.paths.OUTPUT_DIR/alerts`，可用 `REMINDER_STATE_DIR` 指定。
  Windows 与 Lightsail 本地账本互不共享，因此必须保证每个渠道只有一个生产节点。

## 事件与防重

ETF 动作键：交易日 | ETF500K | preview | 正式持仓基准日 | 策略参数指纹 |
六位 ETF 代码 | 买入/卖出 | 净数量；SQLite 主键再加 channel（fangtang/wechat）。
指纹读取原 service 的 MA、阈值、仓位规则、权重、停车来源、资金、费用及整手参数。
不把价格、报价时间和提醒时点当作新买卖事件。同一天相同净操作在后续时点不重发；
数量或方向变化是新指令。一个批次仅渲染该渠道尚未成功发送的标的，保留先卖后买。
“无需操作”保留原时点通知，14:54 与 14:50 共用无需操作键；错误通知按日期/时点/错误类别内容区分。
`preview` 与正式持仓基准日明确写入键，盘中建议不产生正式成交记录。

`deliveries.sqlite3` 使用 SQLite `BEGIN IMMEDIATE` 原子认领，Windows/Linux 均可用，
没有模块级 fcntl 依赖。脚本运行锁在 POSIX 使用 flock，在 Windows 使用 msvcrt。
记录只含事件、渠道、状态、上海时间，不存正文或 secret。

- `sending` 在 HTTP 前提交；成功确认后才标记 `sent`。
- 明确 API 拒绝、连接建立超时记录 `failed`，下一次原定触发可重试。
- 响应读取超时、连接中断、未知异常记录 `uncertain`；进程崩溃留下 `sending`。
  两者都阻止自动重发，且不会错误标记成功。
- 方糖接口没有本项目可用的端到端幂等确认机制，无法同时保证模糊网络失败下
  “绝不重复”和“必达”。这里选择避免重复，保留待核对状态。
  核对服务商送达记录后，维护者可将具体事件/渠道改为 sent（已送达）或 failed
  （确认未送达）；不要删除整个账本，也不要自动清理待核对事件。
- JSON 时点状态仅供兼容/运行信息；新发送决策以事件账本为准。
  从旧 Windows 版本升级时不要在已发送的同一时点手动强制补跑。

## 安装、运行、升级

实际安装入口：`bash deploy/install-etf-reminder.sh`。初次只创建禁用发送的 dry-run 配置；
再次安装保留私有配置。服务不依赖 Docker 重建，使用仓库 `.venv`。

`position-etf-reminder.timer` 每天上海时间 09:45、11:45、14:45、14:50、14:54 触发。
Python 仍调用原交易日判断，节假日跳过；不补跑停机期间错过的盘中时点。
宿主机可保持 UTC，timer 的 OnCalendar 和 Python 均明确 Asia/Shanghai。
`position-etf-reminder.service` 为 oneshot，结束退出，不常驻；CPU 上限半个核，
MemoryHigh 256M / MemoryMax 384M / SwapMax 384M，最多 2 个行情获取线程、8 分钟总截止。
服务运行期间重复 start 不会新建进程；脚本锁和账本额外保护手工重复启动。

代码升级前停止 timer 并等待正在运行的 service 完成；切到已审查提交、在 `.venv`
安装需要的依赖、运行测试后重新安装 unit 并启动 timer。不要在盘中覆盖正在执行的代码。
安装器不安装系统 Python 包，也不安装 Hermes。

## 检查与日志

`systemctl list-timers position-etf-reminder.timer` 查看下一次时刻（宿主机可能显示 UTC）。
`systemctl show position-etf-reminder.service -p Result -p ExecMainStatus` 查看结果。
`journalctl -u position-etf-reminder.service` 查看 dry-run 正文、event key 和检查信息。
`reminder-state/position_timing_trade_alert.log` 按 2 MB 轮转，保留 2 份备份。
不要打印完整 EnvironmentFile、进程环境或包含密钥的外部响应。

临时设置 service 环境 `REMINDER_CHECK_NOW=true` 可在 dry-run 下检查非通知时点，
入口拒绝在真实发送模式使用它。检查完移除这个临时设置并 daemon-reload。
收盘后检查会由原业务服务拒绝生成盘中建议，这是正确状态，不能改时间伪装盘中实测。

## 测试与备份

运行 `.venv/bin/python -m pytest -q tests/test_alert_delivery.py tests/test_position_timing_trade_alert.py tests/test_price_alerts.py tests/test_notify.py`
及 position/Web 回归。所有通知测试 mock 传输，显式开启测试渠道，不会向微信/方糖发送。
Linux 测试包含独立进程和并发 SQLite 验证；Windows 导入/锁分支用隔离测试验证，
原生 Windows 全量运行仍应由本地节点升级时验证。

备份时停止 ETF timer，等待 service 退出，备份整个 reminder-state 目录及私有配置
到受保护位置；或使用 SQLite backup API 做在线账本快照。恢复必须连同发送账本，
不能只恢复行情缓存。重新启动 timer 前确认渠道开关与另一节点不重叠。
