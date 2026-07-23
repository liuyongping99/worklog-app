// Smoke test: 验证 shipping-records.html 中 applyRecordImageOverlay
// 能正确从 record 行读取 product_name/spec,并叠加到 .img-item-record 上。
// 跑法: node tests/test_shipping_overlay.js

const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');

const tpl = fs.readFileSync(path.join(__dirname, '..', 'templates', 'shipping-records.html'), 'utf8');
const commonJs = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'common.js'), 'utf8');

// 从模板抽取函数:支持两种写法
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

const dom = new JSDOM(`<!DOCTYPE html><html><body>
<div class="shipping-page">
  <div class="date-group" data-order-id="100">
    <table class="record-table">
      <tbody>
        <tr data-record-id="1494" data-verified="1">
          <td>1</td>
          <td>杂胶</td>
          <td>1.0-黑中加面</td>
          <td>1290</td>
          <td>y</td>
          <td>30支（硬标签）</td>
          <td>30支</td>
          <td>
            <button class="record-image-btn" data-record-id="1494">🖼️</button>
          </td>
        </tr>
      </tbody>
    </table>
    <div class="order-images-area columns-5" data-order-id="100">
      <div class="img-item img-item-record" data-record-pk="1494">
        <img src="/upload/test1.jpg" alt="商品图片">
        <button class="del-img-btn" data-image-id="1">×</button>
      </div>
      <div class="img-item img-item-record" data-record-pk="1494">
        <img src="/upload/test2.jpg" alt="商品图片">
        <button class="del-img-btn" data-image-id="2">×</button>
      </div>
      <div class="img-item img-item-order">
        <img src="/upload/order1.jpg" alt="订单图">
        <button class="del-img-btn" data-image-id="3">×</button>
      </div>
    </div>
  </div>
</div>
</body></html>`, { runScripts: 'dangerously' });

const { window } = dom;
const { document } = window;

// 通过往 jsdom DOM 注入 <script> 标签来跑代码,确保 window/document 都已绑定
function runInDom(code) {
  const script = document.createElement('script');
  script.textContent = code;
  document.body.appendChild(script);
}

// 注入共享 escape 函数(模板里通过 common.js 暴露)
// common.js 顶层引用 document/event listener,只在浏览器跑;
// 我们只取 escHtml/escAttr 两个纯函数。
const escOnly = commonJs.split('\n').slice(0, 14).join('\n') + '\nwindow.escHtml = escHtml;\nwindow.escAttr = escAttr;';
runInDom(escOnly);

// 注入待测函数
const targetFuncs = [
  'applyRecordImageOverlay',
  'applyAllRecordImageOverlays',
];
for (const fn of targetFuncs) {
  const code = extractFunc(tpl, fn);
  if (!code) { console.error('未找到函数:', fn); process.exit(1); }
  runInDom(code + '\nwindow.' + fn + ' = ' + fn + ';');
}

// 注入 appendOrderImageArea 用于测上传后的叠加路径
runInDom(extractFunc(tpl, 'appendOrderImageArea') + '\nwindow.appendOrderImageArea = appendOrderImageArea;');
runInDom(extractFunc(tpl, 'recordImageUploaded') + '\nwindow.recordImageUploaded = recordImageUploaded;');
// stub bindDeleteImageButtons (appendOrderImageArea 会调它)
runInDom('window.bindDeleteImageButtons = function() {};');

// 1. 跑初始化:全部 record 图应被挂上叠加
dom.window.applyAllRecordImageOverlays();

const recordItems = document.querySelectorAll('.img-item-record[data-record-pk]');
const orderItems = document.querySelectorAll('.img-item-order');
console.log('record 图数量:', recordItems.length);
console.log('order 图数量:', orderItems.length);

if (recordItems.length !== 2) {
  console.error('❌ record 图数量异常,期望 2,实为', recordItems.length);
  process.exit(1);
}

let pass = true;
recordItems.forEach((item, idx) => {
  const name = item.querySelector('.img-overlay-name');
  const spec = item.querySelector('.img-overlay-spec');
  if (!name) { console.error(`❌ record 图 ${idx + 1} 缺 name 叠加`); pass = false; return; }
  if (!spec) { console.error(`❌ record 图 ${idx + 1} 缺 spec 叠加`); pass = false; return; }
  console.log(`record 图 ${idx + 1}: name="${name.textContent}" (top), spec="${spec.textContent}" (bottom)`);
  if (name.textContent !== '杂胶') { console.error('❌ name 不匹配'); pass = false; }
  if (spec.textContent !== '1.0-黑中加面') { console.error('❌ spec 不匹配'); pass = false; }
});

