// === block 0 (HTML 转义工具，全局共用) ===
// 用于把用户数据塞到 innerHTML / input value / src 属性前必须过这一关
// 覆盖场景:品名 / 规格 / 备注 / 客户 / 图片路径 / 任何不可信字符串
function escHtml(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}
// alias:写到 HTML 属性时语义更清晰
function escAttr(s) { return escHtml(s); }

// === block 1 ===
var _imgPreviewRotation = 0;
function showImgPreview(src) {
    var m = document.getElementById('imgPreviewModal');
    var img = document.getElementById('imgPreviewTarget');
    img.src = src;
    img.style.transform = 'rotate(0deg)';
    _imgPreviewRotation = 0;
    m.style.display = 'flex';
}
function closeImgPreview() {
    document.getElementById('imgPreviewModal').style.display = 'none';
}
function imgPreviewRotate() {
    _imgPreviewRotation = (_imgPreviewRotation + 90) % 360;
    document.getElementById('imgPreviewTarget').style.transform = 'rotate(' + _imgPreviewRotation + 'deg)';
}
document.getElementById('imgPreviewModal').addEventListener('click', function(e) {
    if (e.target === this) closeImgPreview();
});
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') closeImgPreview();
});

// === block 2 ===
console.log('nav-dropdown script loaded');
var toggles = document.querySelectorAll('.nav-dropdown-toggle');
console.log('found toggles:', toggles.length);
toggles.forEach(function(toggle) {
    toggle.addEventListener('click', function(e) {
        e.preventDefault();
        console.log('toggle clicked');
        var menu = toggle.nextElementSibling;
        console.log('menu:', menu);
        console.log('menu tagName:', menu ? menu.tagName : 'null');
        // 手动添加/移除 class 而不用 toggle
        if (menu.classList.contains('show')) {
            menu.classList.remove('show');
        } else {
            menu.classList.add('show');
        }
        console.log('menu classes after toggle:', menu.className);
    });
});
document.addEventListener('click', function(e) {
    if (!e.target.closest('.nav-dropdown')) {
        document.querySelectorAll('.nav-dropdown-menu.show').forEach(function(m) {
            m.classList.remove('show');
            m.style.display = 'none';
        });
    }
});

// === block 3 ===
function navScroll(dir) {
    var nav = document.querySelector('.nav-links');
    var items = nav.querySelectorAll('li');
    if (items.length === 0) return;
    // 计算10个按钮的宽度
    var tenWidth = 0;
    for (var i = 0; i < Math.min(10, items.length); i++) {
        tenWidth += items[i].offsetWidth;
    }
    tenWidth += 10 * 4; // 加上 gap
    nav.scrollBy({ left: dir * tenWidth, behavior: 'smooth' });
}

function updateNavArrows() {
    var nav = document.querySelector('.nav-links');
    // 2026-09-09: 移动端页(/m/...)无 .nav-links,直接读 scrollWidth 会抛 null 错误,加守卫
    if (!nav) return;
    var leftBtn = document.querySelector('.nav-arrow-left');
    var rightBtn = document.querySelector('.nav-arrow-right');
    var hasOverflow = nav.scrollWidth > nav.clientWidth + 2;
    var itemCount = nav.querySelectorAll('li').length;
    // 11个以上按钮时永远显示箭头（配合横向滚动）
    var alwaysShow = itemCount >= 11;
    if (leftBtn) leftBtn.classList.toggle('show', alwaysShow || (hasOverflow && nav.scrollLeft > 5));
    if (rightBtn) rightBtn.classList.toggle('show', alwaysShow || (hasOverflow && nav.scrollLeft < nav.scrollWidth - nav.clientWidth - 5));
}

document.querySelector('.nav-links')?.addEventListener('scroll', updateNavArrows);
window.addEventListener('DOMContentLoaded', updateNavArrows);
window.addEventListener('resize', updateNavArrows);
// ── 全局 Loading ──
var _loadingTimer = null;
function showLoading(msg) {
    var overlay = document.getElementById('globalLoading');
    var msgEl = document.getElementById('globalLoadingMsg');
    if (!overlay) return;
    msgEl.textContent = msg || '处理中...';
    overlay.style.display = 'flex';
    // 超过 15 秒自动隐藏，防止卡死
    clearTimeout(_loadingTimer);
    _loadingTimer = setTimeout(function() { hideLoading(); }, 15000);
}
function hideLoading() {
    var overlay = document.getElementById('globalLoading');
    if (overlay) overlay.style.display = 'none';
    clearTimeout(_loadingTimer);
}

// ── 按钮 Loading ──
var _btnOriginals = new WeakMap();
function showBtnLoading(btn, label) {
    if (!btn) return;
    // 保存原始文本
    if (!_btnOriginals.has(btn)) {
        _btnOriginals.set(btn, { html: btn.innerHTML, disabled: btn.disabled });
    }
    btn.disabled = true;
    btn.classList.add('loading');
    btn.innerHTML = (label || '保存中...');
}
function hideBtnLoading(btn) {
    if (!btn) return;
    btn.classList.remove('loading');
    btn.disabled = false;
    var orig = _btnOriginals.get(btn);
    if (orig) {
        btn.innerHTML = orig.html;
        btn.disabled = orig.disabled;
    }
}

function confirmDialog(msg, onConfirm) {
    var d = document.createElement('dialog');
    d.innerHTML = '<p>' + msg + '</p><menu><button value="cancel">取消</button><button value="ok">确定</button></menu>';
    document.body.appendChild(d);
    d.querySelector('button[value=ok]').onclick = function() { d.close('ok'); };
    d.querySelector('button[value=cancel]').onclick = function() { d.close('cancel'); };
    d.addEventListener('close', function() {
        if (d.returnValue === 'ok') onConfirm();
        d.remove();
    });
    d.showModal();
}
