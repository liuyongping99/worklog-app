// Smoke test: 验证 renderAiResults 对 `纯胶 / 0.6黑软纯胶` 真的渲染出 2 个独立警告块。
// 用 jsdom 模拟浏览器,直接验 DOM 结构。
//
// 跑法: node tests/render_check.js

const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');

const tpl = fs.readFileSync(path.join(__dirname, '..', 'templates', '_smart_add_modal.html'), 'utf8');
const baseTpl = fs.readFileSync(path.join(__dirname, '..', 'templates', 'base.html'), 'utf8');
const imgModalTpl = fs.readFileSync(path.join(__dirname, '..', 'templates', '_image_upload_modal.html'), 'utf8');

// 从模板抽取函数:支持两种写法
//   - 函数声明: function foo() {...}
//   - 表达式赋值: window.foo = function() {...} 或 var foo = function() {...}
// 用大括号配对计数避免正则贪婪越界
function extractFunc(src, name) {
  // 先尝试函数声明
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
  // 再尝试表达式赋值(window.X = function 或 var X = function)
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

// jsdom 初始化(包含共享图片上传弹框的占位 DOM)
const dom = new JSDOM(`<!DOCTYPE html><html><body>
<nav class="navbar">
  <a href="/" class="navbar-brand">📋 丰源工作台</a>
  <span id="validationStatusIndicator" class="nav-warn-indicator" style="display:none;">
    ⚠️ <span class="nav-warn-count" id="navWarnCount">0</span> 条需核查
  </span>
</nav>
<table class="ai-result-table">
  <thead><tr><th>商品</th><th>规格</th><th>数量</th><th>单位</th><th>备注</th><th></th></tr></thead>
  <tbody id="aiResultBody"></tbody>
</table>
<!-- 共享图片上传弹框的最小 DOM stub -->
<div id="imageModal" class="img-upload-modal"><div class="modal-content">
  <div id="pasteBox"></div>
  <div id="imagePreviewBox"></div>
  <input type="file" id="imageFileInput">
  <button id="confirmUploadBtn" onclick="confirmUpload()"></button>
</div></div>
</body></html>`);
const { window } = dom;
const { document } = window;

// 把抽取的函数 eval 进 jsdom 的全局。jsdom 的 window.eval 把 var/function 提到 window 全局,
// 但 `window.xxx = ...` 这种限定反而不工作,这里直接声明全局函数即可。
window.eval(`
  this.escHtml = function(s){
    if(s==null) return "";
    return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
  };
`);

const wFn = extractFunc(tpl, 'getItemWarnings');
const rFn = extractFunc(tpl, 'renderAiResults');
if (!wFn || !rFn) {
  console.error('FAIL: 函数抽取失败 wLen=', wFn.length, 'rLen=', rFn.length);
  process.exit(1);
}

// jsdom 的 window.eval 在间接 eval scope 里没有 `window`,改用 IIFE 直接拿到 window 引用
// 并显式把函数挂到 window 上,Node 这边再读 window.xxx 即可。
// 注意函数体内部对 `getItemWarnings`/`collectRowWarnings` 的引用,在被 `window.X = function()` 重写后会找不到裸名,
// 所以把函数体里所有 `getItemWarnings(`/`collectRowWarnings(` 改成走 `window.` 引用。
function rewriteForWindow(fnSrc) {
  return fnSrc.replace(/^function\s+(\w+)\s*\(([^)]*)\)\s*\{/, 'window.$1 = function($2){')
              .replace(/\b(getItemWarnings|collectRowWarnings|addRowWarning|removeRowWarning|addVerifiedToggle|removeVerifiedToggle|validateAllRows|updateNavIndicator|isOrderLocked|bindRowWarningClickHandler|openImageModal|openImageModalForRecord|closeImageModal|confirmUpload|_setTarget)\(/g, 'window.$1(');
}
const wFnAssign = rewriteForWindow(wFn);
const cFnAssign = rewriteForWindow(extractFunc(tpl, 'collectRowWarnings'));
const rFnAssign = rewriteForWindow(rFn);
const sFnAssign = rewriteForWindow(extractFunc(tpl, 'submitAiResults'));
const aFnAssign = rewriteForWindow(extractFunc(tpl, 'addRowWarning'));
const rmFnAssign = rewriteForWindow(extractFunc(tpl, 'removeRowWarning'));
const addTogFnAssign = rewriteForWindow(extractFunc(tpl, 'addVerifiedToggle'));
const rmTogFnAssign = rewriteForWindow(extractFunc(tpl, 'removeVerifiedToggle'));
const lockFnAssign = rewriteForWindow(extractFunc(tpl, 'isOrderLocked'));
const bindFnAssign = rewriteForWindow(extractFunc(tpl, 'bindRowWarningClickHandler'));
const vFnAssign = rewriteForWindow(extractFunc(tpl, 'validateAllRows'));
const navFnAssign = rewriteForWindow(extractFunc(baseTpl, 'updateNavIndicator'));

(function (window) {
  var escHtml = function(s) {
    if (s == null) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  };
  window.escHtml = escHtml;
  eval(wFnAssign);
  eval(cFnAssign);
  eval(rFnAssign);
  eval(sFnAssign);
  eval(aFnAssign);
  eval(rmFnAssign);
  eval(addTogFnAssign);
  eval(rmTogFnAssign);
  eval(lockFnAssign);
  eval(vFnAssign);
  eval(navFnAssign);
  eval(bindFnAssign);

  // 从 _image_upload_modal.html 抽取核心 setter + URL 模板
  // 只验核心契约:setTarget 设置 + URL 拼接(不抽整个 IIFE — var 在 eval scope 隔离)
  // IMAGE_API 在模板里是占位符,设个假值便于断言
  window.IMAGE_API = '/api/v1/shipping-orders';
  // _setTarget 的核心逻辑:currentUploadTarget = {type, pk, orderPk}
  // 抽出来手动重写,确保写在 window 上(用 IIFE 包 window 引用,绕过 eval scope 没 window 标识符的问题)
  var setTargetSrc = extractFunc(imgModalTpl, '_setTarget');
  var setTargetRewritten = setTargetSrc
    .replace(/^function\s+_setTarget/, 'window._setTarget = function')
    .replace(/currentUploadTarget/g, 'window.currentUploadTarget')
    .replace(/currentImageOrderPk/g, 'window.currentImageOrderPk');
  // jsdom 的 window.eval 是间接 eval,看不到 IIFE 参数。用 new Function 构造普通函数,
// 函数体内 'window' 参数可见,赋值会写到传入的 window 对象。
new Function('window', setTargetRewritten)(window);

  // confirmUpload 的 URL 拼接逻辑 — 直接从源码 grep 出 URL 模板分支
  // (源码行: var url = savedTarget.type === 'record' ? IMAGE_API + '/records/' + savedTarget.pk + '/images' : IMAGE_API + '/' + savedTarget.pk + '/images';)
  // 这里人工重建 URL 模板用于断言
  function buildUploadUrl(target, imageApi) {
      return target.type === 'record'
          ? imageApi + '/records/' + target.pk + '/images'
          : imageApi + '/' + target.pk + '/images';
  }
  globalThis.buildUploadUrl = buildUploadUrl;
  // 模板里 `typeof getItemWarnings !== 'function'` 这种裸名 typeof 检查,
  // 在被 window.X = function() 重写后,函数体里裸名 getItemWarnings 找不到。
  // 把 window.X 同时也挂到全局,让 typeof 检查能命中。
  globalThis.getItemWarnings = window.getItemWarnings;
  globalThis.collectRowWarnings = window.collectRowWarnings;
  globalThis.addRowWarning = window.addRowWarning;
  globalThis.removeRowWarning = window.removeRowWarning;
  globalThis.isOrderLocked = window.isOrderLocked;
  globalThis.validateAllRows = window.validateAllRows;
  globalThis.updateNavIndicator = window.updateNavIndicator;
  globalThis.bindRowWarningClickHandler = window.bindRowWarningClickHandler;
  globalThis.openImageModal = window.openImageModal;
  globalThis.openImageModalForRecord = window.openImageModalForRecord;
  globalThis.closeImageModal = window.closeImageModal;
  globalThis.confirmUpload = window.confirmUpload;
  globalThis._setTarget = window._setTarget;
  globalThis.escHtml = window.escHtml;
})(window);

// 1. 先直接验证 getItemWarnings
const ws = window.getItemWarnings({product_name:'纯胶', specification:'0.6黑软纯胶'});
console.log('getItemWarnings 返回:', ws.length, '条');
ws.forEach((w, i) => console.log('  #', i+1, ':', w));

// 2. 渲染弹框(新行为:弹框内不显示警告,只渲染数据行)
window.renderAiResults([{product_name:'纯胶', specification:'0.6黑软纯胶', quantity:'100', unit:'y', remark:''}]);

// 3. 验证 modal 内确实没有警告(用户要求:警告挪到明细行下方)
const modalWarnItems = document.querySelectorAll('#aiResultBody .ai-row-warn-item');
const modalWarnHeads = document.querySelectorAll('#aiResultBody .ai-row-warn-head');
const modalRows = document.querySelectorAll('#aiResultBody tr');

console.log('\n--- 弹框渲染输出(应当纯净,无警告) ---');
console.log('弹框 <tr> 数:', modalRows.length, '(应该是 1,只有数据行)');
console.log('弹框内警告块数:', modalWarnItems.length, '(应该是 0 — 已搬走)');
console.log('弹框内警告头部数:', modalWarnHeads.length, '(应该是 0)');

// 4. 验证 addRowWarning 把警告落到目标 tr 的下方
console.log('\n--- addRowWarning 测试(警告落到明细行下方) ---');
// 模拟订单明细表中已有的行(8 列:序号/品名/规格/数量/单位/备注/辅助提示/操作)
const ordersTbody = document.createElement('tbody');
const dataRow = document.createElement('tr');
dataRow.innerHTML =
  '<td>1</td>' +
  '<td data-field="product_name">纯胶</td>' +
  '<td data-field="specification">0.6黑软纯胶</td>' +
  '<td>100</td>' +
  '<td>y</td>' +
  '<td>—</td>' +
  '<td>—</td>' +
  '<td>✏️</td>';
ordersTbody.appendChild(dataRow);
document.body.appendChild(ordersTbody);

window.addRowWarning(dataRow, {product_name: '纯胶', specification: '0.6黑软纯胶'});

const orderWarnHeads = ordersTbody.querySelectorAll('.record-warn-head');
const orderWarnItems = ordersTbody.querySelectorAll('.record-warn-item');
const orderRows = ordersTbody.querySelectorAll('tr');

console.log('明细表 <tr> 数:', orderRows.length, '(应该是 2:1 数据行 + 1 警告行)');
console.log('明细表警告头数:', orderWarnHeads.length, '(应该是 1)');
console.log('明细表独立警告块数:', orderWarnItems.length, '(应该是 2)');
console.log('警告头文案:', orderWarnHeads[0] && orderWarnHeads[0].textContent.trim());
orderWarnItems.forEach((el, i) => console.log('  警告 #' + (i+1) + ':', el.textContent.trim()));
console.log('警告行位置:', orderRows[1] === dataRow.nextElementSibling ? '✅ 紧跟数据行' : '❌ 不对');

// 5. 断言
let failed = 0;
if (modalRows.length !== 1) { console.error('❌ 弹框内应只有 1 行数据,实际', modalRows.length); failed++; }
if (modalWarnItems.length !== 0) { console.error('❌ 弹框内不应有警告块'); failed++; }
if (modalWarnHeads.length !== 0) { console.error('❌ 弹框内不应有警告头'); failed++; }

if (orderRows.length !== 2) { console.error('❌ 明细表应 2 行(数据 + 警告)'); failed++; }
if (orderWarnHeads.length !== 1) { console.error('❌ 警告头数不对'); failed++; }
if (orderWarnItems.length !== 2) { console.error('❌ 独立警告块数不对'); failed++; }
if (orderWarnHeads[0] && !orderWarnHeads[0].textContent.includes('2 项需核查')) {
  console.error('❌ 头部文案缺少"2 项需核查"');
  failed++;
}
if (orderRows[1] !== dataRow.nextElementSibling) {
  console.error('❌ 警告行未紧跟数据行');
  failed++;
}

// 7. inline edit 重渲:模拟编辑 product_name 从 "纯胶" → "环保纯胶",警告应自动清理
console.log('\n--- inline edit 路径(edit 后警告自动重渲) ---');
// 复用 dataRow 但先改 cell[1] 文本为 "环保纯胶",然后再调 addRowWarning
dataRow.children[1].textContent = '环保纯胶';
dataRow.children[2].textContent = '1.0黑硬纯胶';
window.addRowWarning(dataRow, {product_name: '环保纯胶', specification: '1.0黑硬纯胶'});
const afterEdit = ordersTbody.querySelectorAll('.record-warn-row').length;
console.log('edit 后警告行数:', afterEdit, '(应该是 0 — 改成完全合规)');

// 反向:再改回不合规值,警告应重新出现
dataRow.children[1].textContent = '纯胶';
dataRow.children[2].textContent = '0.6黑软纯胶';
window.addRowWarning(dataRow, {product_name: '纯胶', specification: '0.6黑软纯胶'});
const afterEdit2 = ordersTbody.querySelectorAll('.record-warn-row').length;
const afterEdit2Items = ordersTbody.querySelectorAll('.record-warn-item').length;
console.log('再编辑回后警告行数:', afterEdit2, '(应该是 1)');
console.log('再编辑回独立警告块:', afterEdit2Items, '(应该是 2)');

if (afterEdit !== 0) { console.error('❌ edit 后警告应清空,实际', afterEdit); failed++; }
if (afterEdit2 !== 1) { console.error('❌ 再 edit 警告应恢复,实际', afterEdit2); failed++; }
if (afterEdit2Items !== 2) { console.error('❌ 重渲后独立警告块数不对,实际', afterEdit2Items); failed++; }

// 9. 删除路径:removeRowWarning 移除紧邻的 .record-warn-row,避免孤儿行
console.log('\n--- 删除路径(removeRowWarning) ---');
// 此时明细表里有:1 数据行 + 1 警告行(从上面 #7 后改回 "纯胶 / 0.6黑软纯胶")
console.log('删前明细表 <tr> 数:', ordersTbody.querySelectorAll('tr').length, '(应该是 2)');
window.removeRowWarning(dataRow);
console.log('removeRowWarning 后 <tr> 数:', ordersTbody.querySelectorAll('tr').length, '(应该是 1)');
const remainingWarn = ordersTbody.querySelectorAll('.record-warn-row').length;
console.log('剩余警告行:', remainingWarn, '(应该是 0)');
if (remainingWarn !== 0) { console.error('❌ 警告行未被清空'); failed++; }

// 反向:重复调用 removeRowWarning 是幂等的
window.removeRowWarning(dataRow);
window.removeRowWarning(dataRow);
const after3 = ordersTbody.querySelectorAll('.record-warn-row').length;
console.log('重复调用后:', after3, '(应该是 0 — 幂等)');
if (after3 !== 0) { console.error('❌ removeRowWarning 不幂等'); failed++; }

// 10. CSS 抽取检查:警告块不带 inline style,只依赖 class
console.log('\n--- CSS 抽取检查 ---');
window.addRowWarning(dataRow, {product_name: '纯胶', specification: '0.6黑软纯胶'});
const warnHeadInline = document.querySelector('.record-warn-head').getAttribute('style') || '';
const warnItemInline = document.querySelector('.record-warn-item').getAttribute('style') || '';
console.log('警告头 inline style:', warnHeadInline === '' ? '✅ 空(走 CSS)' : '❌ 仍有: ' + warnHeadInline);
console.log('警告项 inline style:', warnItemInline === '' ? '✅ 空(走 CSS)' : '❌ 仍有: ' + warnItemInline);
if (warnHeadInline !== '') { console.error('❌ .record-warn-head 还有 inline style,没抽干净'); failed++; }
if (warnItemInline !== '') { console.error('❌ .record-warn-item 还有 inline style,没抽干净'); failed++; }

// 11. 批量校验 validateAllRows:扫多行,返回 {total, withWarnings, items}
console.log('\n--- 批量校验 validateAllRows ---');
// 构造 3 个订单组,每组 1 行明细,3 个不同状态:
//   - 订单 A: 纯胶 / 0.6黑软纯胶  → 2 条警告(R1+R3b)
//   - 订单 B: 环保纯胶 / 1.0黑硬纯胶 → 0 条警告
//   - 订单 C: 7P环保杂胶 / 1.0黑中 → 1 条警告(R2a)
const multiTbody = document.createElement('tbody');
multiTbody.id = 'multi-test-tbody';
const buildDataRow = function(pn, sp, orderId, recordId) {
  const tr = document.createElement('tr');
  tr.setAttribute('data-record-id', recordId);
  tr.innerHTML =
    '<td>1</td>' +
    '<td data-field="product_name">' + pn + '</td>' +
    '<td data-field="specification">' + sp + '</td>' +
    '<td>100</td>' +
    '<td>y</td>' +
    '<td>—</td>' +
    '<td>—</td>' +
    '<td>✏️</td>';
  return tr;
};
const dateGroupA = document.createElement('div');
dateGroupA.className = 'date-group';
dateGroupA.setAttribute('data-order-id', 'A1');
const tableA = document.createElement('table');
tableA.className = 'record-table';
tableA.appendChild(multiTbody.cloneNode(false));
tableA.querySelector('tbody').appendChild(buildDataRow('纯胶', '0.6黑软纯胶', 'A1', 'rec-1'));
dateGroupA.appendChild(tableA);
document.body.appendChild(dateGroupA);

const dateGroupB = document.createElement('div');
dateGroupB.className = 'date-group';
dateGroupB.setAttribute('data-order-id', 'B2');
const tableB = document.createElement('table');
tableB.className = 'record-table';
const tbodyB = document.createElement('tbody');
tbodyB.appendChild(buildDataRow('环保纯胶', '1.0黑硬纯胶', 'B2', 'rec-2'));
tableB.appendChild(tbodyB);
dateGroupB.appendChild(tableB);
document.body.appendChild(dateGroupB);

const dateGroupC = document.createElement('div');
dateGroupC.className = 'date-group';
dateGroupC.setAttribute('data-order-id', 'C3');
const tableC = document.createElement('table');
tableC.className = 'record-table';
const tbodyC = document.createElement('tbody');
tbodyC.appendChild(buildDataRow('7P环保杂胶', '1.0黑中', 'C3', 'rec-3'));
tableC.appendChild(tbodyC);
dateGroupC.appendChild(tableC);
document.body.appendChild(dateGroupC);

const summary = window.validateAllRows();
console.log('total:', summary.total, '(应该是 3:这次新建的 3 个订单明细行,旧 dataRow 不在 .record-table 里不计)');
console.log('withWarnings:', summary.withWarnings, '(应该是 2:A + C)');
console.log('items 数:', summary.items.length, '(应该是 2:A 有 2 warnings 算 1 item,C 有 1 warning 算 1 item)');
summary.items.forEach(function(it, i) {
  console.log('  item[' + i + ']:', it.orderId, '/', it.pn, '/', it.sp, '→', it.warnings.length, '条');
});

if (summary.total !== 3) { console.error('❌ total 错'); failed++; }
if (summary.withWarnings !== 2) { console.error('❌ withWarnings 错'); failed++; }
if (summary.items.length !== 2) { console.error('❌ items 数错'); failed++; }
// 验证幂等:再次调用结果一致
const summary2 = window.validateAllRows();
if (summary2.total !== summary.total || summary2.withWarnings !== summary.withWarnings) {
  console.error('❌ validateAllRows 不幂等');
  failed++;
}

// 12. 验证警告行的 colspan 等于目标数据行的列数
console.log('\n--- colspan 自适应 ---');
const dataTrCols = dataRow.children.length;
const warnTrCols = document.querySelector('.record-warn-row > td').getAttribute('colspan');
console.log('数据行 td 数:', dataTrCols, '| 警告行 colspan:', warnTrCols);
if (parseInt(warnTrCols) !== dataTrCols) {
  console.error('❌ colspan 不匹配数据行列数');
  failed++;
}

// 13. Nav 校验状态指示器 updateNavIndicator
console.log('\n--- Nav 指示器 ---');
var navEl = document.getElementById('validationStatusIndicator');
var navCountEl = document.getElementById('navWarnCount');
// 触发一次刷新
window.updateNavIndicator();
var navDisplay1 = navEl.style.display;
var navCount1 = navCountEl.textContent;
var navTitle1 = navEl.getAttribute('title');
console.log('初次刷新 → display:', navDisplay1, '| 计数:', navCount1, '| 标题:', navTitle1);
// 此时应该有 2 行需核查(A1 + C3),显示并显示 "2"
if (navDisplay1 === 'none') { console.error('❌ 应显示,但被隐藏'); failed++; }
if (navCount1 !== '2') { console.error('❌ 计数应是 2,实际', navCount1); failed++; }
if (!navTitle1 || !navTitle1.includes('2')) { console.error('❌ 标题应包含 2'); failed++; }

// addRowWarning 触发:加一行新的 → 计数应自动 +1
var extraDateGroup = document.createElement('div');
extraDateGroup.className = 'date-group';
extraDateGroup.setAttribute('data-order-id', 'D4');
var extraTable = document.createElement('table');
extraTable.className = 'record-table';
var extraTbody = document.createElement('tbody');
extraTbody.appendChild(buildDataRow('纯胶', '0.6黑软纯胶', 'D4', 'rec-4'));
extraTable.appendChild(extraTbody);
extraDateGroup.appendChild(extraTable);
document.body.appendChild(extraDateGroup);
// 取出该行单独调 addRowWarning(也会触发 updateNavIndicator)
var extraDataRow = extraTbody.querySelector('tr');
window.addRowWarning(extraDataRow, {product_name: '纯胶', specification: '0.6黑软纯胶'});
var navCount2 = navCountEl.textContent;
console.log('加 1 行后 → 计数:', navCount2, '(应该是 3)');
if (navCount2 !== '3') { console.error('❌ addRowWarning 后计数未自动更新'); failed++; }

// removeRowWarning 触发:把该行删掉(连带警告行)→ 计数应自动 -1
// 模拟生产代码路径:removeRowWarning(tr) → tr.remove() → updateNavIndicator()
window.removeRowWarning(extraDataRow);
extraDataRow.remove();
window.updateNavIndicator();  // 生产代码的 3 处删除路径都会调
var navCount3 = navCountEl.textContent;
console.log('删 1 行后 → 计数:', navCount3, '(应该是 2)');
if (navCount3 !== '2') { console.error('❌ 删行后计数未自动更新'); failed++; }

// 把所有行都改成完全合规 → 指示器应隐藏
document.querySelectorAll('.date-group .record-table tbody tr').forEach(function(tr) {
  if (tr.classList && tr.classList.contains('record-warn-row')) return;
  tr.children[1].textContent = '环保纯胶';
  tr.children[2].textContent = '1.0黑硬纯胶';
  window.addRowWarning(tr, {product_name: '环保纯胶', specification: '1.0黑硬纯胶'});
});
window.updateNavIndicator();  // 全合规后强制刷新一次
var navDisplayEnd = navEl.style.display;
console.log('全部合规后 → display:', navDisplayEnd, '(应该是 none)');
if (navDisplayEnd !== 'none') { console.error('❌ 全合规后指示器应隐藏'); failed++; }

// 14. 锁定订单不显示警告 + 不计入校验
console.log('\n--- 锁定订单跳过 ---');
// 新建一个锁定的订单组
var lockedGroup = document.createElement('div');
lockedGroup.className = 'date-group';
lockedGroup.setAttribute('data-order-id', 'LOCK1');
lockedGroup.setAttribute('data-locked', '1');  // ← 锁定
var lockedTable = document.createElement('table');
lockedTable.className = 'record-table';
var lockedTbody = document.createElement('tbody');
var lockedDataRow = buildDataRow('纯胶', '0.6黑软纯胶', 'LOCK1', 'rec-locked-1');
lockedTbody.appendChild(lockedDataRow);
lockedTable.appendChild(lockedTbody);
lockedGroup.appendChild(lockedTable);
document.body.appendChild(lockedGroup);

// isOrderLocked 验证
var isLockedCheck = window.isOrderLocked(lockedDataRow);
console.log('isOrderLocked 锁定行:', isLockedCheck, '(应该是 true)');
if (!isLockedCheck) { console.error('❌ isOrderLocked 没识别锁定'); failed++; }

// addRowWarning 应不渲染警告
window.addRowWarning(lockedDataRow, {product_name: '纯胶', specification: '0.6黑软纯胶'});
var lockedWarns = lockedTbody.querySelectorAll('.record-warn-row').length;
console.log('锁定行下方警告行数:', lockedWarns, '(应该是 0)');
if (lockedWarns !== 0) { console.error('❌ 锁定行不应有警告,但有', lockedWarns); failed++; }

// validateAllRows 应排除锁定行
var sum2 = window.validateAllRows();
console.log('含锁定行时 total:', sum2.total, '(应该是 3:原 3 + 锁 0)');
console.log('含锁定行时 withWarnings:', sum2.withWarnings, '(应该是 0)');
if (sum2.total !== 3) { console.error('❌ total 把锁定行算进去了'); failed++; }
if (sum2.withWarnings !== 0) { console.error('❌ withWarnings 把锁定行算进去了'); failed++; }

// 解锁后应恢复校验
lockedGroup.setAttribute('data-locked', '0');
window.addRowWarning(lockedDataRow, {product_name: '纯胶', specification: '0.6黑软纯胶'});
var lockedWarns2 = lockedTbody.querySelectorAll('.record-warn-row').length;
console.log('解锁后警告行数:', lockedWarns2, '(应该是 1)');
if (lockedWarns2 !== 1) { console.error('❌ 解锁后没恢复警告'); failed++; }
var sum3 = window.validateAllRows();
console.log('解锁后 withWarnings:', sum3.withWarnings, '(应该是 1)');
if (sum3.withWarnings !== 1) { console.error('❌ 解锁后 validateAllRows 没算'); failed++; }

// 重新锁回,避免影响下游 section (sum4)
lockedGroup.setAttribute('data-locked', '1');
window.addRowWarning(lockedDataRow, {product_name: '纯胶', specification: '0.6黑软纯胶'});

// 15. "已核查" 字段(verified)
console.log('\n--- 已核查(verified)字段 ---');
// 已核查行:不渲染警告,渲染绿色状态行
var verifiedDg = document.createElement('div');
verifiedDg.className = 'date-group';
verifiedDg.setAttribute('data-order-id', 'V1');
verifiedDg.setAttribute('data-locked', '0');
var verifiedTable = document.createElement('table');
verifiedTable.className = 'record-table';
var verifiedTbody = document.createElement('tbody');
var verifiedRow = buildDataRow('纯胶', '0.6黑软纯胶', 'V1', 'rec-verified-1');
verifiedRow.setAttribute('data-record-id', 'rec-verified-1');
verifiedRow.setAttribute('data-verified', '1');
verifiedTbody.appendChild(verifiedRow);
verifiedTable.appendChild(verifiedTbody);
verifiedDg.appendChild(verifiedTable);
document.body.appendChild(verifiedDg);

window.addRowWarning(verifiedRow, {
  product_name: '纯胶', specification: '0.6黑软纯胶', verified: 1, record_id: 'rec-verified-1'
});
var verifiedStatusRow = verifiedTbody.querySelector('.record-verified-row');
var verifiedStatusBtn = verifiedTbody.querySelector('.record-verified-unverify');
var verifiedToggleBtn = verifiedRow.querySelector('.record-verified-toggle');
console.log('已核查状态行(绿色)存在:', verifiedStatusRow ? '✅' : '❌ (新折叠模式不应渲染)');
console.log('数据行右侧 toggle 存在:', verifiedToggleBtn ? '✅' : '❌');
console.log('无黄色警告头:', verifiedTbody.querySelectorAll('.record-warn-head').length, '(应该是 0)');
// 新折叠模式:不再渲染绿色状态行,而是在数据行右侧加 toggle
if (verifiedStatusRow) { console.error('❌ 新模式下不应再渲染绿色状态行'); failed++; }
if (!verifiedToggleBtn) { console.error('❌ 已核查时未在数据行右侧渲染 toggle 按钮'); failed++; }

// validateAllRows 应排除已核查行
var sum4 = window.validateAllRows();
// 此处其他订单已全部合规/锁定/已核查,所以 withWarnings 应为 0
console.log('含已核查行时 withWarnings:', sum4.withWarnings, '(应该是 0)');
if (sum4.withWarnings !== 0) { console.error('❌ 已核查没被排除,实际=' + sum4.withWarnings); failed++; }

// 反向:未核查行有警告 → 渲染黄色警告头 + 已核查按钮
var unverifiedRow = buildDataRow('纯胶', '0.6黑软纯胶', 'V2', 'rec-unverified-1');
unverifiedRow.setAttribute('data-record-id', 'rec-unverified-1');
unverifiedRow.setAttribute('data-verified', '0');
verifiedTbody.appendChild(unverifiedRow);
window.addRowWarning(unverifiedRow, {
  product_name: '纯胶', specification: '0.6黑软纯胶', verified: 0, record_id: 'rec-unverified-1'
});
var warnHeadsAfter = verifiedTbody.querySelectorAll('.record-warn-head').length;
var verifyBtnAfter = verifiedTbody.querySelectorAll('.record-warn-verify-btn').length;
console.log('未核查行警告头数:', warnHeadsAfter, '(应该是 1)');
console.log('未核查行已核查按钮数:', verifyBtnAfter, '(应该是 1)');
if (warnHeadsAfter !== 1) { console.error('❌ 未核查应渲染警告头'); failed++; }
if (verifyBtnAfter !== 1) { console.error('❌ 未核查应渲染已核查按钮'); failed++; }

// 16. 点击事件代理:模拟点已核查按钮(用 mock window.markRecordVerified 验证它会被调)
console.log('\n--- 事件代理 ---');
// 先把事件代理绑上(test 没自动执行模块级 addEventListener)
window.bindRowWarningClickHandler();

var verifyBtn = verifiedTbody.querySelector('.record-warn-verify-btn');
var verifyCalled = false;
window.markRecordVerified = function(recordId, dataRow) {
    verifyCalled = true;
    console.log('  ✓ markRecordVerified 被调用,recordId=' + recordId);
};
var ev = new dom.window.Event('click', {bubbles: true});
verifyBtn.dispatchEvent(ev);
console.log('点击已核查按钮后 markRecordVerified 调用:', verifyCalled ? '✅' : '❌');
if (!verifyCalled) { console.error('❌ 事件代理没触发 markRecordVerified'); failed++; }

// 模拟点取消核查(新模式下用数据行右侧的 .record-verified-toggle)
var unverifyBtn = verifiedRow.querySelector('.record-verified-toggle');
var unverifyCalled = false;
window.unmarkRecordVerified = function(recordId, dataRow) {
    unverifyCalled = true;
    console.log('  ✓ unmarkRecordVerified 被调用,recordId=' + recordId);
};
var ev2 = new dom.window.Event('click', {bubbles: true});
unverifyBtn.dispatchEvent(ev2);
console.log('点击 toggle 后 unmarkRecordVerified 调用:', unverifyCalled ? '✅' : '❌');
if (!unverifyCalled) { console.error('❌ 事件代理没触发 unmarkRecordVerified'); failed++; }

// 17. 锁定切换局部刷新:模拟 toggleLock 后,已渲染的警告行应被清掉
console.log('\n--- 锁定切换局部刷新 ---');
// 构造一个独立订单组,模拟 toggleLock 在 PATCH 成功后的回调
var toggleDg = document.createElement('div');
toggleDg.className = 'date-group';
toggleDg.setAttribute('data-order-id', 'TG1');
toggleDg.setAttribute('data-locked', '0');
var toggleTable = document.createElement('table');
toggleTable.className = 'record-table';
var toggleTbody = document.createElement('tbody');
var toggleRow = buildDataRow('纯胶', '0.6黑软纯胶', 'TG1', 'rec-tg-1');
toggleRow.setAttribute('data-record-id', 'rec-tg-1');
toggleTbody.appendChild(toggleRow);
toggleTable.appendChild(toggleTbody);
toggleDg.appendChild(toggleTable);
document.body.appendChild(toggleDg);

// 先渲染警告行(解锁状态)
window.addRowWarning(toggleRow, {
  product_name: '纯胶', specification: '0.6黑软纯胶',
  verified: 0, record_id: 'rec-tg-1'
});
var beforeLock = toggleTbody.querySelectorAll('.record-warn-row').length;
console.log('锁定前警告行数:', beforeLock, '(应该是 1)');
if (beforeLock !== 1) { console.error('❌ 锁定前警告行应存在'); failed++; }

// 模拟 toggleLock 成功后的 DOM 更新(data-locked=1 + 局部刷新)
toggleDg.setAttribute('data-locked', '1');
toggleDg.querySelectorAll('.record-table tbody tr').forEach(function(tr) {
  if (tr.classList && tr.classList.contains('record-warn-row')) { tr.remove(); return; }
  var pn = (tr.children[1] && tr.children[1].textContent || '').trim();
  var sp = (tr.children[2] && tr.children[2].textContent || '').trim();
  if (!pn) return;
  var verified = (tr.dataset && tr.dataset.verified === '1') ? 1 : 0;
  var recordId = tr.dataset.recordId || tr.getAttribute('data-record-id') || null;
  if (typeof addRowWarning === 'function') {
    addRowWarning(tr, {product_name: pn, specification: sp, verified: verified, record_id: recordId});
  }
});
var afterLock = toggleTbody.querySelectorAll('.record-warn-row').length;
console.log('锁定后警告行数:', afterLock, '(应该是 0,已核查行也不算)');
if (afterLock !== 0) { console.error('❌ 锁定后警告行应消失'); failed++; }

// 模拟解锁:警告行应回来
toggleDg.setAttribute('data-locked', '0');
toggleDg.querySelectorAll('.record-table tbody tr').forEach(function(tr) {
  if (tr.classList && tr.classList.contains('record-warn-row')) { tr.remove(); return; }
  var pn = (tr.children[1] && tr.children[1].textContent || '').trim();
  var sp = (tr.children[2] && tr.children[2].textContent || '').trim();
  if (!pn) return;
  var verified = (tr.dataset && tr.dataset.verified === '1') ? 1 : 0;
  var recordId = tr.dataset.recordId || tr.getAttribute('data-record-id') || null;
  if (typeof addRowWarning === 'function') {
    addRowWarning(tr, {product_name: pn, specification: sp, verified: verified, record_id: recordId});
  }
});
var afterUnlock = toggleTbody.querySelectorAll('.record-warn-row').length;
console.log('解锁后警告行数:', afterUnlock, '(应该是 1)');
if (afterUnlock !== 1) { console.error('❌ 解锁后警告行应回来'); failed++; }

// 8. 关键:验证"点添加到订单"的循环不抛 TypeError(回归保护 2026-07-XX 报 bug)
//    submitAiResults 会遍历 tbody tr 并读 [data-field] input,
//    警告行没有 input — 必须跳过,否则 row.querySelector(...).value 抛 null.value TypeError
console.log('\n--- 提交路径 (点击"添加到订单") ---');
const iterStart = Date.now();
let crashed = false;
let crashErr = '';
let collectedItems = [];
try {
  // 直接复用 submitAiResults 里的核心 for 循环片段(避免触发真实 fetch)
  var rowsIter = document.querySelectorAll('#aiResultBody tr');
  for (var i = 0; i < rowsIter.length; i++) {
    var rowX = rowsIter[i];
    var pnInput = rowX.querySelector('[data-field="product_name"]');
    if (!pnInput) continue;  // 警告行直接跳过
    var pnV = pnInput.value.trim();
    var spV = rowX.querySelector('[data-field="specification"]').value.trim();
    var qtV = rowX.querySelector('[data-field="quantity"]').value.trim();
    var unV = rowX.querySelector('[data-field="unit"]').value;
    var rmV = rowX.querySelector('[data-field="remark"]').value.trim();
    if (pnV && qtV) collectedItems.push({product_name: pnV, specification: spV, quantity: qtV, unit: unV, remark: rmV});
  }
} catch (e) {
  crashed = true;
  crashErr = e.message;
}
const elapsed = Date.now() - iterStart;
console.log('迭代耗时:', elapsed, 'ms');
console.log('是否抛异常:', crashed ? '❌ 是: ' + crashErr : '✅ 否');
console.log('收集到的数据行:', collectedItems.length, '(应该是 1)');
console.log('  内容:', JSON.stringify(collectedItems[0]));

// 6. 验证 collectRowWarnings 也不抛错,并能识别到 2 条警告
let warnCollect = [];
let collectCrashed = false;
try {
  warnCollect = window.collectRowWarnings();
} catch (e) { collectCrashed = true; }
console.log('collectRowWarnings:', collectCrashed ? '❌ 抛错' : '✅ 正常', '→', warnCollect.length, '项');
if (warnCollect.length) {
  console.log('  第一个:', warnCollect[0].pn, '/', warnCollect[0].sp, '共', warnCollect[0].warnings.length, '条 warning');
}

if (crashed) { console.error('❌ 提交循环抛 TypeError — 警告行没被过滤'); failed++; }
if (collectedItems.length !== 1) { console.error('❌ 数据行没收齐'); failed++; }
if (collectCrashed) { console.error('❌ collectRowWarnings 抛错'); failed++; }
if (!collectCrashed && warnCollect.length !== 1) { console.error('❌ 警告没收齐'); failed++; }
if (!collectCrashed && warnCollect[0] && warnCollect[0].warnings.length !== 2) {
  console.error('❌ 警告数不是 2');
  failed++;
}

if (failed) process.exit(1);
console.log('\n✅ render_check 通过:渲染 + 提交循环 + 警告收集全部 OK,无 TypeError');
