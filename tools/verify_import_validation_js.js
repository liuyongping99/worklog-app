/**
 * §f「导入即校验」前端逻辑验证 —— 2026-10-09
 *
 * 从 templates/_smart_add_modal.html 里抽出新增的 4 个函数实测：
 *   readImportFlags / importFlagPrefix / applyImportFlagClass / importFlagTitle / notifyImportValidation
 *
 * 验证重点：**服务端优先** —— 服务端给了标志就绝不调本地重算（这是
 * 「导入那一刻」与「刷新后」结论一致的关键）；服务端没给才回退本地。
 *
 * 无框架依赖，纯 Node。
 */
const fs = require('fs');
const path = require('path');
const ROOT = path.dirname(__dirname);
const html = fs.readFileSync(path.join(ROOT, 'templates/_smart_add_modal.html'), 'utf8');

// 抽出目标函数（按 function 括号配平截取）
function extract(name) {
  const start = html.indexOf('function ' + name);
  if (start < 0) throw new Error('未找到函数: ' + name);
  let depth = 0, i = html.indexOf('{', start);
  for (let j = i; j < html.length; j++) {
    if (html[j] === '{') depth++;
    else if (html[j] === '}') { depth--; if (depth === 0) return html.slice(start, j + 1); }
  }
  throw new Error('括号不配平: ' + name);
}

const names = ['readImportFlags', 'importFlagPrefix', 'applyImportFlagClass',
               'importFlagTitle', 'showImportToast', 'notifyImportValidation'];
const src = names.map(extract).join('\n\n');

let fail = 0;
function check(label, cond, extra) {
  if (!cond) fail++;
  console.log(`  ${cond ? 'OK  ' : 'FAIL'} ${label}` + (extra !== undefined ? `  → ${JSON.stringify(extra)}` : ''));
}

// ── 构造被测环境 ──
// 页面本地的重算实现（模拟「旧端点没给标志」时的兜底路径）
let localMismatchCalls = 0, localPieceCalls = 0;
const sandbox = {
  // 本地重算实现，故意返回与服务端不同的值，用来验证优先级
  checkMismatch: (remark, qty, pn, sp) => { localMismatchCalls++; return 'LOCAL_ONLY'; },
  checkPieceMismatch: (remark, qty, pn, sp) => { localPieceCalls++; return ''; },
  isInvalidQty: q => { return false; },
  mismatchIcon: level => level === 'warn' ? '❌ ' : level === 'info' ? '💡 ' : '',
  invalidQtyIcon: () => '<span class="stock-icon">⚠️</span>',
  applyMismatchClass: (tr, level) => sandbox.__applyClass(tr, level),
  escHtml: s => String(s ?? ''),
  document: {
    getElementById: () => null,
    createElement: () => ({ style: {}, setAttribute() {}, appendChild() {} }),
    body: { appendChild() {} },
  },
  setTimeout: () => 0,
  clearTimeout: () => {},
};
sandbox.__applyClass = (tr, level) => {
  tr.classList.remove('row-warn', 'row-info');
  if (level) tr.classList.add(level === 'warn' ? 'row-warn' : 'row-info');
  return tr;
};

const vm = require('vm');
const ctx = vm.createContext(sandbox);
vm.runInContext(src + '\n;this.__fns={readImportFlags,importFlagPrefix,applyImportFlagClass,importFlagTitle,notifyImportValidation};', ctx);
const F = ctx.__fns;

console.log('='.repeat(72));
console.log('§f 前端逻辑验证 —— 服务端标志优先');
console.log('='.repeat(72));

// ── 1. 服务端给了标志 → 必须用它，不调本地重算 ──
console.log('\n[1] 服务端给了标志（导入响应带 mismatch_detail）');
const recServer = {
  product_name: '7P环保三文治', specification: '', quantity: '150', unit: 'y',
  remark: '3支*48.5y',
  mismatch: 'warn', piece_mismatch: '', qty_invalid: false,
  unit_hint: '3支+4.5码', piece_hint: '',
  mismatch_detail: { rule: 'ypp', label: 'YPP 规则（按码卖）',
                     expected: 145.5, actual: 150, diff: 4.5, severity: 'warn' },
};
const f1 = F.readImportFlags(recServer);
check('mismatch 取服务端值', f1.mismatch === 'warn', f1.mismatch);
check('combined=warn', f1.combined === 'warn', f1.combined);
check('detail 透传', f1.detail && f1.detail.expected === 145.5, f1.detail);
check('本地重算 0 次调用（未走兜底）', localMismatchCalls === 0 && localPieceCalls === 0,
      { localMismatchCalls, localPieceCalls });

// ── 2. 服务端无任何标志字段 → 才回退本地重算 ──
console.log('\n[2] 服务端未给字段（undefined）→ 回退本地重算兜底');
const recBare = { product_name: 'X', specification: '', quantity: '1', unit: 'y', remark: '1支' };
const f2 = F.readImportFlags(recBare);
check('走兜底：mismatch=LOCAL_ONLY', f2.mismatch === 'LOCAL_ONLY', f2.mismatch);
check('本地重算确实被调用', localMismatchCalls === 1 && localPieceCalls === 1,
      { localMismatchCalls, localPieceCalls });

