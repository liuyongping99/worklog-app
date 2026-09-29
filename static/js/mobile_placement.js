/* 移动端点数页:复用 PC 摆放图后端机制(placement-images / marks / mark-scale / loose-count),
   手机拍照/相册上传 + 手指点击计数 + 撤销 + 缩放 + 散码录入。
   每个摆放图一张独立卡片,自带按钮组(撤销/A±/散码/删除),与 PC 同享后端 API。 */
(function () {
  "use strict";
  const C = window.__PLACEMENT__ || {};
  const RID = C.recordId;
  const REMARK = C.remark || "";

  const API = {
    list: () => `/api/v1/shipping-orders/records/${RID}/placement-images`,
    upload: () => `/api/v1/shipping-orders/records/${RID}/placement-images`,
    del: (iid) => `/api/v1/shipping-orders/placement-images/${iid}`,
    marks: (iid) => `/api/v1/shipping-orders/placement-images/${iid}/marks`,
    marksUndo: (iid) => `/api/v1/shipping-orders/placement-images/${iid}/marks/last`,
    markScale: (iid) => `/api/v1/shipping-orders/placement-images/${iid}/mark-scale`,
    loose: (iid) => `/api/v1/shipping-orders/placement-images/${iid}/loose-count`,
    manual: (iid) => `/api/v1/shipping-orders/placement-images/${iid}/manual-count`,
    unload: (iid) => `/api/v1/shipping-orders/placement-images/${iid}/unload`,
  };

  const SCALE_STEP = 1.2;
  const BASE_MARK_R = 0.03375;

  const STATE = {
    images: [],
  };

  // 已渲染卡片的 DOM 引用, key = imageId
  const CARDS = {};

  // ---- DOM ----
  const $ = (sel, root) => (root || document).querySelector(sel);
  const root = $(".m-place");
  const listEl = $('[data-role="list"]', root);
  const emptyEl = $('[data-role="empty"]', root);
  const compareEl = $('[data-role="compare"]', root);
  const uploadProgress = $('[data-role="uploadProgress"]', root);
  const captureInput = $('[data-role="capture-input"]', root);
  const albumInput = $('[data-role="album-input"]', root);

  // ---- 数据加载 ----
  function loadImages() {
    return fetch(API.list())
      .then((r) => r.json())
      .then((d) => {
        STATE.images = (d.images || []).map((im) => ({
          id: im.id,
          relative_path: im.relative_path,
          marks: im.marks || [],
          n_marks: im.n_marks || (im.marks ? im.marks.length : 0),
          mark_scale: im.mark_scale || 1,
          loose_count: im.loose_count || 0,
          manual_count: (im.manual_count != null ? im.manual_count : null),
          is_unload: !!im.is_unload,
        }));
        buildCards();
        renderCompare();
      });
  }

  function getImage(iid) {
    return STATE.images.find((i) => i.id === iid) || null;
  }

  // ---- 卡片构建(一次建好,数据变化只局部更新) ----
  function buildCards() {
    // 清空(保留 empty 占位)
    listEl.querySelectorAll(".place-card").forEach((n) => n.remove());
    for (const k in CARDS) delete CARDS[k];

    if (!STATE.images.length) {
      emptyEl.style.display = "block";
      return;
    }
    emptyEl.style.display = "none";

    STATE.images.forEach((im, idx) => {
      const card = document.createElement("div");
      card.className = "place-card";
      card.dataset.imageId = im.id;
      card.innerHTML = `
        <div class="place-card-head">
          <span class="place-card-idx">图 ${idx + 1}</span>
          <span class="place-card-count"><b data-role="countBadge">0</b> 支</span>
          <span class="place-card-manual" data-role="manualLabel" hidden>已直输</span>
          <span class="place-card-unload" data-role="unloadLabel" hidden>⬇ 卸载中</span>
          <span class="place-card-loose" data-role="looseLabel">散码 0</span>
        </div>
        <div class="place-img-wrap" data-role="imgWrap">
          <img data-role="mainImg" src="/upload/${im.relative_path}" alt="摆放图">
          <div class="place-marks" data-role="marksLayer"></div>
          <div class="place-hint" data-role="imgHint">点击图中每捆/每件计数</div>
        </div>
        <div class="place-card-tools">
          <button data-action="undo">↩ 撤销</button>
          <button data-action="bigger">A＋</button>
          <button data-action="smaller">A－</button>
          <button data-action="manual" class="manual-btn">支数</button>
          <button data-action="loose" class="loose-btn">散码</button>
          <button data-action="unload" class="unload-btn">⬇ 卸载</button>
          <button data-action="delete" class="danger">🗑 删除</button>
        </div>`;

      const refs = {
        card,
        imgWrap: $('[data-role="imgWrap"]', card),
        mainImg: $('[data-role="mainImg"]', card),
        marksLayer: $('[data-role="marksLayer"]', card),
        countBadge: $('[data-role="countBadge"]', card),
        looseLabel: $('[data-role="looseLabel"]', card),
        manualLabel: $('[data-role="manualLabel"]', card),
        unloadLabel: $('[data-role="unloadLabel"]', card),
        scale: im.mark_scale || 1,
      };
      CARDS[im.id] = refs;

      // 点击计数(手指)
      refs.imgWrap.addEventListener("click", (e) => onImgClick(e, im, refs));
      // 图片加载后重绘计数点
      refs.mainImg.addEventListener("load", () => renderMarks(im, refs));

      // 按钮组
      $('[data-action="undo"]', card).addEventListener("click", () => doUndo(im, refs));
      $('[data-action="bigger"]', card).addEventListener("click", () => adjustScale(im, refs, SCALE_STEP));
      $('[data-action="smaller"]', card).addEventListener("click", () => adjustScale(im, refs, 1 / SCALE_STEP));
      $('[data-action="loose"]', card).addEventListener("click", () => openLoose(im, refs));
      $('[data-action="manual"]', card).addEventListener("click", () => openManual(im, refs));
      $('[data-action="unload"]', card).addEventListener("click", () => toggleUnload(im, refs));
      $('[data-action="delete"]', card).addEventListener("click", () => doDelete(im, refs));

      listEl.appendChild(card);
      renderMarks(im, refs);
    });
  }

  // ---- 计数点渲染 ----
  function renderMarks(im, refs) {
    if (!refs) return;
    const layer = refs.marksLayer;
    layer.innerHTML = "";
    const W = refs.imgWrap.clientWidth || refs.mainImg.clientWidth || 300;
    const fs = W * BASE_MARK_R * 2 * (refs.scale || 1);
    (im.marks || []).forEach((mk, idx) => {
      const s = document.createElement("span");
      s.className = "place-mark";
      s.style.left = (mk.x_ratio * 100).toFixed(3) + "%";
      s.style.top = (mk.y_ratio * 100).toFixed(3) + "%";
      s.style.fontSize = fs.toFixed(1) + "px";
      s.textContent = idx + 1;
      layer.appendChild(s);
    });
    // 支数口径:直接输入(manual_count)优先,无则回退点击计数点 marks.length(对齐 PC 端 effectiveZhi)
    const cnt = (im.manual_count != null) ? im.manual_count : ((im.marks || []).length);
    const displayCnt = im.is_unload ? -cnt : cnt;
    refs.countBadge.textContent = displayCnt;
    // 高亮优先级:卸载(红,含 sign) > 直输(蓝)。有效支数为负时红色 badge 更醒目,
    // 已直输 / 卸载中 pill 各自独立显示不互斥(用户改后立刻反映)。
    const isManual = im.manual_count != null;
    const isUnload = !!im.is_unload;
    refs.countBadge.classList.toggle("is-manual", !isUnload && isManual);
    refs.countBadge.classList.toggle("is-unload", isUnload);
    if (refs.manualLabel) refs.manualLabel.hidden = !isManual;
    if (refs.unloadLabel) refs.unloadLabel.hidden = !(isUnload);
    // 工具栏按钮 active 态(用户改后立刻反映)
    const unloadBtn = $('[data-action="unload"]', refs.card);
    if (unloadBtn) unloadBtn.classList.toggle("active", isUnload);
    refs.looseLabel.textContent = "散码 " + (im.loose_count || 0);
  }

  // ---- 备注对比(整单) ----
  // 支数口径:manual_count 优先,无则回退点击计数点 n_marks(对齐 PC 端 effectiveZhi 与后端 _eff_zhi)
  // 卸载模式 is_unload=true 时取负,从记录累计中扣减(与 PC 端 PC effectiveZhi 一致)
  function _effZhi(i) {
    const base = (i.manual_count != null) ? i.manual_count : (i.n_marks || 0);
    return i.is_unload ? -base : base;
  }
  function renderCompare() {
    const zhiTotal = STATE.images.reduce((s, i) => s + _effZhi(i), 0);
    const looseTotal = STATE.images.reduce((s, i) => s + (i.loose_count || 0), 0);

    const zhiExp = sumMatches(REMARK, /(\d+)\s*支/g);
    const looseExp = sumMatches(REMARK, /(\d+)\s*[yY]/g);
    let html = "";
    html += compareBadge("支", zhiTotal, zhiExp.value, zhiExp.matched);
    html += compareBadge("散码", looseTotal, looseExp.value, looseExp.matched);
    compareEl.innerHTML = html;
  }

  function sumMatches(text, re) {
    let m,
      total = 0,
      matched = false;
    const r = new RegExp(re.source, "g");
    while ((m = r.exec(text)) !== null) {
      total += parseInt(m[1], 10);
      matched = true;
    }
    return { value: total, matched: matched };
  }

  function compareBadge(kind, actual, expected, hasExpected) {
    if (!hasExpected) {
      return `<span class="place-compare-badge none">${kind} ${actual}（备注无${kind}）</span>`;
    }
    let cls = "ok";
    let txt = `${kind} ${actual} = 备注 ${expected}`;
    if (actual > expected) {
      cls = "bad";
      txt = `${kind} ${actual} > 备注 ${expected}（多 ${actual - expected}）`;
    } else if (actual < expected) {
      cls = "warn";
      txt = `${kind} ${actual} < 备注 ${expected}（少 ${expected - actual}）`;
    }
    return `<span class="place-compare-badge ${cls}">${txt}</span>`;
  }

  // ---- 点击计数 ----
  // 直接输入模式下,先清空 manual_count 再继续点击计数(对齐 PC 端交互)
function _clearManualThen(maybeIm, next) {
  if (maybeIm.manual_count == null) { next(); return; }
  fetch(API.manual(maybeIm.id), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ count: null }),
  })
    .then((r) => r.json())
    .then((d) => {
      if (!d.success) { alert(d.error || "清除失败"); return; }
      maybeIm.manual_count = null;
      next();
    })
    .catch((e) => alert("清除异常: " + e));
}

