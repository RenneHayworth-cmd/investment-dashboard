import React, { useEffect, useState, lazy, Suspense } from 'react'
import { createRoot } from 'react-dom/client'
import { Activity, ArrowDownUp, BarChart3, LayoutDashboard, RefreshCw, Search, Wallet } from 'lucide-react'
import { Button } from './components/ui/button'
const LazyChart = lazy(() => import('./Chart').then(module => ({ default: module.Chart })))
function Chart(props: {rows: Row[]; value?: string; date?: string; strategy?: boolean}) { return <Suspense fallback={<p className="empty">正在加载图表…</p>}><LazyChart {...props}/></Suspense> }
import type { Dashboard, Instrument, Row, Value } from './types'
import './style.css'

const fmt = (v: Value | undefined, digits = 2) => typeof v === 'number' ? v.toLocaleString('zh-CN', { maximumFractionDigits: digits, minimumFractionDigits: digits }) : v == null || v === '' ? '—' : Array.isArray(v) ? v.join('、') : String(v)
const sign = (v: Value | undefined) => typeof v === 'number' && v > 0 ? 'up' : typeof v === 'number' && v < 0 ? 'down' : ''
const signed = (v: Value | undefined, suffix = '') => typeof v === 'number' ? `${v > 0 ? '+' : ''}${fmt(v)}${suffix}` : '—'
const label = (v: Value | undefined) => v == null || v === '-' ? '待数据' : String(v)
const actionClass = (action: Value | undefined) => action === '买入' ? 'action-buy' : action === '卖出' ? 'action-sell' : ''
const actionTextClass = (action: Value | undefined) => action === '买入' ? 'up' : action === '卖出' ? 'down' : ''
const recordAction = (row: Row) => row['操作'] ?? row['操作指引']
const fieldDigits = (key: string) => /数量/.test(key) ? 0 : /价格|参考价|均线|收盘|成交价/.test(key) ? 3 : /净值/.test(key) ? 4 : 2
function Notices({ messages }: { messages: string[] }) { return <>{messages.filter(Boolean).map((m, i) => <p key={i} className="notice" role="status">{m}</p>)}</> }
function Fields({ row, keys }: { row: Row; keys?: string[] }) { return <dl className="fields">{(keys || Object.keys(row)).map(k => <div key={k}><dt>{k}</dt><dd className={k === '操作' || k === '操作指引' ? actionTextClass(row[k]) : /盈亏|收益|涨跌|涨幅|偏离/.test(k) ? sign(row[k]) : ''}>{fmt(row[k], fieldDigits(k))}</dd></div>)}</dl> }
function RecordCard({ row, tradeAction = false }: { row: Row; tradeAction?: boolean }) {
 const priority = ['基金名称','ETF名称','标的名称','代码','日期','操作','择时判断','策略参数','最新收盘','持仓数量','持仓市值','账户权重(%)','当日盈亏','每日盈亏','每日收益率(%)','净值','成交价','成交金额']
 const shown = priority.filter(k => k in row)
 const remaining = Object.keys(row).filter(k=>!shown.includes(k))
 return <article data-code={String(row['代码'] || '')} className={`record ${tradeAction ? actionClass(recordAction(row)) : ''}`}><Fields row={row} keys={[...shown,...remaining]}/></article>
}
function Rows({ rows, empty = '暂无记录', limit = 30, actionCards = false }: { rows: Row[]; empty?: string; limit?: number; actionCards?: boolean }) {
 const [all, setAll] = useState(false)
 return rows.length ? <><div className="record-grid">{(all ? rows : rows.slice(0,limit)).map((r,i) => <RecordCard row={r} tradeAction={actionCards} key={i}/>)}</div>{rows.length > limit && <Button variant="outline" onClick={() => setAll(!all)}>{all ? '收起记录' : `查看全部 ${rows.length} 条`}</Button>}</> : <p className="empty">{empty}</p>
}
function StrategyCurve({ rows }: { rows: Row[] }) {
 const options = [{label:'净值曲线',key:'净值'},{label:'每日盈亏',key:'每日盈亏'}]
 const [metric,setMetric] = useState('净值')
 return <div data-testid="strategy-curve"><div className="curve-switch" role="group" aria-label="曲线指标">{options.map(o=><Button key={o.key} variant={metric===o.key?'default':'outline'} aria-pressed={metric===o.key} onClick={()=>setMetric(o.key)}>{o.label}</Button>)}</div>
 {rows.length?<Chart rows={rows} value={metric} strategy/>:<p className="empty">数据不足，暂不生成曲线</p>}</div>
}
function IndexRows({ rows }: { rows: Row[] }) {
 return rows.length ? <div className="record-grid">{rows.map(row=><details className="panel index-reference" key={String(row['代码'])}><summary><span className="index-title"><strong>{String(row['指数名称'])}</strong><span className="code">{String(row['代码'])}</span></span><span>{label(row['择时判断'])}</span></summary><p className="section-note">{fmt(row['数据状态'])}</p><Fields row={row} keys={Object.keys(row).filter(k=>!['指数名称','代码','数据状态'].includes(k))}/></details>)}</div> : <p className="empty">暂无指数参考数据</p>
}
function StrategySummary({ data }: { data: Dashboard }) {
 const s = data.strategy.summary, latest = data.strategy.daily.at(-1), live = data.strategy_live
 const estimating = live.mode === 'intraday' || live.mode === 'pending_close'
 const complete = live.available && live.complete
 const pnl = estimating ? (complete ? live.daily_pnl : null) : latest?.['每日盈亏']
 const pnlRate = estimating ? (complete ? live.daily_return_pct : null) : latest?.['每日收益率(%)']
 return <section className="panel strategy-summary"><div className="section-title"><h2>50万元择时策略</h2><span className={`badge ${estimating ? 'preview' : ''}`}>{live.mode === 'pending_close' ? '收盘待确认 / 实时估算' : estimating ? '盘中实时 · 不写缓存' : '正式收盘'}</span></div>
 <p className="muted">{String(s['开始日期'] || data.strategy_parameters['开始日期']).slice(0,10)} 起 · 数据截至 {String(s['正式数据截止日'] || '等待完整正式数据')}</p>
 <div className="asset"><span>当日盈亏（百分比）</span><strong className={sign(pnl)} data-testid="strategy-pnl">{signed(pnl)}</strong><span className={sign(pnlRate)} data-testid="strategy-pnl-rate">{signed(pnlRate,'%')}</span></div>
 {estimating && <p className="section-note">持仓基准 {live.formal_date || '待数据'} · 持仓报价时间 {live.quote_time || '暂无'}<br/>按已有持仓估值，未执行今日预判买卖；下列指标截至上方数据日期。</p>}
 {estimating && !complete && <p className="notice">实时估值数据不完整{live.missing_codes.length ? `，缺少持仓报价：${live.missing_codes.join('、')}` : '，等待有效正式持仓及报价'}。</p>}
 <div className="metrics"><div><span>总资产</span><b>{fmt(s['策略资产'])}</b></div><div><span>总盈亏</span><b className={sign(s['累计盈亏'])}>{signed(s['累计盈亏'])}</b></div><div><span>净值</span><b>{fmt(s['当前净值'], 4)}</b></div><div><span>仓位</span><b>{s['当前仓位比例(%)'] == null ? '—' : `${fmt(s['当前仓位比例(%)'])}%`}</b></div></div>
 <Notices messages={[...data.strategy.errors, ...data.strategy.warnings, ...live.warnings]}/>
 </section>
}
function SymbolPnl({ data }: { data: Dashboard }) {
 const live = data.strategy_live, estimating = live.mode === 'intraday' || live.mode === 'pending_close'
 const rows = (estimating ? live.by_symbol : data.strategy.daily_by_symbol) || []
 return <section className="panel symbol-pnl" data-testid="symbol-pnl"><div className="section-title"><h2>持仓股</h2><span className={`badge ${estimating ? 'preview' : ''}`}>{live.mode === 'pending_close' ? '收盘待确认' : estimating ? '盘中估算' : '正式收盘'}</span></div><p className="muted small">{estimating ? live.quote_time || '等待报价' : live.formal_date || '等待正式数据'} · 元</p>
 <p className="section-note">← 左滑查看当日盈亏、仓位 · 盈亏为持仓浮动盈亏 · 仓位按策略总资产计算</p>
 {rows.length ? <div className="holdings-scroll" tabIndex={0} role="region" aria-label="模拟持仓，左右滑动查看指标"><table className="holdings-table"><colgroup><col className="holding-label"/>{Array.from({length:5},(_,i)=><col key={i}/>)}</colgroup><thead><tr>{['标的／市值','盈亏','持仓','成本／现价','当日盈亏','仓位'].map(k=><th key={k}>{k}</th>)}</tr></thead><tbody>{rows.map(row=><tr key={String(row['代码'])}>
 <th scope="row"><span className="holding-name" title={String(row['基金名称'])}>{String(row['基金名称'])}</span><small>{String(row['代码'])}{row['持仓数量']===0?' · 已清仓':''}</small><b>{fmt(row['持仓市值'])}</b></th>
 <td className={sign(row['浮动盈亏'])}><b>{signed(row['浮动盈亏'])}</b><small>{signed(row['浮动收益率(%)'],'%')}</small></td>
 <td><b>{fmt(row['持仓数量'],0)}</b><small>份</small></td>
 <td><b>{fmt(row['成本价'],3)}</b><small>{fmt(row['最新价'],3)}</small></td>
 <td className={`holding-daily ${sign(row['当日盈亏'])}`}><b>{row['当日盈亏']==null?'缺报价':signed(row['当日盈亏'])}</b><small>{signed(row['当日收益率(%)'],'%')}</small></td>
 <td><b>{row['账户权重(%)']==null?'—':`${fmt(row['账户权重(%)'],1)}%`}</b></td>
 </tr>)}</tbody></table></div> : <p className="empty">{estimating && !live.complete ? '实时估值数据不足' : '暂无当日持仓盈亏'}</p>}
 </section>
}
function TradePreview({ data }: { data: Dashboard }) {
 const preview = data.trade_preview
 return <section className="section" data-testid="trade-preview"><div className="section-title"><h2>ETF盘中实时预判</h2><span className="badge preview">仅盘中预览 · 不记录成交</span></div><p className="section-note">预判日期 {preview.preview_date || '暂无'} · 行情时间 {preview.quote_time || '暂无'}</p><Notices messages={[...preview.errors,...preview.warnings]}/>{preview.actions.length ? <div className="record-grid">{preview.actions.map((row,i)=><RecordCard key={i} row={row} tradeAction/>)}</div> : <p className="empty">当前无盘中买卖预判</p>}</section>
}
function EtfCard({ formal, preview, item, data, action }: { formal: Row; preview?: Row; item?: Instrument; data: Dashboard; action?: Value }) {
 const code = String(formal['代码']), active = data.preview_codes.includes(code), shown = active && preview ? preview : formal
 return <article data-code={code} className={`panel etf-card ${actionClass(action)}`}><div className="card-heading"><div><span className="code">{code}</span><h3>{String(formal['ETF名称'])}</h3></div><span className="param">{code === '512890' ? '承接资产' : String(formal['策略参数'])}</span></div>
 {actionClass(action) && <p className={`trade-label ${actionTextClass(action)}`}>预判{String(action)}</p>}
 <div className="price"><strong>{fmt(shown['最新价'],3)}</strong><span className={sign(shown['当日涨跌幅(%)'])}>{signed(shown['当日涨跌幅(%)'], '%')}</span></div>
 <div className="signal"><span>正式状态 <b>{label(formal['择时判断'])}</b></span><span className={active ? 'preview' : 'muted'}>{active ? `预览 ${label(shown['择时判断'])}` : '无盘中预览'}</span></div>
 <div className="metrics mini"><div><span>{active ? '预览均线' : '对应均线'}</span><b>{fmt(shown['对应均线'],3)}</b></div><div><span>偏离率</span><b className={sign(shown['偏离率(%)'])}>{signed(shown['偏离率(%)'],'%')}</b></div><div><span>组合权重</span><b>{fmt(shown['组合权重比例'])}</b></div></div>
 <p className="muted small">正式日期 {data.formal_dates[code] || '无'}{active ? ` · 预览 ${data.quote_time || '无'}` : ''}</p>
 {data.missing_formal_codes.includes(code) && <p className="notice">正式数据未达到目标日 {data.expected_formal_date}；当前状态仅对应上述正式日期。</p>}
 {item?.error && <p className="notice">{item.error}</p>}
 <details className="interval-metrics"><summary>区间表现与数据来源</summary><Fields row={formal} keys={['状态转换时间','区间涨幅(%)','上一状态转换时间','上一区间涨幅(%)','数据状态']}/><p className="muted small">{item?.source} · 缓存更新 {item?.cache_time || '无'}</p></details>
 </article>
}
function Derivatives({ data }: { data: Dashboard }) {
 return <><p className="section-note">具体合约原始价格 · 盘中报价不写入正式日线</p><div className="card-grid">{[...data.derivatives,...data.spreads].map(item => <article className="panel" key={item.code}><span className="code">{item.category}</span><h3>{item.name}</h3><p className="muted">数据日期 {item.latest_date || '无'} · {item.status}</p><Fields row={item.metrics} keys={Object.keys(item.metrics).slice(0,4)}/><details><summary>指标详情与走势</summary><Fields row={item.metrics} keys={Object.keys(item.metrics).slice(4)}/><History code={item.code}/></details><Notices messages={[item.error]}/><p className="muted small">{item.source}</p></article>)}</div></>
}
function History({ code }: { code: string }) {
 const [rows, setRows] = useState<Row[]>([]), [error,setError] = useState(''), [open,setOpen] = useState(false)
 async function load() { setOpen(!open); if (rows.length) return; try { const r=await fetch(`/api/instruments/${encodeURIComponent(code)}`); if (!r.ok) throw Error('历史暂不可用'); setRows((await r.json()).history) } catch { setError('历史数据加载失败，请稍后重试') } }
 const value = rows.length ? Object.keys(rows[0]).find(k => k.startsWith('spread_') && k.includes('_vs_')) || 'close' : 'close'
 return <><Button variant="outline" onClick={load}>{open ? '收起走势' : '查看走势'}</Button>{open && (error ? <p className="notice">{error}</p> : rows.length ? <Chart rows={rows.slice(-500)} date="date" value={value}/> : <p className="empty">暂无历史</p>)}</>
}
function App() {
 const [data,setData] = useState<Dashboard>(), [error,setError] = useState(''), [tab,setTab] = useState('概览'), [query,setQuery] = useState(''), [message,setMessage] = useState(''), [busy,setBusy] = useState(false)
 useEffect(() => {
  let live=true; const controller=new AbortController()
  async function load() { try { const response=await fetch('/api/dashboard',{signal:controller.signal}); if (!response.ok) throw new Error(response.status===503 ? '正在读取服务器缓存…' : response.status===401 ? '登录已失效，请重新载入页面登录' : '暂时无法连接服务器，保留上次页面数据'); const next=await response.json(); if(live){setData(next);setError('')} } catch(e) { if(live)setError(e instanceof Error ? e.message : '网络连接失败') } }
  void load(); const timer=setInterval(() => { if(document.visibilityState==='visible')void load() },5000)
  const visible=()=>{if(document.visibilityState==='visible')void load()};document.addEventListener('visibilitychange',visible)
  return ()=>{live=false;controller.abort();clearInterval(timer);document.removeEventListener('visibilitychange',visible)}
 },[])
 async function refresh(){setBusy(true);try{const r=await fetch('/api/refresh',{method:'POST',headers:{'X-Position-Client':'web'}});if(!r.ok)throw Error();setMessage((await r.json()).message)}catch{setMessage('刷新请求失败，请稍后重试')}finally{setBusy(false)}}
 const tabs=[{name:'概览',icon:LayoutDashboard},{name:'ETF',icon:Activity},{name:'策略',icon:Wallet},{name:'衍生品',icon:ArrowDownUp}]
 const actionByCode = new Map(data?.trade_preview.actions.map(row=>[String(row['代码']),row['操作']]) || [])
 return <><header><div className="brand"><BarChart3 size={22}/><div><h1>持仓分析</h1><span>市场观察与择时模拟</span></div></div><Button variant="outline" onClick={refresh} disabled={busy || data?.refreshing}><RefreshCw size={15}/>{data?.refreshing ? '更新中' : '刷新'}</Button></header>
 <main><div className="page-intro"><span className="eyebrow">投资工作台 / {tab}</span><h2>{tab==='概览'?'今日观察':tab==='ETF'?'ETF 择时':tab==='策略'?'策略表现':'衍生品监控'}</h2></div>
 <Notices messages={[error,message]}/>
 {!data ? <div className="panel skeleton"><p>正在读取服务器已有缓存…</p><p className="muted">首次初始化可能需要数分钟；取得数据后会自动显示。</p></div> : <>
 <div className="status-strip"><div><span className="dot"/>{data.session} · 上海时间</div><span>正式缓存更新 {data.formal_updated_at || '暂无'}</span><span>实时报价 {data.quote_time || '暂无当日报价'}</span>{data.refreshing && <span role="status">{data.refresh_stage}</span>}</div>
 <Notices messages={[data.refresh_error, data.missing_quote_codes.length ? `当前刷新时段尚缺有效报价：${data.missing_quote_codes.join('、')}。对应标的保留正式状态。` : '']}/>
 {tab==='策略'&&<><StrategySummary data={data}/><SymbolPnl data={data}/></>}
 {tab==='概览'&&<><TradePreview data={data}/><section className="section" data-testid="guidance"><div className="section-title"><h2>近期操作指引</h2><span className="badge">正式收盘 · 近7天</span></div><p className="section-note">不含盘中预览。正式数据缺失时，不应将“暂无记录”理解为已确认无需操作。</p><Rows rows={data.guidance} actionCards empty="当前正式缓存暂无新的操作记录" limit={4}/></section></>}
 {tab==='ETF'&&<><label className="search"><Search size={18}/><input placeholder="搜索代码或基金名称" value={query} onChange={e=>setQuery(e.target.value)}/></label><p className="section-note">按正式均线偏离率排序。正式信号与实时预览分别显示；预览不产生正式操作记录。</p><div className="card-grid">{data.etf_formal.filter(r=>`${r['代码']}${r['ETF名称']}`.includes(query)).map(row=><EtfCard key={String(row['代码'])} formal={row} preview={data.etf_preview.find(p=>p['代码']===row['代码'])} item={data.items.find(i=>i.code===row['代码'])} data={data} action={actionByCode.get(String(row['代码']))}/>)}</div></>}
 {tab==='策略'&&<><section className="panel"><StrategyCurve rows={data.strategy.daily}/><p className="section-note">初始资金 {fmt(data.strategy_parameters['初始资金'])} 元 · {String(data.strategy_parameters['开始日期']).slice(0,10)} 起 · 前复权正式收盘 · 同收盘成交 · {fmt(data.strategy_parameters['整手份数'],0)} 份整手 · 单边费率 {fmt(data.strategy_parameters['单边费率'],5)}。初始持有须先退出再买入，承接仓位按业务规则延迟激活。</p><details><summary>策略摘要与费用</summary><Fields row={data.strategy.summary}/></details></section><TradePreview data={data}/><details className="panel"><summary>模拟交易记录</summary><Rows rows={[...data.strategy.trades].reverse()} empty="暂无模拟成交" limit={15}/></details><details className="panel"><summary>每日收益与盈亏</summary><Rows rows={[...data.strategy.daily].reverse()} limit={10}/></details></>}
 {tab==='衍生品'&&<Derivatives data={data}/>}
 {tab==='ETF'&&<section className="section"><h2>指数择时参考</h2><p className="section-note">独立参考，不计入 ETF 权重或50万元模拟策略。</p><Notices messages={data.missing_index_codes.length ? [`指数正式数据尚缺目标日 ${data.expected_formal_date}：${data.missing_index_codes.join('、')}，请以各行日期为准。`] : []}/><IndexRows rows={data.index_formal}/>{data.index_preview.length>0&&<div><h3>指数实时预判</h3><IndexRows rows={data.index_preview}/></div>}</section>}
 <footer>页面快照 {data.generated_at} · 正式目标日 {data.expected_formal_date}<br/>所有盘中预览仅保留在服务器内存中</footer>
 </>}
 </main><nav aria-label="主要导航">{tabs.map(({name,icon:Icon})=><button key={name} aria-current={tab===name?'page':undefined} onClick={()=>{setTab(name);window.scrollTo({top:0})}}><Icon size={20}/><span>{name}</span></button>)}</nav></>
}
createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>)
