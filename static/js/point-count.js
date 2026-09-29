/* 独立点数工具 - 详情页 JS */
(function () {
  "use strict";
  var C = window.__POINT_COUNT__ || {};
  var SID = C.sessionId;
  var UNIT = C.unit || "\u652f";
  var IS_CLOSED = C.status === "closed";
  var API = {
    images: function () { return "/api/v1/point-count/sessions/" + SID + "/images"; },
    delImg: function (iid) { return "/api/v1/point-count/images/" + iid; },
    marks: function (iid) { return "/api/v1/point-count/images/" + iid + "/marks"; },
    undo: function (iid) { return "/api/v1/point-count/images/" + iid + "/marks/last"; },
    scale: function (iid) { return "/api/v1/point-count/images/" + iid + "/mark-scale"; },
    loose: function (iid) { return "/api/v1/point-count/images/" + iid + "/loose-count"; },
    sessionUpdate: function () { return "/api/v1/point-count/sessions/" + SID + "/update"; },
    sessionClose: function () { return "/api/v1/point-count/sessions/" + SID + "/close"; },
    sessionReopen: function () { return "/api/v1/point-count/sessions/" + SID + "/reopen"; },
    sessionDelete: function () { return "/api/v1/point-count/sessions/" + SID; }
  };
  var SCALE_STEP = 1.2;
  var BASE_MARK_R = 0.03375;
  var STATE = { images: [] };
  var CARDS = {};
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var root = $(".m-place");
  var listEl = $("[data-role=\"list\"]", root);
  var emptyEl = $("[data-role=\"empty\"]", root);
  var uploadProgress = $("[data-role=\"uploadProgress\"]", root);
  var captureInput = $("[data-role=\"capture-input\"]", root);
  var albumInput = $("[data-role=\"album-input\"]", root);

  function loadImages() {
    return fetch(API.images()).then(function (r) { return r.json(); }).then(function (d) {
      STATE.images = (d.images || []).map(function (im) {
        return { id: im.id, relative_path: im.relative_path,
                 marks: im.marks || [], n_marks: im.marks ? im.marks.length : 0,
                 mark_scale: im.mark_scale || 1, loose_count: im.loose_count || 0 };
      });
      buildCards(); updateTotal();
    });
  }
  function updateTotal() {
    var t = 0;
    STATE.images.forEach(function (im) { t += (im.n_marks || 0) + (im.loose_count || 0); });
    var el = document.getElementById("mpcTotalValue");
    if (el) el.textContent = t;
  }
  function buildCards() {
    listEl.querySelectorAll(".place-card").forEach(function (n) { n.remove(); });
    for (var k in CARDS) delete CARDS[k];
    if (!STATE.images.length) { emptyEl.style.display = "block"; return; }
    emptyEl.style.display = "none";
    STATE.images.forEach(function (im, idx) {
      var card = document.createElement("div");
      card.className = "place-card";
      card.dataset.imageId = im.id;
      card.innerHTML = ""
        + "<div class=\"place-card-head\">"
        +   "<span class=\"place-card-idx\">图 " + (idx + 1) + "</span>"
        +   "<span class=\"place-card-count\"><b data-role=\"countBadge\">0</b> " + UNIT + "</span>"
        +   "<span class=\"place-card-loose\" data-role=\"looseLabel\">散码 0</span>"
        + "</div>"
        + "<div class=\"place-img-wrap\" data-role=\"imgWrap\">"
        +   "<img data-role=\"mainImg\" src=\"/upload/" + im.relative_path + "\" alt=\"点数图\">"
        +   "<div class=\"place-marks\" data-role=\"marksLayer\"></div>"
        +   "<div class=\"place-hint\" data-role=\"imgHint\">点击图中每件计数</div>"
        + "</div>"
        + "<div class=\"place-card-tools\">"
        +   "<button data-action=\"undo\">↩ 撤销</button>"
        +   "<button data-action=\"bigger\">A＋</button>"
        +   "<button data-action=\"smaller\">A－</button>"
        +   "<button data-action=\"loose\" class=\"loose-btn\">散码</button>"
        +   "<button data-action=\"delete\" class=\"danger\">🗑 删除</button>"
        + "</div>";
      var refs = {
        card: card,
        imgWrap: $("[data-role=\"imgWrap\"]", card),
        mainImg: $("[data-role=\"mainImg\"]", card),
        marksLayer: $("[data-role=\"marksLayer\"]", card),
        countBadge: $("[data-role=\"countBadge\"]", card),
        looseLabel: $("[data-role=\"looseLabel\"]", card),
        scale: im.mark_scale || 1
      };
      CARDS[im.id] = refs;
      if (!IS_CLOSED) {
        refs.imgWrap.addEventListener("click", function (e) { onImgClick(e, im, refs); });
      } else {
        refs.imgWrap.style.cursor = "default";
      }
      refs.mainImg.addEventListener("load", function () { renderMarks(im, refs); });
      $("[data-action=\"undo\"]", card).addEventListener("click", function () { doUndo(im, refs); });
      $("[data-action=\"bigger\"]", card).addEventListener("click", function () { adjustScale(im, refs, SCALE_STEP); });
      $("[data-action=\"smaller\"]", card).addEventListener("click", function () { adjustScale(im, refs, 1 / SCALE_STEP); });
      $("[data-action=\"loose\"]", card).addEventListener("click", function () { openLoose(im, refs); });
      $("[data-action=\"delete\"]", card).addEventListener("click", function () { doDelete(im, refs); });
      listEl.appendChild(card);
      refs.looseLabel.textContent = "散码 " + im.loose_count;
      refs.countBadge.textContent = im.n_marks;
      renderMarks(im, refs);
    });
  }
  function renderMarks(im, refs) {
    refs.marksLayer.innerHTML = "";
    var marks = im.marks || [];
    var scale = refs.scale || 1;
    var fontPx = Math.round(13 * scale);
    marks.forEach(function (mk) {
      var span = document.createElement("span");
      span.className = "place-mark";
      span.textContent = mk.seq;
      span.style.left = (mk.x_ratio * 100).toFixed(3) + "%";
      span.style.top = (mk.y_ratio * 100).toFixed(3) + "%";
      span.style.fontSize = fontPx + "px";
      refs.marksLayer.appendChild(span);
    });
  }
  function onImgClick(e, im, refs) {
    if (IS_CLOSED) return;
    var rect = refs.imgWrap.getBoundingClientRect();
    var x = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    var y = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));
    fetch(API.marks(im.id), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ x_ratio: x, y_ratio: y })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.success) { alert(d.error || "添加失败"); return; }
      im.marks = d.marks; im.n_marks = im.marks.length;
      refs.countBadge.textContent = im.n_marks;
      renderMarks(im, refs); updateTotal();
    }).catch(function () { alert("网络异常"); });
  }
  function doUndo(im, refs) {
    if (IS_CLOSED) return;
    if (!im.marks.length) return;
    fetch(API.undo(im.id), { method: "DELETE" }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.success) { alert(d.error || "撤销失败"); return; }
      im.marks = d.marks || []; im.n_marks = im.marks.length;
      refs.countBadge.textContent = im.n_marks;
      renderMarks(im, refs); updateTotal();
    });
  }
  function adjustScale(im, refs, factor) {
    var next = Math.max(0.3, Math.min(4.0, (refs.scale || 1) * factor));
    fetch(API.scale(im.id), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scale: next })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.success) { alert(d.error || "缩放失败"); return; }
      refs.scale = d.scale; im.mark_scale = d.scale;
      renderMarks(im, refs);
    });
  }
  function doDelete(im, refs) {
    if (IS_CLOSED) { alert("会话已完结, 请先重新打开"); return; }
    if (!confirm("确定删除该图片（含所有计数点）？")) return;
    fetch(API.delImg(im.id), { method: "DELETE" }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.success) { alert(d.error || "删除失败"); return; }
      loadImages();
    });
  }
  var looseMask = document.getElementById("placeLooseMask");
  var looseInput = document.querySelector("[data-role=\"looseInput\"]", looseMask);
  var looseTarget = null;
  function openLoose(im, refs) {
    if (IS_CLOSED) { alert("会话已完结, 请先重新打开"); return; }
    looseTarget = { im: im, refs: refs };
    looseInput.value = im.loose_count || 0;
    looseMask.hidden = false;
    setTimeout(function () { looseInput.focus(); }, 50);
  }
  document.querySelector("[data-action=\"looseCancel\"]", looseMask).addEventListener("click", function () { looseMask.hidden = true; });
  document.querySelector("[data-action=\"looseSave\"]", looseMask).addEventListener("click", function () {
    if (!looseTarget) { looseMask.hidden = true; return; }
    var t = looseTarget;
    var v = parseInt(looseInput.value || "0", 10);
    var count = (isNaN(v) || v < 0) ? 0 : v;
    fetch(API.loose(t.im.id), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ count: count })
    }).then(function (r) { return r.json(); }).then(function (d) {
      looseMask.hidden = true;
      if (!d.success) { alert(d.error || "保存失败"); return; }
      t.im.loose_count = d.count;
      if (t.refs) t.refs.looseLabel.textContent = "散码 " + d.count;
      updateTotal();
    });
  });

  // Edit meta
  var editMask = document.getElementById("mpcEditModal");
  var editTitle = document.getElementById("mpcEditTitle");
  var editExpected = document.getElementById("mpcEditExpected");
  var editUnit = document.getElementById("mpcEditUnit");
  var editRemark = document.getElementById("mpcEditRemark");
  var editBtn = document.querySelector(".js-edit-meta");
  if (editBtn) editBtn.addEventListener("click", function () {
    editTitle.value = C.title || "";
    editExpected.value = (C.expectedCount === null || C.expectedCount === undefined) ? "" : C.expectedCount;
    editUnit.value = UNIT;
    editRemark.value = C.remark || "";
    editMask.hidden = false;
  });
  var ec1 = document.querySelector("[data-action=\"editCancel\"]", editMask);
  var ec2 = document.querySelector("[data-action=\"editSave\"]", editMask);
  if (ec1) ec1.addEventListener("click", function () { editMask.hidden = true; });
  if (ec2) ec2.addEventListener("click", function () {
    var payload = {
      title: editTitle.value,
      expected_count: editExpected.value === "" ? null : parseInt(editExpected.value, 10),
      unit: editUnit.value || "支",
      remark: editRemark.value
    };
    fetch(API.sessionUpdate(), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (r) { return r.json(); }).then(function (d) {
      editMask.hidden = true;
      if (!d.success) { alert(d.error || "保存失败"); return; }
      location.reload();
    });
  });

  var closeBtn = document.querySelector(".js-close-session");
  if (closeBtn) closeBtn.addEventListener("click", function () {
    var isClosed = this.dataset.status === "closed";
    var url = isClosed ? API.sessionReopen() : API.sessionClose();
    var verb = isClosed ? "重新打开" : "完结";
    if (!confirm("确定要" + verb + "该点数组吗？")) return;
    fetch(url, { method: "POST" }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.success) { alert(d.error || "操作失败"); return; }
      location.reload();
    });
  });
  var delSessBtn = document.querySelector(".js-delete-session");
  if (delSessBtn) delSessBtn.addEventListener("click", function () {
    if (!confirm("确定删除整个点数组（含所有图与计数点）？该操作不可恢复。")) return;
    fetch(API.sessionDelete(), { method: "DELETE" }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.success) { alert(d.error || "删除失败"); return; }
      window.location.href = "/tools/point-count/";
    });
  });

  if (IS_CLOSED) {
    var cb = document.querySelector("[data-action=\"capture\"]", root);
    var ab = document.querySelector("[data-action=\"album\"]", root);
    if (cb) cb.disabled = true;
    if (ab) ab.disabled = true;
  } else {
    document.querySelector("[data-action=\"capture\"]", root).addEventListener("click", function () { captureInput.click(); });
    document.querySelector("[data-action=\"album\"]", root).addEventListener("click", function () { albumInput.click(); });
  }
  captureInput.addEventListener("change", function (e) { onPickFile(e.target.files); });
  albumInput.addEventListener("change", function (e) { onPickFile(e.target.files); });

  var previewDeg = 0;
  var previewImgSrc = null;
  var mask = document.getElementById("placePreviewMask");
  var canvas = document.getElementById("placePreviewCanvas");

  function onPickFile(files) {
    if (!files || !files.length) return;
    var file = files[0];
    var reader = new FileReader();
    reader.onload = function () {
      previewImgSrc = reader.result;
      previewDeg = 0;
      drawPreview();
      mask.hidden = false;
    };
    reader.readAsDataURL(file);
  }
  function drawPreview() {
    if (!previewImgSrc) return;
    var img = new Image();
    img.onload = function () {
      var ctx = canvas.getContext("2d");
      var w = img.width, h = img.height;
      if (previewDeg % 180 === 90) { canvas.width = h; canvas.height = w; }
      else { canvas.width = w; canvas.height = h; }
      ctx.save();
      ctx.translate(canvas.width / 2, canvas.height / 2);
      ctx.rotate((previewDeg * Math.PI) / 180);
      ctx.drawImage(img, -w / 2, -h / 2);
      ctx.restore();
    };
    img.src = previewImgSrc;
  }
  document.getElementById("placeRotateLeft").addEventListener("click", function () {
    previewDeg = (previewDeg + 270) % 360; drawPreview();
  });
  document.getElementById("placeRotateRight").addEventListener("click", function () {
    previewDeg = (previewDeg + 90) % 360; drawPreview();
  });
  document.getElementById("placeRetake").addEventListener("click", function () {
    mask.hidden = true; previewImgSrc = null;
  });
  document.getElementById("placeConfirm").addEventListener("click", function () {
    if (IS_CLOSED) { mask.hidden = true; alert("会话已完结"); return; }
    var dataUrl = canvas.toDataURL("image/jpeg", 0.85);
    mask.hidden = true;
    uploadProgress.hidden = false;
    fetch(API.images(), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ image: dataUrl })
    }).then(function (r) { return r.json(); }).then(function (d) {
      uploadProgress.hidden = true;
      if (!d.success) { alert(d.error || "上传失败"); return; }
      loadImages();
    }).catch(function () { uploadProgress.hidden = true; alert("上传失败"); });
  });

  loadImages();
})();
