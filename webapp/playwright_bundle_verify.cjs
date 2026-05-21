const { chromium } = require('./node_modules/playwright');

(async () => {
  const baseUrl = process.env.TASKPLANNER_WEB_URL || 'http://127.0.0.1:4173/';
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1680, height: 1480 } });
  const logs = [];
  page.on('console', (msg) => logs.push({ type: msg.type(), text: msg.text() }));
  page.on('pageerror', (err) => logs.push({ type: 'pageerror', text: String(err) }));

  async function bodyText() {
    return (await page.locator('body').innerText()).replace(/\s+/g, ' ').trim();
  }

  function normalized(text) {
    return text.replace(/\s+/g, '').toUpperCase();
  }

  function ensure(condition, message) {
    if (!condition) {
      throw new Error(message);
    }
  }

  async function startCurrentBundle() {
    await page.getByRole('button', { name: 'Reset' }).click();
    await page.waitForTimeout(4000);
    await page.getByRole('button', { name: 'Apply Bundle' }).click();
    await page.waitForTimeout(5000);
    await page.getByRole('button', { name: 'Start' }).click();
    await page.waitForTimeout(25000);
  }

  async function switchBundle(bundleValue) {
    await page.getByRole('button', { name: 'Stop' }).click();
    await page.waitForTimeout(10000);
    await page.getByRole('button', { name: 'Reset' }).click();
    await page.waitForTimeout(7000);
    await page.locator('select').first().selectOption(bundleValue);
    await page.getByRole('button', { name: 'Apply Bundle' }).click();
    await page.waitForTimeout(5000);
    await page.getByRole('button', { name: 'Start' }).click();
    await page.waitForTimeout(25000);
  }

  async function assertScene(bundleValue) {
    const text = await bodyText();
    const compact = normalized(text);
    ensure(compact.includes(`SIMULATIONRUNNINGON${bundleValue.toUpperCase()}`), `bundle ${bundleValue} did not reach running state`);
    if (bundleValue === 'thyroidectomy') {
      ensure(compact.includes('NECKFIELD'), 'thyroidectomy scene did not render Neck Field');
    }
    if (bundleValue === 'nephrectomy') {
      ensure(compact.includes('KIDNEYHILUM'), 'nephrectomy scene did not render Kidney Hilum');
      ensure(!compact.includes('NECKFIELD'), 'nephrectomy scene still shows Neck Field');
    }
    return text;
  }

  await page.goto(baseUrl, { waitUntil: 'networkidle' });
  await page.getByText('ROS Bridge Online', { exact: false }).waitFor({ timeout: 20000 });
  await page.getByText('Digital Twin Control Room', { exact: false }).waitFor({ timeout: 15000 });

  await startCurrentBundle();
  const thyroidText = await assertScene('thyroidectomy');
  await page.screenshot({ path: '/tmp/taskplanner_thyroidectomy_scene.png', fullPage: true });

  await page.getByRole('button', { name: 'Request Tool' }).click();
  await page.waitForTimeout(5000);
  const requestText = await bodyText();
  ensure(/Handover Ready:\s*yes/i.test(requestText), 'Request Tool did not set handover readiness');

  await page.getByRole('button', { name: 'Voice Override' }).click();
  await page.waitForTimeout(5000);
  const voiceText = await bodyText();
  ensure(/Spoken:\s*(?!none)/i.test(voiceText), 'Voice Override did not surface spoken text');

  await page.getByRole('button', { name: 'Return Tool' }).click();
  await page.waitForTimeout(5000);
  const returnText = await bodyText();
  ensure(/Retrieval Ready:\s*yes/i.test(returnText), 'Return Tool did not set retrieval readiness');
  await page.screenshot({ path: '/tmp/taskplanner_thyroidectomy_controls.png', fullPage: true });

  await switchBundle('nephrectomy');
  const nephrectomyText = await assertScene('nephrectomy');
  await page.screenshot({ path: '/tmp/taskplanner_nephrectomy_scene.png', fullPage: true });

  const result = {
    baseUrl,
    logs,
    thyroidExcerpt: thyroidText.slice(0, 2400),
    nephrectomyExcerpt: nephrectomyText.slice(0, 2400),
    requestExcerpt: requestText.slice(0, 1800),
    voiceExcerpt: voiceText.slice(0, 1800),
    returnExcerpt: returnText.slice(0, 1800),
  };

  console.log(JSON.stringify(result, null, 2));
  await browser.close();
})();
