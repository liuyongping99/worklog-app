/* 独立点数工具 - 列表页 JS */
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var form = document.getElementById("pcNewForm");
  var toggleBtn = document.getElementById("pcToggleFormBtn");
  var cancelBtn = document.getElementById("pcNewCancelBtn");
  var submitBtn = document.getElementById("pcNewSubmitBtn");
  var titleEl = document.getElementById("pcNewTitle");
  var expectedEl = document.getElementById("pcNewExpected");
  var unitEl = document.getElementById("pcNewUnit");
  var remarkEl = document.getElementById("pcNewRemark");

  function showForm(show) {
    if (show) { form.hidden = false; setTimeout(function () { titleEl.focus(); }, 30); }
    else { form.hidden = true; }
  }
  if (toggleBtn) toggleBtn.addEventListener("click", function () { showForm(form.hidden); });
  if (cancelBtn) cancelBtn.addEventListener("click", function () { showForm(false); });
  if (submitBtn) submitBtn.addEventListener("click", function () {
    var title = (titleEl.value || "").trim();
    if (!title) { alert("请填写标题"); titleEl.focus(); return; }
    var payload = {
      title: title,
      expected_count: expectedEl.value === "" ? null : parseInt(expectedEl.value, 10),
      unit: (unitEl.value || "支").trim() || "支",
      remark: (remarkEl.value || "").trim()
    };
    submitBtn.disabled = true;
    fetch("/api/v1/point-count/sessions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (r) { return r.json(); }).then(function (d) {
      submitBtn.disabled = false;
      if (!d.success) { alert(d.error || "创建失败"); return; }
      window.location.href = "/tools/point-count/session/" + d.session_id;
    }).catch(function () { submitBtn.disabled = false; alert("网络异常"); });
  });

  document.addEventListener("click", function (e) {
    var btn = e.target.closest(".js-close-btn");
    if (!btn) return;
    var id = btn.dataset.id;
    var status = btn.dataset.status;
    var url = "/api/v1/point-count/sessions/" + id + (status === "closed" ? "/reopen" : "/close");
    var verb = status === "closed" ? "重新打开" : "完结";
    if (!confirm("确定要" + verb + "该点数组吗？")) return;
    fetch(url, { method: "POST" }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.success) { alert(d.error || "操作失败"); return; }
      location.reload();
    });
  });

  document.addEventListener("click", function (e) {
    var btn = e.target.closest(".js-delete-btn");
    if (!btn) return;
    var id = btn.dataset.id;
    if (!confirm("确定删除该点数组（含所有图与计数点）？该操作不可恢复。")) return;
    fetch("/api/v1/point-count/sessions/" + id, { method: "DELETE" }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.success) { alert(d.error || "删除失败"); return; }
      location.reload();
    });
  });

  document.addEventListener("click", function (e) {
    var btn = e.target.closest(".js-export-btn");
    if (!btn) return;
    var id = btn.dataset.id;
    fetch("/api/v1/point-count/sessions/" + id + "/export").then(function (r) { return r.json(); }).then(function (d) {
      if (!d.success) { alert(d.error || "导出失败"); return; }
      var blob = new Blob([JSON.stringify(d.export, null, 2)], { type: "application/json" });
      var a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "point-count-session-" + id + ".json";
      a.click();
      URL.revokeObjectURL(a.href);
    });
  });
})();
