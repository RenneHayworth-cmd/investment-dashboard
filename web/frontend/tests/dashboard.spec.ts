import { test, expect, type Page } from '@playwright/test'
import fs from 'node:fs'
import https from 'node:https'

async function noOverflow(page: Page) {
 expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
 expect(await page.locator('.metrics b').evaluateAll(nodes => nodes.every(node => node.scrollWidth <= node.clientWidth + 1))).toBe(true)
}
const fixtureData = () => JSON.parse(fs.readFileSync('/tmp/position-web-fixture.json','utf8'))

test('部署首页和真实 API 可用', async ({ page, request }) => {
 const errors: string[] = []; page.on('pageerror', error => errors.push(error.message))
 const health = await request.get('/api/health'); expect(health.ok()).toBe(true)
 expect((await health.json()).status).toBe('ok')
 const dashboard = await request.get('/api/dashboard')
 expect((await dashboard.json()).schema_version).toBe(2)
 await page.goto('/')
 await expect(page.getByRole('heading',{name:'持仓分析',exact:true})).toBeVisible()
 await expect(page.getByRole('heading',{name:'ETF盘中实时预判'})).toBeVisible({timeout:30000})
 await expect(page.getByRole('heading',{name:'50万元择时策略'})).toHaveCount(0)
 // Validate the production response rendered on screen, not only fixtures.
 const actualCards = page.getByTestId('guidance').locator('.record')
 for (let i=0; i<await actualCards.count(); i++) {
  const card = actualCards.nth(i)
  const operation = card.locator('.fields > div').filter({has:page.locator('dt', {hasText:/^操作指引$/})}).locator('dd')
  const action = await operation.innerText()
  await expect(card).toHaveCSS('background-color',action==='买入'?'rgb(255, 240, 241)':action==='卖出'?'rgb(237, 248, 241)':'rgb(255, 255, 255)')
 }
 await page.getByRole('button',{name:'ETF',exact:true}).click()
 await expect(page.getByPlaceholder('搜索代码或基金名称')).toBeVisible()
 await expect(page.getByText('159501',{exact:true})).toBeVisible()
 await noOverflow(page)
 await page.getByRole('button',{name:'策略',exact:true}).click()
 await expect(page.getByRole('heading',{name:'50万元择时策略'})).toBeVisible()
 await noOverflow(page)
 expect(errors).toEqual([])
})

test('fixture 完整数据四个视图、图表与窄屏', async ({ page }) => {
 const fixture=fixtureData()
 await page.route('**/api/dashboard', route=>route.fulfill({json:fixture}))
 const errors: string[]=[];page.on('pageerror',e=>errors.push(e.message))
 await page.goto('/')
 await expect(page.getByRole('heading',{name:'ETF盘中实时预判'})).toBeVisible()
 await expect(page.getByText('ETF观察池',{exact:true})).toHaveCount(0)
 await expect(page.getByRole('heading',{name:'50万元择时策略'})).toHaveCount(0)
 await expect(page.getByText('近期操作指导',{exact:true})).toHaveCount(0)
 const preview = await page.getByTestId('trade-preview').boundingBox()
 const guidance = await page.getByTestId('guidance').boundingBox()
 expect(preview!.y).toBeLessThan(guidance!.y)
 await expect(page.getByText('更多指标',{exact:true})).toHaveCount(0)
 await noOverflow(page)
 await page.getByRole('button',{name:'ETF',exact:true}).click()
 await page.getByPlaceholder('搜索代码或基金名称').fill('159501')
 await expect(page.getByRole('heading',{name:'纳指ETF嘉实',exact:true})).toBeVisible()
 await expect(page.locator('.etf-card').getByText('状态转换时间',{exact:true})).not.toBeVisible()
 await page.locator('.etf-card summary').click()
 await expect(page.locator('.etf-card').getByText('状态转换时间',{exact:true})).toBeVisible()
 await expect(page.locator('.etf-card').getByText('上一区间涨幅(%)',{exact:true})).toBeVisible()
 await expect(page.locator('.etf-card details')).toHaveAttribute('open','')
 await noOverflow(page)
 await page.getByRole('button',{name:'策略',exact:true}).click()
 await expect(page.getByRole('heading',{name:'持仓股'})).toBeVisible()
 await expect(page.getByRole('heading',{name:'净值曲线'})).toHaveCount(0)
 await expect(page.getByText('模拟策略当前仓位',{exact:true})).toHaveCount(0)
 await expect(page.getByRole('img',{name:/净值趋势图/})).toBeVisible()
 await expect(page.getByText('当日盈亏（百分比）',{exact:true})).toBeVisible()
 await noOverflow(page)
 await page.screenshot({path:`/tmp/position-web-verification/strategy-${test.info().project.name}.png`,fullPage:false})
 await page.getByRole('button',{name:'衍生品',exact:true}).click()
 await expect(page.getByRole('heading',{name:'衍生品监控'})).toBeVisible()
 await noOverflow(page)
 expect(errors).toEqual([])
})

