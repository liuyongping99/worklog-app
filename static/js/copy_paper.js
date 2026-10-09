// 拷贝纸/日本纸 标签图 (2026-09-06 创建 / 2026-09-09 重构)
//
// **2026-09-09 重构**:点数已统一走 placement 体系(存 shipping_images(source='placement'),
// 见 placement_count.js),标签图也迁入 shipping_images(source='copy_paper_label'),
// copy_paper_images 表已废弃。本文件**只保留标签图(留档,不做 OCR/AI)路径**:
//   - 标签图上传 → POST /records/<rid>/copy-paper-images (落 shipping_images)
//   - 标签图删除 → DELETE /images/<id>  (通用订单图端点,自带锁单防御 + 审计)
// 已删除:张数录入弹框 / sheet-count PATCH / .copy-paper-area 相关 DOM 操作(随点数走 placement 废弃)
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

    // 接管 placement 上传回调（placement_count.js 检测 _copyPaperUploadSource 分流到此）
    window.copyPaperImageUploaded = function (data, recordPk) {
        if (!data || !data.success) {
            alert('上传失败: ' + ((data && data.error) || '未知错误'));
            return;
        }
        refreshCopyPaperBlock(recordPk);
        window._copyPaperUploadSource = null;
        window._copyPaperTargetRecord = null;
    };

    window.openCopyPaperUpload = function (recordPk, orderPk, source) {
        openUploadModal(recordPk, orderPk, source);
    };

    window.refreshCopyPaperBlock = function (recordPk) {
        // 简化方案:整页刷新（防止局部替换搞错 DOM）
        // TODO: 后续可优化为局部刷新
        location.reload();
    };

    document.addEventListener('DOMContentLoaded', function () {
        // 2026-09-09: 免 AI 比对标签图删除 (渲染在普通商品图区, 独立 handler)
        document.body.addEventListener('click', function (e) {
            var btn = e.target.closest('.copy-paper-label-del-btn');
            if (!btn) return;
            if (!confirm('删除这张标签图？')) return;
            var iid = btn.getAttribute('data-image-id');
            // 2026-09-09: 标签图已存 shipping_images,复用通用订单图删除端点
            fetch('/api/v1/shipping-orders/images/' + iid, { method: 'DELETE' })
                .then(function (r) { return r.json(); })
                .then(function (j) {
                    if (!j.success) { alert('删除失败: ' + (j.error || '')); return; }
                    location.reload();
                })
                .catch(function (err) { alert('删除失败: ' + err); });
        });
    });
})();
