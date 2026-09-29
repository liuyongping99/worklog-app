// placement_match_toggle_check — 前端 updatePlacementMatch 双向同步「✓ 点数」徽章测试
//
// 2026-09-10 修复「✓ 点数」时有时无:
//   服务端模板(shipping-records.html:937 / inbound-records.html:622 / loading-orders.html:480)
//   按 placement_match=True 渲染「✓ 点数」徽章(.placement-badge),但模板只在整页加载时跑一次。
//   若用户不刷新页面就操作 placement,会让「✓ 点数」徽章(模板)与「点数」按钮绿框(前端 toggle)
//   出现不同步 —— 比如「按钮绿框消失但 ✓ 点数还在」。
//   前端 updatePlacementMatch() 现在 pm=True 时补建徽章(模板没渲染),pm=False 时主动移除
//   徽章(覆盖模板残留),保证两边始终同步。
//
// 验证 4 个 case:
//   Case 1: pm=False + 模板已渲染 .placement-badge → 徽章被移除(按钮 placement-ok 也移除)
//   Case 2: pm=True + 模板未渲染 + 有 placement-add-btn(外层条件满足) → 补建徽章
//   Case 3: pm=True + 模板未渲染 + 无 placement-add-btn(外层条件不满足) → 不创建
//   Case 4: pm=True + 模板已渲染 .placement-badge → 不重复创建(保留原徽章)
//
// 跑法: node tests/placement_match_toggle_check.js
const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');

const placementSrc = fs.readFileSync(
  path.join(__dirname, '..', 'static', 'js', 'placement_count.js'), 'utf8');

function extractFunc(src, name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\([^)]*\\)\\s*\\{');
  const m = re.exec(src);
  if (!m) return '';
  let i = m.index + m[0].length;
  let depth = 1;
  while (i < src.length && depth > 0) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.substring(m.index, i);
}

const errors = [];
function check(cond, msg) { if (!cond) errors.push(msg); }

const fnSrc = extractFunc(placementSrc, 'updatePlacementMatch');
if (!fnSrc) {
  console.error('❌ 抽不出 updatePlacementMatch');
  process.exit(1);
}

// 抽出来的函数体内只用了 DOM API(querySelector / createElement / remove / appendChild),
// 没有依赖 IIFE 内其他变量,可以直接 eval 跑。

// ── Case 1: pm=False → 移除 .placement-badge + 移除按钮 placement-ok ──────
{
  const dom = new JSDOM(`<!DOCTYPE html><html><body>
<table><tbody>
  <tr data-record-id="1">
    <td class="actions-cell">
      <button class="placement-add-btn lock-hide placement-ok" data-record-id="1">点数</button>
      <span class="status-badge placement-badge lock-show" title="该行已添加点数图">✓ 点数</span>
    </td>
  </tr>
</tbody></table>
</body></html>`);
  global.window = dom.window;
  global.document = dom.window.document;
  eval(fnSrc);
  updatePlacementMatch(1, false);

  const tr = dom.window.document.querySelector('tr[data-record-id="1"]');
  const badge = tr.querySelector('.placement-badge');
  const btn = tr.querySelector('.placement-add-btn');
  check(!badge, `Case 1: pm=False 时 .placement-badge 应被移除,实际仍存在`);
  check(btn && !btn.classList.contains('placement-ok'),
        `Case 1: pm=False 时按钮 placement-ok class 应被移除`);
}

// ── Case 2: pm=True + 模板未渲染 + 有 btn → 补建徽章 ─────────────────────
{
  const dom = new JSDOM(`<!DOCTYPE html><html><body>
<table><tbody>
  <tr data-record-id="2">
    <td class="actions-cell">
      <button class="placement-add-btn lock-hide" data-record-id="2">点数</button>
    </td>
  </tr>
</tbody></table>
</body></html>`);
  global.window = dom.window;
  global.document = dom.window.document;
  eval(fnSrc);
  updatePlacementMatch(2, true);

  const tr = dom.window.document.querySelector('tr[data-record-id="2"]');
  const badge = tr.querySelector('.placement-badge');
  const btn = tr.querySelector('.placement-add-btn');
  check(!!badge, `Case 2: pm=True + 有 btn → 应补建 .placement-badge`);
  check(badge && badge.textContent.trim() === '✓ 点数',
        `Case 2: 补建徽章文本应为 "✓ 点数",实际 "${badge && badge.textContent.trim()}"`);
  check(badge && badge.classList.contains('status-badge'),
        `Case 2: 补建徽章应带 class status-badge`);
  check(badge && badge.classList.contains('lock-show'),
        `Case 2: 补建徽章应带 class lock-show(模板同款)`);
  check(btn && btn.classList.contains('placement-ok'),
        `Case 2: pm=True 时按钮应加 placement-ok class`);
}