test('fixture 买卖卡与真实指引字段的背景和文字着色', async ({ page }) => {
 const fixture=fixtureData()
 const codes = fixture.etf_formal.slice(0,3).map((row: Record<string,unknown>)=>row['代码'])
 fixture.trade_preview.actions = ['买入','卖出'].map((action,i)=>({操作:action,代码:codes[i],基金名称:`测试基金${i}`,数量:100,参考价:1.2345,预计金额:123.45,原因:'服务返回的实际持仓差额'}))
 // Formal guidance uses 操作指引, unlike preview's 操作.
 fixture.guidance = fixture.trade_preview.actions.map(({操作, ...row}: Record<string, unknown>)=>({...row, 操作指引:操作}))
 await page.route('**/api/dashboard', route=>route.fulfill({json:fixture}))
 await page.goto('/')
 const preview=page.getByTestId('trade-preview'), guidance=page.getByTestId('guidance')
 await expect(preview.locator('.action-buy')).toHaveCSS('background-color','rgb(255, 240, 241)')
 await expect(preview.locator('.action-sell')).toHaveCSS('background-color','rgb(237, 248, 241)')
 for (const key of ['操作','代码','基金名称','数量','参考价','预计金额','原因']) {
  await expect(preview.locator('.action-buy').locator('dt').filter({hasText:new RegExp(`^${key}$`)})).toBeVisible()
 }
 await expect(preview.getByText('仅盘中预览 · 不记录成交')).toBeVisible()
 await expect(guidance.locator('dd').getByText('买入',{exact:true})).toHaveCSS('color','rgb(202, 58, 68)')
 await expect(guidance.locator('dd').getByText('卖出',{exact:true})).toHaveCSS('color','rgb(24, 131, 102)')
 await expect(guidance.locator('.record.action-buy')).toHaveCSS('background-color','rgb(255, 240, 241)')
 await expect(guidance.locator('.record.action-sell')).toHaveCSS('background-color','rgb(237, 248, 241)')
 await noOverflow(page)
 await page.getByRole('button',{name:'ETF',exact:true}).click()
 await expect(page.locator(`.etf-card[data-code="${codes[0]}"]`)).toHaveCSS('background-color','rgb(255, 240, 241)')
 await expect(page.locator(`.etf-card[data-code="${codes[1]}"]`)).toHaveCSS('background-color','rgb(237, 248, 241)')
 await expect(page.locator(`.etf-card[data-code="${codes[2]}"]`)).toHaveCSS('background-color','rgb(255, 255, 255)')
 await noOverflow(page)
})

test('fixture 实时缺失、待确认与正式盈亏切换', async ({ page }) => {
 const fixture=fixtureData()
 fixture.strategy_live={...fixture.strategy_live,mode:'intraday',available:true,complete:true,daily_pnl:234.56,daily_return_pct:0.046912,estimated_assets:500234.56}
 await page.route('**/api/dashboard', route=>route.fulfill({json:fixture}))
 await page.goto('/')
 await page.getByRole('button',{name:'策略',exact:true}).click()
 await expect(page.getByTestId('strategy-pnl')).toHaveText('+234.56')
 await expect(page.getByTestId('strategy-pnl-rate')).toHaveText('+0.05%')
 for (const label of ['总资产','总盈亏','净值','仓位']) await expect(page.locator('.strategy-summary').getByText(label,{exact:true})).toBeVisible()
 await expect(page.getByText('盘中实时 · 不写缓存')).toBeVisible()
 fixture.strategy_live={...fixture.strategy_live,available:false,complete:false,missing_codes:['159501'],daily_pnl:null,estimated_assets:null}
 await expect(page.getByTestId('strategy-pnl')).toHaveText('—',{timeout:10000})
 await expect(page.getByText(/缺少持仓报价：159501/)).toBeVisible()
 fixture.strategy_live={...fixture.strategy_live,mode:'pending_close',available:true,complete:true,missing_codes:[],daily_pnl:345.67}
 await expect(page.getByText('收盘待确认 / 实时估算',{exact:true})).toBeVisible({timeout:10000})
 await expect(page.getByTestId('strategy-pnl')).toHaveText('+345.67')
 fixture.strategy_live={...fixture.strategy_live,mode:'formal',available:false,daily_pnl:null,formal_date:fixture.strategy_live.valuation_date}
 fixture.strategy.daily.at(-1)['每日盈亏']=456.78
 await expect(page.getByText('当日盈亏（百分比）')).toBeVisible({timeout:10000})
 await expect(page.getByTestId('strategy-pnl')).toHaveText('+456.78',{timeout:10000})
 await expect(page.getByText('盘中估算策略资产（元）')).toHaveCount(0)
 await noOverflow(page)
})

