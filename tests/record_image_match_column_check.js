// match-col 虚拟列重构 — JS 行为测试 (jsdom)
//
// 验证 _ensureMatchColumn / setRowMatchBadge 在三种状态下的行为:
//   Case 1: 空 table + 1 行 → 调一次 → 表头出现 th、所有 tr 有 td、目标行 td 含徽章
//   Case 2: 已有 match-col → 调一次 → 只更新目标行 td,不重复插列头 / 不重复插空 td
//   Case 3: 5 行 table + 目标为第 3 行 → 调一次 → 表头 1 个 th、所有 5 行有 td
//
// 跑法: node tests/record_image_match_column_check.js
const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');

const tpl = fs.readFileSync(
  path.join(__dirname, '..', 'templates', '_record_image_script.html'), 'utf8');

function extractFunc(src, name) {
  const patterns = [
    new RegExp('function\\s+' + name + '\\s*\\([^)]*\\)\\s*\\{'),
    new RegExp('(?:window\\.|var\\s+)?' + name.replace('.', '\\.') +
               '\\s*=\\s*function\\s*\\([^)]*\\)\\s*\\{'),
  ];
  for (const re of patterns) {
    const m = re.exec(src);
    if (!m) continue;
    let i = m.index + m[0].length;
    let depth = 1;
    while (i < src.length && depth > 0) {
      if (src[i] === '{') depth++;
      else if (src[i] === '}') depth--;
      i++;
    }
    return src.substring(m.index, i);
  }
  return '';
}

const errors = [];
function check(cond, msg) { if (!cond) errors.push(msg); }

// ── 抽三个函数（_ensureMatchColumn / setRowMatchBadge / badgeHtml）──
// setRowMatchBadge 内部依赖 badgeHtml 构造徽章 HTML,所以三个都得抽出来在 eval 内串联。
const ensureFnSrc = extractFunc(tpl, '_ensureMatchColumn');
const setBadgeFnSrc = extractFunc(tpl, 'setRowMatchBadge');
const badgeFnSrc = extractFunc(tpl, 'badgeHtml');
if (!ensureFnSrc) {
  console.error('❌ 抽不出 _ensureMatchColumn');
  process.exit(1);
}
if (!setBadgeFnSrc) {
  console.error('❌ 抽不出 setRowMatchBadge');
  process.exit(1);
}
if (!badgeFnSrc) {
  console.error('❌ 抽不出 badgeHtml');
  process.exit(1);
}

// ── Case 1: 空 table + 1 行 → 表头 th、目标行 td 含徽章 ──────
{
  const dom = new JSDOM(`<!DOCTYPE html><html><body>
<div class="date-group" data-order-id="100">
  <table class="record-table">
    <thead>
      <tr><th>序号</th><th>重点</th><th>商品名称</th>
          <th class="spec-cell">规格</th><th>数量</th></tr>
    </thead>
    <tbody>
      <tr data-record-id="7">
        <td>1</td><td></td>
        <td class="product-name-cell">环保杂胶</td>
        <td class="spec-cell">0.8黑中加面</td>
        <td>50</td>
      </tr>
    </tbody>
  </table>
</div>
</body></html>`);
  global.window = dom.window;
  global.document = dom.window.document;
  eval(badgeFnSrc + '\n' + ensureFnSrc + '\n' + setBadgeFnSrc);
  // setRowMatchBadge 内部需要 _ensureMatchColumn + badgeHtml
  setRowMatchBadge(7, 'green', '命中 7P 等价', 'local_fuzzy');

  const table = dom.window.document.querySelector('table');
  const ths = table.querySelectorAll('thead th');
  const trs = table.querySelectorAll('tbody tr');
  check(ths.length === 6, `Case 1: 表头应有 6 个 th(原 5 + match-col),实际 ${ths.length}`);
  check(ths[4] && ths[4].classList.contains('match-col'),
        'Case 1: 第 5 个 th 应是 match-col');
  check(ths[4] && ths[4].textContent === 'AI 比对',
        'Case 1: match-col th 文本应为 "AI 比对"');
  check(trs[0].querySelector('td.match-col'),
        'Case 1: 目标行应有 td.match-col');
  check(trs[0].querySelector('td.match-col').innerHTML.includes('green'),
        'Case 1: 目标行 td.match-col 应含 green 徽章');
}

