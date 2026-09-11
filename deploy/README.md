# 持仓分析 Web

只增加 `pages/5_持仓分析.py` 对应的观察池与模拟策略 Web 入口。Streamlit
页面保留；没有迁移 ETF/期货真实账户记录。后端导入原仓库 services，
不包含第二份 MA、仓位、512890 或收益算法。

## 本次环境与审计

- 开发分支 `feat/portfolio-web`；Ubuntu / Python 3.12 / Node 22。
- 原始四组 position 测试无法收集：`position_close_audit` 未提交到仓库。
  已补齐该审计适配模块，使用既有 jobs 表，仅记录标的、目标日期与结果；
  不存储调用参数、密钥或原始异常。原四组测试修复后为 123 passed / 4 subtests。
- ETF 来源与复权校验：`position_market` → `fund_analysis`；161128 保留
  EastMoney/AkShare → 经复权元数据确认的新浪历史/正式收盘备用源。
- 正式过滤与目标交易日：`position_sessions` → `market_calendar`。
- MA、半仓、512890 承接及近期指导：`position_timing`。
- 固定模拟组合/交易预判：`position_performance` → `fund_rotation`。
  初始50万元、2026-08-05、100份整手、0.006%单边费用等从业务层读取。
- 衍生品：`position_derivatives` → `futures_spread` / `futures_options_analysis`。
  修复正式价差更新使用盘中合并函数的问题：正式路径只追加未见日期；
  盘中覆盖只发生在瞬时对象中，不写缓存。
- 指数参考：`position_timing` 读取 long/history/final/correction overlay；
  更新经过稳定门面 `services.update_tasks`，只选择微盘股、中证500。
  内部 orchestration 依赖兼容门面注入，不应直接作为外部入口调用。
- `components/position/realtime.py` 的 session/fragment 编排不被 Web 导入。
  Web 服务替换这部分运行状态管理，策略运算仍然共用。

## 运行与环境变量

开发后端（项目虚拟环境）：

```bash
.venv/bin/python -m pip install -r web/backend/requirements.lock
.venv/bin/python -m uvicorn web.backend.app:app --host 127.0.0.1 --port 8000
```

开发前端（Node 22）：

```bash
cd web/frontend
npm ci
npm run dev
```

Vite 将同源 `/api` 代理至本机8000。开发端口只绑定回环地址。
`.env` 由后端 dotenv 或 Compose 读取；不得进入镜像、前端或 Git。
示例 `.env.example` 只包含空变量。生产所需配置：

| 变量 | 作用 |
| --- | --- |
| `TICKFLOW_API_KEY` | 仅注入后端容器 |
| `WEB_USERNAME` | Caddy 单用户登录名 |
| `WEB_PASSWORD_HASH` | bcrypt 密码散列；`.env` 中使用单引号包围，保留 `$` |
| `WEB_DOMAIN` | 已指向服务器的域名；默认 `portfolio.nineskyit.top` |
| `WEB_ORIGIN` | 精确的 HTTPS origin，保护刷新请求 |
| `WEB_BIND_IP` | 默认0.0.0.0，对公网开放80/443；本机调试可设127.0.0.1 |
| `POSITION_DATA_DIR` | 宿主机数据目录，默认 `/srv/investment-dashboard` |
| `INVESTMENT_RUNTIME_DIR` | 非 Docker 运行时的数据路径；Docker 固定 `/runtime` |

本次生成的 Web 密码保存在服务器 `/srv/investment-dashboard/web-access.txt`，
权限0600。不在聊天、日志或 Git 输出。Caddy 只接收散列，前端没有密钥输入框。
修改密码时生成新的 bcrypt hash 并更新 `.env`，重新创建 Caddy 容器。

## 生产启动、升级与日志

仓库根目录执行：

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail 100 backend
```

Docker daemon 已设开机启动，两个服务均为 `unless-stopped`。API 只暴露于
Docker 内部网络；没有宿主机8000端口、数据库端口、API调试文档或目录浏览。
Caddy 统一提供静态前端、认证、HTTPS及反向代理，无跨域前端配置。
刷新 POST 额外要求同源和自定义请求头，不允许浏览器绕过节流。

升级先快进同步已审查的 feature branch，再运行上述 Compose 命令。
后端 Python 和前端 npm 均有锁文件。镜像中的源码不绑定宿主机，因此修改源码
后需 build。运行目录始终挂载到同一 `/runtime`，SQLite 内部的绝对 CSV 路径
在镜像升级后仍有效。不要执行 `docker compose down -v`，它会删除证书卷。
回退可检出上一已验证 commit 后重建；运行数据不随源码回退。

正式入口为 `https://portfolio.nineskyit.top`，DNS指向本机公网IP
`18.139.5.171`。Caddy使用Let’s Encrypt自动签发、续期的公网可信证书；
HTTP自动308跳转HTTPS。现有Basic Auth保护首页和全部API，匿名访问返回401。
证书和ACME状态保存在持久化Caddy卷中，重启无需重新申请证书。

