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
    const b = badgeElForStatus(img.match_status);
    const badge = $('[data-role="badge"]', card);
    if (badge) { badge.className = "badge " + b.cls; badge.textContent = b.text; }
    if (!line) return;
    if (!img.match_status) {
      line.className = "status-line"; line.textContent = "尚未拍照";
      return;
    }
    const blurHint = (img.reason || "").includes("模糊") ? " · 图像可能模糊，建议重拍" : "";
    const text = img.match_status === "green" ? "最近一次识别：标签与规格一致"
      : img.match_status === "yellow" ? "AI 判定与标签不一致"
      : "OCR 失败 / AI 不符";
    line.className = "status-line" + (img.match_status !== "green" ? " warn" : "");
    line.innerHTML = `<span class="dot"></span>${text}${blurHint} <a href="#" data-role="detail">详情</a>`;
    const confirm = $('[data-action="confirm"]', card);
    if (confirm) {
      confirm.hidden = !(img.match_status !== "green" && img.match_status !== null);
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
    fd.append("image", file);
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
    fd.append("image", file);
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

  function bindRecord(card) {
    const recordPk = card.dataset.recordId;
    const recordName = $(".product-name", card)?.textContent || "";
    const input = $('[data-role="record-input"]', card);
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
      let check;
      if (typeof window.mobileBlurCheck === "function") {
        check = await window.mobileBlurCheck(file);
      } else {
        check = { ok: true, variance: -1, dataURL: null, skipped: true };
      }
      if (!check.ok) { alert("图太糊，请重拍"); input.value = ""; return; }
      const orderPk = $(".mobile-order").dataset.orderId;
      const dataURL = check.dataURL;
      if (dataURL) {
        const blob = await (await fetch(dataURL)).blob();
        await uploadRecordImage(recordPk, orderPk, blob, card, recordName);
      } else {
        await uploadRecordImage(recordPk, orderPk, file, card, recordName);
      }
      input.value = "";
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
      let check;
      if (typeof window.mobileBlurCheck === "function") {
        check = await window.mobileBlurCheck(file);
      } else {
        check = { ok: true, variance: -1, dataURL: null, skipped: true };
      }
      if (!check.ok) { alert("图太糊，请重拍"); input.value = ""; return; }
      const thumbEl = $(`[data-thumb-source="${pendingSource}"]`, section);
      const dataURL = check.dataURL;
      const fileToUpload = dataURL ? await (await fetch(dataURL)).blob() : file;
      await uploadOverallImage(orderPk, fileToUpload, pendingSource, thumbEl);
      input.value = "";
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    $$(".product-card").forEach(bindRecord);
    const overall = $(".overall");
    if (overall) bindOverall(overall);
  });
})();
