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

  async function uploadRecordImage(recordPk, orderPk, file, card, recordName) {
    const fd = new FormData();
    fd.append("image", file);
    fd.append("source", "upload");
    const resp = await fetch(`${API}/records/${recordPk}/images`, { method: "POST", body: fd });
    if (!resp.ok) {
      const t = await resp.text();
      alert("上传失败：" + t);
      return;
    }
    const body = await resp.json();
    const imageId = body.images[0].image_id;
    card.dataset.lastImageId = imageId;
    await poll(imageId, card, recordName);
  }

  async function uploadOverallImage(orderPk, file, sourceTag, thumbEl) {
    const fd = new FormData();
    fd.append("image", file);
    fd.append("source", "upload");
    fd.append("original_name", `${sourceTag}-${Date.now()}.jpg`);
    const resp = await fetch(`${API}/${orderPk}/images`, { method: "POST", body: fd });
    if (!resp.ok) { alert("整体图上传失败"); return; }
    if (thumbEl) { thumbEl.classList.add("filled"); thumbEl.textContent = `${sourceTag} ✓`; }
    const countEl = $('[data-role="overall-count"]');
    if (countEl) {
      const current = parseInt(countEl.textContent, 10) || 0;
      const next = Math.min(current + 1, 1);  // 整体图 0/1；后续按"成功后替换"扩展
      countEl.textContent = `${next} / 1`;
    }
  }

  function bindRecord(card) {
    const recordPk = card.dataset.recordId;
    const recordName = $(".product-name", card)?.textContent || "";
    const input = $('[data-role="record-input"]', card);
    $$('[data-action]', card).forEach(btn => {
      if (btn.dataset.action === "capture" || btn.dataset.action === "album") {
        btn.addEventListener("click", () => {
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