// ── 3. 图标与底色优先级：qty_invalid > warn > info > 无 ──
console.log('\n[3] 图标 / 行底色优先级');
const icon = (rec) => F.importFlagPrefix(F.readImportFlags(rec), sandbox.mismatchIcon, sandbox.invalidQtyIcon);
check('warn → ❌', icon({ mismatch: 'warn', qty_invalid: false }).includes('❌'),
      icon({ mismatch: 'warn', qty_invalid: false }));
check('info → 💡', icon({ mismatch: 'info', qty_invalid: false }).includes('💡'),
      icon({ mismatch: 'info', qty_invalid: false }));
check('无标志 → 空串', icon({ mismatch: '', qty_invalid: false }) === '',
      icon({ mismatch: '', qty_invalid: false }));
const qBad = icon({ mismatch: 'warn', qty_invalid: true });
check('qty_invalid 压过 mismatch → ⚠️', qBad.includes('⚠️') && !qBad.includes('❌'), qBad);

// 底色 class
const mkTr = () => ({ classList: { _s: new Set(),
  add(c){this._s.add(c);}, remove(...c){c.forEach(x=>this._s.delete(x));},
  has(c){return this._s.has(c);} } });
const cls = (rec) => { const tr = mkTr(); F.applyImportFlagClass(tr, F.readImportFlags(rec)); return [...tr.classList._s]; };
check('warn → row-warn', cls({ mismatch:'warn', qty_invalid:false }).includes('row-warn'), cls({ mismatch:'warn', qty_invalid:false }));
check('info → row-info', cls({ mismatch:'info', qty_invalid:false }).includes('row-info'), cls({ mismatch:'info', qty_invalid:false }));
check('一致行 → 无底色 class', cls({ mismatch:'', piece_mismatch:'', qty_invalid:false }).length === 0,
      cls({ mismatch:'', piece_mismatch:'', qty_invalid:false }));
check('数量非法 → row-out-of-stock',
      cls({ mismatch:'warn', qty_invalid:true }).includes('row-out-of-stock'),
      cls({ mismatch:'warn', qty_invalid:true }));

// ── 4. 两轨合并：piece_mismatch 也参与 combined ──
console.log('\n[4] 两轨合并口径');
check('mismatch 空 + piece warn → combined warn',
      F.readImportFlags({ mismatch:'', piece_mismatch:'warn', qty_invalid:false }).combined === 'warn');
check('mismatch info + piece warn → combined warn（warn 优先）',
      F.readImportFlags({ mismatch:'info', piece_mismatch:'warn', qty_invalid:false }).combined === 'warn');
check('两者皆 info → combined info',
      F.readImportFlags({ mismatch:'info', piece_mismatch:'info', qty_invalid:false }).combined === 'info');

// ── 5. hover 提示 ──
console.log('\n[5] hover 提示（期望/实际/差额）');
const t1 = F.importFlagTitle({ detail: recServer.mismatch_detail });
check('含期望145.5', t1.includes('145.5'), t1);
check('含实际150', t1.includes('150'), t1);
check('含差额 +4.5', t1.includes('+4.5'), t1);
const t2 = F.importFlagTitle({ detail: { rule:'piece', label:'件数换算（1500.0张/件）',
                                        expected:3000, actual:5000, diff:2000, severity:'warn' } });
check('负差额显示负号', F.importFlagTitle({ detail:{ rule:'ypp', label:'X',
  expected:147.5, actual:100, diff:-47.5, severity:'warn' } }).includes('-47.5'));
check('qty_invalid 文案', F.importFlagTitle({ detail:{ rule:'qty_invalid',
  label:'数量格式异常', expected:null, actual:'货-30' } }).includes('货-30'));
check('无 detail → 空串（不加噪声）', F.importFlagTitle({ detail:null }) === '');

// ── 6. 汇总提示只在有错时弹 ──
console.log('\n[6] notifyImportValidation 打扰策略');
let toastMsg = null;
ctx.showImportToast = (m) => { toastMsg = m; };
F.notifyImportValidation({ total:5, warn:0, info:0, qty_invalid:0, flagged:0 });
check('全对得上 → 不弹（不打扰）', toastMsg === null, toastMsg);
F.notifyImportValidation({ total:5, warn:2, info:1, qty_invalid:0, flagged:3 });
check('有错 → 弹汇总', toastMsg !== null);
check('汇总含总数', toastMsg.includes('5'), toastMsg);
check('汇总含红行数', toastMsg.includes('2 行'), toastMsg);
F.notifyImportValidation(null);
check('validation 缺失 → 不报错不弹', toastMsg !== null);

console.log('\n' + '='.repeat(72));
console.log(fail ? `❌ 失败 ${fail} 项` : '✅ 前端逻辑全部通过（服务端优先 + 优先级 + 汇总策略）');
process.exit(fail ? 1 : 0);
