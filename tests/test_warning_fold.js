// Smoke test: 验证"已核查"折叠 + 数据行右侧 toggle 切换流程。
// 跑法: node tests/test_warning_fold.js

const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');

const modalTpl = fs.readFileSync(path.join(__dirname, '..', 'templates', '_smart_add_modal.html'), 'utf8');
const commonJs = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'common.js'), 'utf8');

// 函数抽取
function extractFunc(src, name) {
  let re = new RegExp('function\\s+' + name + '\\s*\\([^)]*\\)\\s*\\{');
  let m = re.exec(src);
  if (m) {
    let i = m.index + m[0].length;
    let depth = 1;
    while (i < src.length && depth > 0) {
      if (src[i] === '{') depth++;
      else if (src[i] === '}') depth--;
      i++;
    }
    return src.substring(m.index, i);
  }
  re = new RegExp('(?:window\\.|var\\s+)?' + name + '\\s*=\\s*function\\s*\\([^)]*\\)\\s*\\{');
  m = re.exec(src);
  if (m) {
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

// 构造测试 DOM:一个订单组,一条商品行(有警告),一个操作列含 5 个按钮(模拟真实结构)
const dom = new JSDOM(`<!DOCTYPE html><html><body>
<div class="date-group" data-order-id="100" data-locked="0">
  <table class="record-table">
    <tbody>
      <tr data-record-id="42" data-verified="0">
        <td>1</td>
        <td>纯胶</td>
        <td>0.6黑软纯胶</td>
        <td>100</td>
        <td>y</td>
        <td></td>
        <td></td>
        <td style="white-space:nowrap;">
          <button class="move-up-btn">▲</button>
          <button class="move-down-btn">▼</button>
          <button class="edit-record-btn">✏️</button>
          <button class="delete-record-btn">🗑️</button>
          <button class="record-image-btn">🖼️</button>
        </td>
      </tr>
    </tbody>
  </table>
</div>
</body></html>`, { runScripts: 'dangerously' });

const { window } = dom;
const { document } = window;

function runInDom(code) {
  const s = document.createElement('script');
  s.textContent = code;
  document.body.appendChild(s);
}

// 注入 escHtml/escAttr(common.js 前 14 行)
const escOnly = commonJs.split('\n').slice(0, 14).join('\n') + '\nwindow.escHtml = escHtml;\nwindow.escAttr = escAttr;';
runInDom(escOnly);

// stub: getItemWarnings 返回 2 条警告,模拟真实场景
runInDom(`window.getItemWarnings = function(item) {
  return [
    '商品名称含「纯胶」,请核查是否应为「环保纯胶」',
    '规格含「软」,是否应为「中软纯胶」?'
  ];
};`);

// stub: isOrderLocked 读 data-locked 属性
runInDom(`window.isOrderLocked = function(tr) {
  if (!tr) return false;
  var dg = tr.closest('.date-group');
  return dg && dg.getAttribute('data-locked') === '1';
};`);

// stub: updateNavIndicator(原模板里有,这里 no-op)
runInDom(`window.updateNavIndicator = function() {};`);

// 注入待测函数
const targets = ['addRowWarning', 'addVerifiedToggle', 'removeVerifiedToggle', 'removeRowWarning'];
for (const fn of targets) {
  const code = extractFunc(modalTpl, fn);
  if (!code) { console.error('未找到函数:', fn); process.exit(1); }
  runInDom(code + '\nwindow.' + fn + ' = ' + fn + ';');
}

let pass = true;
const dataRow = document.querySelector('tr[data-record-id="42"]');

// === 阶段 1:未核查 + 有警告 → 黄色警告行 + 无 toggle ===
dom.window.addRowWarning(dataRow, { product_name: '纯胶', specification: '0.6黑软纯胶', verified: 0, record_id: 42 });
let warnRow = dataRow.nextElementSibling;
console.log('=== 阶段 1:未核查 ===');
console.log('警告行存在:', !!warnRow && warnRow.classList.contains('record-warn-row'));
console.log('toggle 按钮存在:', !!dataRow.querySelector('.record-verified-toggle'));
if (!warnRow || !warnRow.classList.contains('record-warn-row')) { console.error('❌ 阶段 1:警告行未渲染'); pass = false; }
if (dataRow.querySelector('.record-verified-toggle')) { console.error('❌ 阶段 1:不应有 toggle'); pass = false; }
const verifyBtn = warnRow && warnRow.querySelector('.record-warn-verify-btn');
if (!verifyBtn) { console.error('❌ 阶段 1:警告行缺 ✓ 已核查 按钮'); pass = false; }

// === 阶段 2:点击 ✓ 已核查(模拟)→ verified=1,折叠警告行 + 加 toggle ===
dom.window.addRowWarning(dataRow, { product_name: '纯胶', specification: '0.6黑软纯胶', verified: 1, record_id: 42 });
warnRow = dataRow.nextElementSibling;  // 警告行应被清掉,nextElementSibling 可能是 tbody 或 null
console.log('\n=== 阶段 2:已核查 ===');
const stillWarn = warnRow && warnRow.classList && warnRow.classList.contains('record-warn-row');
console.log('警告行存在(应否):', !!stillWarn);
console.log('toggle 按钮存在:', !!dataRow.querySelector('.record-verified-toggle'));
if (stillWarn) { console.error('❌ 阶段 2:警告行应被折叠'); pass = false; }
const toggle = dataRow.querySelector('.record-verified-toggle');
if (!toggle) { console.error('❌ 阶段 2:数据行右侧应有 toggle'); pass = false; }
else {
  console.log('toggle 文案:', toggle.textContent);
  console.log('toggle record-id:', toggle.getAttribute('data-record-id'));
  if (toggle.getAttribute('data-record-id') !== '42') { console.error('❌ toggle 缺 record-id'); pass = false; }
}

// === 阶段 3:点击 toggle → verified=0,警告行恢复 + toggle 消失 ===
dom.window.addRowWarning(dataRow, { product_name: '纯胶', specification: '0.6黑软纯胶', verified: 0, record_id: 42 });
warnRow = dataRow.nextElementSibling;
console.log('\n=== 阶段 3:再次取消核查 ===');
console.log('警告行恢复:', !!warnRow && warnRow.classList.contains('record-warn-row'));
console.log('toggle 已消失:', !dataRow.querySelector('.record-verified-toggle'));
if (!warnRow || !warnRow.classList.contains('record-warn-row')) { console.error('❌ 阶段 3:警告行应恢复'); pass = false; }
if (dataRow.querySelector('.record-verified-toggle')) { console.error('❌ 阶段 3:toggle 应消失'); pass = false; }

// === 阶段 4:防重复 toggle(连续 verified=1 两次不应叠加) ===
dom.window.addRowWarning(dataRow, { product_name: '纯胶', specification: '0.6黑软纯胶', verified: 1, record_id: 42 });
dom.window.addRowWarning(dataRow, { product_name: '纯胶', specification: '0.6黑软纯胶', verified: 1, record_id: 42 });
const toggleCount = dataRow.querySelectorAll('.record-verified-toggle').length;
console.log('\n=== 阶段 4:防重复 ===');
console.log('toggle 数:', toggleCount);
if (toggleCount !== 1) { console.error('❌ 阶段 4:toggle 重复出现'); pass = false; }

// === 阶段 5:订单锁定 → toggle 应消失 ===
document.querySelector('.date-group').setAttribute('data-locked', '1');
dom.window.addRowWarning(dataRow, { product_name: '纯胶', specification: '0.6黑软纯胶', verified: 1, record_id: 42 });
console.log('\n=== 阶段 5:锁定订单 ===');
console.log('toggle 在锁定时应消失:', !dataRow.querySelector('.record-verified-toggle'));
if (dataRow.querySelector('.record-verified-toggle')) { console.error('❌ 阶段 5:锁定时 toggle 应消失'); pass = false; }

// === 阶段 6:CSS 钩子存在 ===
const cssBlockMatch = modalTpl.match(/<style>([\s\S]*?)<\/style>/g) || [];
const baseTpl = fs.readFileSync(path.join(__dirname, '..', 'templates', 'base.html'), 'utf8');
const baseCss = baseTpl.match(/<style>([\s\S]*?)<\/style>/g) || [];
const allCss = cssBlockMatch.concat(baseCss).join('\n');
console.log('\n=== 阶段 6:CSS 钩子 ===');
const hasToggleCss = /\.record-verified-toggle\s*\{/.test(allCss);
console.log('base.html 含 .record-verified-toggle CSS:', hasToggleCss);
if (!hasToggleCss) { console.error('❌ 阶段 6:缺 .record-verified-toggle CSS'); pass = false; }

if (pass) {
  console.log('\n✅ 所有检查通过');
  process.exit(0);
} else {
  console.log('\n❌ 至少一项失败');
  process.exit(1);
}