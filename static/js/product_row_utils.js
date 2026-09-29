// ============================================================
// Product row utilities (shared by shipping / loading / inbound)
// ============================================================
// 三个页面共享的商品行明细工具: 出货 / 装柜 / 入库
//   - 数据计算: 件数转换、单位/数量校验、辅助单位提示、备注→数量自动填充
//   - 视觉/样式: mismatch icon、invalid qty icon、row 样式 class
//   - 行内编辑: 开始编辑 / 收集编辑数据 / 应用编辑后的 row
//   - 操作交互: 上移/下移 / 删除 / 备注汇总刷新
//   - 汇总行: refreshRemarkSummary 支持两种布局:
//       'row-after-table' (出货, div.summary-row 跟在 .record-table 之后)
//       'tbody-tr'        (装柜/入库, tr.summary-row 在 tbody 末尾)
//
// 依赖 (来自 common.js):
//   escHtml / escAttr / confirmDialog / showLoading / hideLoading
//   showBtnLoading / hideBtnLoading
//
// 全局依赖:
//   window.PIECE_CONVERSIONS  - 后端注入的件数转换规则
//   ProductRowUtils           - 本工具命名空间
//
// 用法:
//   <script src="/static/js/product_row_utils.js"></script>
//   var u = ProductRowUtils.findUnit(UNIT_LIST, name, spec);
//   ProductRowUtils.bindMoveButtons('/api/v1/shipping-orders');
//   ProductRowUtils.refreshRemarkSummary(dateGroup, 'row-after-table');
// ============================================================