test('fixture 空预判保持不确定语义并展示警告', async ({ page }) => {
 const fixture=fixtureData()
 fixture.trade_preview.actions=[]
 fixture.trade_preview.warnings=['测试行情缺失提醒']
 fixture.trade_preview.errors=['测试预判不可用']
 await page.route('**/api/dashboard', route=>route.fulfill({json:fixture}))
 await page.goto('/')
 await expect(page.getByText('当前无盘中买卖预判')).toBeVisible()
 await expect(page.getByText('测试行情缺失提醒')).toBeVisible()
 await expect(page.getByText('测试预判不可用')).toBeVisible()
 await noOverflow(page)
})

test('fixture 逐标的盈亏手机单行及实时正式切换', async ({ page }) => {
 const fixture=fixtureData()
 fixture.strategy_live.by_symbol=[
  {代码:'159501',基金名称:'纳指ETF嘉实很长的基金名称用于检查窄屏',当日盈亏:1234.56,持仓市值:12345.67,持仓数量:1000,成本价:10.123,最新价:12.346,浮动盈亏:2222.67,'浮动收益率(%)':21.96,'账户权重(%)':2.5},
  {代码:'510500',基金名称:'中证500ETF',当日盈亏:-78.9},
  {代码:'512890',基金名称:'红利低波ETF',当日盈亏:null},
 ]
 await page.route('**/api/dashboard',route=>route.fulfill({json:fixture}))
 await page.goto('/')
 await page.getByRole('button',{name:'策略',exact:true}).click()
 const card=page.getByTestId('symbol-pnl')
 await expect(card.getByRole('heading',{name:'持仓股'})).toBeVisible()
 await expect(card.locator('.holding-daily b').nth(0)).toHaveCSS('color','rgb(202, 58, 68)')
 await expect(card.locator('.holding-daily b').nth(1)).toHaveCSS('color','rgb(24, 131, 102)')
 await expect(card.locator('.holding-daily b').nth(2)).toHaveText('缺报价')
 expect(await card.locator('.holdings-table tbody tr').evaluateAll(nodes=>nodes.every(node=>{
  const rects=Array.from(node.children).map(child=>child.getBoundingClientRect())
  return Math.max(...rects.map(r=>r.top)) < Math.min(...rects.map(r=>r.bottom))
 }))).toBe(true)
 const scroller=card.locator('.holdings-scroll')
 await expect(card.getByText('10.123',{exact:true})).toBeVisible()
 const before=await card.locator('tbody th').first().boundingBox()
 await scroller.evaluate(el=>{el.scrollLeft=el.scrollWidth})
 const after=await card.locator('tbody th').first().boundingBox()
 expect(Math.abs(after!.x-before!.x)).toBeLessThan(2)
 const daily=await card.locator('.holding-daily').first().boundingBox()
 const bounds=await scroller.boundingBox()
 expect(daily!.x+daily!.width).toBeLessThanOrEqual(bounds!.x+bounds!.width+1)
 await noOverflow(page)
 fixture.strategy_live.mode='formal'
 fixture.strategy.daily_by_symbol=[{代码:'159501',基金名称:'纳指ETF嘉实',当日盈亏:99.}]
 await expect(card.locator('.badge')).toHaveText('正式收盘',{timeout:10000})
 await expect(card.locator('.holding-daily b')).toHaveText('+99.00')
 await noOverflow(page)
})

