import { useEffect, useRef } from 'react'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, DataZoomComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { Row } from './types'
echarts.use([LineChart, GridComponent, TooltipComponent, DataZoomComponent, CanvasRenderer])
export function Chart({ rows, value = '净值', date = '日期' }: { rows: Row[]; value?: string; date?: string }) {
 const ref = useRef<HTMLDivElement>(null)
 useEffect(() => {
  if (!ref.current || !rows.length) return
  const chart = echarts.init(ref.current)
  chart.setOption({ animation: false, grid: { left: 54, right: 16, top: 16, bottom: 36 }, tooltip: { trigger: 'axis', confine: true }, xAxis: { type: 'category', data: rows.map(r => String(r[date]).slice(0,10)), axisLabel: { fontSize: 10, color: '#737d8c' } }, yAxis: { type: 'value', scale: true, axisLabel: { fontSize: 10, color: '#737d8c' }, splitLine: { lineStyle: { color: '#edf0f3' } } }, series: [{ name: value, type: 'line', data: rows.map(r => r[value]), symbol: 'none', lineStyle: { color: '#334e70', width: 2 }, areaStyle: { color: '#edf2f8' } }] })
  const observer = new ResizeObserver(() => chart.resize()); observer.observe(ref.current)
  return () => { observer.disconnect(); chart.dispose() }
 }, [rows, value, date])
 return <div ref={ref} className="chart" role="img" aria-label={`${value}趋势图，共${rows.length}个正式交易日`} />
}
