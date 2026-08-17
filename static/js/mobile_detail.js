(function () {
  const POLL_MS = 1500;
  const POLL_TIMEOUT_MS = 150000;
  const API = "/api/v1/shipping-orders";
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  function badgeElForStatus(status) {
    if (status === "green") return { cls: "done", text: "✓ 通过" };
    if (status === "yellow") return { cls: "warn", text: "⚠ 待确认" };
    if (status === "red") return { cls: "warn", text: "✕ 不符" };
    return { cls: "todo", text: "待拍" };
  }

  function updateStatusLine(card, img) {
    const line = $('[data-role="status"]', card);
    // 人工覆盖:黄/红 + human_verified=1 → 视为已确认(green)
    const confirmed = !!(img.human_verified && (img.match_status === "yellow" || img.match_status === "red"));
    const eff = confirmed ? "green" : img.match_status;
    const b = confirmed ? { cls: "done", text: "✓ 已确认" } : badgeElForStatus(img.match_status);
    const badge = $('[data-role="badge"]', card);
    if (badge) { badge.className = "badge " + b.cls; badge.textContent = b.text; }
    if (!line) return;
    if (!img.match_status) {
      line.className = "status-line"; line.textContent = "尚未拍照";
      return;
    }
    const blurHint = (img.reason || "").includes("模糊") ? " · 图像可能模糊，建议重拍" : "";
    const text = confirmed ? "已人工确认通过"
      : img.match_status === "green" ? "最近一次识别：标签与规格一致"
      : img.match_status === "yellow" ? "AI 判定与标签不一致"
      : "OCR 失败 / AI 不符";
    line.className = "status-line" + (eff !== "green" ? " warn" : "");
    line.innerHTML = `<span class="dot"></span>${text}${blurHint} <a href="#" data-role="detail" data-image-id="${img.image_id || card.dataset.lastImageId || ''}">详情</a>`;
    const confirm = $('[data-action="confirm"]', card);
    if (confirm) {
      confirm.hidden = !(eff !== "green" && eff !== null);
    }
  }

  async function poll(imageId, card, recordName) {
    const started = Date.now();
    while (Date.now() - started < POLL_TIMEOUT_MS) {
      await new Promise(r => setTimeout(r, POLL_MS));
      const resp = await fetch(`${API}/images/${imageId}/match-status`);
      if (!resp.ok) continue;
      const body = await resp.json();
      if (body.processing) continue;
      const img = body.image;
      if (!img) return;
      updateStatusLine(card, img);
      return img;
    }
  }

  function setProgress(card, text) {
    const el = $('[data-role="upload-progress"]', card);
    if (!el) return;
    if (text) { el.textContent = text; el.hidden = false; }
    else { el.hidden = true; }
  }

  function appendImageToOrderGrid(relPath, alt, sourceTag) {
    let grid = $('[data-role="order-images-grid"]');
    if (!grid) {
      const section = document.createElement("section");
      section.className = "order-images";
      section.innerHTML = `<h3 data-role="order-images-title">📷 本单图片（<span data-role="order-images-count">1</span>）</h3><div class="record-images" data-role="order-images-grid"></div>`;
      const root = $(".mobile-order");
      if (root) root.appendChild(section);
      grid = $('[data-role="order-images-grid"]');
    }
    if (!grid) return;
    const cell = document.createElement("div");
    cell.className = "record-image-cell";
    const img = document.createElement("img");
    img.src = `/upload/${relPath}`;
    img.alt = alt || "";
    img.loading = "lazy";
    img.onclick = () => showImgPreview(img.src);
    cell.appendChild(img);
    if (sourceTag) {
      const tag = document.createElement("span");
      tag.className = "img-source-tag";
      tag.textContent = sourceTag;
      cell.appendChild(tag);
    }
    grid.appendChild(cell);
    const countEl = $('[data-role="order-images-count"]');
    const titleEl = $('[data-role="order-images-title"]');
    if (countEl) countEl.textContent = String(grid.children.length);
    if (titleEl && !countEl) titleEl.innerHTML = `📷 本单图片（${grid.children.length}）`;
  }

  async function uploadRecordImage(recordPk, orderPk, file, card, recordName) {
    const fd = new FormData();
    // Blob(如旋转后的预览图)无 filename,补 .jpg 以免后端 check_uploaded_image 因空扩展名拒绝
    fd.append("image", file, file.name || "photo.jpg");
    fd.append("source", "upload");
    setProgress(card, "⏳ 上传中…");
    const resp = await fetch(`${API}/records/${recordPk}/images`, { method: "POST", body: fd });
    if (!resp.ok) {
      const t = await resp.text();
      setProgress(card, null);
      alert("上传失败：" + t);
      return;
    }
    setProgress(card, "🔄 识别中…");
    const body = await resp.json();
    const imageId = body.images[0].image_id;
    const relPath = body.images[0].image;
    card.dataset.lastImageId = imageId;
    // 立刻把新图追加到底部 grid(无需等 OCR 完成)
    if (relPath) appendImageToOrderGrid(relPath, body.images[0].original_name);
    await poll(imageId, card, recordName);
    setProgress(card, null);  // 识别完成,隐藏进度
  }

  async function uploadOverallImage(orderPk, file, sourceTag, thumbEl) {
    const fd = new FormData();
    // Blob(如旋转后的预览图)无 filename,补 .jpg 以免后端 check_uploaded_image 因空扩展名拒绝
    fd.append("image", file, file.name || "photo.jpg");
    fd.append("source", "upload");
    fd.append("source_tag", sourceTag);
    fd.append("original_name", `${sourceTag}-${Date.now()}.jpg`);
    const overallSection = thumbEl && thumbEl.closest('.overall');
    const progressEl = overallSection && $('[data-role="overall-progress"]', overallSection);
    if (progressEl) { progressEl.textContent = `⏳ 上传${sourceTag}中…`; progressEl.hidden = false; }
    const resp = await fetch(`${API}/${orderPk}/images`, { method: "POST", body: fd });
    if (!resp.ok) { if (progressEl) progressEl.hidden = true; alert("整体图上传失败"); return; }
    const body = await resp.json();
    const relPath = body.image;
    if (relPath) appendImageToOrderGrid(relPath, sourceTag, body.source_tag);
    if (thumbEl) {
      thumbEl.classList.add("filled");
      const now = new Date();
      const hh = String(now.getHours()).padStart(2, "0");
      const mm = String(now.getMinutes()).padStart(2, "0");
      thumbEl.textContent = `${sourceTag} ✓ ${hh}:${mm}`;
    }
    if (progressEl) progressEl.hidden = true;
    const countEl = $('[data-role="overall-count"]');
    if (countEl) {
      const current = parseInt(countEl.textContent, 10) || 0;
      const next = Math.min(current + 1, 1);
      countEl.textContent = `${next} / 1`;
    }
  }

  // ── 拍照预览 + 旋转弹窗 ──
  let previewState = null; // { drawable, w, h, angle, onConfirm, input }

  function closePreview() {
    const mask = $("#photoPreviewMask");
    if (mask) mask.hidden = true;
    if (previewState && previewState.drawable && typeof previewState.drawable.close === "function") {
      try { previewState.drawable.close(); } catch (e) {}
    }
    previewState = null;
  }

  function renderPreview() {
    if (!previewState) return;
    const { drawable, w, h, angle } = previewState;
    const swap = angle % 180 !== 0;
    const cw = swap ? h : w;
    const ch = swap ? w : h;
    const canvas = $("#photoPreviewCanvas");
    const maxDim = 1600;
    const scale = Math.max(cw, ch) > maxDim ? maxDim / Math.max(cw, ch) : 1;
    canvas.width = Math.round(cw * scale);
    canvas.height = Math.round(ch * scale);
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.save();
    ctx.translate(canvas.width / 2, canvas.height / 2);
    ctx.rotate((angle * Math.PI) / 180);
    ctx.scale(scale, scale);
    ctx.drawImage(drawable, -w / 2, -h / 2);
    ctx.restore();
  }

  async function openPreview(file, input, onConfirm) {
    let drawable = null, w = 0, h = 0;
    if (window.createImageBitmap) {
      try {
        const bmp = await createImageBitmap(file, { imageOrientation: "from-image" });
        drawable = bmp; w = bmp.width; h = bmp.height;
      } catch (e) { drawable = null; }
    }
    if (!drawable) {
      const dataURL = await new Promise((res, rej) => {
        const r = new FileReader();
        r.onload = () => res(r.result);
        r.onerror = rej;
        r.readAsDataURL(file);
      });
      const img = new Image();
      img.src = dataURL;
      await new Promise((res, rej) => { img.onload = res; img.onerror = rej; });
      drawable = img; w = img.naturalWidth; h = img.naturalHeight;
    }
    previewState = { drawable, w, h, angle: 0, onConfirm, input };
    const mask = $("#photoPreviewMask");
    if (mask) mask.hidden = false;
    renderPreview();
  }

  function bindPreviewControls() {
    const mask = $("#photoPreviewMask");
    const box = mask && mask.querySelector(".photo-preview-box");
    if (mask && box) {
      mask.addEventListener("click", (e) => { if (e.target === mask) closePreview(); });
    }
    const left = $("#photoRotateLeft");
    const right = $("#photoRotateRight");
    const retake = $("#photoRetake");
    const confirm = $("#photoConfirm");
    if (left) left.addEventListener("click", () => {
      if (previewState) { previewState.angle = (previewState.angle + 270) % 360; renderPreview(); }
    });
    if (right) right.addEventListener("click", () => {
      if (previewState) { previewState.angle = (previewState.angle + 90) % 360; renderPreview(); }
    });
    if (retake) retake.addEventListener("click", () => {
      const inp = previewState && previewState.input;
      closePreview();
      if (inp) { try { inp.value = ""; inp.click(); } catch (e) {} }
    });
    if (confirm) confirm.addEventListener("click", () => {
      if (!previewState) return;
      const canvas = $("#photoPreviewCanvas");
      const onConfirm = previewState.onConfirm;
      closePreview();
      canvas.toBlob(async (blob) => {
        if (!blob) { alert("图片处理失败，请重拍"); return; }
        await onConfirm(blob);
      }, "image/jpeg", 0.92);
    });
  }

  // ── 识别详情弹窗（OCR + AI 推理,不含提示词） ──
  async function openOcrDetail(imageId) {
    const mask = $("#ocrDetailMask");
    const body = $("#ocrDetailBody");
    const title = $("#ocrDetailTitle");
    if (!mask || !body || !imageId) return;
    mask.hidden = false;
    body.innerHTML = '<div class="ocr-detail-loading">加载中…</div>';
    try {
      const resp = await fetch(`${API}/images/${imageId}/ocr-detail`);
      if (!resp.ok) { body.innerHTML = '<div class="ocr-detail-err">加载失败</div>'; return; }
      const data = await resp.json();
      const d = data.detail || {};
      title.textContent = (d.product_name || "识别详情") + (d.specification ? " · " + d.specification : "");
      const esc = (s) => String(s == null ? "" : s).replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
      const st = (d.ai && d.ai.ai_match_status) || d.effective_match_status || d.match_status || "";
      const stText = st === "green" ? "✓ 一致" : st === "yellow" ? "⚠ 待确认" : st === "red" ? "✕ 不符" : (st || "—");
      let html = "";
      // OCR 段
      html += '<div class="ocr-detail-sec"><div class="ocr-detail-sec-h">OCR 识别结果</div>';
      html += '<div class="ocr-detail-text">' + (d.ocr && d.ocr.ocr_text ? esc(d.ocr.ocr_text) : '<span class="muted">无</span>') + '</div></div>';
      // AI 段（仅状态 + 推理理由,不含 prompt_payload 提示词）
      html += '<div class="ocr-detail-sec"><div class="ocr-detail-sec-h">AI 推理结果</div>';
      if (d.ai) {
        html += `<div class="ocr-detail-ai-status ${st}">${stText}</div>`;
        html += '<div class="ocr-detail-text">' + (d.ai.ai_match_reason ? esc(d.ai.ai_match_reason) : '<span class="muted">无推理说明</span>') + '</div>';
      } else {
        html += '<div class="muted">未做 AI 判别</div>';
      }
      html += '</div>';
      // 人工确认（简短一行）
      if (d.human) {
        html += '<div class="ocr-detail-sec"><div class="ocr-detail-sec-h">人工确认</div>';
        html += '<div class="ocr-detail-text">' + esc((d.human.human_status || "") + (d.human.operator_name ? " · " + d.human.operator_name : "")) + '</div></div>';
      }
      body.innerHTML = html;
    } catch (e) {
      body.innerHTML = '<div class="ocr-detail-err">加载失败</div>';
    }
  }

  function bindRecord(card) {
    const recordPk = card.dataset.recordId;
    const recordName = $(".product-name", card)?.textContent || "";
    const input = $('[data-role="record-input"]', card);
    // 详情链接(动态生成)用事件委托
    card.addEventListener("click", (e) => {
      const a = e.target.closest('[data-role="detail"]');
      if (a) {
        e.preventDefault();
        const iid = a.dataset.imageId || card.dataset.lastImageId;
        if (iid) openOcrDetail(iid);
      }
    });
    if (!input) return;  // 板材类无拍照 input,跳过事件绑定
    $$('[data-action]', card).forEach(btn => {
      if (btn.dataset.action === "capture" || btn.dataset.action === "album") {
        btn.addEventListener("click", () => {
          if (!input) return;
          if (btn.dataset.action === "album") input.removeAttribute("capture");
          else input.setAttribute("capture", "environment");
          input.click();
        });
      } else if (btn.dataset.action === "confirm") {
        btn.addEventListener("click", async () => {
          const iid = card.dataset.lastImageId;
          if (!iid) return;
          await fetch(`${API}/images/${iid}/manual-verify`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ verified: true }),
          });
          const card2 = btn.closest(".product-card");
          await poll(iid, card2, recordName);
        });
      }
    });
    input.addEventListener("change", async () => {
      const file = input.files[0];
      if (!file) return;
      input.value = ""; // 立即清空,便于重拍后同文件再次触发 change
      openPreview(file, input, async (blob) => {
        let check;
        if (typeof window.mobileBlurCheck === "function") {
          check = await window.mobileBlurCheck(blob);
        } else {
          check = { ok: true, variance: -1, dataURL: null, skipped: true };
        }
        if (!check.ok) { alert("图太糊，请重拍"); return; }
        const orderPk = $(".mobile-order").dataset.orderId;
        await uploadRecordImage(recordPk, orderPk, blob, card, recordName);
      });
    });
  }

  function bindOverall(section) {
    const orderPk = section.dataset.orderId;
    const input = $('[data-role="overall-input"]', section);
    let pendingSource = null;
    $$('[data-overall-source]', section).forEach(btn => {
      btn.addEventListener("click", () => {
        pendingSource = btn.dataset.overallSource;
        input.click();
      });
    });
    input.addEventListener("change", async () => {
      const file = input.files[0];
      if (!file) return;
      input.value = ""; // 立即清空,便于重拍后同文件再次触发 change
      openPreview(file, input, async (blob) => {
        let check;
        if (typeof window.mobileBlurCheck === "function") {
          check = await window.mobileBlurCheck(blob);
        } else {
          check = { ok: true, variance: -1, dataURL: null, skipped: true };
        }
        if (!check.ok) { alert("图太糊，请重拍"); return; }
        const thumbEl = $(`[data-thumb-source="${pendingSource}"]`, section);
        await uploadOverallImage(orderPk, blob, pendingSource, thumbEl);
      });
    });
  }

  function bindOcrDetailControls() {
    const mask = $("#ocrDetailMask");
    if (mask) mask.addEventListener("click", (e) => { if (e.target === mask) mask.hidden = true; });
    const close = $("#ocrDetailClose");
    if (close) close.addEventListener("click", () => { if (mask) mask.hidden = true; });
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindPreviewControls();
    bindOcrDetailControls();
    $$(".product-card").forEach(bindRecord);
    const overall = $(".overall");
    if (overall) bindOverall(overall);
  });
})();
