// Smoke test: 验证 shipping-records.html 模板中
// 产品名含"环保"的行被自动加 row-eco class + 🌿 图标。
// 跑法: node tests/test_eco_row.js

const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');

const tpl = fs.readFileSync(path.join(__dirname, '..', 'templates', 'shipping-records.html'), 'utf8');
const commonJs = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'common.js'), 'utf8');

// 编译 Jinja2 风格:用 Python jinja2 渲染太重,这里我们走更简单的办法 —
// 直接手写一份迷你 HTML,用真实模板的 CSS + 模拟 row class + eco-icon 的插入规则来验证渲染逻辑
// 因为 Jinja2 模板需要 flask 上下文(数据模型),不易直接渲染。
// 我们改为:验证模板源码里含两个关键片段 + CSS + 模拟 DOM 渲染验证样式生效。

let pass = true;

// 1. 模板源码必须含:
console.log('=== 模板源码检查 ===');
const hasEcoClassLogic = /{%\s*if\s+['"]环保['"]\s+in\s+item\.product_name\s*%}\s*row-eco/.test(tpl);
console.log('模板含 row-eco class 条件:', hasEcoClassLogic ? '✅' : '❌');
if (!hasEcoClassLogic) { console.error('❌ 模板缺 row-eco 条件'); pass = false; }

const hasEcoIconLogic = /{%\s*if\s+['"]环保['"]\s+in\s+item\.product_name\s*%}<span class="eco-icon"[^>]*>🌿<\/span>/.test(tpl);
console.log('模板含 🌿 图标插入:', hasEcoIconLogic ? '✅' : '❌');
if (!hasEcoIconLogic) { console.error('❌ 模板缺 🌿 图标'); pass = false; }

// 2. CSS 规则存在
const styleMatch = tpl.match(/<style>([\s\S]*?)<\/style>/);
const cssText = styleMatch ? styleMatch[1] : '';
const hasEcoBg = /\.row-eco[^{]*\{[^}]*background[^}]*\}/.test(cssText);
const hasEcoIcon = /\.eco-icon\s*\{/.test(cssText);
const hasEcoNot = /\.row-eco:not\(\.row-(warn|info|out-of-stock)\)/.test(cssText);
console.log('CSS 含 .row-eco 背景:', hasEcoBg ? '✅' : '❌');
console.log('CSS 含 .eco-icon:', hasEcoIcon ? '✅' : '❌');
console.log('CSS .row-eco 用 :not 排除警告:', hasEcoNot ? '✅' : '❌');
if (!hasEcoBg) { console.error('❌ CSS 缺 .row-eco 背景'); pass = false; }
if (!hasEcoIcon) { console.error('❌ CSS 缺 .eco-icon'); pass = false; }
if (!hasEcoNot) { console.error('❌ CSS 应排除警告/缺货行'); pass = false; }

// 3. CSS 优先级:警告行的 background 应该能盖过 row-eco
// 用 jsdom 模拟:row-warn + row-eco 时,只匹配 row-warn 规则
console.log('\n=== CSS 优先级模拟 ===');
const dom = new JSDOM(`<!DOCTYPE html><html><body>
<style>
  .record-table tr.row-warn td { background-color: yellow; }
  .record-table tr.row-eco:not(.row-warn):not(.row-out-of-stock):not(.row-info) td { background-color: #f0f9f3; }
</style>
<table class="record-table"><tbody>
  <tr class="row-eco" id="r1"><td>eco</td></tr>
  <tr class="row-warn row-eco" id="r2"><td>warn+eco</td></tr>
  <tr class="row-warn" id="r3"><td>warn only</td></tr>
</tbody></table>
</body></html>`);
const rows = dom.window.document.querySelectorAll('tr');
rows.forEach(tr => {
  const td = tr.querySelector('td');
  const bg = dom.window.getComputedStyle(td).backgroundColor;
  console.log(`  ${tr.id} (${tr.className}) → ${bg}`);
});
// jsdom 默认 getComputedStyle 不解析 CSS,但能查到 rule 命中情况
// 用 querySelector 替代:匹配哪个 selector
const ecoRule = dom.window.document.querySelector('#r1').matches('.row-eco:not(.row-warn)');
const warnEcoRule = dom.window.document.querySelector('#r2').matches('.row-eco:not(.row-warn)');
const warnRule = dom.window.document.querySelector('#r2').matches('.row-warn');
console.log('  r1 命中 .row-eco:not(.row-warn):', ecoRule ? '✅' : '❌');
console.log('  r2 不命中 .row-eco:not(.row-warn):', !warnEcoRule ? '✅' : '❌');
console.log('  r2 命中 .row-warn:', warnRule ? '✅' : '❌');
if (!ecoRule || warnEcoRule || !warnRule) { console.error('❌ CSS 优先级不对'); pass = false; }

if (pass) {
  console.log('\n✅ 所有检查通过');
  process.exit(0);
} else {
  console.log('\n❌ 至少一项失败');
  process.exit(1);
}