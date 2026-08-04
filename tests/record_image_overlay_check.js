// 验证 _record_image_script.html 的两处行为:
//   1) recordImageUploaded 从 record 行读 product_name / specification 时用 class 选择器
//   2) bindDeleteImageButtons 删完行级图最后一张后,把 🖼️ 按钮的 has-image 清除
//
// 跑法: node tests/record_image_overlay_check.js
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

// ── 1) recordImageUploaded ─────────────────────────────────────────
const uploadFnSrc = extractFunc(tpl, 'window.recordImageUploaded') ||
                    extractFunc(tpl, 'recordImageUploaded');
if (!uploadFnSrc) {
  console.error('❌ 抽不出 recordImageUploaded');
  process.exit(1);
}

const dom = new JSDOM(`<!DOCTYPE html><html><body>
<div class="date-group" data-order-id="100">
  <table class="record-table">
    <tbody>
      <tr data-record-id="7">
        <td>1</td>
        <td class="eco-col"></td>
        <td class="product-name-cell">环保杂胶</td>
        <td class="spec-cell">1.0-黑中加面</td>
        <td class="match-col"></td>
        <td class="qty-cell">1290</td>
      </tr>
    </tbody>
  </table>
  <button class="record-image-btn" data-record-id="7"></button>
</div>
</body></html>`);

global.window = dom.window;
global.document = dom.window.document;

let captured = null;
global.appendOrderImageArea = function (orderPk, images, meta) { captured = meta; };
global.setRowMatchBadge = function () {};
global.pollRecordImageMatch = function () {};
dom.window.appendOrderImageArea = global.appendOrderImageArea;
dom.window.setRowMatchBadge = global.setRowMatchBadge;
dom.window.pollRecordImageMatch = global.pollRecordImageMatch;

eval(uploadFnSrc);
const uploadFn = global.window.recordImageUploaded || global.recordImageUploaded;
uploadFn({ success: true, images: [{ image_id: 1, processing: false }] }, 7, 100);

check(!!captured, 'appendOrderImageArea 未被调用');
check(captured && captured.product_name === '环保杂胶',
  `product_name 应为「环保杂胶」，实际「${captured && captured.product_name}」`);
check(captured && captured.specification === '1.0-黑中加面',
  `specification 应为「1.0-黑中加面」，实际「${captured && captured.specification}」`);

const imgBtn = dom.window.document.querySelector('.record-image-btn[data-record-id="7"]');
check(imgBtn && imgBtn.classList.contains('has-image'),
  'recordImageUploaded 后 🖼️ 按钮未加 .has-image');

// ── 2) bindDeleteImageButtons: 删完最后一张图后清 has-image ─────────
const deleteFnSrc = extractFunc(tpl, 'bindDeleteImageButtons');
if (!deleteFnSrc) {
  console.error('❌ 抽不出 bindDeleteImageButtons');
  process.exit(1);
}

// 重新搭一个 DOM:record 7 有 1 张图,模拟行级图容器 + 删除按钮
const dom2 = new JSDOM(`<!DOCTYPE html><html><body>
<button class="record-image-btn has-image" data-record-id="42"></button>
<div class="img-item img-item-record" data-record-pk="42">
  <button class="del-img-btn" data-image-id="999">删除</button>
</div>
</body></html>`);

global.window = dom2.window;
global.document = dom2.window.document;
// confirmDialog 在产品代码里调 → jsdom 没有,塞个空函数
global.confirmDialog = function (msg, okCb) { okCb(); };
dom2.window.confirmDialog = global.confirmDialog;
// fetch mock:后端返回 success:true
let fetchCalls = 0;
global.fetch = function () {
  fetchCalls++;
  return Promise.resolve({
    json: function () { return Promise.resolve({ success: true }); },
  });
};
dom2.window.fetch = global.fetch;

eval(deleteFnSrc);
bindDeleteImageButtons();

// 点删除按钮 → 触发 fetch + 成功回调 + setTimeout(300ms)里清 has-image
dom2.window.document.querySelector('.del-img-btn').click();

// setTimeout 异步,用 setImmediate / process.nextTick 都不行;jsdom 默认没真 setTimeout,
// 这里用 setTimeout(fn, 400) 等清 class 走完
setTimeout(function () {
  const btn = dom2.window.document.querySelector('.record-image-btn[data-record-id="42"]');
  const stillThere = dom2.window.document.querySelector(
    '.img-item-record[data-record-pk="42"]');
  check(fetchCalls === 1, `fetch 应被调用 1 次，实际 ${fetchCalls}`);
  check(!stillThere, '删除成功后 .img-item-record 应从 DOM 移除');
  check(btn && !btn.classList.contains('has-image'),
    '删完最后一张行级图后 🖼️ 按钮的 .has-image 必须清除');

  if (errors.length) {
    errors.forEach(e => console.error('❌ ' + e));
    process.exit(1);
  }
  console.log('✅ record_image_overlay_check 通过');
}, 500);