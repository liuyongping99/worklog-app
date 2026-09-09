// placement_count.js — 出货页「摆放图」计数功能
// 依赖: common.js 的 escHtml
// 2026-08-15: 在「支」类商品的辅助单位提示右侧提供摆放图按钮;
//   上传的摆放图存 shipping_images(source='placement'),与 OCR 图隔离;
//   点击任意位置计 1 支,数字为固定可读尺寸,「撤销」可连续撤销。
(function () {
    'use strict';
    // 2026-08-26:make API base configurable so this script can serve shipping,
    // loading and inbound pages. Default still /api/v1/shipping-orders.
    var API = (typeof window !== 'undefined' && window.PLACEMENT_API) || '/api/v1/shipping-orders';
    var currentCountImageId = null;     // 计数弹框当前 image
    var currentRecordId = null;         // 计数弹框当前 image 所属 record(用于读累计/目标)
    var currentLooseCount = 0;          // 当前图散码数量(持久化到库)
    var currentManualCount = null;      // 当前图直接输入的支数(持久化到库;null=回退点击计数)
    var currentIsUnload = false;        // 当前图是否「卸载货物」(勾选后支数从总数扣减)
    var currentMarkScale = 1;           // 当前图计数数字的整体系缩放(放大/缩小按钮,持久化到库)
    var SCALE_STEP = 1.2;               // 每次点击缩放步进(约 ±20%)
    var SCALE_MIN = 0.3, SCALE_MAX = 4.0;
    // 2026-09-09: 计数单位参数化(支/令/张),从 record tr[data-count-unit] 读;
    //   拷贝纸=令 / 日本纸=张 / 普通支基产品=支。散码(has-loose)拷贝纸/日本纸=false。
    var currentCountUnit = '支';
    var currentHasLoose = true;

    // 读 record tr 上的计数配置(单位 + 是否有散码)
    function readCountConfig(recordId) {
        if (!recordId) return;
        var tr = document.querySelector('tr[data-record-id="' + recordId + '"]');
        if (!tr) return;
        var u = tr.getAttribute('data-count-unit');
        if (u) currentCountUnit = u;
        var hl = tr.getAttribute('data-has-loose');
        currentHasLoose = (hl !== '0');
        // 散码入口按 has-loose 显隐
        var looseBtn = el('pcmLoose');
        if (looseBtn) looseBtn.style.display = currentHasLoose ? '' : 'none';
        // 2026-09-09: 弹框静态文字参数化 — 标题/KPI unit/title/act-hint 按 currentCountUnit(支/令/张) 更新
        var cu2 = currentCountUnit;
        var t = el('pcmTitle'); if (t) t.textContent = '🧮 清点' + cu2 + '数';
        var cuu = el('pcmCurUnit'); if (cuu) cuu.textContent = cu2;
        var cmu = el('pcmCumUnit'); if (cmu) cmu.textContent = cu2;
        var man = el('pcmManual'); if (man) man.title = '直接输入' + cu2 + '数 (M) — 与点击计数等效';
        var unw = el('pcmUnloadWrap'); if (unw) unw.title = '勾选后,本图清点' + cu2 + '数以负数计入累计(卸载/退货) (U)';
        var hint = el('pcmActHint'); if (hint) hint.textContent = '点击图个位置计 1 ' + cu2 + '、数字键 1–9 直输';
    }

    // ── 工具 ──
    function el(id) { return document.getElementById(id); }
    function clamp01(v) { return Math.max(0, Math.min(1, v)); }
    // 备注栏分别解析「支」(Σ N支)与「散码」(Σ Ny,可多个累加),与清点结果分开比较
    function parseRemark(remarkText) {
        var t = remarkText || '';
        var zhi = 0, hasZhi = false, m, re;
        re = /(\d+)\s*支/g;
        while ((m = re.exec(t)) != null) { zhi += parseInt(m[1], 10); hasZhi = true; }
        var san = 0, hasSan = false;
        re = /(\d+)\s*[yY]/g;
        while ((m = re.exec(t)) != null) { san += parseInt(m[1], 10); hasSan = true; }
        return { zhi: zhi, hasZhi: hasZhi, san: san, hasSan: hasSan };
    }
    // 单类对比角标:label 为单位名(支/令/张)或「散」
    function oneCompareBadge(label, actual, expected, hasExpected) {
        if (!hasExpected) return '<span class="placement-compare-badge none">备注无' + label + '</span>';
        if (actual === expected) return '<span class="placement-compare-badge ok">✓ ' + label + ' ' + actual + ' = 备注 ' + expected + '</span>';
        if (actual < expected) return '<span class="placement-compare-badge bad">✗ ' + label + ' ' + actual + ' &lt; 备注 ' + expected + '（差 ' + (expected - actual) + '）</span>';
        return '<span class="placement-compare-badge warn">⚠ ' + label + ' ' + actual + ' &gt; 备注 ' + expected + '（多 ' + (actual - expected) + '）</span>';
    }
    // 返回支(或令/张)、散码两个独立对比角标的 HTML
    // 2026-09-09: unit/hasLoose 参数化 — 拷贝纸/日本纸无散码,只显示主单位角标
    function compareBadgesHtml(zhiTotal, sanmaTotal, remarkText, unit, hasLoose) {
        var p = parseRemark(remarkText);
        var mainBadge = oneCompareBadge(unit || '支', zhiTotal, p.zhi, p.hasZhi);
        if (!hasLoose) return mainBadge;
        return mainBadge + oneCompareBadge('散', sanmaTotal, p.san, p.hasSan);
    }

    function findPlacementArea(recordId) {
        var recTr = document.querySelector('tr[data-record-id="' + recordId + '"]');
        if (recTr) {
            var group = recTr.closest('.date-group');
            if (group) return group.querySelector('.placement-area');
        }
        var blk = document.querySelector('.placement-record-block[data-record-id="' + recordId + '"]');
        return blk ? blk.closest('.placement-area') : null;
    }

    // ── 上传回调(由共享图片弹框 _image_upload_modal.html 的 confirmUpload 触发) ──
    // 与右侧 🖼️ 核对规格按钮走同一个 imageModal,这里只处理摆放图的后续(进展示区 + 计数)
    window.placementImageUploaded = function (data, recordPk) {
        // 2026-09-06: copy-paper 上传分流(由 copy_paper.js 标记 _copyPaperUploadSource)
        if (window._copyPaperUploadSource) {
            window.copyPaperImageUploaded(data, recordPk);
            return;
        }
        if (!data || !data.success) { alert('上传失败: ' + ((data && data.error) || '未知错误')); return; }
        refreshRecordBlock(recordPk);
        // 自动打开第一张新图的计数弹框,便于立即清点
        if (data.images && data.images.length) {
            var first = data.images[0];
            openPlacementCount(first.image_id, '/upload/' + first.image, recordPk);
        }
    };

    // ── 展示区渲染 ──
    function refreshRecordBlock(recordId) {
        return fetch(API + '/records/' + recordId + '/placement-images')
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (d.success) {
                    renderPlacementBlock(recordId, d.images || []);
                    // 弹框打开且正是本记录 → 同步头部累计/目标/差(block dataset 刚被刷新)
                    if (currentRecordId && String(currentRecordId) === String(recordId)) syncHead();
                }
            });
    }

    function renderPlacementBlock(recordId, images) {
        var area = findPlacementArea(recordId);
        if (!area) return;
        var existing = area.querySelector('.placement-record-block[data-record-id="' + recordId + '"]');
        if (!images || !images.length) {
            if (existing) existing.remove();
            return;
        }
        var recTr = document.querySelector('tr[data-record-id="' + recordId + '"]');
        var recName = recTr ? (recTr.querySelector('.product-name-cell') || {}).textContent || '' : '';
        var recSpec = recTr ? (recTr.querySelector('.spec-cell') || {}).textContent || '' : '';
        // 2026-09-09: 读本 record 的计数单位(支/令/张)与是否有散码
        var unit = (recTr && recTr.getAttribute('data-count-unit')) || '支';
        var hasLoose = (!recTr || recTr.getAttribute('data-has-loose') !== '0');
        var total = 0;
        var looseTotal = 0;
        // 支数:直接输入优先,无则回退点击计数点(二者等效参与比对)。
        // 卸载货物(is_unload)时取负,从记录总数中扣减。
        function effectiveZhi(im) {
            var base = (im.manual_count != null) ? im.manual_count
                : (im.n_marks != null ? im.n_marks : (im.marks || []).length);
            return im.is_unload ? -base : base;
        }
        images.forEach(function (im) {
            total += effectiveZhi(im);
            looseTotal += (im.loose_count != null ? im.loose_count : 0);
        });
        // 支与支累加、散码与散码累加,分别与备注中的支、散码分开比较
        var remarkText = recTr ? (recTr.querySelector('.remark-cell') || {}).textContent || '' : '';
        var cmpHtml = compareBadgesHtml(total, looseTotal, remarkText, unit, hasLoose);

        var html = '<div class="placement-record-head">'
            + '<span class="placement-record-name">' + escHtml(recName) + (recSpec ? ' · ' + escHtml(recSpec) : '') + '</span>'
            + '<span class="placement-count-badge" data-total="' + total + '">已点 ' + total + ' ' + unit + '</span>'
            + cmpHtml
            + '</div><div class="placement-images">';
        images.forEach(function (im, idx) {
            var cnt = effectiveZhi(im);
            // 2026-08-24: 本行码数(qty+unit)展示在缩略图右上,与有效支数并列。
            // 来源:数据行 DOM(qty-cell/unit-cell),数据行不存在时 fallback 到空。
            var recQty = recTr ? (recTr.querySelector('.qty-cell') || {}).textContent || '' : '';
            var recUnit = recTr ? (recTr.querySelector('.unit-cell') || {}).textContent || '' : '';
            var yardLabel = recQty ? (' / ' + recQty.trim() + ' ' + (recUnit || '').trim()) : '';
            var countLabel = im.is_unload
                ? '<span class="placement-thumb-count unload">⬇ 卸载 ' + Math.abs(cnt) + ' ' + unit + '</span>'
                : '<span class="placement-thumb-count">' + cnt + ' ' + unit + '</span>'
                  + (yardLabel ? '<span class="placement-thumb-yard">' + yardLabel + '</span>' : '');
            html += '<div class="placement-thumb-wrap" data-image-id="' + im.id + '" data-record-id="' + recordId + '" data-img-index="' + (idx + 1) + '">'
                + '<img class="placement-thumb" src="/upload/' + im.relative_path + '" data-image-id="' + im.id + '" alt="摆放图">'
                + countLabel
                + (hasLoose && im.loose_count ? '<span class="placement-thumb-loose">散码 ' + im.loose_count + ' y</span>' : '')
                + '<button type="button" class="placement-del-btn lock-hide" data-image-id="' + im.id + '" title="删除此摆放图">×</button>'
                + '</div>';
        });
        html += '</div>';

        var block = document.createElement('div');
        block.className = 'placement-record-block';
        block.setAttribute('data-record-id', recordId);
        // v2: 把累计/目标/张数/散码写在 block 上,供计数弹框 syncHead 读取(多图累计对目标)
        var p = parseRemark(remarkText);
        block.setAttribute('data-total', total);
        block.setAttribute('data-expected-zhi', p.zhi);
        block.setAttribute('data-has-zhi', p.hasZhi ? '1' : '0');
        block.setAttribute('data-img-count', images.length);
        block.setAttribute('data-loose-total', looseTotal);
        block.innerHTML = html;
        if (existing) existing.replaceWith(block);
        else area.appendChild(block);
    }

    // ── 计数弹框 ──
    function openPlacementCount(imageId, src, recordId) {
        currentCountImageId = imageId;
        currentRecordId = recordId || currentUploadRecordIdFor(imageId);
        currentManualCount = null;
        readCountConfig(currentRecordId);  // 2026-09-09: 读单位(支/令/张)+散码显隐
        el('pcmImg').src = src;
        // 重置(图片加载完会按真实尺寸重排徽章)
        el('pcmMarks').innerHTML = '';
        loadImage(imageId);
        el('placementCountModal').classList.add('show');
        // v3: 身份条填记录名(品名 · 规格);数据行 tr[data-record-id] 已在表里
        fillRecordName();
        // 先按 block dataset 出一版头部(累计/目标),loadImage 完成后 renderMarks→syncHead 再刷新本图
        syncHead();
    }
    // v3 (2026-08-30): 把记录名 + 规格填进 #pcmRecordName,身份条用
    function fillRecordName() {
        if (!currentRecordId) return;
        var tr = document.querySelector('tr[data-record-id="' + currentRecordId + '"]');
        if (!tr) return;
        var name = (tr.querySelector('.product-name-cell') || {}).textContent || '';
        var spec = (tr.querySelector('.spec-cell') || {}).textContent || '';
        var node = el('pcmRecordName');
        if (node) node.textContent = (name + (spec ? ' · ' + spec : '')).trim() || '—';
    }
    function closePlacementCount() {
        el('placementCountModal').classList.remove('show');
        currentCountImageId = null;
    }
    // 获取单图(计数点 marks + 数字缩放比例)
    function loadImage(imageId) {
        fetch(API + '/placement-images/' + imageId)
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (!d.success || !d.image) return;
                currentMarkScale = (d.image.mark_scale != null ? d.image.mark_scale : 1) || 1;
                currentLooseCount = (d.image.loose_count != null ? d.image.loose_count : 0) || 0;
                currentManualCount = (d.image.manual_count != null ? d.image.manual_count : null);
                currentIsUnload = !!d.image.is_unload;
                renderLoose(currentLooseCount);
                renderManual(currentManualCount);
                renderUnload(currentIsUnload);
                renderMarks(d.image.marks || []);
            });
    }
    // 数字大小为固定可读尺寸(纯红色文字,无背景圈),直径≈图片宽度 6.75%(上一版 0.0225 的 1.5 倍)
    function estimateMarkR() {
        return 0.03375;
    }
    // 按 mark_r(相对展示区宽度的半径比例)把数字字号缩放到合适大小
    function sizeMarkEl(span, markR) {
        // 2026-08-16:用图片宽度算 mark 尺寸 - wrap 在某些 flex 布局下与图片宽度不一致
        var img = el('pcmImg');
        var W = img && img.naturalWidth ? img.getBoundingClientRect().width : 0;
        if (!W) W = 200; // 图片未就绪时的兜底
        var d = 2 * (markR || 0.03375) * W;
        if (d < 10) d = 10;
        span.style.width = d + 'px';
        span.style.height = d + 'px';
        span.style.lineHeight = d + 'px';
        span.style.fontSize = (d * 0.42) + 'px';
    }
    // 分段控件(点击计数 / 直输支数)高亮:直输模式下「直输支数」亮,否则「点击计数」亮
    function updateSegState() {
        var click = el('pcmSegClick'), man = el('pcmManual');
        if (!click || !man) return;
        if (currentManualCount != null) { click.classList.remove('on'); man.classList.add('on'); }
        else { click.classList.add('on'); man.classList.remove('on'); }
    }

    // ── 头部 + 进度条联动 ──
    // 本图计数(读 currentManualCount / marks)、本记录累计与目标(读 .placement-record-block 的 dataset)
    // 多张点数图时,差值按「累计 vs 目标」算,不拿单图计数对目标。
    // 2026-08-29: 老模板(只有 pcmBigger/pcmSmaller/pcmLoose/pcmUndo/pcmClose)里
    // 没有 pcmCumulative/pcmDiffChip/pcmProgBar/pcmLooseVal/pcmImgPosLabel/pcmCurLabel/pcmUndoTarget
    // 等新元素,syncHead 全部 null-guard 后再写。
    function syncHead() {
        var rid = currentRecordId;
        if (!rid) return;
        var block = document.querySelector('.placement-record-block[data-record-id="' + rid + '"]');
        var total = block ? (parseInt(block.getAttribute('data-total'), 10) || 0) : 0;
        var expected = block ? (parseInt(block.getAttribute('data-expected-zhi'), 10) || 0) : 0;
        var hasZhi = block ? block.getAttribute('data-has-zhi') === '1' : false;
        var imgCount = block ? (parseInt(block.getAttribute('data-img-count'), 10) || 0) : 0;
        var looseTotal = block ? (parseFloat(block.getAttribute('data-loose-total')) || 0) : 0;
        // 本图有效支数(直输优先,无则点击计数点;卸载取负)
        var curEff = (currentManualCount != null) ? currentManualCount
            : (el('pcmMarks') ? el('pcmMarks').children.length : 0);
        var curSigned = currentIsUnload ? -curEff : curEff;
        // 本图序号(第几张)
        var curIdx = 0;
        var tw = document.querySelector('.placement-thumb-wrap[data-image-id="' + currentCountImageId + '"]');
        if (tw) curIdx = parseInt(tw.getAttribute('data-img-index'), 10) || 0;

        el('pcmCount').textContent = (currentIsUnload ? '⬇ −' : '') + Math.abs(curEff);
        var cum = el('pcmCumulative');
        // v3: KPI 卡片自带 unit span,这里只写数字比值
        if (cum) cum.textContent = total + ' / ' + (hasZhi ? expected : '—');

        var chip = el('pcmDiffChip');
        if (chip) {
            var u = currentCountUnit;
            if (!hasZhi) { chip.className = 'pcm-diff-chip none'; chip.textContent = '备注无' + u + '数'; }
            else if (total === expected) { chip.className = 'pcm-diff-chip ok'; chip.textContent = '已齐 ' + total + ' ' + u; }
            else if (total < expected) { chip.className = 'pcm-diff-chip bad'; chip.textContent = '差 ' + (expected - total) + ' ' + u; }
            else { chip.className = 'pcm-diff-chip warn'; chip.textContent = '多 ' + (total - expected) + ' ' + u; }
        }

        var diffTxt = hasZhi
            ? (total === expected ? '已齐' : (total < expected ? '差 ' + (expected - total) : '多 ' + (total - expected)) + ' ' + currentCountUnit)
            : '无目标';
        var progTxt = el('pcmProgText');
        // v3: 主数字已经在卡片值显示,这里只保留 diff + 共几张
        if (progTxt) progTxt.textContent = diffTxt + ' · 共 ' + imgCount + ' 张';

        var bar = el('pcmProgBar');
        if (bar) {
            var pct = (hasZhi && expected > 0) ? Math.min(100, Math.round(total / expected * 100)) : 0;
            bar.style.width = pct + '%';
            // v3: CSS 用 .pcm-kpi-fill 系列(老 .pcm-prog-fill 已删),这里同步
            bar.className = 'pcm-kpi-fill' + (hasZhi ? (total === expected ? ' ok' : (total > expected ? ' over' : '')) : '');
        }

        var lv = el('pcmLooseVal');
        if (lv) {
            // v3: KPI 散码卡片自带 label + unit,这里只写数字,常显(0 时也显示)
            // 2026-09-09: 拷贝纸/日本纸无散码 → 隐藏散码卡片
            lv.textContent = (looseTotal || 0);
            lv.style.display = currentHasLoose ? '' : 'none';
            var looseCard = lv.closest('.pcm-kpi-card');
            if (looseCard) looseCard.style.display = currentHasLoose ? '' : 'none';
        }

        var posTxt = imgCount ? ('本图第 ' + curIdx + '/' + imgCount + ' 张') : '本图第 -/- 张';
        var imgPosLbl = el('pcmImgPosLabel');
        if (imgPosLbl) imgPosLbl.textContent = posTxt;
        // 头部「本图」标签也带上图序,更显眼
        var curLbl = el('pcmCurLabel');
        if (curLbl) curLbl.textContent = imgCount ? ('本图 · 第 ' + curIdx + '/' + imgCount + ' 张') : '本图';

        var ut = el('pcmUndoTarget');
        var layer = el('pcmMarks');
        var lastSeq = (layer && layer.children.length) ? layer.children[layer.children.length - 1].textContent : '';
        if (ut) {
            if (lastSeq) { ut.textContent = '下一撤销目标 = 第 ' + lastSeq + ' 点'; ut.style.display = ''; }
            else ut.style.display = 'none';
        }
    }

    function renderMarks(marks) {
        marks = marks || [];
        var layer = el('pcmMarks');
        layer.innerHTML = '';
        marks.forEach(function (m, i) {
            var s = document.createElement('span');
            s.className = 'pcm-mark' + (i === marks.length - 1 ? ' last' : '');
            s.style.left = (m.x_ratio * 100) + '%';
            s.style.top = (m.y_ratio * 100) + '%';
            s.textContent = m.seq;
            // 有效半径 = 该点基准 mark_r × 当前整体缩放(缩放已并入 data-mark-r,resize 时直接复用)
            var effR = (m.mark_r || 0.03375) * currentMarkScale;
            s.setAttribute('data-mark-r', effR);
            layer.appendChild(s);
            sizeMarkEl(s, effR);
        });
        // 计数显示与头部/进度联动由 syncHead 统一算(本图计数 + 累计/目标/差)
        var eff = (currentManualCount != null) ? currentManualCount : marks.length;
        el('pcmUndo').disabled = eff === 0;
        // 2026-08-21: 「直接输入支数」模式下,鼠标 cursor 改为 not-allowed,视觉提示点击已禁用
        var img = el('pcmImg');
        if (img) img.style.cursor = (currentManualCount != null) ? 'not-allowed' : 'crosshair';
        updateSegState();
        syncHead();
    }
    // 图片加载完后尺寸才确定,重排已有徽章
    function resizeAllMarks() {
        var layer = el('pcmMarks');
        if (!layer) return;
        Array.prototype.forEach.call(layer.children, function (s) {
            var mr = parseFloat(s.getAttribute('data-mark-r'));
            sizeMarkEl(s, mr);
        });
    }
    // 放大/缩小计数数字:调整整体系缩放并持久化到该图(重开弹框仍保留)
    function adjustMarkScale(factor) {
        if (!currentCountImageId) return;
        var next = currentMarkScale * factor;
        next = Math.max(SCALE_MIN, Math.min(SCALE_MAX, next));
        if (next === currentMarkScale) return;
        fetch(API + '/placement-images/' + currentCountImageId + '/mark-scale', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ scale: next })
        }).then(function (r) { return r.json(); }).then(function (d) {
            if (!d.success) { alert('调整失败: ' + (d.error || '')); return; }
            currentMarkScale = d.scale != null ? d.scale : next;
            // 后端已按新比例返回 marks(每点 mark_r 为基准),renderMarks 会乘 currentMarkScale
            renderMarks(d.marks || []);
        }).catch(function (e) { alert('调整异常: ' + e); });
    }
    // 散码数量:只记本图状态(累计散码由 syncHead 读 block dataset 统一显示)
    function renderLoose(count) {
        currentLooseCount = count || 0;
    }
    function openLooseModal() {
        if (!currentCountImageId) return;
        var input = el('looseCountInput');
        input.value = currentLooseCount > 0 ? currentLooseCount : '';
        el('looseCountModal').classList.add('show');
        input.focus();
    }
    function closeLooseModal() {
        el('looseCountModal').classList.remove('show');
    }
    function saveLooseCount() {
        if (!currentCountImageId) { closeLooseModal(); return; }
        var raw = (el('looseCountInput').value || '').trim();
        // 2026-08-21: 支持小数(0.5 等),parseInt 会截成 0 看不见
        var count = raw === '' ? 0 : (parseFloat(raw) || 0);
        if (count < 0) count = 0;
        // 保留 1 位小数(0.5 / 1.2 都行;不让 0.05 这种更小精度进来)
        count = Math.round(count * 10) / 10;
        fetch(API + '/placement-images/' + currentCountImageId + '/loose-count', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ count: count })
        }).then(function (r) { return r.json(); }).then(function (d) {
            if (!d.success) { alert('保存失败: ' + (d.error || '')); return; }
            renderLoose(d.count != null ? d.count : count);
            closeLooseModal();
            // 同步刷新主页面缩略图角标(录入散码后列表需要立即显示)
            var rid = currentUploadRecordIdFor(currentCountImageId);
            if (rid) refreshRecordBlock(rid);
            // 散码对上备注 → 即时点亮「点数」按钮绿框 / 「✅ 点数匹配」标记
            if (d.placement_match !== undefined) updatePlacementMatch(d.record_id, d.placement_match);
        }).catch(function (e) { alert('保存异常: ' + e); });
    }
    // 直输支数:pcmManualVal 已移除,这里只同步分段控件高亮(直输/点击)
    function renderManual(count) {
        updateSegState();
    }
    // 录入散码/手动支数后,即时同步主表「点数」按钮绿框 + 「✅ 点数匹配」标记(锁定后也可见)
    function updatePlacementMatch(recordId, pm) {
        if (recordId == null) return;
        var tr = document.querySelector('tr[data-record-id="' + recordId + '"]');
        var td = tr ? tr.querySelector('.actions-cell') : null;
        var btn = document.querySelector('.placement-add-btn[data-record-id="' + recordId + '"]');
        if (btn) {
            if (pm) btn.classList.add('placement-ok'); else btn.classList.remove('placement-ok');
        }
        var marker = document.querySelector('.placement-match-marker[data-placement-match-marker="' + recordId + '"]');
        if (pm) {
            if (!marker && td) {
                marker = document.createElement('span');
                marker.className = 'placement-match-marker';
                marker.setAttribute('data-placement-match-marker', recordId);
                marker.title = '点数已与备注核对一致';
                marker.textContent = '✅ 点数匹配';
                td.appendChild(marker);
            }
        } else if (marker) {
            marker.remove();
        }
    }
    // 卸载货物勾选项渲染(勾选后支数从总数扣减)
    function renderUnload(flag) {
        var cb = el('pcmUnload');
        var wrap = el('pcmUnloadWrap');
        if (!cb || !wrap) return;
        currentIsUnload = !!flag;
        cb.checked = currentIsUnload;
        if (currentIsUnload) wrap.classList.add('checked');
        else wrap.classList.remove('checked');
    }
    // 勾选 / 取消「卸载货物」:持久化到库并即时刷新计数显示与比对角标
    function toggleUnload() {
        if (!currentCountImageId) return;
        var cb = el('pcmUnload');
        var flag = cb.checked;
        fetch(API + '/placement-images/' + currentCountImageId + '/unload', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ unload: flag })
        }).then(function (r) { return r.json(); }).then(function (d) {
            if (!d.success) {
                alert('设置失败: ' + (d.error || ''));
                cb.checked = !flag;
                renderUnload(!flag);
                return;
            }
            currentIsUnload = !!d.is_unload;
            renderUnload(currentIsUnload);
            loadImage(currentCountImageId);   // 重渲计数显示(体现 卸载 -N)
            var rid = currentUploadRecordIdFor(currentCountImageId);
            if (rid) refreshRecordBlock(rid); // 立即刷新列表比对角标(总数已扣减)
            if (d.placement_match !== undefined) updatePlacementMatch(d.record_id, d.placement_match);
        }).catch(function (e) { alert('异常: ' + e); });
    }
    function openManualModal() {
        if (!currentCountImageId) return;
        var eff = (currentManualCount != null) ? currentManualCount
            : (el('pcmMarks') ? el('pcmMarks').children.length : 0);
        el('manualCountInput').value = (eff > 0) ? eff : '';
        el('manualCountModal').classList.add('show');
        el('manualCountInput').focus();
    }
    function closeManualModal() {
        el('manualCountModal').classList.remove('show');
    }
    function saveManualCount() {
        if (!currentCountImageId) { closeManualModal(); return; }
        var raw = (el('manualCountInput').value || '').trim();
        var count = (raw === '') ? null : parseInt(raw, 10);
        if (count != null && (isNaN(count) || count < 0)) count = null;
        fetch(API + '/placement-images/' + currentCountImageId + '/manual-count', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ count: count })
        }).then(function (r) { return r.json(); }).then(function (d) {
            if (!d.success) { alert('保存失败: ' + (d.error || '')); return; }
            currentManualCount = (d.manual_count != null) ? d.manual_count : null;
            renderManual(currentManualCount);
            closeManualModal();
            loadImage(currentCountImageId);   // 同步计数点(直接输入会清空 marks)
            var rid = currentUploadRecordIdFor(currentCountImageId);
            if (rid) refreshRecordBlock(rid); // 立即刷新列表比对角标
            if (d.placement_match !== undefined) updatePlacementMatch(d.record_id, d.placement_match);
        }).catch(function (e) { alert('保存异常: ' + e); });
    }
    // 清空直接输入支数(回退到点击计数)
    function clearManualCount() {
        if (!currentCountImageId) return;
        // 关闭弹框(对齐移动端 manualClear 行为)
        closeManualModal();
        fetch(API + '/placement-images/' + currentCountImageId + '/manual-count', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ count: null })
        }).then(function (r) { return r.json(); }).then(function (d) {
            if (!d.success) { alert('清除失败: ' + (d.error || '')); return; }
            currentManualCount = null;
            renderManual(null);
            loadImage(currentCountImageId);
            var rid = currentUploadRecordIdFor(currentCountImageId);
            if (rid) refreshRecordBlock(rid);
            if (d.placement_match !== undefined) updatePlacementMatch(d.record_id, d.placement_match);
        }).catch(function (e) { alert('清除异常: ' + e); });
    }
    function addMark(imageId, x, y, markR) {
        fetch(API + '/placement-images/' + imageId + '/marks', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ x_ratio: x, y_ratio: y, r: markR })
        }).then(function (r) { return r.json(); }).then(function (d) {
            if (!d.success) { alert('计数失败: ' + (d.error || '')); return; }
            renderMarks(d.marks || []);
            refreshRecordBlock(currentUploadRecordIdFor(imageId));
            if (d.placement_match !== undefined) updatePlacementMatch(d.record_id, d.placement_match);
        }).catch(function (e) { alert('计数异常: ' + e); });
    }
    // 由 imageId 反查其所属 record(缩略图 data-record-id)
    function currentUploadRecordIdFor(imageId) {
        var wrap = document.querySelector('.placement-thumb-wrap[data-image-id="' + imageId + '"]');
        return wrap ? wrap.getAttribute('data-record-id') : null;
    }
    function undoMark(imageId) {
        fetch(API + '/placement-images/' + imageId + '/marks/last', { method: 'DELETE' })
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (!d.success) { alert('撤销失败: ' + (d.error || '')); return; }
                renderMarks(d.marks || []);
                refreshRecordBlock(currentUploadRecordIdFor(imageId));
                if (d.placement_match !== undefined) updatePlacementMatch(d.record_id, d.placement_match);
            }).catch(function (e) { alert('撤销异常: ' + e); });
    }
    function deletePlacementImage(imageId) {
        if (!confirm('确定删除这张摆放图？其计数点也会一并删除。')) return;
        fetch(API + '/placement-images/' + imageId, { method: 'DELETE' })
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (!d.success) { alert('删除失败: ' + (d.error || '')); return; }
                var wrap = document.querySelector('.placement-thumb-wrap[data-image-id="' + imageId + '"]');
                var recordId = wrap ? wrap.getAttribute('data-record-id') : null;
                if (recordId) refreshRecordBlock(recordId);
            });
    }
    // 轻提示(不阻断)
    var toastTimer = null;
    function showToast(msg) {
        var t = el('pcmToast');
        if (!t) {
            t = document.createElement('div');
            t.id = 'pcmToast';
            t.style.cssText = 'position:absolute;left:50%;top:12px;transform:translateX(-50%);'
                + 'background:rgba(0,0,0,0.78);color:#fff;padding:0.4rem 0.9rem;border-radius:6px;'
                + 'font-size:0.85rem;z-index:10;pointer-events:none;transition:opacity .2s;';
            el('placementCountModal').appendChild(t);
        }
        t.textContent = msg;
        t.style.opacity = '1';
        if (toastTimer) clearTimeout(toastTimer);
        toastTimer = setTimeout(function () { t.style.opacity = '0'; }, 1600);
    }

    // ── 事件绑定 ──
    function bind() {
        // 委托:缩略图 / 删除(摆放图按钮走 onclick 直接调 openPlacementImageModal,复用 imageModal)
        document.addEventListener('click', function (e) {
            var delBtn = e.target.closest('.placement-del-btn');
            if (delBtn) { deletePlacementImage(delBtn.getAttribute('data-image-id')); return; }
            var thumb = e.target.closest('.placement-thumb');
            if (thumb) {
                var wrap = thumb.closest('.placement-thumb-wrap');
                var rid = wrap ? wrap.getAttribute('data-record-id') : null;
                openPlacementCount(thumb.getAttribute('data-image-id'), thumb.getAttribute('src'), rid);
                return;
            }
        });

        // 2026-08-29: 老模板里没有 pcmManual/pcmUnload/manualCountModal 等新元素,
// 加 helper 在元素缺失时跳过,而不是直接抛 TypeError 中断初始化。
        function bindIf(id, evt, fn) {
            var e = el(id);
            if (e) e.addEventListener(evt, fn);
        }
        bindIf('pcmClose', 'click', closePlacementCount);
        bindIf('pcmBigger', 'click', function () {
            if (currentCountImageId) adjustMarkScale(SCALE_STEP);
        });
        bindIf('pcmSmaller', 'click', function () {
            if (currentCountImageId) adjustMarkScale(1 / SCALE_STEP);
        });
        bindIf('pcmLoose', 'click', openLooseModal);
        // 2026-08-21: 直接输入支数 UI 已暴露在模板,移除 null-guard;
        // 清空按钮(回退到点击计数)直接绑 clearManualCount
        bindIf('pcmManual', 'click', openManualModal);
        bindIf('pcmUnload', 'change', toggleUnload);
        bindIf('manualCountSave', 'click', saveManualCount);
        bindIf('manualCountCancel', 'click', closeManualModal);
        bindIf('manualCountClear', 'click', clearManualCount);
        bindIf('manualCountInput', 'keydown', function (e) {
            if (e.key === 'Enter') { e.preventDefault(); saveManualCount(); }
        });
        bindIf('manualCountModal', 'click', function (e) {
            if (e.target === this) closeManualModal();
        });
        bindIf('looseCountSave', 'click', saveLooseCount);
        bindIf('looseCountCancel', 'click', closeLooseModal);
        bindIf('looseCountInput', 'keydown', function (e) {
            if (e.key === 'Enter') { e.preventDefault(); saveLooseCount(); }
        });
        bindIf('looseCountModal', 'click', function (e) {
            if (e.target === this) closeLooseModal();
        });
        bindIf('pcmUndo', 'click', function () {
            if (!currentCountImageId) return;
            // 直接输入模式下,「撤销」改为清空直接输入,回退到点击计数
            if (currentManualCount != null) { clearManualCount(); return; }
            undoMark(currentCountImageId);
        });
        // 点击图片:任意位置都计 1 支,数字为固定可读尺寸
        el('pcmImg').addEventListener('click', function (e) {
            if (!currentCountImageId) return;
            // 2026-08-21: 「直接输入支数」模式下,鼠标点击禁用(忽略,不切换回点击计数),
            // 用「撤销」或「直接输入弹框」中的「清空」才能回退到点击计数。
            if (currentManualCount != null) return;
            // 2026-08-16:用图片 rect 而非 wrap rect 算点击坐标 -
            // wrap 在 flex 父容器里曾被拉伸到 max-height,导致下半部点击 cy 被 clamp 到 1.0,数字与点击位置错位
            var img = el('pcmImg');
            var rect = img.getBoundingClientRect();
            var cx = clamp01((e.clientX - rect.left) / rect.width);
            var cy = clamp01((e.clientY - rect.top) / rect.height);
            // 数字大小固定可读尺寸(不受圆柱识别影响)
            var markR = estimateMarkR();
            addMark(currentCountImageId, cx, cy, markR);
        });
        // 图片加载完后,按真实尺寸重排徽章
        el('pcmImg').addEventListener('load', resizeAllMarks);
        el('placementCountModal').addEventListener('click', function (e) {
            if (e.target === this) closePlacementCount();
        });
        // 「点击计数」分段按钮:直输模式下点它 = 清空直输,回退到点击计数
        bindIf('pcmSegClick', 'click', function () {
            if (currentManualCount != null) clearManualCount();
        });
        document.addEventListener('keydown', function (e) {
            // 在散码/直输输入框内只保留 Esc 关弹框,其余快捷键不抢
            var tag = (e.target.tagName || '').toLowerCase();
            if (tag === 'input' || tag === 'textarea') {
                if (e.key === 'Escape') closePlacementCount();
                return;
            }
            if (!el('placementCountModal').classList.contains('show')) return;
            // 旧模板没有这些元素时,快捷键对应的 click() 也不能直接调,否则 null.click() 会抛
            function clickIf(id) { var x = el(id); if (x) x.click(); }
            switch (e.key) {
                case 'Escape': closePlacementCount(); break;
                case 'z': case 'Z': e.preventDefault(); clickIf('pcmUndo'); break;
                case 'u': case 'U': e.preventDefault(); clickIf('pcmUnload'); break;
                case 'l': case 'L': e.preventDefault(); if (currentHasLoose) openLooseModal(); break;
                case 'm': case 'M': e.preventDefault(); openManualModal(); break;
                case '+': case '=': e.preventDefault(); if (currentCountImageId) adjustMarkScale(SCALE_STEP); break;
                case '-': case '_': e.preventDefault(); if (currentCountImageId) adjustMarkScale(1 / SCALE_STEP); break;
                default:
                    if (/^[1-9]$/.test(e.key)) {
                        e.preventDefault();
                        openManualModal();
                        var inp = el('manualCountInput');
                        inp.value = e.key;
                        inp.focus();
                    }
            }
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bind);
    } else {
        bind();
    }
})();
