import { test, expect } from '@playwright/test'
import fs from 'node:fs'
import https from 'node:https'

async function noOverflow(page: import('@playwright/test').Page) {
 expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
 expect(await page.locator('.metrics b').evaluateAll(nodes => nodes.every(node => node.scrollWidth <= node.clientWidth + 1))).toBe(true)
}

test('部署首页和真实 API 可用', async ({ page, request }) => {
 const errors: string[] = []; page.on('pageerror', error => errors.push(error.message))
 const health = await request.get('/api/health'); expect(health.ok()).toBe(true)
 expect((await health.json()).status).toBe('ok')
 await page.goto('/')
 await expect(page.getByRole('heading',{name:'持仓分析',exact:true})).toBeVisible()
 await expect(page.getByRole('heading',{name:'50万元择时策略'})).toBeVisible({timeout:30000})
 await page.getByRole('button',{name:'ETF',exact:true}).click()
 await expect(page.getByPlaceholder('搜索代码或基金名称')).toBeVisible()
 await expect(page.getByText('159501',{exact:true})).toBeVisible()
 await noOverflow(page)
 expect(errors).toEqual([])
})

test('完整数据下四个视图、预览、图表与窄屏', async ({ page }) => {
 const fixture=JSON.parse(fs.readFileSync('/tmp/position-web-fixture.json','utf8'))
 await page.route('**/api/dashboard', route=>route.fulfill({json:fixture}))
 const errors: string[]=[];page.on('pageerror',e=>errors.push(e.message))
 await page.goto('/')
 await expect(page.getByRole('heading',{name:'50万元择时策略'})).toBeVisible()
 await noOverflow(page)
 await page.getByRole('button',{name:'ETF',exact:true}).click()
 await page.getByPlaceholder('搜索代码或基金名称').fill('159501')
 await expect(page.getByRole('heading',{name:'纳指ETF嘉实',exact:true})).toBeVisible()
 await expect(page.getByText('正式状态',{exact:false}).first()).toBeVisible()
 await noOverflow(page)
 await page.getByRole('button',{name:'策略',exact:true}).click()
 await expect(page.getByRole('heading',{name:'模拟策略当前仓位'})).toBeVisible()
 await expect(page.getByRole('img',{name:/净值趋势图/})).toBeVisible()
 await noOverflow(page)
 await page.screenshot({path:`/tmp/position-web-verification/strategy-${test.info().project.name}.png`,fullPage:false})
 await page.getByRole('button',{name:'衍生品',exact:true}).click()
 await expect(page.getByRole('heading',{name:'衍生品监控'})).toBeVisible()
 await noOverflow(page)
 expect(errors).toEqual([])
})

test('无鉴权入口被拒绝',async ({ baseURL })=>{
 const status=await new Promise<number|undefined>((resolve,reject)=>{
  https.get(`${baseURL}/api/dashboard`,{rejectUnauthorized:process.env.WEB_TEST_INSECURE_TLS !== '1'},response=>{response.resume();resolve(response.statusCode)}).on('error',reject)
 })
 expect(status).toBe(401)
})