如改回本机开发，可显式配置 `WEB_DOMAIN=localhost`、
`WEB_ORIGIN=https://localhost`、`WEB_BIND_IP=127.0.0.1`；此模式使用本地CA，
不能当作公网可信HTTPS。域名/IP变更必须同时核验DNS及Lightsail入站规则。

## 状态与数据语义

- **仅一个 API worker、一个后端副本**。多 worker 不共享 Python 内存，不可
  随意扩容。当前硬件约1 GB内存，已配置2 GB swap。
- 唯一后台写线程负责串行刷新与发布不可变 JSON 快照，HTTP GET 不触发
  昂贵计算或数据源网络请求。页面每5秒只读取服务器快照，后台每30秒检查。
- 启动先展示本地缓存，再自动补齐缺失正式历史；Web不要求用户输入Key。
- 报价批次复用 `position_runtime` 的带锁缓存、10/30/2分钟时段和午间成功
  去重规则。多设备请求及手动刷新都不能绕过行情节奏。
- 15:00停止报价，15:05后仅在相应ETF正式收盘已确认时清除该ETF预览；
  缺失时保留当天最后报价，显示旧正式日期和目标日缺口。
- 正式数据逐标的检查；失败按目标日及10分钟间隔重试，目标日变化立即重查。
  无缺口的指数不调用更新器。已有缓存因来源失败保留，错误持续显示。
- 报价、衍生品预览、JSON快照和模拟计算结果只在内存中，不写数据库/CSV。
  重启后盘中报价丢失，正式历史仍在。模拟结果每次从正式数据重建，停止于
  第一处不完整交易日，不将预览写入近期指导、模拟成交或正式收益。
- ETF正式缓存保持版本及复权口径。漂移重建失败仍显示最后行情，暂停正式
  策略计算；不以过期或空缓存冒充当天正式收盘。

指数预判对完整历史检查交易日缺口。A股静态休市日覆盖2022—2026年，
历史来源链接见 `services/market_calendar.py`；不依赖服务器安装可选交易日历库。
超出已知日历覆盖时，显示“交易日历未覆盖”，不将未知休市日宣称为缺行情。
真实历史缺口仍暂停预判，不能仅检查最近一个均线窗口来绕过滞回状态完整性校验。

东方财富通用日线的港股补充/过滤仅用于配置了港股来源的指数。
BK1158 属于 A 股，必须保留香港单独休市日的日线。2026-09-11排查确认旧缓存
因此缺少2026-04-03、04-07、05-25、07-01四天；当日同源补数接口断连，
尚未修复存量四行。后续补数必须从 `90.BK1158` 同口径历史核对已有日期重合价格，
仅追加缺失的正式交易日，不把这些日期当成 A 股假日、不填造价格、不改写已有日期。

## API 合约 v2

主页面只请求 `GET /api/dashboard`：包含正式/预览ETF和指数、逐标的日期、
目标正式日期、缺失标的、刷新进度、指导、模拟摘要/持仓/交易/每日收益、
交易预判、衍生品。金额和百分数不重新计算，直接序列化 service 的中文字段。
NaN/NaT/pd.NA/Infinity转换为null，时间使用`YYYY-MM-DD HH:MM:SS`（上海时区）。

`schema_version=2` 新增独立的 `strategy_live`，不改变 `strategy.daily`：

- `mode`：`intraday`（盘中）、`pending_close`（收盘待确认）、`formal`
  （显示正式 daily）或 `unavailable`。
- `formal_date` 是正式持仓基准日，盘中必须为上一完整 A 股交易日；
  `valuation_date` 是上海当前日期，`quote_time` 是参与估值报价中最早的时间。
- `available` 和 `complete` 同为 true 才能展示实时金额。缺少有效持仓报价时，
  `missing_codes` 列出代码，`daily_pnl`、`estimated_assets` 为 null，不返回部分合计。
- `services.position_performance.build_position_timing_intraday_valuation` 使用正式
  positions 的数量和最后正式价格，对共享内存报价计算
  `Σ 数量 × (实时价 − 正式价)`，估算资产为最后正式资产加该盈亏。
  不执行交易预判、不估算成交费用、不改变现金和正式 NAV。未持有标的不需要报价。
- 只接受上海当日、不晚于估值时刻的正数有限报价；正式基准过旧时暂停估值。
  15:00 后保留有效报价并标记待确认；15:05 后，原有 audited formal close 流程
  确认完整日线、正式 daily 包含当天后，切换为当天正式盈亏。不会另建策略数据库。
- 所有盘中估值只在内存和 API 快照中，不能写 CSV、SQLite、正式持仓或收益曲线。
  Coordinator 调用现有共享行情，不因该估值增加行情请求。

概览只展示盘中预判、近期操作指引和指数参考；50万元策略摘要只在策略页。
预判及 ETF 卡片的红/绿背景严格来自 `trade_preview.actions` 的买入/卖出；
正式操作指引仅操作文字着色，且不包含盘中预览。记录指标与 ETF 区间数据直接显示，
走势图仍按需加载。

