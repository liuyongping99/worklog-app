// Smoke test: 验证 shipping-records.html 中
// 商品行右侧 ✏️修改 / 🗑️删除 / 🖼️图片 / ✅已核查 toggle 四个按钮的尺寸规则一致,
// 且不污染 ▲▼ move 按钮(那俩保留各自内联样式)。
// 跑法: node tests/test_button_height.js

const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');

const tpl = fs.readFileSync(path.join(__dirname, '..', 'templates', 'shipping-records.html'), 'utf8');

// 抽 shipping-page 的 <style>...</style> 块
const styleBlocks = tpl.match(/<style>([\s\S]*?)<\/style>/g) || [];
const shippingCss = styleBlocks.join('\n');

const hasUnifiedRule =
  /\.shipping-page\s+\.record-table\s+\.edit-record-btn[\s\S]*?\.record-verified-toggle[\s\S]*?\{[\s\S]*?(min-height|padding|font-size)[\s\S]*?\}/.test(shippingCss);
console.log('=== 模板 CSS 检查 ===');
console.log('含统一按钮规则:', hasUnifiedRule ? '✅' : '❌');
if (!hasUnifiedRule) { console.error('❌ CSS 缺统一规则'); process.exit(1); }

// 检查 rule 命中 4 个 class
const ruleMatch = shippingCss.match(/\.shipping-page\s+\.record-table\s+\.edit-record-btn,[\s\S]*?\{([\s\S]*?)\}/);
if (!ruleMatch) { console.error('❌ 找不到统一规则'); process.exit(1); }
const ruleBody = ruleMatch[1];
const ruleSelectors = shippingCss.substring(ruleMatch.index, ruleMatch.index + ruleMatch[0].length);
const hasAll4 = ['edit-record-btn', 'delete-record-btn', 'record-image-btn', 'record-verified-toggle']
  .every(c => ruleSelectors.includes(c));
console.log('规则命中 4 个 class:', hasAll4 ? '✅' : '❌');
if (!hasAll4) { console.error('❌ 规则少某个 class'); process.exit(1); }

// 关键属性都在
const hasPadding = /padding:\s*0\.25rem\s+0\.5rem/.test(ruleBody);
const hasFontSize = /font-size:\s*0\.8rem/.test(ruleBody);
const hasMinHeight = /min-height:\s*26px/.test(ruleBody);
console.log('  padding 0.25 0.5:', hasPadding ? '✅' : '❌');
console.log('  font-size 0.8rem:', hasFontSize ? '✅' : '❌');
console.log('  min-height 26px:', hasMinHeight ? '✅' : '❌');
if (!(hasPadding && hasFontSize && hasMinHeight)) { process.exit(1); }

// 检查 .record-image-btn 的 inline style 不再包含 padding/font-size
const btnLineMatch = tpl.match(/<button[^>]*record-image-btn[^>]*style="([^"]+)"[^>]*>/);
console.log('\n=== record-image-btn 内联样式 ===');
console.log('行:', btnLineMatch ? btnLineMatch[1] : '(未找到)');
if (btnLineMatch) {
  const inlineStyle = btnLineMatch[1];
  const stillHasPadding = /padding:\s*0\.1rem/.test(inlineStyle);
  const stillHasFontSize = /font-size:\s*0\.75rem/.test(inlineStyle);
  console.log('  inline padding 0.1rem(应移除):', stillHasPadding ? '❌' : '✅');
  console.log('  inline font-size 0.75rem(应移除):', stillHasFontSize ? '❌' : '✅');
  if (stillHasPadding || stillHasFontSize) { console.error('❌ inline 没清干净'); process.exit(1); }
}

// jsdom 模拟:4 个按钮的 padding/font-size 一致;move 按钮的内联不被新规则覆盖
console.log('\n=== jsdom 计算样式模拟 ===');
const dom = new JSDOM(`<!DOCTYPE html><html><body>
<style>${shippingCss}</style>
<table class="record-table"><tbody><tr>
  <td style="white-space:nowrap;">
    <button class="btn btn-sm move-up-btn" style="background:#f0f0f0;color:#555;border:1px solid #ccc;padding:0.1rem 0.35rem;font-size:0.75rem;">▲</button>
    <button class="btn btn-sm move-down-btn" style="background:#f0f0f0;color:#555;border:1px solid #ccc;padding:0.1rem 0.35rem;font-size:0.75rem;">▼</button>
    <button class="btn btn-primary btn-sm edit-record-btn">✏️</button>
    <button class="btn btn-danger btn-sm delete-record-btn">🗑️</button>
    <button class="btn btn-sm record-image-btn" style="background:#74b9ff;color:#fff;">🖼️</button>
    <button class="btn btn-sm record-verified-toggle">✅ 已核查</button>
  </td>
</tr></tbody></table>
</body></html>`);

const doc = dom.window.document;
const buttons = ['edit-record-btn', 'delete-record-btn', 'record-image-btn', 'record-verified-toggle'];
const styles = buttons.map(cls => {
  const el = doc.querySelector('.' + cls);
  const cs = dom.window.getComputedStyle(el);
  return { cls, padding: cs.padding, fontSize: cs.fontSize, lineHeight: cs.lineHeight };
});
styles.forEach(s => console.log(`  ${s.cls}: padding=${s.padding}, font-size=${s.fontSize}, line-height=${s.lineHeight}`));

const allSamePadding = styles.every(s => s.padding === styles[0].padding);
const allSameFontSize = styles.every(s => s.fontSize === styles[0].fontSize);
console.log('4 按钮 padding 一致:', allSamePadding ? '✅' : '❌');
console.log('4 按钮 font-size 一致:', allSameFontSize ? '✅' : '❌');
if (!allSamePadding || !allSameFontSize) { console.error('❌ 4 按钮尺寸不一致'); process.exit(1); }

// 确认 move 按钮的 inline style 没被覆盖
const moveUp = doc.querySelector('.move-up-btn');
const moveUpPadding = dom.window.getComputedStyle(moveUp).padding;
console.log('  move-up-btn padding(应保留 0.1rem 0.35rem):', moveUpPadding);
if (moveUpPadding !== '2px 7px') {  // 0.1rem ≈ 2px, 0.35rem ≈ 5.6px → jsdom 简化
  // jsdom getComputedStyle 的 padding 返回 '2px 5.6px' 之类;不强匹配,只确认不是 '4px 8px'
  if (moveUpPadding === '4px 8px') { console.error('❌ move 按钮被新规则覆盖了!'); process.exit(1); }
  console.log('  (jsdom 渲染有偏差,但未被新规则覆盖)');
}

console.log('\n✅ 所有检查通过');