function onImgClick(e, im, refs) {
    const rect = refs.mainImg.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return;
    let x = (e.clientX - rect.left) / rect.width;
    let y = (e.clientY - rect.top) / rect.height;
    x = Math.max(0, Math.min(1, x));
    y = Math.max(0, Math.min(1, y));
    const placeMark = () => fetch(API.marks(im.id), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ x_ratio: x, y_ratio: y, r: 0 }),
    })
      .then((r) => r.json())
      .then((d) => {
        if (!d.success) {
          alert(d.error || "计数失败");
          return;
        }
        im.marks = d.marks || [];
        im.n_marks = im.marks.length;
        renderMarks(im, refs);
        renderCompare();
      });
    // 直接输入模式下先清空 manual_count,确保与服务端互斥状态一致
    _clearManualThen(im, placeMark);
  }

  // ---- 撤销 ----
  // 直接输入模式下,「撤销」改为清空手动支数,回退到点击计数(对齐 PC 端交互)
  function doUndo(im, refs) {
    if (im.manual_count != null) {
      _clearManualThen(im, () => { renderMarks(im, refs); renderCompare(); });
      return;
    }
    fetch(API.marksUndo(im.id), { method: "DELETE" })
      .then((r) => r.json())
      .then((d) => {
        if (!d.success) return;
        im.marks = d.marks || [];
        im.n_marks = im.marks.length;
        renderMarks(im, refs);
        renderCompare();
      });
  }

  // ---- 缩放 ----
  function adjustScale(im, refs, factor) {
    const next = Math.max(0.3, Math.min(4.0, (refs.scale || 1) * factor));
    fetch(API.markScale(im.id), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scale: next }),
    })
      .then((r) => r.json())
      .then((d) => {
        if (!d.success) return;
        refs.scale = d.scale;
        im.mark_scale = d.scale;
        renderMarks(im, refs);
      });
  }

  // ---- 删除 ----
  function doDelete(im, refs) {
    if (!confirm("确定删除该摆放图（含计数点）？")) return;
    fetch(API.del(im.id), { method: "DELETE" })
      .then((r) => r.json())
      .then((d) => {
        if (!d.success) {
          alert(d.error || "删除失败");
          return;
        }
        loadImages();
      });
  }

  // ---- 卸载模式(本图清点支数以负数计入累计,用于卸载/退货) ----
  function toggleUnload(im, refs) {
    const next = !im.is_unload;
    fetch(API.unload(im.id), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ unload: next }),
    })
      .then((r) => r.json())
      .then((d) => {
        if (!d.success) { alert(d.error || "设置失败"); return; }
        im.is_unload = !!d.is_unload;
        renderMarks(im, refs);
        renderCompare();
      })
      .catch((e) => alert("异常: " + e));
  }

  // ---- 散码 ----
  const looseMask = $("#placeLooseMask");
  const looseInput = $('[data-role="looseInput"]', looseMask);
  let looseTarget = null;
  function openLoose(im, refs) {
    looseTarget = im;
    looseInput.value = im.loose_count || 0;
    looseMask.hidden = false;
  }
  $('[data-action="looseCancel"]', looseMask).addEventListener("click", () => (looseMask.hidden = true));
  $('[data-action="looseSave"]', looseMask).addEventListener("click", function () {
    if (!looseTarget) {
      looseMask.hidden = true;
      return;
    }
    const im = looseTarget;
    const refs = CARDS[im.id];
    const count = parseInt(looseInput.value || "0", 10) || 0;
    fetch(API.loose(im.id), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ count: count }),
    })
      .then((r) => r.json())
      .then((d) => {
        looseMask.hidden = true;
        if (!d.success) {
          alert(d.error || "保存失败");
          return;
        }
        im.loose_count = d.count;
        if (refs) {
          refs.looseLabel.textContent = "散码 " + d.count;
        }
        renderCompare();
      });
  });

  // ---- 直接输入支数(与点击计数等效,用于备注比对) ----
  const manualMask = $("#placeManualMask");
  const manualInput = $('[data-role="manualInput"]', manualMask);
  let manualTarget = null;
  function openManual(im, refs) {
    manualTarget = im;
    // 默认展示当前口径:已直输的数值,无则回退点击计数点
    const cur = (im.manual_count != null) ? im.manual_count : ((im.marks || []).length);
    manualInput.value = cur > 0 ? cur : "";
    manualMask.hidden = false;
    manualInput.focus();
  }
  $('[data-action="manualCancel"]', manualMask).addEventListener("click", () => (manualMask.hidden = true));
  // 保存(后端会清空点击计数点,与手输互斥)
  $('[data-action="manualSave"]', manualMask).addEventListener("click", function () {
    if (!manualTarget) { manualMask.hidden = true; return; }
    const im = manualTarget;
    const refs = CARDS[im.id];
    const raw = (manualInput.value || "").trim();
    const v = raw === "" ? 0 : (parseInt(raw, 10) || 0);
    const count = v < 0 ? 0 : v;
    fetch(API.manual(im.id), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ count: count }),
    })
      .then((r) => r.json())
      .then((d) => {
        if (!d.success) { alert(d.error || "保存失败"); return; }
        manualMask.hidden = true;
        im.manual_count = (d.manual_count != null) ? d.manual_count : count;
        // 后端会清空 marks,与直输互斥
        im.marks = [];
        im.n_marks = 0;
        renderMarks(im, refs);
        renderCompare();
      });
  });
  // 清空(回退到点击计数)
  $('[data-action="manualClear"]', manualMask).addEventListener("click", function () {
    if (!manualTarget) { manualMask.hidden = true; return; }
    const im = manualTarget;
    const refs = CARDS[im.id];
    fetch(API.manual(im.id), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ count: null }),
    })
      .then((r) => r.json())
      .then((d) => {
        if (!d.success) { alert(d.error || "清除失败"); return; }
        manualMask.hidden = true;
        im.manual_count = null;
        renderMarks(im, refs);
        renderCompare();
      });
  });

  // ---- 拍照 / 相册 预览上传 ----
  const captureBtn = $('[data-action="capture"]', root);
  const albumBtn = $('[data-action="album"]', root);
  captureBtn.addEventListener("click", () => captureInput.click());
  albumBtn.addEventListener("click", () => albumInput.click());
  captureInput.addEventListener("change", (e) => onPickFile(e.target.files));
  albumInput.addEventListener("change", (e) => onPickFile(e.target.files));

  let previewDeg = 0;
  let previewImgSrc = null;
  const mask = $("#placePreviewMask");
  const canvas = $("#placePreviewCanvas");

  function onPickFile(files) {
    if (!files || !files.length) return;
    const file = files[0];
    const reader = new FileReader();
    reader.onload = () => {
      previewImgSrc = reader.result;
      previewDeg = 0;
      drawPreview();
      mask.hidden = false;
    };
    reader.readAsDataURL(file);
  }

  function drawPreview() {
    if (!previewImgSrc) return;
    const img = new Image();
    img.onload = () => {
      const ctx = canvas.getContext("2d");
      const w = img.width,
        h = img.height;
      if (previewDeg % 180 === 90) {
        canvas.width = h;
        canvas.height = w;
      } else {
        canvas.width = w;
        canvas.height = h;
      }
      ctx.save();
      ctx.translate(canvas.width / 2, canvas.height / 2);
      ctx.rotate((previewDeg * Math.PI) / 180);
      ctx.drawImage(img, -w / 2, -h / 2);
      ctx.restore();
    };
    img.src = previewImgSrc;
  }

  $("#placeRotateLeft").addEventListener("click", () => {
    previewDeg = (previewDeg + 270) % 360;
    drawPreview();
  });
  $("#placeRotateRight").addEventListener("click", () => {
    previewDeg = (previewDeg + 90) % 360;
    drawPreview();
  });
  $("#placeRetake").addEventListener("click", () => {
    mask.hidden = true;
    previewImgSrc = null;
  });
  $("#placeConfirm").addEventListener("click", () => {
    const dataUrl = canvas.toDataURL("image/jpeg", 0.85);
    mask.hidden = true;
    uploadProgress.hidden = false;
    fetch(API.upload(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ image: dataUrl }),
    })
      .then((r) => r.json())
      .then((d) => {
        uploadProgress.hidden = true;
        if (!d.success) {
          alert(d.error || "上传失败");
          return;
        }
        loadImages();
      })
      .catch(() => {
        uploadProgress.hidden = true;
        alert("上传失败");
      });
  });

  // 初始化
  loadImages();
})();
