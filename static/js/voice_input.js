// 语音录入弹框 — 录音 + 候选确认
(function () {
  var modal = document.getElementById('voiceModal');
  var recordView = document.getElementById('voiceRecordView');
  var loadingView = document.getElementById('voiceLoadingView');
  var confirmView = document.getElementById('voiceConfirmView');
  var recordBtn = document.getElementById('voiceRecordBtn');
  var recordStatus = document.getElementById('voiceRecordStatus');
  var recognizedTextEl = document.getElementById('voiceRecognizedText');
  var candidatesEl = document.getElementById('voiceCandidates');
  var confirmBtn = document.getElementById('voiceConfirmBtn');
  var addRowBtn = document.getElementById('voiceAddRowBtn');

  var mediaRecorder = null;
  var audioChunks = [];
  var recordStartTime = 0;
  var statusTimer = null;
  var currentResult = null;  // 识别结果 JSON
  var rowCounter = 0;

  function showModal() {
    modal.classList.add('show');
    showView('record');
  }

  function hideModal() {
    modal.classList.remove('show');
    stopRecording();
  }

  function showView(name) {
    recordView.style.display = name === 'record' ? 'block' : 'none';
    loadingView.style.display = name === 'loading' ? 'block' : 'none';
    confirmView.style.display = name === 'confirm' ? 'block' : 'none';
  }

  async function startRecording() {
    try {
      var stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      recordStatus.textContent = '❌ 请允许麦克风权限';
      return;
    }
    audioChunks = [];
    mediaRecorder = new MediaRecorder(stream, { mimeType: 'audio/webm' });
    mediaRecorder.ondataavailable = function (e) {
      audioChunks.push(e.data);
    };
    mediaRecorder.onstop = function () {
      stream.getTracks().forEach(function (t) { t.stop(); });
      uploadAndRecognize();
    };
    mediaRecorder.start();
    recordBtn.classList.add('recording');
    recordBtn.textContent = '⏹';
    recordStartTime = Date.now();
    recordStatus.textContent = '录音中... 0s';
    statusTimer = setInterval(function () {
      var sec = Math.floor((Date.now() - recordStartTime) / 1000);
      recordStatus.textContent = '录音中... ' + sec + 's';
    }, 200);
  }

  function stopRecording() {
    if (statusTimer) clearInterval(statusTimer);
    statusTimer = null;
    if (mediaRecorder && mediaRecorder.state === 'recording') {
      mediaRecorder.stop();
    }
    recordBtn.classList.remove('recording');
    recordBtn.textContent = '🎙️';
  }

  async function uploadAndRecognize() {
    showView('loading');
    var blob = new Blob(audioChunks, { type: 'audio/webm' });
    var formData = new FormData();
    formData.append('audio', blob, 'recording.webm');
    try {
      var r = await fetch('/api/v1/voice/recognize', { method: 'POST', body: formData });
      var data = await r.json();
      if (!data.success) {
        alert('识别失败:' + (data.error || '未知错误') + '\n' + (data.hint || ''));
        showView('record');
        return;
      }
      currentResult = data;
      renderConfirmView(data);
    } catch (e) {
      alert('网络错误:' + e.message);
      showView('record');
    }
  }

  function renderConfirmView(data) {
    // 顶部:识别原文 + 匹配路径统计
    var procText = '已识别:' + (data.recognized_text || '');
    var items = data.items || [];
    var procStats = { mapping: 0, fuzzy: 0, llm_fallback: 0, none: 0 };
    items.forEach(function (it) {
      var srcs = (it.candidates || []).map(function (c) { return c.source; });
      if (srcs.indexOf('mapping') >= 0) procStats.mapping++;
      else if (srcs.indexOf('fuzzy') >= 0) procStats.fuzzy++;
      else if (srcs.indexOf('llm_fallback') >= 0) procStats.llm_fallback++;
      else procStats.none++;
    });
    var procSummary = ' | 匹配:映射表 ' + procStats.mapping +
                      ' / 模糊 ' + procStats.fuzzy +
                      ' / LLM兜底 ' + procStats.llm_fallback +
                      ' / 未命中 ' + procStats.none;
    recognizedTextEl.textContent = procText + procSummary;
    recognizedTextEl.style.fontSize = '0.8rem';

    candidatesEl.innerHTML = '';
    rowCounter = 0;
    items.forEach(function (item) {
      addRow(item);
    });
    showView('confirm');
  }

  function addRow(item) {
    var rowId = 'voice-row-' + (++rowCounter);
    var row = document.createElement('div');
    row.className = 'candidate-row';
    row.id = rowId;
    row.dataset.phrasePart = item.phrase_part || '';
    row.dataset.specPart = item.spec_part || '';

    var phraseCell = document.createElement('div');
    phraseCell.className = 'phrase';
    phraseCell.textContent = item.phrase_part || '(手动添加)';
    row.appendChild(phraseCell);

    var productSelect = document.createElement('select');
    productSelect.dataset.field = 'product';
    var candidates = item.candidates || [];
    candidates.forEach(function (c) {
      var opt = document.createElement('option');
      opt.value = c.product_id;
      opt.dataset.spec = c.specification || '';
      // source 中文映射 + score
      var srcLabel = { mapping: '🎯映射', fuzzy: '🔍模糊', llm_fallback: '🤖LLM' }[c.source] || c.source;
      var scoreTxt = (c.score != null) ? (' ' + Math.round(c.score)) : '';
      var nameTxt = c.product_name
        ? (c.product_name + ' - ' + (c.specification || '无规格'))
        : ('商品#' + c.product_id);
      opt.textContent = nameTxt + '  [' + srcLabel + scoreTxt + ']';
      productSelect.appendChild(opt);
    });
    var manualOpt = document.createElement('option');
    manualOpt.value = '__manual__';
    manualOpt.textContent = '— 手动选择商品 —';
    productSelect.appendChild(manualOpt);
    row.appendChild(productSelect);

    var specSelect = document.createElement('select');
    specSelect.dataset.field = 'specification';
    specSelect.innerHTML = '<option value="">— 选规格 —</option>';
    row.appendChild(specSelect);

    var qtyInput = document.createElement('input');
    qtyInput.type = 'number';
    qtyInput.value = item.quantity != null ? item.quantity : '';
    qtyInput.placeholder = '数量';
    qtyInput.dataset.field = 'quantity';
    row.appendChild(qtyInput);

    var unitInput = document.createElement('input');
    unitInput.type = 'text';
    unitInput.value = item.unit || '';
    unitInput.placeholder = '单位';
    unitInput.dataset.field = 'unit';
    row.appendChild(unitInput);

    var delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'del-btn';
    delBtn.textContent = '❌';
    delBtn.onclick = function () { row.remove(); updateConfirmState(); };
    row.appendChild(delBtn);

    // 切换 product 时,加载该商品的规格列表
    productSelect.onchange = function () {
      var pid = productSelect.value;
      specSelect.innerHTML = '';
      if (pid === '__manual__') {
        // 弹框让用户填 product_id(spec_hint 也手填)
        var newPid = prompt('输入 product_id:');
        if (newPid && /^\d+$/.test(newPid)) {
          productSelect.value = newPid;
          productSelect.options[productSelect.selectedIndex].text = '商品#' + newPid + '(手动)';
        }
        return;
      }
      // 简化:直接放当前候选的 spec 作默认,实际应 fetch /api/v1/products/<id> 拿规格
      var selectedOpt = productSelect.options[productSelect.selectedIndex];
      var defaultSpec = selectedOpt.dataset.spec || '';
      var optEl = document.createElement('option');
      optEl.value = defaultSpec;
      optEl.textContent = defaultSpec || '(无规格)';
      optEl.selected = true;
      specSelect.appendChild(optEl);
    };

    // 触发一次 onchange 初始化规格
    productSelect.onchange();

    candidatesEl.appendChild(row);
    updateConfirmState();
  }

  function updateConfirmState() {
    var rows = candidatesEl.querySelectorAll('.candidate-row');
    var allValid = true;
    rows.forEach(function (row) {
      var qty = row.querySelector('[data-field=quantity]').value;
      var product = row.querySelector('[data-field=product]').value;
      var invalid = !qty || !product || product === '__manual__';
      row.classList.toggle('invalid', invalid);
      if (invalid) allValid = false;
    });
    confirmBtn.disabled = !allValid || rows.length === 0;
  }

  candidatesEl.addEventListener('input', updateConfirmState);

  addRowBtn.onclick = function () {
    addRow({ phrase_part: '', candidates: [] });
  };

  confirmBtn.onclick = async function () {
    var orderIdInput = document.querySelector('#orderIdInput');
    var orderId = orderIdInput ? orderIdInput.value : null;
    if (!orderId) {
      alert('请先创建订单(填日期+客户)');
      return;
    }
    var items = [];
    candidatesEl.querySelectorAll('.candidate-row').forEach(function (row) {
      items.push({
        phrase_part: row.dataset.phrasePart || '',
        product_id: parseInt(row.querySelector('[data-field=product]').value, 10),
        specification: row.querySelector('[data-field=specification]').value || '',
        quantity: parseFloat(row.querySelector('[data-field=quantity]').value) || 0,
        unit: row.querySelector('[data-field=unit]').value || '',
        source: 'user_confirmed',
      });
    });
    try {
      var r = await fetch('/api/v1/voice/confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ order_id: parseInt(orderId, 10), items: items }),
      });
      var data = await r.json();
      if (data.success) {
        alert('✅ 已添加 ' + data.records.length + ' 条明细');
        location.reload();
      } else {
        alert('失败:' + data.error);
      }
    } catch (e) {
      alert('网络错误:' + e.message);
    }
  };

  recordBtn.onclick = function () {
    if (mediaRecorder && mediaRecorder.state === 'recording') {
      stopRecording();
    } else {
      startRecording();
    }
  };

  document.querySelectorAll('[data-action=close]').forEach(function (btn) {
    btn.onclick = hideModal;
  });
  modal.addEventListener('click', function (e) {
    if (e.target === modal) hideModal();
  });

  // 全局入口
  window.openVoiceModal = showModal;
})();