// ── Case 2: 已有 match-col → 不重复插入 ──────────────────────
{
  const dom = new JSDOM(`<!DOCTYPE html><html><body>
<table class="record-table">
  <thead><tr><th>序号</th><th>重点</th><th>品名</th>
         <th class="spec-cell">规格</th>
         <th class="match-col">AI 比对</th><th>数量</th></tr></thead>
  <tbody>
    <tr data-record-id="7">
      <td>1</td><td></td>
      <td class="product-name-cell">环保杂胶</td>
      <td class="spec-cell">0.8黑中加面</td>
      <td class="match-col">旧绿</td><td>50</td>
    </tr>
  </tbody>
</table>
</body></html>`);
  global.window = dom.window;
  global.document = dom.window.document;
  eval(badgeFnSrc + '\n' + ensureFnSrc + '\n' + setBadgeFnSrc);
  setRowMatchBadge(7, 'red', '规格不符', 'deepseek');

  const table = dom.window.document.querySelector('table');
  const ths = table.querySelectorAll('thead th.match-col');
  const trs = table.querySelectorAll('tbody tr');
  check(ths.length === 1, `Case 2: 表头 match-col 应仍是 1 个,实际 ${ths.length}`);
  check(trs[0].querySelectorAll('td.match-col').length === 1,
        `Case 2: 目标行 td.match-col 应仍是 1 个,实际 ${trs[0].querySelectorAll('td.match-col').length}`);
  check(trs[0].querySelector('td.match-col').innerHTML.includes('red'),
        'Case 2: 目标行 td.match-col 应已更新为 red 徽章');
  check(!trs[0].querySelector('td.match-col').innerHTML.includes('旧绿'),
        'Case 2: 旧的「旧绿」文本应被替换');
}

// ── Case 3: 5 行 table, 目标为第 3 行 → 全部行都有 td ───────
{
  const dom = new JSDOM(`<!DOCTYPE html><html><body>
<table class="record-table">
  <thead><tr><th>序号</th><th>重点</th><th>品名</th>
         <th class="spec-cell">规格</th><th>数量</th><th>操作</th></tr></thead>
  <tbody>
    <tr data-record-id="1"><td>1</td><td></td><td class="product-name-cell">a</td><td class="spec-cell">x</td><td>1</td><td>—</td></tr>
    <tr data-record-id="2"><td>2</td><td></td><td class="product-name-cell">b</td><td class="spec-cell">y</td><td>2</td><td>—</td></tr>
    <tr data-record-id="3"><td>3</td><td></td><td class="product-name-cell">c</td><td class="spec-cell">z</td><td>3</td><td>—</td></tr>
    <tr data-record-id="4"><td>4</td><td></td><td class="product-name-cell">d</td><td class="spec-cell">w</td><td>4</td><td>—</td></tr>
    <tr data-record-id="5"><td>5</td><td></td><td class="product-name-cell">e</td><td class="spec-cell">v</td><td>5</td><td>—</td></tr>
  </tbody>
</table>
</body></html>`);
  global.window = dom.window;
  global.document = dom.window.document;
  eval(badgeFnSrc + '\n' + ensureFnSrc + '\n' + setBadgeFnSrc);
  // 选第 3 行 (record-id=3)
  setRowMatchBadge(3, 'yellow', '存疑', 'deepseek');

  const table = dom.window.document.querySelector('table');
  const ths = table.querySelectorAll('thead th.match-col');
  const trs = table.querySelectorAll('tbody tr');
  check(ths.length === 1, `Case 3: 表头 match-col 应只有 1 个,实际 ${ths.length}`);
  check(trs.length === 5, `Case 3: tbody 应仍是 5 行,实际 ${trs.length}`);
  trs.forEach((tr, i) => {
    const tds = tr.querySelectorAll('td.match-col');
    if (tds.length !== 1) {
      errors.push(`Case 3: 第 ${i+1} 行 td.match-col 应有 1 个,实际 ${tds.length}`);
    }
  });
  // 目标行(第 3 行)含 yellow,其他行空 td
  check(trs[2].querySelector('td.match-col').innerHTML.includes('yellow'),
        'Case 3: 目标行 td.match-col 应含 yellow 徽章');
  check(trs[0].querySelector('td.match-col').innerHTML.trim() === '',
        'Case 3: 兄弟行 td.match-col 应为空');
}

