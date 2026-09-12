import { useEffect, useRef } from 'react'
import * as echarts from 'echarts/core'
import { LineChart, BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, DataZoomComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { Row } from './types'
echarts.use([LineChart, BarChart, GridComponent, TooltipComponent, DataZoomComponent, CanvasRenderer])
export function Chart({ rows, value = '净值', date = '日期', strategy = false }: { rows: Row[]; value?: string; date?: string; strategy?: boolean }) {
 const ref = useRef<HTMLDivElement>(null)
 const bar = strategy && value === '每日盈亏'
 useEffect(() => {
  if (!ref.current || !rows.length) return
  const chart = echarts.init(ref.current)
  const tooltip = (params: unknown) => {
   const point = (Array.isArray(params) ? params[0] : params) as {dataIndex: number}
   const row = rows[point.dataIndex]
   const box = document.createElement('div'); box.className = 'strategy-chart-tooltip'
   const title = document.createElement('div'); title.textContent = String(row[date]).slice(0,10); box.append(title)
   const fields = bar ? [['每日盈亏','当日盈亏（元）'],['每日收益率(%)','当日收益率'],['累计收益率(%)','累计收益率']] : [['净值','净值'],['每日收益率(%)','当日收益率'],['累计收益率(%)','累计收益率']]
   for (const [key,label] of fields) {
    const line = document.createElement('div'), name = document.createElement('span'), amount = document.createElement('b')
    const n = row[key], numeric = typeof n === 'number' && Number.isFinite(n), percent = key.includes('(%)')
    name.textContent = label; amount.dataset.field = key
    amount.textContent = numeric ? `${key!=='净值'&&n>0?'+':''}${n.toFixed(key==='净值'?6:2)}${percent?'%':''}` : '—'
    if (numeric && key !== '净值') amount.style.color = n>0?'#ca3a44':n<0?'#188366':'#737d8c'
    line.style.cssText='display:flex;justify-content:space-between;gap:18px'; line.append(name,amount); box.append(line)
   }
   return box
  }
  chart.setOption({ animation: false, grid: { left: 54, right: 16, top: 16, bottom: 36 }, tooltip: { trigger: 'axis', triggerOn: 'mousemove|click', confine: true, ...(strategy?{formatter:tooltip}:{}) }, xAxis: { type: 'category', data: rows.map(r => String(r[date]).slice(0,10)), axisLabel: { fontSize: 10, color: '#737d8c' } }, yAxis: { type: 'value', scale: !bar, axisLabel: { fontSize: 10, color: '#737d8c' }, splitLine: { lineStyle: { color: '#edf0f3' } } }, series: [bar ? { name:'当日盈亏（元）',type:'bar',barMaxWidth:20,data:rows.map(r=>({value:r[value],itemStyle:{color:typeof r[value]==='number' ? (r[value]>0?'#ca3a44':r[value]<0?'#188366':'#9ca3af'):'#9ca3af'}}))} : { name: value, type: 'line', data: rows.map(r => r[value]), symbol: 'none', lineStyle: { color: '#334e70', width: 2 }, areaStyle: { color: '#edf2f8' } }] })
  const observer = new ResizeObserver(() => chart.resize()); observer.observe(ref.current)
  return () => { observer.disconnect(); chart.dispose() }
 }, [rows, value, date, strategy, bar])
 return <div ref={ref} className="chart" role="img" aria-label={`${value}${bar?'柱状图':'趋势图'}，共${rows.length}个正式交易日`} />
}
