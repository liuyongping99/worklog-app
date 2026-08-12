// jsdom 渲染验证:移动端 /m/shipping-today/order/<oid> 详情页
//
// 验证项:
//   1) viewport meta + mobile.css(移动端契约)
//   2) 模拟 iPhone 12 viewport (390x844)
//   3) .overall 整体图区存在 + 3 个 .data-overall-source 按钮(整体照/堆放/装车)
//   4) .product-card 商品卡片存在 + .product-name 文案非空
//   5) "拍照识别" + "相册" 两个动作按钮存在
//   6) 详情页挂载了 mobile_blur.js / mobile_detail.js
//
// 用法: node tests/render_mobile_shipping_order.js <html_file>
//
// 退出码: 0 = 通过,1 = 失败

const fs = require('fs');
const { JSDOM } = require('jsdom');

const htmlFile = process.argv[2];
if (!htmlFile) {
  console.error('用法: node tests/render_mobile_shipping_order.js <html_file>');
  process.exit(2);
}

const html = fs.readFileSync(htmlFile, 'utf8');

// jsdom 29 默认硬编码 1024x768(viewport 选项对 innerWidth/innerHeight 无效),
// 构造后手动覆盖为 iPhone 12 尺寸。jsdom 不做真实布局,viewport 模拟的契约
// 仅体现为 window.innerWidth/innerHeight + meta viewport 标签。
const dom = new JSDOM(html, {
  pretendToBeVisual: true,
  runScripts: 'outside-only',
  resources: 'usable',
});

const { window } = dom;
const { document } = window;

try {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 390 });
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: 844 });
} catch (e) {
  // jsdom 上 makeReplaceablePropertyDescriptor 通常允许覆盖;若失败则跳过
}

const errors = [];
function check(cond, msg) { if (!cond) errors.push(msg); }

// 1) viewport meta
const viewportMeta = document.querySelector('meta[name="viewport"]');
check(viewportMeta !== null, '缺少 <meta name="viewport"> 标签');
if (viewportMeta) {
  const content = viewportMeta.getAttribute('content') || '';
  check(/width=device-width/.test(content),
    `viewport content 应包含 width=device-width,实际="${content}"`);
}

// 2) 移动端样式 — base.html 已加 {% block head %},mobile.css link 必须注入 <head>
// 同时基线 app.css 也存在(基础样式契约)。
const stylesheetLinks = Array.from(document.querySelectorAll('link[rel="stylesheet"]'))
  .map(l => l.getAttribute('href') || '');
check(stylesheetLinks.some(href => href.includes('mobile.css')),
  'mobile.css link 必须存在(说明 base.html 声明了 {% block head %} 且 mobile 模板正确注入)');
const appCss = stylesheetLinks.find(href => href.includes('app.css'));
check(appCss !== undefined, '缺少 app.css 基础样式表 link');

// 3) viewport 数值
check(window.innerWidth === 390, `window.innerWidth 应为 390,实际 ${window.innerWidth}`);
check(window.innerHeight === 844, `window.innerHeight 应为 844,实际 ${window.innerHeight}`);

// 4) 整体图区 — 必拍
const overall = document.querySelector('.overall');
check(overall !== null, '缺少 .overall 整体图区');

const overallBtns = overall ? overall.querySelectorAll('[data-overall-source]') : [];
check(overallBtns.length === 3,
  `[data-overall-source] 应有 3 个按钮(整体照/堆放/装车),实际 ${overallBtns.length}`);

if (overall && overallBtns.length === 3) {
  const sources = Array.from(overallBtns).map(b => b.getAttribute('data-overall-source'));
  const expected = ['整体照', '堆放', '装车'];
  expected.forEach(e => {
    check(sources.includes(e), `[data-overall-source] 应含 "${e}",实际 [${sources.join(', ')}]`);
  });
  // 缩略图占位(3 个,初始显示 "未拍")
  const thumbs = overall.querySelectorAll('.overall-thumb[data-thumb-source]');
  check(thumbs.length === 3,
    `[data-thumb-source] 缩略图占位应有 3 个,实际 ${thumbs.length}`);
}

// 5) 商品卡片 — 至少 1 个,含拍照/相册两个动作按钮
const productCards = document.querySelectorAll('.product-card');
check(productCards.length >= 1, `至少应有 1 个 .product-card,实际 ${productCards.length}`);

if (productCards.length >= 1) {
  const first = productCards[0];
  const productName = first.querySelector('.product-name');
  check(productName !== null, '首张 .product-card 应含 .product-name');
  if (productName) {
    const text = (productName.textContent || '').trim();
    check(text.length > 0, `.product-name 文案应非空,实际 "${text}"`);
  }
  const captureBtn = first.querySelector('[data-action="capture"]');
  const albumBtn = first.querySelector('[data-action="album"]');
  check(captureBtn !== null, '首张 .product-card 应含 [data-action="capture"] 拍照按钮');
  check(albumBtn !== null, '首张 .product-card 应含 [data-action="album"] 相册按钮');
  if (captureBtn) {
    check((captureBtn.textContent || '').includes('拍照识别'),
      `capture 按钮文案应包含 "拍照识别",实际 "${captureBtn.textContent}"`);
  }
  if (albumBtn) {
    check((albumBtn.textContent || '').includes('相册'),
      `album 按钮文案应包含 "相册",实际 "${albumBtn.textContent}"`);
  }
  // 状态行(尚未拍照 / 识别状态)
  const statusLine = first.querySelector('[data-role="status"]');
  check(statusLine !== null, '首张 .product-card 应含 [data-role="status"] 状态行');
  // 人工确认按钮 — 初始 hidden,但 DOM 节点必须存在
  const confirmBtn = first.querySelector('[data-action="confirm"]');
  check(confirmBtn !== null, '首张 .product-card 应含 [data-action="confirm"] 人工确认按钮');
}

// 6) JS 资源 — 详情页特有
const scriptSrcs = Array.from(document.querySelectorAll('script[src]'))
  .map(s => s.getAttribute('src') || '');
check(scriptSrcs.some(s => s.includes('mobile_blur.js')),
  `应引用 mobile_blur.js,实际 scripts=[${scriptSrcs.join(', ')}]`);
check(scriptSrcs.some(s => s.includes('mobile_detail.js')),
  `应引用 mobile_detail.js,实际 scripts=[${scriptSrcs.join(', ')}]`);

// 7) 识别记录区
const recognitionLog = document.querySelector('[data-role="recognition-list"]');
check(recognitionLog !== null, '缺少 [data-role="recognition-list"] 识别记录容器');

// 8) 详情页整体包装
const wrap = document.querySelector('.mobile-order');
check(wrap !== null, '页面应包含 .mobile-order 容器');

if (errors.length) {
  console.error('❌ render_mobile_shipping_order 失败:');
  errors.forEach(e => console.error('  - ' + e));
  process.exit(1);
}

console.log('✅ render_mobile_shipping_order 通过 (product-card 数=' + productCards.length +
  ', viewport=' + window.innerWidth + 'x' + window.innerHeight + ')');
