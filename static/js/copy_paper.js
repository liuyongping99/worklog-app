// 拷贝纸/日本纸 行级图 + 张数 (2026-09-06)
(function () {
    'use strict';

    // 复用现有 _image_upload_modal 的弹框入口（与 placement-add-btn 同款）
    function openUploadModal(recordPk, orderPk, source) {
        if (typeof window.openPlacementImageModal !== 'function') {
            alert('上传弹框未就绪');
            return;
        }
        // 临时重定向 placement 上传回调到 copy-paper
        window._copyPaperUploadSource = source;
        window._copyPaperTargetRecord = recordPk;
        window.openPlacementImageModal(recordPk, orderPk);
    }

    // 接管 placement 上传回调（如果现有 placementImageUploaded 不可重入，可改写 placement_count.js
    // 让它检测 window._copyPaperUploadSource 走 copy-paper 分支）
    window.copyPaperImageUploaded = function (data, recordPk) {
        if (!data || !data.success) {
            alert('上传失败: ' + ((data && data.error) || '未知错误'));
            return;
        }
        var source = window._copyPaperUploadSource;
        if (source === 'count' && data.image) {
            // 弹张数输入框
            openCopyPaperCountModal(data.image.id);
        }
        refreshCopyPaperBlock(recordPk);
        window._copyPaperUploadSource = null;
        window._copyPaperTargetRecord = null;
    };

    window.openCopyPaperUpload = function (recordPk, orderPk, source) {
        openUploadModal(recordPk, orderPk, source);
    };

    window.openCopyPaperCountModal = function (imageId) {
        var modal = document.getElementById('copyPaperCountModal');
        var input = document.getElementById('copyPaperCountInput');
        var hidden = document.getElementById('copyPaperCountImageId');
        if (!modal || !input || !hidden) return;
        hidden.value = imageId;
        input.value = '';
        modal.style.display = 'flex';
        setTimeout(function () { input.focus(); }, 50);
    };

    window.closeCopyPaperCountModal = function () {
        var modal = document.getElementById('copyPaperCountModal');
        if (modal) modal.style.display = 'none';
    };

    window.saveCopyPaperCount = function () {
        var hidden = document.getElementById('copyPaperCountImageId');
        var input = document.getElementById('copyPaperCountInput');
        if (!hidden || !input || !hidden.value) return;
        var val = input.value.trim();
        var body = { sheet_count: val === '' ? null : parseInt(val, 10) };
        fetch('/api/v1/shipping-orders/copy-paper-images/' + hidden.value + '/sheet-count', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        }).then(function (r) { return r.json(); }).then(function (j) {
            if (!j.success) { alert('保存失败: ' + (j.error || '')); return; }
            closeCopyPaperCountModal();
            // 找出该 image 所在 record 并刷新
            var thumb = document.querySelector('.copy-paper-thumb[data-image-id="' + hidden.value + '"]');
            if (thumb) {
                var block = thumb.closest('.record-block');
                if (block) {
                    var recId = block.getAttribute('data-record-id');
                    if (recId) refreshCopyPaperBlock(parseInt(recId, 10));
                }
            }
        }).catch(function (err) {
            alert('保存失败: ' + err);
        });
    };

    window.refreshCopyPaperBlock = function (recordPk) {
        // 简化方案:整行刷新（防止局部替换搞错 DOM）
        var block = document.querySelector('.record-block[data-record-id="' + recordPk + '"]');
        if (!block) { location.reload(); return; }
        location.reload();  // TODO: 后续可优化为局部刷新
    };

    // 绑定缩略图上的删除按钮 + 张数输入框
    document.addEventListener('DOMContentLoaded', function () {
        document.body.addEventListener('click', function (e) {
            var del = e.target.closest('.copy-paper-delete-btn');
            if (del) {
                if (!confirm('删除这张图片?')) return;
                var iid = del.getAttribute('data-image-id');
                fetch('/api/v1/shipping-orders/copy-paper-images/' + iid, { method: 'DELETE' })
                    .then(function (r) { return r.json(); }).then(function (j) {
                        if (!j.success) { alert('删除失败'); return; }
                        var thumb = del.closest('.copy-paper-thumb');
                        if (thumb) {
                            var block = thumb.closest('.record-block');
                            var recId = block && block.getAttribute('data-record-id');
                            if (recId) refreshCopyPaperBlock(parseInt(recId, 10));
                        }
                    });
                return;
            }
        });
        document.body.addEventListener('change', function (e) {
            var inp = e.target.closest('.copy-paper-count-input');
            if (inp) {
                var iid = inp.getAttribute('data-image-id');
                var val = inp.value.trim();
                var body = { sheet_count: val === '' ? null : parseInt(val, 10) };
                fetch('/api/v1/shipping-orders/copy-paper-images/' + iid + '/sheet-count', {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body)
                }).then(function (r) { return r.json(); }).then(function (j) {
                    if (!j.success) { alert('保存失败'); return; }
                    var thumb = inp.closest('.copy-paper-thumb');
                    if (thumb) {
                        var block = thumb.closest('.record-block');
                        var recId = block && block.getAttribute('data-record-id');
                        if (recId) refreshCopyPaperBlock(parseInt(recId, 10));
                    }
                });
            }
        });
    });
})();
