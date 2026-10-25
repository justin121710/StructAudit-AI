// 由 rc-member-designer 的 index.html 擷取輸入參數清單 → data/rcmd_params.json（開發時執行，產物提交進 repo）
//   node tools/build_rcmd_params.js /path/to/rc-member-designer/index.html
// 內容：PIN_META（參數 id、符號、名稱、單位、群組）＋ 下拉選單之可選值。
// Round 3 外審依此清單指出「要改 rc-member-designer 的哪一個欄位、改成多少」，系統再據以產生調整後的案件檔。
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(process.argv[2], 'utf8');

function literal(start) {
  let j = src.indexOf('{', start), d = 0, k = j, q = null;
  for (; k < src.length; k++) {
    const c = src[k];
    if (q) { if (c === '\\') { k++; continue; } if (c === q) q = null; continue; }
    if (c === "'" || c === '"' || c === '`') { q = c; continue; }
    if (c === '{') d++; else if (c === '}') { d--; if (!d) break; }
  }
  return Function('return ' + src.slice(j, k + 1))();
}

const strip = s => String(s || '').replace(/<[^>]+>/g, '').replace(/\\\(|\\\)|\\displaystyle\s*/g, '').trim();
const meta = literal(src.indexOf('const PIN_META = {'));
const options = {};
for (const m of src.matchAll(/<select id="([^"]+)"[^>]*>([\s\S]*?)<\/select>/g)) {
  options[m[1]] = [...m[2].matchAll(/<option value="([^"]*)"[^>]*>([^<]*)<\/option>/g)].map(o => [o[1], o[2].trim()]);
}
// 號數選單由程式動態填入（MAIN_SIZES／TIE_SIZES），靜態 HTML 中是空的
const sizes = name => Function('return ' + src.match(new RegExp('const ' + name + '\\s*=\\s*(\\[[^\\]]*\\])'))[1])();
const BAR = Function('return ' + src.match(/const BAR\s*=\s*(\{[\s\S]*?\});/)[1])();
for (const [ids, list] of [[['barSize', 'bBotSize', 'bTopSize'], sizes('MAIN_SIZES')], [['tieSize', 'bTieSize'], sizes('TIE_SIZES')]])
  for (const id of ids) options[id] = list.map(b => [b, `${b}（d_b = ${BAR[b].d} cm，A_b = ${BAR[b].A} cm²）`]);
const out = { source: 'https://github.com/justin121710/rc-member-designer', app: 'rc-section-designer', modules: {} };
for (const mod of Object.keys(meta)) {
  out.modules[mod] = [];
  for (const [group, items] of meta[mod]) for (const p of items) {
    const row = { id: p[0], symbol: strip(p[1]), label: strip(p[2]), unit: strip(p[3]), group };
    if (options[p[0]] && options[p[0]].length) row.options = options[p[0]];
    out.modules[mod].push(row);
  }
}
const dst = path.join(__dirname, '..', 'data', 'rcmd_params.json');
fs.writeFileSync(dst, JSON.stringify(out, null, 1));
console.log(Object.entries(out.modules).map(([k, v]) => `${k} ${v.length} 項`).join('、'), '→', dst);