// ── Case 3: pm=True + 模板未渲染 + 无 btn → 不创建徽章(尊重外层条件) ──────
{
  const dom = new JSDOM(`<!DOCTYPE html><html><body>
<table><tbody>
  <tr data-record-id="3">
    <td class="actions-cell">
      <span class="placeholder">无 placement-add-btn</span>
    </td>
  </tr>
</tbody></table>
</body></html>`);
  global.window = dom.window;
  global.document = dom.window.document;
  eval(fnSrc);
  updatePlacementMatch(3, true);

  const tr = dom.window.document.querySelector('tr[data-record-id="3"]');
  const badge = tr.querySelector('.placement-badge');
  check(!badge,
        `Case 3: pm=True 但无 placement-add-btn(外层条件不满足) → 不应创建徽章`);
}

// ── Case 4: pm=True + 模板已渲染 .placement-badge → 不重复创建 ─────────────
{
  const dom = new JSDOM(`<!DOCTYPE html><html><body>
<table><tbody>
  <tr data-record-id="4">
    <td class="actions-cell">
      <button class="placement-add-btn lock-hide" data-record-id="4">点数</button>
      <span class="status-badge placement-badge lock-show" title="模板渲染的徽章">模板原始</span>
    </td>
  </tr>
</tbody></table>
</body></html>`);
  global.window = dom.window;
  global.document = dom.window.document;
  eval(fnSrc);
  updatePlacementMatch(4, true);

  const tr = dom.window.document.querySelector('tr[data-record-id="4"]');
  const badges = tr.querySelectorAll('.placement-badge');
  check(badges.length === 1,
        `Case 4: pm=True + 模板已渲染 → 应仍是 1 个徽章(不重复创建),实际 ${badges.length}`);
  check(badges[0] && badges[0].textContent.trim() === '模板原始',
        `Case 4: 保留模板原始徽章文本,不应被替换`);
}

// ── Case 5: 反复切换(模拟用户撤销 mark 后又点 mark) → 状态保持同步 ──────
{
  const dom = new JSDOM(`<!DOCTYPE html><html><body>
<table><tbody>
  <tr data-record-id="5">
    <td class="actions-cell">
      <button class="placement-add-btn lock-hide" data-record-id="5">点数</button>
    </td>
  </tr>
</tbody></table>
</body></html>`);
  global.window = dom.window;
  global.document = dom.window.document;
  eval(fnSrc);
  // 初始 pm=True → 补建徽章
  updatePlacementMatch(5, true);
  const tr = dom.window.document.querySelector('tr[data-record-id="5"]');
  check(!!tr.querySelector('.placement-badge'), 'Case 5a: pm=True 后徽章存在');

  // 撤销 mark → pm=False → 徽章消失
  updatePlacementMatch(5, false);
  check(!tr.querySelector('.placement-badge'), 'Case 5b: pm=False 后徽章消失');

  // 重新点 mark → pm=True → 徽章重现
  updatePlacementMatch(5, true);
  const reBadge = tr.querySelector('.placement-badge');
  check(!!reBadge, 'Case 5c: pm=True 后徽章重现');
  check(reBadge && reBadge.textContent.trim() === '✓ 点数',
        'Case 5c: 重建徽章文本应仍是 "✓ 点数"');
}

if (errors.length) {
  errors.forEach(e => console.error('❌ ' + e));
  process.exit(1);
}
console.log('✅ placement_match_toggle_check 通过');
