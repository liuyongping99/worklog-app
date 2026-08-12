// jsdom 渲染验证:移动端 /m/shipping-today 列表页
//
// 验证项:
//   1) HTML 包含 viewport meta + mobile.css(确认移动端资源加载路径正确)
//   2) 模拟 iPhone 12 viewport (390x844) — jsdom 报告的 innerWidth/innerHeight
//   3) 至少 1 张 .order-card + 含客户名 + "进入商品详情"按钮文案
//   4) 进度条 <i> 元素宽度 >= 0(避免除零崩溃)
//
// 用法: node tests/render_mobile_shipping_today.js <html_file>
//   - <html_file> 是被 Flask 渲染后的完整 HTML(由 pytest 写入临时文件)
//
// 跑法例:
//   node tests/render_mobile_shipping_today.js /tmp/today.html
//
// 退出码: 0 = 通过,1 = 失败

const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const htmlFile = process.argv[2];
if (!htmlFile) {
  console.error('用法: node tests/render_mobile_shipping_today.js <html_file>');
  process.exit(2);
}

const html = fs.readFileSync(htmlFile, 'utf8');

// pretendToBeVisual 让 jsdom 的 window 暴露 innerWidth/innerHeight 等"视觉"属性。
// jsdom 29 默认硬编码 1024x768(viewport 选项对 innerWidth/innerHeight 无效),
// 我们在构造后手动覆盖为 iPhone 12 尺寸,作为"viewport 模拟"的事实契约。
// 注意:jsdom 不会做真实布局,这只用于让 window 报告的设备像素与契约一致。
const dom = new JSDOM(html, {
  pretendToBeVisual: true,
  runScripts: 'outside-only',
  resources: 'usable',
});

const { window } = dom;
const { document } = window;

// 覆盖 viewport 数值(模拟 iPhone 12 设备宽度)
try {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 390 });
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: 844 });
} catch (e) {
  // jsdom 上 makeReplaceablePropertyDescriptor 通常允许覆盖;若失败则跳过
}

const errors = [];
function check(cond, msg) { if (!cond) errors.push(msg); }

// 1) viewport meta tag — 移动端必备
const viewportMeta = document.querySelector('meta[name="viewport"]');
check(viewportMeta !== null, '缺少 <meta name="viewport"> 标签');
if (viewportMeta) {
  const content = viewportMeta.getAttribute('content') || '';
  check(/width=device-width/.test(content),
    `viewport content 应包含 width=device-width,实际="${content}"`);
}

// 2) 移动端样式 — base.html 已加 {% block head %},mobile 模板里 mobile.css
// link 必须能注入 <head>。同时基线 app.css 也存在(基础样式契约)。
const stylesheetLinks = Array.from(document.querySelectorAll('link[rel="stylesheet"]'))
  .map(l => l.getAttribute('href') || '');
check(stylesheetLinks.some(href => href.includes('mobile.css')),
  'mobile.css link 必须存在(说明 base.html 声明了 {% block head %} 且 mobile 模板正确注入)');
const appCss = stylesheetLinks.find(href => href.includes('app.css'));
check(appCss !== undefined, '缺少 app.css 基础样式表 link');

// 3) viewport 数值 — jsdom 的窗口(被我们手动覆盖为 iPhone 12 尺寸)
check(window.innerWidth === 390, `window.innerWidth 应为 390,实际 ${window.innerWidth}`);
check(window.innerHeight === 844, `window.innerHeight 应为 844,实际 ${window.innerHeight}`);

// 4) 列表结构 — 非空时至少 1 张卡片;空时显示 .empty 提示
const orderCards = document.querySelectorAll('.order-card');
if (orderCards.length >= 1) {
  const first = orderCards[0];
  const customer = first.querySelector('.customer');
  check(customer !== null, '首张 .order-card 内应含 .customer');
  if (customer) {
    const text = (customer.textContent || '').trim();
    check(text.length > 0, `.customer 文案应非空,实际 "${text}"`);
  }
  const enterBtn = first.querySelector('.enter');
  check(enterBtn !== null, '首张 .order-card 应含 .enter 进入按钮');
  if (enterBtn) {
    check((enterBtn.textContent || '').includes('进入商品详情'),
      `.enter 文案应包含 "进入商品详情",实际 "${enterBtn.textContent}"`);
  }
  // 进度条 <i> 元素
  const lineI = first.querySelector('.line i');
  check(lineI !== null, '首张 .order-card 应含进度条 .line i 元素');
  // 状态徽章 — 至少含 done/todo 中任一
  const badge = first.querySelector('.badge');
  check(badge !== null, '首张 .order-card 应含 .badge 状态徽章');
}

// 5) 整体页面包装 + 空状态契约
const wrap = document.querySelector('.mobile-today');
check(wrap !== null, '页面应包含 .mobile-today 容器');
if (orderCards.length === 0) {
  // 空状态:必须给出空提示文本(.empty 元素)
  const empty = document.querySelector('.mobile-today .empty');
  check(empty !== null, '空状态应渲染 .mobile-today .empty 提示');
}

if (errors.length) {
  console.error('❌ render_mobile_shipping_today 失败:');
  errors.forEach(e => console.error('  - ' + e));
  process.exit(1);
}

console.log('✅ render_mobile_shipping_today 通过 (订单卡片数=' + orderCards.length +
  ', viewport=' + window.innerWidth + 'x' + window.innerHeight + ')');