(function (global) {
    'use strict';

    // ---- 内部小工具 ----
    function _safeText(s) { return String(s == null ? '' : s); }
    function _escAttrValue(s) { return (typeof escAttr === 'function') ? escAttr(s) : _safeText(s).replace(/"/g, '&quot;'); }

    // ====================================================================
    // 1. 件数转换 (PIECE_CONVERSIONS 全局) - 出货/装柜/入库逻辑一致
    // ====================================================================
    function findPieceConversion(productName, spec) {
        var list = global.PIECE_CONVERSIONS || [];
        if (spec) {
            for (var i = 0; i < list.length; i++) {
                var pc = list[i];
                if (pc.product_name === productName && pc.spec_keyword && spec.indexOf(pc.spec_keyword) !== -1) {
                    return pc;
                }
            }
        }
        for (var j = 0; j < list.length; j++) {
            var pc2 = list[j];
            if (pc2.product_name === productName && !pc2.spec_keyword) {
                return pc2;
            }
        }
        return null;
    }

    function extractPiecesFromRemark(remark) {
        if (!remark) return null;
        var m = remark.match(/(\d+)件/);
        return m ? parseInt(m[1]) : null;
    }

    function getPieceHint(productName, spec) {
        var pc = findPieceConversion(productName, spec);
        if (pc) return pc.units_per_piece + pc.target_unit + '/件';
        return '';
    }

    function calcPieceQuantity(remark, productName, spec) {
        var pieces = extractPiecesFromRemark(remark);
        if (pieces === null) return null;
        var pc = findPieceConversion(productName, spec);
        if (!pc) return null;
        return pieces * pc.units_per_piece;
    }

    function checkPieceMismatch(remark, qty, productName, spec) {
        var pieces = extractPiecesFromRemark(remark);
        if (pieces === null) return '';
        var pc = findPieceConversion(productName, spec);
        if (!pc) return '';
        var expected = pieces * pc.units_per_piece;
        var actual = parseFloat(qty);
        if (isNaN(actual)) return '';
        if (Math.abs(expected - actual) <= 0.01) return '';
        return pieces === 1 ? 'info' : 'warn';
    }

    // ====================================================================
    // 2. 单位/数量校验 (传入 unitList) - 出货/装柜/入库逻辑一致
    // ====================================================================
    function findUnit(unitList, productName, spec) {
        var list = unitList || [];
        if (spec) {
            for (var i = 0; i < list.length; i++) {
                var u = list[i];
                if (u.product_name === productName && u.spec_keyword && spec.indexOf(u.spec_keyword) !== -1) {
                    return u;
                }
            }
        }
        for (var j = 0; j < list.length; j++) {
            var u2 = list[j];
            if (u2.product_name === productName && !u2.spec_keyword) {
                return u2;
            }
        }
        return null;
    }

    function getUnitHint(unitList, productName, spec, quantity, unit, remark) {
        var qty = parseFloat(quantity);
        if (isNaN(qty) || qty <= 0) return '';
        if (unit === '支') {
            if (qty === Math.floor(qty)) return Math.floor(qty) + '支';
            return qty + '支';
        }
        var info = findUnit(unitList, productName, spec);
        if (info && info.is_usingyardforcounting && info.yards_per_piece) {
            var ypp = info.yards_per_piece;
            var pieces = Math.floor(qty / ypp);
            var remainder = qty - (pieces * ypp);
            if (remainder < 0.01) return pieces + '支';
            return pieces + '支' + (Math.round(remainder * 100) / 100) + '码';
        }
        if (remark) {
            var rm = remark.match(/(\d+)支/);
            if (rm) return rm[1] + '支';
        }
        return '';
    }

    function checkMismatch(unitList, remark, quantity, productName, spec) {
        var unit = findUnit(unitList, productName, spec);
        if (!unit || !unit.is_usingyardforcounting || unit.yards_per_piece <= 0) return '';
        if (!remark) return '';
        var m = remark.match(/(\d+)支/);
        if (!m) {
            var yardVals = remark.match(/(\d+(?:\.\d+)?)\s*[yY码]/g);
            if (yardVals) {
                var sumYards = 0;
                for (var j = 0; j < yardVals.length; j++) {
                    sumYards += parseFloat(yardVals[j].match(/[\d.]+/)[0]);
                }
                var actualYards = parseFloat(quantity);
                if (!isNaN(actualYards) && Math.abs(sumYards - actualYards) > 0.01) return 'warn';
            }
            return '';
        }
        var pieces = parseInt(m[1]);
        var ypp = unit.yards_per_piece;

        var perPieceYards = null;
        var perPieceMatch = remark.match(/(\d+(?:\.\d+)?)\s*[yY码]\s*\*\s*(\d+)支/);
        if (!perPieceMatch) {
            perPieceMatch = remark.match(/(\d+)支\s*\*\s*(\d+(?:\.\d+)?)\s*[yY码]/);
            if (perPieceMatch) perPieceYards = parseFloat(perPieceMatch[2]);
        } else {
            perPieceYards = parseFloat(perPieceMatch[1]);
        }

        var remarkWithoutMul = remark
            .replace(/(\d+(?:\.\d+)?)\s*[yY码]\s*\*\s*(\d+)支/g, '')
            .replace(/(\d+)支\s*\*\s*(\d+(?:\.\d+)?)\s*[yY码]/g, '');
        var loose = 0;
        var yardRe = /(\d+(?:\.\d+)?)\s*[yY码]/g;
        var ym;
        while ((ym = yardRe.exec(remarkWithoutMul)) !== null) {
            loose += parseFloat(ym[1]);
        }

        var yardsPerPiece = perPieceYards != null ? perPieceYards : ypp;
        var expected = pieces * yardsPerPiece + loose;
        var actual = parseFloat(quantity);
        if (isNaN(actual)) return '';
        if (Math.abs(expected - actual) <= 0.01) return '';
        return pieces === 1 ? 'info' : 'warn';
    }

    // ====================================================================
    // 3. 视觉/样式 - 与出货页面已运行的实现保持一致
    // ====================================================================
    function mismatchIcon(level) {
        if (level === 'warn') return '⚠️';
        if (level === 'info') return 'ℹ️ ';
        return '';
    }

    function applyMismatchClass(tr, level) {
        if (!tr || !tr.classList) return;
        tr.classList.remove('row-warn', 'row-info');
        if (level) tr.classList.add(level === 'warn' ? 'row-warn' : 'row-info');
    }

    function isInvalidQty(quantity) {
        var raw = _safeText(quantity).trim();
        if (!raw) return true;
        return isNaN(parseFloat(raw)) || !isFinite(raw);
    }

    function invalidQtyIcon() {
        return '<span class="stock-icon" title="数量异常">⚠️</span>';
    }

    function applyInvalidQtyClass(tr, quantity) {
        if (!tr) return;
        var bad = isInvalidQty(quantity);
        tr.classList.toggle('row-out-of-stock', bad);
        var firstTd = tr.querySelector('td');
        if (firstTd) {
            var txt = firstTd.textContent || '';
            txt = txt.replace(/^[\s\S]*?(?=\d)/, '');
            firstTd.innerHTML = (bad ? invalidQtyIcon() : '') + txt;
        }
    }

    // ====================================================================
    // 4. 备注→数量自动填充
    //    兼容三种模式:
    //      a) 行内编辑 input: name="product_name" / "spec" / "quantity" / "remark"
    //      b) smart-add form: name="product_name" / "specification" / "quantity" / "remark"
    //      c) 显示行: .product-name-cell (或 .product-name) + .spec-cell (或 .spec)
    // ====================================================================
    function autoFillQtyFromRemark(inputEl) {
        if (!inputEl) return;
        var row = inputEl.closest('tr');
        if (!row) return;
        var remark = inputEl.value.trim();
        var pieces = extractPiecesFromRemark(remark);
        if (pieces === null) return;
        if (/[+*]/.test(remark)) return;

        var productName = '';
        var spec = '';
        var nameInput = row.querySelector('input[name="product_name"], input[name="name"]');
        var specInput = row.querySelector('input[name="specification"], input[name="spec"]');
        if (nameInput) productName = nameInput.value.trim();
        else {
            var nameCell = row.querySelector('.product-name-cell, .product-name');
            if (nameCell) productName = nameCell.textContent.replace(/^[^\w\u4e00-\u9fa5]*/, '').trim();
        }
        if (specInput) spec = specInput.value.trim();
        else {
            var specCell = row.querySelector('.spec-cell, .spec');
            if (specCell) spec = specCell.textContent.trim();
        }

        var pc = findPieceConversion(productName, spec);
        if (!pc) return;
        var qtyInput = row.querySelector(
            'input[name="quantity"], input[name="qty"], input[data-field="quantity"]'
        );
        if (!qtyInput) return;
        if (!qtyInput.dataset.userEdited) {
            qtyInput.value = pieces * pc.units_per_piece;
        }
    }

    // ====================================================================
    // 5. 备注汇总行
    //    mode: 'row-after-table' (出货) | 'tbody-tr' (装柜/入库)
    // ====================================================================
    function refreshRemarkSummary(dateGroup, mode) {
        if (!dateGroup) return;
        mode = mode || 'row-after-table';
        var tbody = dateGroup.querySelector('.record-table tbody');
        var rows = tbody ? tbody.querySelectorAll('tr:not(.quick-add-row):not(.summary-row)') : [];
        var totalPieces = 0, totalLoosePieces = 0, totalUnitPieces = 0;
        rows.forEach(function (row) {
            var remarkCell = row.querySelector('.remark-cell');
            if (remarkCell) {
                var remark = remarkCell.textContent.trim();
                if (remark && remark !== '-') {
                    var m = remark.match(/(\d+)支/);
                    if (m) totalPieces += parseInt(m[1]);
                    var looseMatches = remark.match(/\d+(?:\.\d+)?[yY码]/g);
                    if (looseMatches) totalLoosePieces += looseMatches.length;
                }
            }
            var hintCell = row.querySelector('.unit-hint-cell');
            if (hintCell) {
                var hintText = hintCell.textContent.trim();
                var hm = hintText.match(/(\d+(?:\.\d+)?)支/);
                if (hm) totalUnitPieces += parseFloat(hm[1]);
            }
        });

        if (totalUnitPieces === Math.floor(totalUnitPieces)) {
            totalUnitPieces = Math.floor(totalUnitPieces);
        }

        if (totalPieces === 0 && totalLoosePieces === 0 && totalUnitPieces === 0) {
            var oldRow = (mode === 'tbody-tr')
                ? tbody.querySelector('.summary-row')
                : dateGroup.querySelector('.summary-row');
            if (oldRow) oldRow.remove();
            return;
        }

        var summaryText1 = totalPieces + '支';
        if (totalLoosePieces > 0) summaryText1 += '+' + totalLoosePieces + '支零码';
        var summaryText2 = totalUnitPieces + '支';

        if (mode === 'tbody-tr') {
            var summaryRow = tbody.querySelector('.summary-row');
            if (!summaryRow) {
                summaryRow = document.createElement('tr');
                summaryRow.className = 'summary-row';
                summaryRow.style.cssText = 'background:#f0fdf4;font-weight:600;border-top:2px solid #00b894;';
                var thead = tbody.parentNode.querySelector('thead tr');
                var colspan = (thead && thead.children.length) || 9;
                summaryRow.innerHTML =
                    '<td colspan="' + Math.max(1, colspan - 5) + '" style="text-align:right;padding:0.6rem 0.75rem;color:#00b894;font-size:0.9rem;">备注支数:</td>' +
                    '<td style="padding:0.6rem 0.75rem;color:#2d3436;font-size:0.9rem;">' + summaryText1 + '</td>' +
                    '<td colspan="2" style="padding:0.6rem 0.75rem;color:#00b894;font-size:0.9rem;">明细支数:' + summaryText2 + '</td>' +
                    '<td style="padding:0.6rem 0.75rem;"></td>';
                tbody.appendChild(summaryRow);
            } else {
                var cells = summaryRow.querySelectorAll('td');
                if (cells.length >= 4) {
                    cells[1].textContent = summaryText1;
                    cells[2].textContent = '明细支数:' + summaryText2;
                }
            }
        } else {
            var summaryDiv = dateGroup.querySelector('.summary-row');
            if (!summaryDiv) {
                summaryDiv = document.createElement('div');
                summaryDiv.className = 'summary-row';
                summaryDiv.style.cssText = 'background:#f0fdf4;font-weight:600;border-top:2px solid #00b894;padding:0.6rem 0.75rem;display:flex;justify-content:flex-end;align-items:center;gap:1.5rem;flex-wrap:wrap;';
                summaryDiv.innerHTML =
                    '<span style="color:#00b894;font-size:0.9rem;">备注支数:<span class="summary-pieces-val" style="color:#2d3436;"></span></span>' +
                    '<span style="color:#00b894;font-size:0.9rem;white-space:nowrap;">明细支数:<span class="summary-unit-pieces-val"></span>支</span>';
                var recordTable = dateGroup.querySelector('.record-table');
                recordTable.parentNode.insertBefore(summaryDiv, recordTable.nextSibling);
            }
            summaryDiv.querySelector('.summary-pieces-val').textContent = summaryText1;
            summaryDiv.querySelector('.summary-unit-pieces-val').textContent = summaryText2;
        }
    }

    // ====================================================================
    // 6. 行内编辑 - 把行内各 td 切换为 input
    //    配套 collectInlineEdit() 在保存时收集数据
    //    依赖 DOM 结构 (出货/装柜/入库 均统一为):
    //      .product-name-cell / .spec-cell / .qty-cell / .unit-cell / .remark-cell / .unit-hint-cell
    // ====================================================================
    function beginInlineEdit(tr) {
        if (!tr) return null;
        var nameTd = tr.querySelector('.product-name-cell');
        var specTd = tr.querySelector('.spec-cell');
        var qtyTd = tr.querySelector('.qty-cell');
        var unitTd = tr.querySelector('.unit-cell');
        var remarkTd = tr.querySelector('.remark-cell');
        var vals = [
            nameTd ? nameTd.textContent.trim() : '',
            specTd ? specTd.textContent.trim() : '',
            qtyTd ? qtyTd.textContent.trim() : '',
            unitTd ? unitTd.textContent.trim() : '',
            remarkTd ? (remarkTd.textContent.trim() === '-' ? '' : remarkTd.textContent.trim()) : ''
        ];
        if (nameTd) nameTd.innerHTML = '<input type="text" class="form-control form-control-sm edit-input" style="min-width:100px;" value="' + _escAttrValue(vals[0]) + '">';
        if (specTd) specTd.innerHTML = '<input type="text" class="form-control form-control-sm edit-input" style="min-width:80px;" value="' + _escAttrValue(vals[1]) + '">';
        if (qtyTd) qtyTd.innerHTML = '<input type="number" class="form-control form-control-sm edit-input" style="min-width:70px;" value="' + _escAttrValue(vals[2]) + '">';
        var unitOpts = ['支', 'kg', 'y', '板', '箱', '件', '筒', '本', '卷', '张', '片'];
        var unitHtml = '<select class="form-control form-control-sm edit-input" style="width:70px;">';
        unitOpts.forEach(function (u) {
            unitHtml += '<option value="' + u + '"' + (u === vals[3] ? ' selected' : '') + '>' + u + '</option>';
        });
        unitHtml += '</select>';
        if (unitTd) unitTd.innerHTML = unitHtml;
        if (remarkTd) remarkTd.innerHTML = '<input type="text" class="form-control form-control-sm edit-input" style="min-width:60px;" value="' + _escAttrValue(vals[4]) + '" oninput="autoFillQtyFromRemark(this)">';
        return vals;
    }

    function collectInlineEdit(tr) {
        if (!tr) return null;
        var inputs = tr.querySelectorAll('.edit-input');
        if (inputs.length < 5) return null;
        var unit = inputs[3].value;
        if (unit === '码') unit = 'y';
        return {
            product_name: inputs[0].value,
            specification: inputs[1].value,
            quantity: inputs[2].value,
            unit: unit,
            remark: inputs[4].value
        };
    }

    function applyEditedRow(tr, rec, unitList, refreshSummaryFn) {
        if (!tr || !rec) return;
        var newMismatch = checkMismatch(unitList, rec.remark || '', rec.quantity, rec.product_name, rec.specification || '');
        var bad = isInvalidQty(rec.quantity);
        var firstTd = tr.querySelector('td');
        var rowNum = firstTd ? ((firstTd.textContent || '').replace(/^[\s\S]*?(?=\d)/, '')) : '';
        var newPieceHint = getPieceHint(rec.product_name, rec.specification || '');
        var newPieceMismatch = checkPieceMismatch(rec.remark || '', rec.quantity, rec.product_name, rec.specification || '');
        var combinedMismatch = '';
        if (newMismatch === 'warn' || newPieceMismatch === 'warn') combinedMismatch = 'warn';
        else if (newMismatch === 'info' || newPieceMismatch === 'info') combinedMismatch = 'info';
        if (firstTd) firstTd.innerHTML = (bad ? invalidQtyIcon() : mismatchIcon(combinedMismatch)) + rowNum;

        var nameTd = tr.querySelector('.product-name-cell');
        var specTd = tr.querySelector('.spec-cell');
        var qtyTd = tr.querySelector('.qty-cell');
        var unitTd = tr.querySelector('.unit-cell');
        var remarkTd = tr.querySelector('.remark-cell');
        var unitHintTd = tr.querySelector('.unit-hint-cell');

        var oldBadgeHtml = tr.dataset.matchBadgeHtml || '';
        if (nameTd) {
            nameTd.textContent = rec.product_name;
            if (oldBadgeHtml) nameTd.insertAdjacentHTML('afterbegin', oldBadgeHtml);
        }
        if (specTd) specTd.textContent = rec.specification || '';
        if (qtyTd) qtyTd.innerHTML = '<span class="qty-badge">' + (typeof escHtml === 'function' ? escHtml(rec.quantity) : _safeText(rec.quantity)) + '</span>';
        if (unitTd) unitTd.innerHTML = '<span class="unit-badge">' + (typeof escHtml === 'function' ? escHtml(rec.unit) : _safeText(rec.unit)) + '</span>';
        if (remarkTd) {
            remarkTd.textContent = rec.remark || '-';
            remarkTd.className = 'remark-cell' + (rec.remark ? ' has-content' : '');
        }
        var hint = getUnitHint(unitList, rec.product_name, rec.specification || '', rec.quantity, rec.unit, rec.remark) || '-';
        if (unitHintTd) {
            var hintEsc = (typeof escHtml === 'function') ? escHtml(hint) : _safeText(hint);
            var pieceEsc = (typeof escHtml === 'function') ? escHtml(newPieceHint) : _safeText(newPieceHint);
            unitHintTd.innerHTML = hintEsc + (newPieceHint ? '<br><span style="color:#0984e3;">' + pieceEsc + '</span>' : '');
        }
        if (newMismatch === 'warn' || newPieceMismatch === 'warn') applyMismatchClass(tr, 'warn');
        else if (newMismatch === 'info' || newPieceMismatch === 'info') applyMismatchClass(tr, 'info');
        else applyMismatchClass(tr, '');
        applyInvalidQtyClass(tr, rec.quantity);
        tr.setAttribute('data-record-id', rec.id);

        if (typeof refreshSummaryFn === 'function') {
            refreshSummaryFn(tr.closest('.date-group'));
        }
        if (typeof global.addRowWarning === 'function') {
            global.addRowWarning(tr, {
                product_name: rec.product_name,
                specification: rec.specification || '',
                verified: tr.getAttribute('data-verified') === '1' ? 1 : 0,
                record_id: tr.getAttribute('data-record-id'),
                verified_warnings: (typeof global.readRowVerifiedWarnings === 'function') ? global.readRowVerifiedWarnings(tr) : {}
            });
        }
    }

    // ====================================================================
    // 7. 上移/下移 (PATCH /records/<id>/move)
    // ====================================================================
    function moveRecord(recordId, direction, btn, apiBase) {
        var tr = btn ? btn.closest('tr') : null;
        fetch(apiBase + '/records/' + recordId + '/move', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ direction: direction })
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.success) {
                    if (!tr) return;
                    if (direction === 'up') {
                        var prev = tr.previousElementSibling;
                        if (prev) tr.parentNode.insertBefore(tr, prev);
                    } else {
                        var nxt = tr.nextElementSibling;
                        if (nxt) tr.parentNode.insertBefore(nxt, tr);
                    }
                } else {
                    alert(data.error || '操作失败');
                }
            })
            .catch(function (err) { alert('请求失败: ' + err); });
    }

    function bindMoveButtons(apiBase) {
        document.querySelectorAll('.move-up-btn').forEach(function (btn) {
            if (btn._moveBound) return;
            btn._moveBound = true;
            btn.addEventListener('click', function () {
                moveRecord(this.getAttribute('data-id'), 'up', this, apiBase);
            });
        });
        document.querySelectorAll('.move-down-btn').forEach(function (btn) {
            if (btn._moveBound) return;
            btn._moveBound = true;
            btn.addEventListener('click', function () {
                moveRecord(this.getAttribute('data-id'), 'down', this, apiBase);
            });
        });
    }

    // ====================================================================
    // 8. 删除 (DELETE /records/<id>) - 软删除 + 视觉淡出 + 刷新汇总
    // ====================================================================
    function deleteRecord(recordId, btn, apiBase, refreshSummaryFn) {
        var proceed = function () { _doDeleteRecord(recordId, btn, apiBase, refreshSummaryFn); };
        if (typeof confirmDialog === 'function') {
            confirmDialog('确定要删除这条记录吗?', proceed);
        } else if (confirm('确定要删除这条记录吗?')) {
            proceed();
        }
    }

    function _doDeleteRecord(recordId, btn, apiBase, refreshSummaryFn) {
        if (typeof showLoading === 'function') showLoading('删除中...');
        fetch(apiBase + '/records/' + recordId, { method: 'DELETE' })
            .then(function (res) { return res.json(); })
            .then(function (data) {
                if (typeof hideLoading === 'function') hideLoading();
                if (data.success) {
                    var tr = btn ? btn.closest('tr') : null;
                    var dateGroup = tr ? tr.closest('.date-group') : null;
                    if (tr) {
                        tr.style.transition = 'opacity 0.3s';
                        tr.style.opacity = '0';
                        setTimeout(function () {
                            if (typeof global.removeRowWarning === 'function') global.removeRowWarning(tr);
                            tr.remove();
                            if (typeof refreshSummaryFn === 'function') refreshSummaryFn(dateGroup);
                            if (typeof global.updateNavIndicator === 'function') global.updateNavIndicator();
                        }, 300);
                    }
                } else {
                    alert('删除失败: ' + (data.error || ''));
                }
            })
            .catch(function (err) {
                if (typeof hideLoading === 'function') hideLoading();
                alert('请求失败: ' + err);
            });
    }

    // ====================================================================
    // 9. 批量绑定编辑/删除/移动 (出货样式表格: 9 列)
    //    依赖 DOM:
    //      - .edit-record-btn    (data-id)
    //      - .delete-record-btn  (data-id)
    //      - .move-up-btn / .move-down-btn (data-id)
    //    内部委托 beginInlineEdit / collectInlineEdit / applyEditedRow
    //    userCtx: { unitList, refreshSummaryFn, afterSaveHook }
    // ====================================================================
    function bindEditDeleteButtons(apiBase, userCtx) {
        userCtx = userCtx || {};
        var unitList = userCtx.unitList || [];
        var refreshSummaryFn = userCtx.refreshSummaryFn;
        var afterSaveHook = userCtx.afterSaveHook;

        document.querySelectorAll('.edit-record-btn').forEach(function (btn) {
            if (btn._bound) return;
            btn._bound = true;
            btn.addEventListener('click', function () {
                var recordId = this.getAttribute('data-id');
                var tr = this.closest('tr');
                if (!recordId || !tr) return;
                // 根据按钮 class 判断当前处于编辑模式 (btn-success) 还是查看模式 (btn-primary)
                var isSaving = this.classList.contains('btn-success');
                if (isSaving) {
                    var data = collectInlineEdit(tr);
                    if (!data) return;
                    if (typeof showBtnLoading === 'function') showBtnLoading(btn, '保存中...');
                    fetch(apiBase + '/records/' + recordId, {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(data)
                    })
                        .then(function (res) { return res.json(); })
                        .then(function (result) {
                            if (typeof hideBtnLoading === 'function') hideBtnLoading(btn);
                            if (result.success) {
                                applyEditedRow(tr, result.record, unitList, refreshSummaryFn);
                                // 保持原按钮文本 (页面可能用了 emoji); 只切回查看模式样式
                                btn.classList.remove('btn-success');
                                btn.classList.add('btn-primary');
                                if (typeof afterSaveHook === 'function') afterSaveHook(tr, result);
                            } else {
                                alert('保存失败: ' + (result.error || ''));
                            }
                        })
                        .catch(function (err) {
                            if (typeof hideBtnLoading === 'function') hideBtnLoading(btn);
                            alert('请求失败: ' + err);
                        });
                } else {
                    beginInlineEdit(tr);
                    // 切换为编辑模式样式; 按钮文本保持不变 (页面可自定义)
                    btn.classList.remove('btn-primary');
                    btn.classList.add('btn-success');
                }
            });
        });

        document.querySelectorAll('.delete-record-btn').forEach(function (btn) {
            if (btn._bound) return;
            btn._bound = true;
            btn.addEventListener('click', function () {
                deleteRecord(this.getAttribute('data-id'), this, apiBase, refreshSummaryFn);
            });
        });

        bindMoveButtons(apiBase);
    }

    // 同时在 window 上直接暴露 autoFillQtyFromRemark, 方便模板里 oninput="autoFillQtyFromRemark(this)" 直接调用
    global.autoFillQtyFromRemark = autoFillQtyFromRemark;

    // 暴露
    global.ProductRowUtils = {
        // 件数转换
        findPieceConversion: findPieceConversion,
        extractPiecesFromRemark: extractPiecesFromRemark,
        getPieceHint: getPieceHint,
        calcPieceQuantity: calcPieceQuantity,
        checkPieceMismatch: checkPieceMismatch,
        // 单位/数量
        findUnit: findUnit,
        getUnitHint: getUnitHint,
        checkMismatch: checkMismatch,
        // 视觉
        mismatchIcon: mismatchIcon,
        applyMismatchClass: applyMismatchClass,
        isInvalidQty: isInvalidQty,
        invalidQtyIcon: invalidQtyIcon,
        applyInvalidQtyClass: applyInvalidQtyClass,
        // 备注→数量
        autoFillQtyFromRemark: autoFillQtyFromRemark,
        // 汇总
        refreshRemarkSummary: refreshRemarkSummary,
        // 行内编辑
        beginInlineEdit: beginInlineEdit,
        collectInlineEdit: collectInlineEdit,
        applyEditedRow: applyEditedRow,
        // 操作
        moveRecord: moveRecord,
        bindMoveButtons: bindMoveButtons,
        deleteRecord: deleteRecord,
        bindEditDeleteButtons: bindEditDeleteButtons
    };

})(window);