// ── Case 4: 页面刷新场景 — 服务端不渲染 match-col,DOM 里只有 img-item-record + match-badge ──
//         调用 initAllMatchColumns() 应补建 th + td + 徽章
{
  const dom = new JSDOM(`<!DOCTYPE html><html><body>
<table class="record-table">
  <thead><tr><th>序号</th><th>重点</th><th>品名</th>
         <th class="spec-cell">规格</th><th>数量</th></tr></thead>
  <tbody>
    <tr data-record-id="7">
      <td>1</td><td></td>
      <td class="product-name-cell">环保杂胶</td>
      <td class="spec-cell">0.8黑中加面</td><td>50</td>
    </tr>
  </tbody>
</table>
<div class="img-item-record" data-record-pk="7" data-image-id="100">
  <div class="img-meta-row-1">
    <span class="match-badge match-badge-deepseek" data-match-status="green" data-match-source="deepseek" title="图文相符">⊛</span>
  </div>
</div>
</body></html>`);
  global.window = dom.window;
  global.document = dom.window.document;
  // 抽 initAllMatchColumns + initRowMatchColumn + refreshRowMatchBadge
  const initAllFnSrc = extractFunc(tpl, 'initAllMatchColumns');
  const initRowFnSrc = extractFunc(tpl, 'initRowMatchColumn');
  const refreshFnSrc = extractFunc(tpl, 'refreshRowMatchBadge');
  if (!initAllFnSrc) {
    console.error('❌ 抽不出 initAllMatchColumns —— 未实现');
    process.exit(1);
  }
  if (!initRowFnSrc) {
    console.error('❌ 抽不出 initRowMatchColumn —— 未实现');
    process.exit(1);
  }
  eval(badgeFnSrc + '\n' + ensureFnSrc + '\n' + setBadgeFnSrc + '\n' + refreshFnSrc + '\n' + initRowFnSrc + '\n' + initAllFnSrc);
  initAllMatchColumns();

  const table = dom.window.document.querySelector('table');
  const ths = table.querySelectorAll('thead th.match-col');
  const trs = table.querySelectorAll('tbody tr');
  check(ths.length === 1, `Case 4: 表头 match-col 应自动补建 1 个,实际 ${ths.length}`);
  check(ths[0] && ths[0].textContent === 'AI 比对', 'Case 4: match-col th 文本应为 "AI 比对"');
  check(trs[0].querySelector('td.match-col'), 'Case 4: 目标行 td.match-col 应自动补建');
  check(trs[0].querySelector('td.match-col').innerHTML.includes('green'),
        'Case 4: 目标行 td.match-col 应已填入 green 徽章(从 img-item-record 读出)');
}

// ── Case 5: 行级徽章符号须与图下徽章来源一致(2026-08-15 一致性修复) ──
//         deepseek → ⊛/◇/◆, local_fuzzy → ✓/⚠/✗;颜色也要一致
{
  const dom = new JSDOM(`<!DOCTYPE html><html><body>
<table class="record-table">
  <thead><tr><th>序号</th><th>重点</th><th>品名</th>
         <th class="spec-cell">规格</th><th>数量</th></tr></thead>
  <tbody>
    <tr data-record-id="11">
      <td>1</td><td></td>
      <td class="product-name-cell">a</td><td class="spec-cell">x</td><td>1</td>
    </tr>
    <tr data-record-id="12">
      <td>2</td><td></td>
      <td class="product-name-cell">b</td><td class="spec-cell">y</td><td>2</td>
    </tr>
  </tbody>
</table>
</body></html>`);
  global.window = dom.window;
  global.document = dom.window.document;
  eval(badgeFnSrc + '\n' + ensureFnSrc + '\n' + setBadgeFnSrc);
  setRowMatchBadge(11, 'green', '', 'deepseek');
  setRowMatchBadge(12, 'green', '', 'local_fuzzy');

  const cellDeep = dom.window.document.querySelector('tr[data-record-id="11"] td.match-col');
  const cellLocal = dom.window.document.querySelector('tr[data-record-id="12"] td.match-col');
  const spanDeep = cellDeep.querySelector('span.match-badge');
  const spanLocal = cellLocal.querySelector('span.match-badge');
  const symDeep = cellDeep.textContent.trim();
  const symLocal = cellLocal.textContent.trim();
  check(symDeep === '⊛', `Case 5: deepseek 行级徽章应为 ⊛,实际 "${symDeep}"`);
  check(symLocal === '✓', `Case 5: local_fuzzy 行级徽章应为 ✓,实际 "${symLocal}"`);
  check(spanDeep && spanDeep.getAttribute('data-match-source') === 'deepseek',
        'Case 5: deepseek 行级徽章应带 data-match-source="deepseek"');
  check(spanLocal && spanLocal.getAttribute('data-match-source') === 'local_fuzzy',
        'Case 5: local_fuzzy 行级徽章应带 data-match-source="local_fuzzy"');
  check(spanDeep && spanDeep.style.color.replace(/\s/g, '').toLowerCase() === 'rgb(13,148,136)',
        `Case 5: deepseek 行级徽章颜色应为 #0d9488(rgb 13,148,136),实际 "${spanDeep && spanDeep.style.color}"`);
  check(spanLocal && spanLocal.style.color.replace(/\s/g, '').toLowerCase() === 'rgb(22,163,74)',
        `Case 5: local_fuzzy 行级徽章颜色应为 #16a34a(rgb 22,163,74),实际 "${spanLocal && spanLocal.style.color}"`);
}

if (errors.length) {
  errors.forEach(e => console.error('❌ ' + e));
  process.exit(1);
}
console.log('✅ record_image_match_column_check 通过');
