const { chromium } = require('./node_modules/playwright');
(async() => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1400 } });
  const logs = [];
  page.on('console', msg => logs.push({ type: msg.type(), text: msg.text() }));
  page.on('pageerror', err => logs.push({ type: 'pageerror', text: String(err) }));
  await page.goto('http://127.0.0.1:4173/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2500);
  await page.getByRole('button', { name: 'Stop' }).click();
  await page.waitForTimeout(2500);
  await page.getByRole('button', { name: 'Reset' }).click();
  await page.waitForTimeout(2500);
  await page.screenshot({ path: '/tmp/taskplanner_idle_after_reset.png', fullPage: true });
  const body = await page.locator('body').innerText();
  console.log(JSON.stringify({ logs, excerpt: body.slice(0, 1300) }, null, 2));
  await browser.close();
})();
