// 以 rc-member-designer 產生示範用計算書 PDF 與案件檔（需 Playwright＋Chromium）：
//   cd /path/to/rc-member-designer && python3 -m http.server 8765 &
//   node examples/make_sample_pdf.js
// 示範柱 C1：60×60、主筋 #8 每面 3 根、箍筋 #3，計算書中有兩項 NG（圍束筋間距無解、強柱弱梁），供審查流程示範。
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const URL = process.env.RCMD_URL || 'http://localhost:8765/index.html';
const OUT = path.join(__dirname, 'sample_C1_calc.pdf');
const TWEAKS = { nB: '3', nH: '3', tieSize: '#3' };

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } });
  await page.goto(URL);
  await page.waitForTimeout(2000);
  await page.click('#setupSkip');
  await page.evaluate(([tw]) => {
    const set = (id, v) => { const el = document.getElementById(id); el.value = v;
      el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); };
    set('memName', 'C1');
    for (const [id, v] of Object.entries(tw)) set(id, v);
  }, [TWEAKS]);
  await page.waitForTimeout(1500);
  await page.evaluate(() => confAll('pylon'));   // 「需確認」項目全部勾選，計算書才能列印
  await page.waitForTimeout(800);
  await page.click('#btnPreview');
  await page.waitForTimeout(3000);
  await page.emulateMedia({ media: 'print' });
  await page.pdf({ path: OUT, format: 'A4', printBackground: true });
  fs.writeFileSync(OUT.replace('_calc.pdf', '_case.json'), await page.evaluate(() => JSON.stringify(caseSnapshot())));
  await browser.close();
  console.log('已產生', OUT);
})();