// 1b. 位置：name 在顶部、spec 在底部；颜色：红
// jsdom 不计算 absolute 布局,改从模板源文件读 CSS 规则断言
const styleBlockMatch = tpl.match(/<style>([\s\S]*?)<\/style>/g) || [];
const cssText = styleBlockMatch.join('\n');
const nameRule = cssText.match(/\.img-item-record\s+\.img-overlay-name\s*\{[^}]+\}/);
const specRule = cssText.match(/\.img-item-record\s+\.img-overlay-spec\s*\{[^}]+\}/);
console.log('  找到 name CSS 规则:', !!nameRule, '| spec CSS 规则:', !!specRule);
if (!nameRule || !specRule) {
  console.error('❌ 找不到 name/spec 的 CSS 规则');
  pass = false;
} else {
  if (!/top:\s*6px/.test(nameRule[0])) { console.error('❌ name CSS 缺 top:6px'); pass = false; }
  if (!/position:\s*absolute/.test(nameRule[0])) { console.error('❌ name CSS 缺 position:absolute'); pass = false; }
  if (!/bottom:\s*6px/.test(specRule[0])) { console.error('❌ spec CSS 缺 bottom:6px'); pass = false; }
  if (!/position:\s*absolute/.test(specRule[0])) { console.error('❌ spec CSS 缺 position:absolute'); pass = false; }
  if (!/color:\s*#ff5252/i.test(nameRule[0])) { console.error('❌ name CSS 不是红色'); pass = false; }
  if (!/color:\s*#ff5252/i.test(specRule[0])) { console.error('❌ spec CSS 不是红色'); pass = false; }
  console.log('  ✅ name=顶部/spec=底部/均红色,断言通过');
}

// 2. order 级图不应被叠加
orderItems.forEach((item, idx) => {
  if (item.querySelector('.img-overlay-name') || item.querySelector('.img-overlay-spec')) {
    console.error(`❌ order 图 ${idx + 1} 不应有叠加,但出现了`);
    pass = false;
  }
});

// 3. 防重复:再跑一次,不应叠加多个 overlay
dom.window.applyAllRecordImageOverlays();
recordItems.forEach((item, idx) => {
  const names = item.querySelectorAll('.img-overlay-name');
  const specs = item.querySelectorAll('.img-overlay-spec');
  if (names.length > 1) { console.error(`❌ record 图 ${idx + 1} 出现 ${names.length} 个 name`); pass = false; }
  if (specs.length > 1) { console.error(`❌ record 图 ${idx + 1} 出现 ${specs.length} 个 spec`); pass = false; }
});

// 4. XSS 防护:product_name 含 <img onerror=...> 时应被 textContent 天然防注入
const xssItem = document.createElement('div');
xssItem.className = 'img-item img-item-record';
xssItem.setAttribute('data-record-pk', '9999');
xssItem.innerHTML = '<img src="/upload/xss.jpg">';
document.querySelector('.order-images-area').appendChild(xssItem);
// 显式传 product_name+spec,跳过行反查(隔离测试)
dom.window.applyRecordImageOverlay(xssItem, '9999', '<img src=x onerror=alert(1)>', '1.0-Spec');
const xssName = xssItem.querySelector('.img-overlay-name');
if (!xssName) {
  console.error('❌ XSS 用例没拿到 name 叠加');
  pass = false;
} else {
  const nestedImg = xssName.querySelector('img');
  const innerHTML = xssName.innerHTML;
  const txt = xssName.textContent;
  console.log('XSS 用例 name.textContent:', JSON.stringify(txt));
  console.log('XSS 用例 name.innerHTML:', JSON.stringify(innerHTML));
  if (nestedImg) {
    console.error('❌ XSS 防护失败:name 内的 <img> 被解析为元素');
    pass = false;
  } else if (txt !== '<img src=x onerror=alert(1)>') {
    console.error('❌ XSS 用例 textContent 不匹配,期望原样字符串');
    pass = false;
  } else {
    console.log('✅ XSS 防护生效(textContent 注入,无 <img> 元素被解析)');
  }
}

// 5. 模拟上传路径:recordImageUploaded 收到 data.images 后
// appendOrderImageArea 应在 recordImg 上挂 data-record-pk + name/spec 叠加
const newImg = document.createElement('div');
newImg.className = 'img-item img-item-record';
newImg.innerHTML = '<img src="/upload/new.jpg">';
document.querySelector('.order-images-area').appendChild(newImg);

dom.window.recordImageUploaded({
  success: true,
  images: [{ image_id: 999, image: '2026-07/new.jpg' }],
}, '1494', '100');
const uploadedItems = document.querySelectorAll('.order-images-area .img-item-record');
console.log('\n上传后 record 图总数:', uploadedItems.length);
const newRecordImg = uploadedItems[uploadedItems.length - 1];
console.log('data-record-pk:', newRecordImg.getAttribute('data-record-pk'));
const newName = newRecordImg.querySelector('.img-overlay-name');
const newSpec = newRecordImg.querySelector('.img-overlay-spec');
if (!newName || !newSpec) {
  console.error('❌ 上传后的 record 图没拿到叠加(name/spec 缺失)');
  pass = false;
} else {
  console.log('上传后叠加 name:', newName.textContent);
  console.log('上传后叠加 spec:', newSpec.textContent);
  if (newName.textContent !== '杂胶') { console.error('❌ 上传后 name 不匹配'); pass = false; }
  if (newSpec.textContent !== '1.0-黑中加面') { console.error('❌ 上传后 spec 不匹配'); pass = false; }
}

if (pass) {
  console.log('\n✅ 所有检查通过');
  process.exit(0);
} else {
  console.log('\n❌ 至少一项失败');
  process.exit(1);
}