2026-09-11 验收：268 项 Python 回归及4项子测试通过，覆盖持仓、提醒和共享缓存；
编译及前端构建通过。候选构建12项浏览器测试通过，正式 HTTPS 部署18项测试通过，
包含 iPhone WebKit、手机 Chromium 和桌面。线上新估值使用2026-09-10正式持仓与
9月11日共享报价；部署前后正式策略和指引一致。缺报价与收盘确认切换通过固定时钟
测试验证，未伪造生产日期或交易；当日实际收盘切换仍由既有正式日线确认流程触发。

细分接口复用同一个快照，不重复计算：

- `GET /api/health`：进程健康和缓存就绪状态。
- `GET /api/timing/etf`、`/api/timing/index`：`formal`和`preview`分开返回。
- `GET /api/guidance/recent`：正式收盘指导。
- `GET /api/strategy/summary|positions|performance|trades`：`data`及warnings/errors。
- `GET /api/strategy/trade-preview`：盘中预计操作，独立warnings/errors。
- `GET /api/derivatives`、`/api/spreads`：当前显示对象。
- `GET /api/instruments/{code}`：快照内已有历史，用于展开走势。
- `POST /api/refresh`：202，要求`X-Position-Client: web`，合并并发请求。

未读入缓存时 dashboard 返回503，前端保持加载状态并重试。认证由Caddy保护
所有路径；生产 traceback 不返回浏览器，数据源错误经过secret和URL清理。

## 验证

```bash
.venv/bin/python -m pytest -q tests/test_position*.py
.venv/bin/python -m compileall app.py core services pages
npm --prefix web/frontend run build
.venv/bin/python tests/export_position_web_fixture.py
npm --prefix web/frontend run test:smoke
```

浏览器测试使用已运行的站点，默认`https://portfolio.nineskyit.top`且严格校验证书。测试专用配置接受
`WEB_TEST_URL`和`WEB_CREDENTIALS_FILE`；只有显式设置
`WEB_TEST_INSECURE_TLS=1`的本机CA测试才忽略证书校验。
不能把这项忽略当作公网HTTPS验证。WebKit iPhone、Chromium手机及桌面场景
覆盖真实API、合成完整业务数据、四个视图、图表、横向溢出及认证。
合成夹具从真实service产生，不是线上假行情。测试产物位于`/tmp/position-web-verification`。

## 备份与恢复

行情、SQLite、输出统一位于宿主机`POSITION_DATA_DIR`；Caddy证书存于named
volumes。可靠备份时短暂停止backend再复制整个数据目录，包含DB和CSV，完成
后立即启动backend。也可用SQLite backup API制作在线数据库副本，但CSV需在
同一无写入窗口复制，不能只备份数据库。备份文件与登录凭据应限制读取权限。
恢复到新主机后重新挂载至`/runtime`，保留UID1000可写。不要将这些文件提交Git。

官方部署参考：[FastAPI进程与内存](https://fastapi.tiangolo.com/deployment/concepts/)、
[Caddy认证](https://caddyserver.com/docs/caddyfile/directives/basic_auth)。

## 本次验收记录（2026-09-10）

- 相关 Python 回归：301 passed，4 subtests；其中 Web/API 21项。
- 两个旧指数源测试补充固定时钟，避免8月样例随时间移出30天窗口。
- compileall 与 TypeScript/Vite production build 通过。
- 最终已部署版本：iPhone WebKit、手机Chromium、桌面共9项浏览器测试通过，
  包括四个视图、图表、整页横向溢出、数字单元格溢出和无认证拒绝。
- 真实正式数据的摘要、持仓、每日收益、交易与直接service调用完全一致。
- ETF和两个指数均初始化至2026-09-09；当时正式日期缺口为0。
- 后端重启前后24个正式CSV文件的SHA256完全一致。
- 本机CA证书校验、登录访问、无认证401和刷新请求保护403均通过。
- 初次验收仅本机入口；现已完成下述正式域名切换。

## 正式公网验收（2026-09-10）

- 域名：`portfolio.nineskyit.top` → `18.139.5.171`；宿主机监听0.0.0.0:80/443。
- 系统UFW未启用；Let’s Encrypt多地HTTP-01验证成功，证实外部80端口可达。
- Let’s Encrypt YE2证书已签发，SAN匹配域名，有效期至2026-12-09 03:43:24 UTC。
  Caddy自动续期，证书卷持久化；两个服务重启后证书、认证和接口仍正常。
- HTTP返回308并跳转同路径HTTPS，匿名HTTPS返回401，认证首页/API返回200。
- 意大利、波兰、乌克兰独立节点均通过公网443取得预期401认证响应；
  [外部探测记录](https://check-host.net/check-report/4ad0fbc6k290)。未向探测服务发送凭据。
- 正式域名启用严格TLS验证的9项WebKit/Chromium手机和桌面测试全部通过。
- 认证和TickFlow密钥未变更。重启后15只ETF和两个指数正式缓存均无目标日期缺口。
