/* Six-dimensional comparison UI. Statistics are supplied by tools/metrics.py. */
(function () {
  'use strict';
  var D = window.RUNNER_DATA || {}, runs = D.runs || [];
  var presets = ((D.metrics || {}).radar || {}).presets || {};
  var bridge = window.__BRIDGE__;
  var lastResult = null, previousDrawResult = null, requestNumber = 0, selected = [], connecting = false;
  var storageKey = 'run-analytics-radar-v1';
  function el(id) { return document.getElementById(id); }
  function serviceAvailable() {
    el('radarConnection').hidden = !!bridge;
    el('radarMode').disabled = !bridge;
    el('radarDays').querySelectorAll('option').forEach(function (o) { if (o.value === 'custom') o.disabled = !bridge; });
    ['radarDistance', 'radarHr', 'radarIntensity'].forEach(function (id) { el(id).disabled = !bridge; });
    if (!bridge) el('radarConnectionText').textContent = '尚未连接本地服务：对照方式、自定义日期和筛选暂不可用。请用“打开看板.cmd”启动，再点击重新连接；四种预设天数仍可使用。';
  }
  function reconnect() {
    if (connecting) return;
    // Recover a stale/static localhost page through the same origin only. Never expose the token in UI.
    if (!window.location || window.location.protocol !== 'http:' || !/^(127\.0\.0\.1|localhost)$/.test(window.location.hostname)) {
      serviceAvailable();
      el('radarConnectionText').textContent = '离线页面无法连接本地服务。请用项目里的“打开看板.cmd”启动看板后使用对照方式与自定义日期。';
      return;
    }
    connecting = true;
    el('radarReconnect').disabled = true;
    el('radarConnectionText').textContent = '正在连接本地服务…';
    var controller = new AbortController();
    var timeout = setTimeout(function () { controller.abort(); }, 4000);
    fetch('/index.html', { cache: 'no-store', signal: controller.signal })
      .then(function (response) { if (!response.ok) throw new Error('连接失败'); return response.text(); })
      .then(function (html) {
        var match = html.match(/<script>window\.__BRIDGE__\s*=\s*([\s\S]*?);<\/script>/);
        var recovered = match && JSON.parse(match[1]);
        if (!recovered || recovered.api !== '/api' || typeof recovered.token !== 'string' || !recovered.token) throw new Error('服务未启动');
        bridge = recovered;
        serviceAvailable(); populateReferences(); update();
      })
      .catch(function () { serviceAvailable(); })
      .finally(function () { clearTimeout(timeout); connecting = false; el('radarReconnect').disabled = false; });
  }
  function esc(value) { return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  }); }
  function run(id) { return runs.find(function (r) { return String(r.id) === String(id); }); }
  function label(r) { return r.date + (r.time ? ' ' + r.time : '') + ' · ' + (+r.distance).toFixed(2) + ' km'; }
  function options(items) { return items.map(function (r) { return '<option value="' + esc(r.id) + '">' + esc(label(r)) + '</option>'; }).join(''); }
  function shiftDate(value, days) {
    var d = new Date(value + 'T12:00:00Z'); d.setUTCDate(d.getUTCDate() + days);
    return d.toISOString().slice(0, 10);
  }
  function prior(r, target) {
    return r.date < target.date || (r.date === target.date && r.time && target.time && r.time < target.time);
  }
  function candidates() {
    var target = run(el('radarTarget').value);
    if (!target) return [];
    var custom = el('radarDays').value === 'custom';
    var start = custom ? el('radarStart').value : shiftDate(target.date, -Number(el('radarDays').value));
    var end = custom ? el('radarEnd').value : target.date;
    return runs.filter(function (r) { return prior(r, target) && r.date >= start && r.date <= end; });
  }
  function populateReferences() {
    var mode = el('radarMode').value, items = candidates();
    var single = el('radarSingle').value;
    el('radarSingle').innerHTML = options(items.slice().reverse());
    if (items.some(function (r) { return String(r.id) === single; })) el('radarSingle').value = single;
    selected = selected.filter(function (id) { return items.some(function (r) { return String(r.id) === id; }); });
    el('radarSingleBox').hidden = mode !== 'single';
    el('radarPickBox').hidden = mode !== 'manual';
    el('radarDates').hidden = el('radarDays').value !== 'custom';
    el('radarPickSummary').textContent = '勾选对照场次 · 范围内 ' + items.length + ' 场';
    el('radarPicks').innerHTML = items.length ? items.slice().reverse().map(function (r) {
      return '<label><input type="checkbox" value="' + esc(r.id) + '"' + (selected.indexOf(String(r.id)) >= 0 ? ' checked' : '') + '> ' + esc(label(r)) + '</label>';
    }).join('') : '<p class="radar-note">范围内没有更早的场次。</p>';
    el('radarPicks').querySelectorAll('input').forEach(function (input) {
      input.addEventListener('change', function () {
        var id = this.value;
        selected = selected.filter(function (v) { return v !== id; });
        if (this.checked) selected.push(id);
        // Clear the previous comparison while the new selection awaits application.
        requestNumber++;
        clearResult('已更改勾选，点击“应用勾选”更新比较。');
      });
    });
    var target = run(el('radarTarget').value), index = runs.indexOf(target);
    el('radarPrev').disabled = index <= 0;
    el('radarNext').disabled = index < 0 || index >= runs.length - 1;
    if (target) {
      el('radarStart').max = target.date;
      el('radarEnd').max = target.date;
    }
  }
  function settings() {
    var mode = el('radarMode').value;
    var data = { target: el('radarTarget').value, mode: mode,
      days: el('radarDays').value === 'custom' ? 56 : Number(el('radarDays').value),
      distancePct: Number(el('radarDistance').value), hrTolerance: Number(el('radarHr').value),
      sameIntensity: el('radarIntensity').checked };
    if (el('radarDays').value === 'custom') { data.start = el('radarStart').value; data.end = el('radarEnd').value; }
    if (mode !== 'auto') data.ids = mode === 'single' ? (el('radarSingle').value ? [el('radarSingle').value] : []) : selected.slice();
    return data;
  }
  function selectTarget(id) {
    el('radarTarget').value = String(id);
    selected = [];
    var target = run(id);
    if (target) { el('radarStart').value = shiftDate(target.date, -56); el('radarEnd').value = target.date; }
    populateReferences(); update();
  }
  function clearResult(message) {
    lastResult = null;
    el('radarChart').innerHTML = '';
    el('radarLegend').innerHTML = '';
    el('radarValues').innerHTML = '';
    el('radarScore').textContent = '';
    el('radarReason').textContent = '';
    el('radarCommentTitle').textContent = '';
    el('radarComment').textContent = '';
    el('radarEvidence').innerHTML = '';
    el('radarBasis').textContent = message;
    el('radarMessage').textContent = message;
    el('radarMessage').hidden = false;
    el('radarStatus').textContent = '等待比较';
    el('radarResult').hidden = true;
    el('radarSelection').textContent = '';
  }
  function update() {
    var number = ++requestNumber, data = settings();
    clearResult('正在更新对比…');
    el('radarResult').setAttribute('aria-busy', 'true');
    if (bridge && data.mode === 'manual' && !data.ids.length) {
      clearResult('请在下方勾选对照场次，再点击“应用勾选”。');
      el('radarResult').setAttribute('aria-busy', 'false');
      return;
    }
    if (!bridge) {
      var result = (presets[data.target] || {})[String(data.days)];
      if (result) draw(result);
      else clearResult('暂无预计算对比。请通过“打开看板”启动本地服务，或刷新数据后再试。');
      el('radarResult').setAttribute('aria-busy', 'false');
      return;
    }
    fetch(bridge.api + '/radar/compare', { method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Bridge-Token': bridge.token }, body: JSON.stringify(data) })
      .then(function (response) { return response.json().then(function (body) {
        if (!response.ok || !body.ok) throw new Error(body.error || '对比暂时不可用');
        return body.comparison;
      }); })
      .then(function (result) { if (number === requestNumber) draw(result); })
      .catch(function (error) { if (number === requestNumber) clearResult(error.message + ' 请调整设置或重试。'); })
      .finally(function () { if (number === requestNumber) el('radarResult').setAttribute('aria-busy', 'false'); });
  }
  function valueText(axis, value) {
    if (value == null) return '暂无数据';
    if (axis.key === 'pace') {
      var seconds = Math.round(value);
      return Math.floor(seconds / 60) + "'" + ('0' + seconds % 60).slice(-2) + '"/km';
    }
    return value.toFixed(axis.key === 'hr' ? 0 : 1) + ' ' + axis.unit;
  }
  function deltaText(axis) {
    var v = axis.delta;
    if (v == null) return '—';
    var magnitude = Math.abs(v).toFixed(axis.key === 'hr' || axis.key === 'pace' ? 0 : 1);
    if (axis.key === 'pace') return v === 0 ? '相同' : (v < 0 ? '快 ' : '慢 ') + magnitude + ' 秒/km';
    return (v > 0 ? '+' : v < 0 ? '−' : '') + magnitude + ' ' + (axis.unit === '%' ? '个百分点' : axis.unit);
  }
  function point(index, radius) {
    var angle = (-90 + index * 60) * Math.PI / 180;
    return [230 + Math.cos(angle) * radius, 204 + Math.sin(angle) * radius];
  }
  function coords(p) { return p[0].toFixed(1) + ',' + p[1].toFixed(1); }
  function scoreText(value) { return value == null ? '暂不评分' : value.toFixed(1).replace(/\.0$/, '') + ' 分'; }
  function polygon(values, color, dashed) {
    var valid = values.map(function (v, i) { return v == null ? null : point(i, 120 * v / 100); });
    var markup = '';
    if (valid.every(Boolean)) markup = '<polygon points="' + valid.map(coords).join(' ') + '" fill="' + color + '" fill-opacity=".09" stroke="none"/>';
    valid.forEach(function (p, i) {
      if (!p) return;
      var next = valid[(i + 1) % 6];
      if (next) markup += '<line x1="' + p[0] + '" y1="' + p[1] + '" x2="' + next[0] + '" y2="' + next[1] + '" stroke="' + color + '" stroke-width="2.5"' + (dashed ? ' stroke-dasharray="6 5"' : '') + '/>';
      markup += '<circle cx="' + p[0] + '" cy="' + p[1] + '" r="3.5" fill="' + color + '"/>';
    });
    return markup;
  }
  function draw(result) {
    lastResult = result;
    el('radarResult').hidden = false;
    el('radarMessage').hidden = true;
    var modeText = { auto: '自动相似跑', single: '指定之前一场', manual: '勾选多场' }[result.mode];
    var rangeText = result.from + ' 至 ' + result.through;
    var sameRefs = previousDrawResult && previousDrawResult.target === result.target && previousDrawResult.mode === result.mode &&
      (previousDrawResult.from !== result.from || previousDrawResult.through !== result.through) &&
      previousDrawResult.ids.slice().sort().join(',') === result.ids.slice().sort().join(',');
    el('radarSelection').textContent = modeText + ' · ' + rangeText + ' · 范围内 ' + (result.candidateIds || []).length +
      ' 场，实际对照 ' + result.ids.length + ' 场' + (sameRefs ? '。符合条件的对照场次相同，因此图形和分数保持一致。' : '。');
    previousDrawResult = result;
    var target = run(result.target), currentColor = el('radarColor').value, baseColor = el('radarBaseColor').value;
    var grid = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100].map(function (v) {
      return '<polygon points="' + result.axes.map(function (_, i) { return coords(point(i, 120 * v / 100)); }).join(' ') + '" class="radar-grid' + (v % 20 && v !== 50 ? ' radar-grid-minor' : ' radar-grid-major') + '"/>';
    }).join('');
    var axes = result.axes.map(function (axis, i) {
      var end = point(i, 120), pos = point(i, 160);
      var itemScore = axis.scored ? (result.score == null ? '暂不评分' : scoreText(axis.plot) + ' · 对照 50') : '条件指标 · 不计分';
      return '<line x1="230" y1="204" x2="' + end[0] + '" y2="' + end[1] + '" class="radar-grid"/>' +
        '<text x="' + pos[0] + '" y="' + pos[1] + '" text-anchor="middle">' + esc(axis.label) + '</text>' +
        '<text x="' + pos[0] + '" y="' + (pos[1] + 19) + '" text-anchor="middle" class="radar-axis-note">' + esc(itemScore) + '</text>';
    }).join('');
    var lines = '';
    if (el('radarShowBase').checked) lines += polygon(result.axes.map(function (a) { return a.baselinePlot; }), baseColor, el('radarBaseLine').value === 'dash');
    if (el('radarShowTarget').checked) lines += polygon(result.axes.map(function (a) { return a.plot; }), currentColor, el('radarLine').value === 'dash');
    el('radarChart').innerHTML = '<svg viewBox="0 0 460 400" aria-hidden="true" focusable="false">' + grid + axes + lines + '</svg>';
    el('radarChart').setAttribute('aria-label', '本场与此前 ' + result.ids.length + ' 场记录的六维对比，' + result.status + '，具体数值见右侧表格');
    function legend(color, style, text, visible) {
      return '<span><i style="border-color:' + color + ';border-top-style:' + (style === 'dash' ? 'dashed' : 'solid') + '"></i>' + esc(text) + (visible ? '' : '（已隐藏）') + '</span>';
    }
    el('radarLegend').innerHTML = legend(currentColor, el('radarLine').value, '本场 · ' + (target ? target.date : ''), el('radarShowTarget').checked) +
      legend(baseColor, el('radarBaseLine').value, '对照 · ' + result.ids.length + ' 场' + (result.mode === 'single' ? '' : '中位数'), el('radarShowBase').checked);
    el('radarStatus').textContent = result.status;
    el('radarStatus').className = 'chip' + (result.score == null ? '' : ' chip-good');
    el('radarBasis').textContent = (target ? label(target) + '；' : '') + '对照范围 ' + result.from + ' 至 ' + result.through +
      '，仅取本场之前的记录 · 实际对照 ' + result.ids.length + ' 场 · 波动参考 ' + result.historyN + ' 场。' +
      (!bridge ? ' 静态直开支持四种预设范围；指定场次、自定义日期与筛选请通过“打开看板”使用。' : '');
    el('radarScore').innerHTML = result.score == null ? '<strong>暂不评分</strong>' : '<strong>' + result.score + '<small> / 100</small></strong><span>比对照 ' + (result.shift > 0 ? '+' : '') + result.shift + ' 分</span>';
    var review = result.review || { headline: '本场对照结果', comment: result.reason || '展开明细查看各项差异。', focus: '' };
    el('radarCommentTitle').textContent = review.headline;
    el('radarComment').textContent = review.comment;
    el('radarReason').textContent = review.focus;
    el('radarValues').innerHTML = result.axes.map(function (axis) {
      var score = axis.scored && result.score != null ? axis.plot : null;
      var ownScore = axis.scored ? '<small class="radar-item-score" data-radar-axis-score="' + esc(axis.key) + '">' + esc(scoreText(score)) + '</small>'
        : '<small class="radar-item-note">不计分</small>';
      var baseScore = score != null ? '<small class="radar-item-note">对照 50 分</small>' : '';
      var scoreDelta = score != null ? '<small class="radar-item-score">' + (score > 50 ? '+' : '') + scoreText(score - 50) + '</small>' : '';
      return '<tr><th scope="row">' + esc(axis.label) + '<small>' + (axis.scored ? '计入总分' : '条件对照') + ' · 对照有效 ' + axis.n + ' 场</small></th><td>' + esc(valueText(axis, axis.value)) + ownScore + '</td><td>' + esc(valueText(axis, axis.baseline)) + baseScore + '</td><td>' + esc(deltaText(axis)) + scoreDelta + '</td></tr>';
    }).join('');
    el('radarEvidence').innerHTML = result.ids.length ? '<p class="radar-note">实际参与对照的 ' + result.ids.length + ' 场</p><div class="radar-evidence">' + result.ids.map(function (id) {
      var r = run(id);
      return r ? '<button type="button" data-radar-run="' + esc(id) + '">' + esc(label(r)) + '</button>' : '';
    }).join('') + '</div>' : '';
    el('radarEvidence').querySelectorAll('[data-radar-run]').forEach(function (button) {
      button.addEventListener('click', function () { document.dispatchEvent(new CustomEvent('radar-open-run', { detail: this.getAttribute('data-radar-run') })); });
    });
  }
  function reset() {
    el('radarTarget').value = runs.length ? String(runs[runs.length - 1].id) : '';
    el('radarMode').value = 'auto'; el('radarDays').value = '56';
    el('radarDistance').value = '20'; el('radarHr').value = '5'; el('radarIntensity').checked = true;
    el('radarColor').value = '#245d7c'; el('radarBaseColor').value = '#888780';
    el('radarLine').value = 'solid'; el('radarBaseLine').value = 'dash';
    el('radarShowTarget').checked = true; el('radarShowBase').checked = true;
    selected = [];
    var target = run(el('radarTarget').value);
    if (target) { el('radarStart').value = shiftDate(target.date, -56); el('radarEnd').value = target.date; }
  }
  var fields = ['radarTarget', 'radarMode', 'radarDays', 'radarStart', 'radarEnd', 'radarDistance', 'radarHr', 'radarColor', 'radarBaseColor', 'radarLine', 'radarBaseLine'];
  var checks = ['radarIntensity', 'radarShowTarget', 'radarShowBase'];
  el('radarTarget').innerHTML = options(runs.slice().reverse()); reset();
  try {
    var saved = JSON.parse(localStorage.getItem(storageKey) || 'null');
    if (saved && saved.version === 1) {
      fields.forEach(function (id) {
        if (typeof saved[id] !== 'string') return;
        var node = el(id), value = saved[id];
        var allowed = node.tagName === 'SELECT' ? Array.from(node.options).some(function (o) { return o.value === value; })
          : node.type === 'color' ? /^#[0-9a-f]{6}$/i.test(value) : /^\d{4}-\d{2}-\d{2}$/.test(value);
        if (allowed) node.value = value;
      });
      checks.forEach(function (id) { if (typeof saved[id] === 'boolean') el(id).checked = saved[id]; });
      selected = Array.isArray(saved.ids) ? saved.ids.filter(function (id) { return typeof id === 'string' && run(id); }) : [];
    }
  } catch (_) { /* A blocked or damaged local preference must not break the chart. */ }
  if (!bridge) {
    el('radarMode').value = 'auto'; el('radarMode').disabled = true;
    if (el('radarDays').value === 'custom') el('radarDays').value = '56';
    el('radarDays').querySelectorAll('option').forEach(function (o) { if (o.value === 'custom') o.disabled = true; });
    el('radarDistance').value = '20'; el('radarDistance').disabled = true;
    el('radarHr').value = '5'; el('radarHr').disabled = true;
    el('radarIntensity').checked = true; el('radarIntensity').disabled = true;
  }
  serviceAvailable();
  el('radarReconnect').addEventListener('click', reconnect);
  ['radarTarget', 'radarMode', 'radarDays', 'radarDistance', 'radarHr', 'radarIntensity'].forEach(function (id) {
    el(id).addEventListener('change', function () {
      el('radarSaved').textContent = '';
      if (id === 'radarMode' && this.value === 'manual') el('radarPickBox').open = true;
      if (id === 'radarTarget') {
        selected = []; var target = run(this.value);
        if (target) { el('radarStart').value = shiftDate(target.date, -56); el('radarEnd').value = target.date; }
      }
      populateReferences(); update();
    });
  });
  ['radarStart', 'radarEnd'].forEach(function (id) { el(id).addEventListener('change', function () {
    requestNumber++; populateReferences(); clearResult('已更改日期，点击“应用日期”更新比较。');
  }); });
  ['radarColor', 'radarBaseColor', 'radarLine', 'radarBaseLine', 'radarShowTarget', 'radarShowBase'].forEach(function (id) {
    el(id).addEventListener('change', function () { el('radarSaved').textContent = ''; if (lastResult) draw(lastResult); });
  });
  el('radarSingle').addEventListener('change', update);
  ['radarApplyDates', 'radarApplyPicks'].forEach(function (id) { el(id).addEventListener('click', function () { populateReferences(); update(); }); });
  ['radarPrev', 'radarNext'].forEach(function (id) { el(id).addEventListener('click', function () {
    var index = runs.indexOf(run(el('radarTarget').value)) + (id === 'radarPrev' ? -1 : 1);
    if (runs[index]) selectTarget(runs[index].id);
  }); });
  el('radarSave').addEventListener('click', function () {
    try {
      var data = { version: 1, ids: selected };
      fields.forEach(function (id) { data[id] = el(id).value; });
      checks.forEach(function (id) { data[id] = el(id).checked; });
      localStorage.setItem(storageKey, JSON.stringify(data)); el('radarSaved').textContent = '已保存在本机';
    } catch (_) { el('radarSaved').textContent = '浏览器不允许保存设置'; }
  });
  el('radarReset').addEventListener('click', function () {
    try { localStorage.removeItem(storageKey); } catch (_) {}
    reset(); populateReferences(); update(); el('radarSaved').textContent = '已恢复默认';
  });
  document.addEventListener('fitness-run-selected', function (event) {
    if (run(event.detail)) selectTarget(event.detail);
  });
  populateReferences();
  if (runs.length) update(); else clearResult('导入跑步记录后，这里会显示单场六维对比。');
  if (!bridge && runs.length) reconnect();
})();
