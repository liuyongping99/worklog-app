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
