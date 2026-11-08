// 以 rc-member-designer「從 Midas 匯入」產生示範計算書 PDF 與案件檔（工具整合 M4，需 Playwright＋Chromium）：
//   RCMD=/path/to/rc-member-designer node examples/make_sample_midas.js
// 橋墩 P1：rc-member-designer tests/fixtures 的 midas_pier.mct＋midas_beam_force.txt（結構群組 P1_COL），設計依據「橋梁」，
// 計算書第 5 章後有「內力來源」段落（群組、元素、各列來源元素端點、控制條件），供審查流程核對內力來源。
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const http = require('http');

const RCMD = path.resolve(process.env.RCMD || path.join(__dirname, '..', '..', 'rc-member-designer'));
const FIX = path.join(RCMD, 'tests', 'fixtures');
const OUT = path.join(__dirname, 'sample_P1_midas_calc.pdf');
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json',
  '.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf', '.png': 'image/png', '.svg': 'image/svg+xml' };

(async () => {
  const srv = http.createServer((req, res) => {
    const f = path.join(RCMD, path.normalize(decodeURIComponent(new URL(req.url, 'http://x').pathname)).replace(/^([/\\])+/, ''));
    if (!f.startsWith(RCMD) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) { res.writeHead(404); return res.end(); }
    res.writeHead(200, { 'content-type': MIME[path.extname(f)] || 'application/octet-stream' });
    fs.createReadStream(f).pipe(res);
  });
  await new Promise(ok => srv.listen(0, '127.0.0.1', ok));
  const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
  const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } });
  await page.goto(`http://127.0.0.1:${srv.address().port}/index.html`);
  await page.waitForTimeout(2000);
  await page.evaluate(() => { for (const id of ['taskSkip', 'setupSkip']) { const b = document.getElementById(id); if (b && b.closest('.open')) b.click(); } });
  await page.evaluate(() => { const el = document.getElementById('codeBasis'); el.value = 'bridge'; el.dispatchEvent(new Event('change', { bubbles: true })); });
  await page.waitForTimeout(500);
  await page.evaluate(() => window.midasImport.open('pylon'));
  await page.setInputFiles('#midMctFile', path.join(FIX, 'midas_pier.mct'));
  await page.waitForFunction(() => /結構群組/.test(document.getElementById('midMctInfo').textContent));
  await page.fill('#midTblBox', fs.readFileSync(path.join(FIX, 'midas_beam_force.txt'), 'utf8'));
  await page.click('#midTblRead');
  await page.check('#midGrpWrap input[data-g="0"]');
  await page.click('#midApply');
  await page.waitForTimeout(300);
  if (await page.isVisible('#askOv.open')) await page.click('#askAct button.btn-primary');
  await page.waitForTimeout(1500);
  await page.evaluate(() => confAll('pylon'));   // 「需確認」項目全部勾選，計算書才能列印
  await page.waitForTimeout(800);
  await page.click('#btnPreview');
  await page.waitForTimeout(3000);
  await page.emulateMedia({ media: 'print' });
  await page.pdf({ path: OUT, format: 'A4', printBackground: true });
  fs.writeFileSync(OUT.replace('_calc.pdf', '_case.json'), await page.evaluate(() => JSON.stringify(caseSnapshot())));
  await browser.close(); srv.close();
  console.log('已產生', OUT);
})();