test('无鉴权入口被拒绝',async ({ baseURL })=>{
 const status=await new Promise<number|undefined>((resolve,reject)=>{
  https.get(`${baseURL}/api/dashboard`,{rejectUnauthorized:process.env.WEB_TEST_INSECURE_TLS !== '1'},response=>{response.resume();resolve(response.statusCode)}).on('error',reject)
 })
 expect(status).toBe(401)
})

 test('fixture 指数去重折叠与净值提示和盈亏柱状图', async ({page})=>{
 const fixture=fixtureData()
 // The baseline strategy fixture only loses money; add both signs for bar rendering coverage.
 fixture.strategy.daily.forEach((row:Record<string,unknown>,i:number)=>{row['每日盈亏']=i%2===0?1000:-1000})
 await page.route('**/api/dashboard',route=>route.fulfill({json:fixture}))
 await page.goto('/')
 await expect(page.getByRole('heading',{name:'ETF盘中实时预判'})).toBeVisible({timeout:30000})
 await expect(page.getByRole('heading',{name:'指数择时参考'})).toHaveCount(0)
 await page.getByRole('button',{name:'ETF',exact:true}).click()
 const index=page.locator('.index-reference').first()
 await expect(index).not.toHaveAttribute('open','')
 const name=await index.locator('strong').boundingBox(), code=await index.locator('.code').boundingBox()
 expect(name!.y+name!.height).toBeLessThanOrEqual(code!.y)
 await index.locator('summary').click()
 await expect(index.locator('dl')).toBeVisible()
 await page.getByRole('button',{name:'策略',exact:true}).click()
 await expect(page.getByText('模拟策略当前仓位',{exact:true})).toHaveCount(0)
 const chart=page.getByTestId('strategy-curve')
 await expect(chart.getByRole('button',{name:'当日收益率',exact:true})).toHaveCount(0)
 const plot=chart.getByRole('img')
 await expect(plot).toHaveAttribute('aria-label',`净值趋势图，共${fixture.strategy.daily.length}个正式交易日`)
 await plot.click({position:{x:100,y:100}})
 const tip=chart.locator('.strategy-chart-tooltip')
 await expect(tip).toBeVisible()
 for(const field of ['净值','每日收益率(%)','累计收益率(%)']) await expect(tip.locator(`[data-field="${field}"]`)).toBeVisible()
 const date=(await tip.locator(':scope > div').first().innerText()).trim()
 const row=fixture.strategy.daily.find((r:Record<string,unknown>)=>String(r['日期']).slice(0,10)===date)
 await expect(tip.locator('[data-field="净值"]')).toHaveText(row['净值'].toFixed(6))
 await expect(tip.locator('[data-field="每日收益率(%)"]')).toHaveText(`${row['每日收益率(%)']>0?'+':''}${row['每日收益率(%)'].toFixed(2)}%`)
 await expect(tip.locator('[data-field="累计收益率(%)"]')).toHaveText(`${row['累计收益率(%)']>0?'+':''}${row['累计收益率(%)'].toFixed(2)}%`)
 await chart.getByRole('button',{name:'每日盈亏',exact:true}).click()
 await expect(plot).toHaveAttribute('aria-label',`每日盈亏柱状图，共${fixture.strategy.daily.length}个正式交易日`)
 await plot.click({position:{x:100,y:100}})
 await expect(tip.locator('[data-field="每日盈亏"]')).toBeVisible()
 const selectedDate=(await tip.locator(':scope > div').first().innerText()).trim()
 const selectedRow=fixture.strategy.daily.find((r:Record<string,unknown>)=>String(r['日期']).slice(0,10)===selectedDate)
 await expect(tip.locator('[data-field="累计盈亏"]')).toHaveText(`${selectedRow['累计盈亏']>0?'+':''}${selectedRow['累计盈亏'].toFixed(2)}`)
 expect(await tip.locator('b').evaluateAll(nodes=>nodes.map(n=>n.dataset.field))).toEqual(['每日盈亏','每日收益率(%)','累计盈亏','累计收益率(%)'])
 await expect(chart.locator(':scope > .section-note')).toHaveCount(0)

 const colors=await plot.locator('canvas').first().evaluate((canvas:HTMLCanvasElement)=>{
 const pixels=canvas.getContext('2d')!.getImageData(0,0,canvas.width,canvas.height).data
 let red=false,green=false
 for(let i=0;i<pixels.length;i+=4){if(pixels[i]===202&&pixels[i+1]===58&&pixels[i+2]===68)red=true;if(pixels[i]===24&&pixels[i+1]===131&&pixels[i+2]===102)green=true}
 return {red,green}
 })
 expect(colors).toEqual({red:true,green:true})
 await noOverflow(page)
 })
