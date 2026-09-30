/**
 * Interactive running overview.
 * Metrics and comparisons come from tools/metrics.py; this file presents them
 * and links a selected period to its runs.
 */
(function () {
  'use strict';

  var D = window.RUNNER_DATA || {};
  var RUNS = D.runs || [];
  var M = D.metrics || {};
  var B = M.board || {};
  var viewMode = 'after';
  var historyMonth = '';
  var historyRunId = RUNS.length ? String(RUNS[RUNS.length - 1].id) : null;
  var selectedWeekKey = null;
  var fitnessRunId = null;

  function el(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>\"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }
  function number(v, digits) {
    return v == null || isNaN(v) ? '—' : (+v).toFixed(digits == null ? 1 : digits);
  }
  function pace(s) {
    if (s == null || !isFinite(s) || s <= 0) return '—';
    s = Math.round(s);
    return Math.floor(s / 60) + "'" + ('0' + (s % 60)).slice(-2) + '"';
  }
  function shortDate(s) { return s ? String(s).slice(5).replace('-', '/') : '—'; }

  function renderHeader() {
    var metaDate = D.generated || M.computed || '';
    var importDate = metaDate ? metaDate.slice(0, 10) : '';
    var last = M.lastRun || (RUNS.length ? RUNS[RUNS.length - 1].date : '');
    var fresh = el('freshChip');
    var age = 0;
    if (importDate) age = Math.max(0, Math.floor((Date.now() - new Date(importDate + 'T00:00:00').getTime()) / 86400000));
    fresh.textContent = importDate ? (age === 0 ? '今天导入' : age + ' 天前导入') : '导入时间未知';
    fresh.className = 'chip ' + (age > 14 ? 'chip-warn' : 'chip-good');
    el('barSub').textContent = '最后一跑 ' + shortDate(last);
  }

  function signed(value, digits, unit) {
    if (value == null || !isFinite(value)) return '—';
    return (value > 0 ? '+' : '') + number(value, digits) + (unit || '');
  }

  function openHistoryRun(runId) {
    historyMonth = '';
    historyRunId = String(runId);
    viewMode = 'history';
    renderMode();
    renderHistory();
    el('historyView').scrollIntoView();
  }
  function renderEvidence(containerId, groups) {
    var box = el(containerId);
    var sections = groups.map(function (group) {
      var runs = (group.ids || []).map(function (id) {
        return RUNS.find(function (run) { return String(run.id) === String(id); });
      }).filter(Boolean);
      if (!runs.length) return '';
      return '<div class="evidence-group"><p>' + esc(group.label) + ' · ' + runs.length + ' 场</p>' +
        runs.map(function (run) {
          return '<button type="button" data-evidence-run="' + esc(run.id) + '">' +
            esc(run.date) + ' · ' + esc(number(run.distance, 2)) + ' km</button>';
        }).join('') + '</div>';
    }).filter(Boolean);
    box.innerHTML = sections.length ? '<details class="evidence-list"><summary>查看用于对照的原始场次</summary>' +
      sections.join('') + '</details>' : '';
    box.querySelectorAll('[data-evidence-run]').forEach(function (button) {
      button.addEventListener('click', function () { openHistoryRun(button.getAttribute('data-evidence-run')); });
    });
  }

  function renderLatestAnalysis() {
    var snapshot = B.insights && B.insights.last;
    el('latestOpenHistory').disabled = !RUNS.length;
    if (!snapshot) {
      el('latestKicker').textContent = '最新一场';
      el('latestVerdict').textContent = RUNS.length ? '暂无可比较的指标' : '还没有跑步记录';
      el('latestExplanation').textContent = RUNS.length
        ? '刷新数据后，这里会显示最新一场与近期相近跑的组合对比。'
        : '从右侧工具栏导入跑步记录后，这里会展示最新一场分析。';
      ['latestDistance', 'latestTime', 'latestPace', 'latestHr', 'latestDate', 'paceDelta', 'hrDelta', 'highDelta']
        .forEach(function (id) { el(id).textContent = '—'; });
      el('latestEvidence').innerHTML = '';
      return;
    }
    var comparable = snapshot.comparable || {};
    renderEvidence('latestEvidence', [{ label: '近 56 天相近的跑步', ids: comparable.ids }]);
    el('latestKicker').textContent = '最新一场';
    el('latestDate').textContent = shortDate(snapshot.date) + (snapshot.time ? ' · ' + snapshot.time : '');
    el('latestDistance').textContent = number(snapshot.distance, 2);
    el('latestTime').textContent = snapshot.duration == null ? '—'
      : Math.floor(snapshot.duration / 60) + '分' + ('0' + (snapshot.duration % 60)).slice(-2) + '秒';
    el('latestPace').textContent = pace(snapshot.pace) + '/km';
    el('latestHr').textContent = snapshot.hr == null ? '—' : snapshot.hr + ' bpm';

    if (comparable.n < 3 || comparable.paceSeconds == null) {
      el('latestVerdict').textContent = '这场完成了 ' + number(snapshot.distance, 2) + ' km';
      el('latestExplanation').textContent = '近 56 天符合距离、强度和心率条件的跑步有 ' +
        (comparable.n || 0) + ' 场；少于 3 场，暂不判断进退。';
      el('paceDelta').textContent = '样本不足';
      el('hrDelta').textContent = '样本不足';
      el('highDelta').textContent = '样本不足';
      return;
    }

    var paceDelta = comparable.paceSeconds;
    var paceWord = Math.abs(paceDelta) < 5 ? '配速接近近期同类跑'
      : '配速' + (paceDelta > 0 ? '慢 ' : '快 ') + Math.abs(paceDelta) + ' 秒/公里';
    el('latestVerdict').textContent = paceWord;
    el('latestExplanation').textContent = '对照近 56 天 ' + comparable.n +
      ' 场距离、强度和平均心率相近的跑步；配速、心率与高强度时间一起看，不单凭快慢判好坏。';
    el('paceDelta').textContent = Math.abs(paceDelta) < 5 ? '接近近期'
      : (paceDelta > 0 ? '慢 ' : '快 ') + Math.abs(paceDelta) + ' 秒/km';
    el('hrDelta').textContent = comparable.hrBpm == null ? '无数据' : signed(comparable.hrBpm, 0, ' bpm');
    el('highDelta').textContent = comparable.highPp == null ? '无区间数据' : signed(comparable.highPp, 0, ' 个百分点');
  }

  function renderInsights() {
    var insight = B.insights || {};
    var recent = insight.recent28 || {};
    var previous = insight.previous28 || {};
    var delta = insight.delta28 || {};
    var periodGroups = [
      { label: '之前 28 天', ids: previous.ids },
      { label: '最近 28 天', ids: recent.ids }
    ];
    el('recentAsOf').textContent = insight.last ? '数据截至 ' + insight.last.date : '等待导入';
    el('recentSessions').textContent = (recent.n || 0) + ' 场';
    el('recentKm').textContent = number(recent.km, 1) + ' km';
    el('recentLongest').textContent = recent.longestKm == null ? '—' : number(recent.longestKm, 2) + ' km';
    if (!insight.periodEnough) {
      el('recentChange').textContent = '先看这段时间实际跑过的记录。';
      el('recentBasis').textContent = '最近 28 天 ' + (recent.n || 0) + ' 场，之前 28 天 ' +
        (previous.n || 0) + ' 场；两段各至少 3 场才比较变化。';
      renderEvidence('recentEvidence', periodGroups);
      return;
    }
    var pair = (insight.paired || []).find(function (item) { return item.signal !== 'mixed'; });
    if (pair) {
      var paceWord = pair.signal === 'faster' ? '快 ' : '慢 ';
      el('recentChange').textContent = pair.band + ' km、' + pair.intensity +
        '的同类跑，配速比前 28 天' + paceWord + Math.abs(pair.paceSeconds) + ' 秒/公里。';
      el('recentBasis').textContent = '前段 ' + pair.previousN + ' 场、近段 ' + pair.currentN +
        ' 场；变化超过个人波动参考 ' + pair.paceNoiseSeconds + ' 秒/公里。';
      renderEvidence('recentEvidence', [
        { label: '前段同类跑', ids: pair.previousIds },
        { label: '近段同类跑', ids: pair.currentIds }
      ]);
      return;
    }
    var kmDelta = delta.km;
    el('recentChange').textContent = kmDelta == null ? '这段时间有跑步记录，暂不判断变化。'
      : '累计跑量比之前 28 天' +
        (kmDelta > 0 ? '多 ' + number(kmDelta, 1) + ' km' :
          kmDelta < 0 ? '少 ' + number(Math.abs(kmDelta), 1) + ' km' : '接近') + '。';
    el('recentBasis').textContent = '之前 ' + number(previous.km, 1) + ' km / ' + previous.n +
      ' 场；最近 ' + number(recent.km, 1) + ' km / ' + recent.n + ' 场。这里只描述记录，不判断训练成效。';
    renderEvidence('recentEvidence', periodGroups);
  }
  function renderMode() {
    el('afterView').hidden = viewMode !== 'after';
    el('fitnessView').hidden = viewMode !== 'fitness';
    el('historyView').hidden = viewMode !== 'history';
    el('weekView').hidden = viewMode !== 'week';
    el('modeSwitch').querySelectorAll('[data-view-mode]').forEach(function (button) {
      var active = button.getAttribute('data-view-mode') === viewMode;
      button.setAttribute('aria-selected', String(active));
      button.classList.toggle('on', active);
    });
  }

  function fitnessSignal(entry) {
    if (entry.signal === 'improved') return entry.restDays > 0 && entry.restDays <= 7 ? '休息后提升线索' : '表现提升线索';
    if (entry.signal === 'declined') return '表现回落线索';
    if (entry.signal === 'similar') return '表现接近';
    return '样本不足';
  }

  function renderFitnessChart(series) {
    if (!series.length) {
      el('fitnessChart').innerHTML = '<p class="empty-note">暂无训练负荷数据。</p>';
      return;
    }
    var left = 54, right = 935, top = 16, bottom = 222;
    var max = Math.max(10, Math.ceil(Math.max.apply(null, series.map(function (row) {
      return Math.max(row[1], row[2]);
    })) / 10) * 10);
    var x = function (i) { return left + (right - left) * i / Math.max(1, series.length - 1); };
    var y = function (value) { return bottom - value / max * (bottom - top); };
    var line = function (column) {
      return series.map(function (row, i) { return (i ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(row[column]).toFixed(1); }).join(' ');
    };
    var grids = [0, 1, 2, 3, 4].map(function (i) {
      var value = max * i / 4, yy = y(value).toFixed(1);
      return '<line x1="' + left + '" x2="' + right + '" y1="' + yy + '" y2="' + yy + '" class="fitness-grid" />' +
        '<text x="' + (left - 10) + '" y="' + (+yy + 4) + '" text-anchor="end">' + Math.round(value) + '</text>';
    }).join('');
    var ticks = [0, .25, .5, .75, 1].map(function (fraction) {
      var index = Math.round((series.length - 1) * fraction);
      return '<text x="' + x(index).toFixed(1) + '" y="251" text-anchor="middle">' +
        esc(shortDate(series[index][0])) + '</text>';
    }).join('');
    el('fitnessChart').innerHTML = '<svg viewBox="0 0 960 265" aria-hidden="true" focusable="false">' +
      grids + ticks + '<path class="fitness-line-base" d="' + line(1) + '" />' +
      '<path class="fitness-line-recent" d="' + line(2) + '" />' +
      '<circle class="fitness-end-base" cx="' + x(series.length - 1) + '" cy="' + y(series[series.length - 1][1]) + '" r="5" />' +
      '<circle class="fitness-end-recent" cx="' + x(series.length - 1) + '" cy="' + y(series[series.length - 1][2]) + '" r="5" />' +
      '</svg>';
  }

  function renderFitnessDetail() {
    var entries = ((B.fitness || {}).runs || []);
    var entry = entries.find(function (item) { return String(item.id) === fitnessRunId; });
    if (!entry) {
      el('fitnessRunDetail').innerHTML = '<p class="empty-note">选择一场跑步后查看比较依据。</p>';
      el('fitnessEvidence').innerHTML = '';
      return;
    }
    var label = fitnessSignal(entry);
    var paceDifference = entry.deltaSeconds === 0 ? '接近' :
      (entry.deltaSeconds < 0 ? '快 ' + Math.abs(entry.deltaSeconds) : '慢 ' + entry.deltaSeconds) + ' 秒/公里';
    var basis = entry.signal === 'insufficient'
      ? '此前 56 天符合距离、强度和平均心率条件的跑步只有 ' + entry.comparableIds.length + ' 场；至少需要 3 场。'
      : '与前 56 天 ' + entry.comparableIds.length + ' 场同类跑的中位数相比，配速' +
        paceDifference + '；个人波动参考为 ±' + entry.noiseSeconds + ' 秒/公里。';
    var recovery = entry.restDays == null ? '这是首场记录，之前的恢复情况未知。'
      : '距上一场间隔 ' + (entry.restDays + 1) + ' 天，其中 ' + entry.restDays + ' 个完整休息日。';
    el('fitnessRunDetail').innerHTML = '<p class="eyebrow">' + esc(entry.date) + ' · ' + esc(number(entry.distance, 2)) + ' km</p>' +
      '<h3>' + esc(label) + '</h3><p class="fitness-detail-lead">本场 ' + esc(pace(entry.pace)) + '/km · 均心 ' +
      esc(entry.hr == null ? '—' : entry.hr + ' bpm') + ' · 训练负荷 ' + esc(entry.load == null ? '—' : entry.load) + '</p>' +
      '<p>' + esc(basis) + '</p><p>' + esc(recovery) + '</p>' +
      '<p class="fitness-caveat">“休息后提升”只表示休息后出现了可比表现改善；天气、路线及其他训练也会影响配速，不能确认由这一场训练产生了超量恢复。</p>' +
      '<button type="button" class="history-link" id="fitnessOpenRun">查看这场完整记录</button>';
    el('fitnessOpenRun').addEventListener('click', function () { openHistoryRun(entry.id); });
    renderEvidence('fitnessEvidence', [{ label: '参与比较的跑步', ids: entry.comparableIds }]);
  }

  function renderFitness() {
    var fitness = B.fitness || {};
    var entries = fitness.runs || [];
    var series = fitness.series || [];
    el('fitnessScope').textContent = fitness.asOf ? '数据截至 ' + fitness.asOf + ' · 共 ' + entries.length + ' 场' : '等待导入跑步记录';
    el('fitnessSummary').innerHTML = '<div><span>训练基础估算</span><strong>' + esc(number(fitness.base, 1)) + '</strong><small>设备负荷 / 日</small></div>' +
      '<div><span>近 28 天变化</span><strong>' + esc(signed(fitness.change28, 1, '')) + '</strong><small>' +
      (fitness.change28 == null ? '两段各至少 3 场才比较' : fitness.change28 > 0 ? '训练积累增加' : fitness.change28 < 0 ? '训练积累减少' : '基本持平') +
      '</small></div><div><span>近期负荷</span><strong>' + esc(number(fitness.recent, 1)) +
      '</strong><small>7 天平滑 · 越高表示近期训练越多</small></div>';
    var count = entries.filter(function (item) { return item.signal !== 'insufficient'; }).length;
    var trend = fitness.change28 == null ? '近 28 天的训练积累暂不判断变化。'
      : '训练积累比 28 天前' + (fitness.change28 > 0 ? '增加 ' : fitness.change28 < 0 ? '减少 ' : '变化 ') +
        number(Math.abs(fitness.change28), 1) + '。';
    var recentWord = fitness.recent == null || fitness.base == null ? '' :
      '近期负荷' + (fitness.recent > fitness.base ? '高于' : fitness.recent < fitness.base ? '低于' : '接近') + '长期基础。';
    el('fitnessState').textContent = trend + recentWord + '已有 ' + count + '/' + entries.length + ' 场具备同类跑基线；实际表现请看逐场判断。';
    renderFitnessChart(series);
    if (!entries.some(function (item) { return String(item.id) === fitnessRunId; })) {
      fitnessRunId = entries.length ? String(entries[entries.length - 1].id) : null;
    }
    el('fitnessRunList').innerHTML = entries.length ? entries.slice().reverse().map(function (entry) {
      var active = String(entry.id) === fitnessRunId;
      return '<button type="button" class="fitness-run' + (active ? ' on' : '') + '" data-fitness-run="' + esc(entry.id) +
        '" aria-pressed="' + active + '"><span>' + esc(entry.date) + '</span><strong>' +
        esc(number(entry.distance, 2)) + ' km</strong><em class="signal-' + esc(entry.signal) + '">' +
        esc(fitnessSignal(entry)) + '</em></button>';
    }).join('') : '<p class="empty-note">暂无跑步记录。</p>';
    el('fitnessRunList').querySelectorAll('[data-fitness-run]').forEach(function (button) {
      button.addEventListener('click', function () {
        fitnessRunId = button.getAttribute('data-fitness-run');
        renderFitness();
        if (document.dispatchEvent) document.dispatchEvent(new CustomEvent('fitness-run-selected', { detail: fitnessRunId }));
      });
    });
    renderFitnessDetail();
  }

  function runMap() {
    var map = {};
    RUNS.forEach(function (run) { map[String(run.id)] = run; });
    return map;
  }
  function runsForRow(row) {
    var map = runMap();
    var ids = row && row.ids ? row.ids : [];
    return ids.map(function (id) { return map[String(id)]; }).filter(Boolean).sort(function (a, b) {
      return (a.date + ' ' + (a.time || '')).localeCompare(b.date + ' ' + (b.time || ''));
    });
  }
  function formatSegmentValue(metric, value) {
    if (value == null) return '样本不足';
    if (metric === 'pace') return pace(value) + ' /km';
    if (metric === 'hr') return Math.round(value) + ' bpm';
    return Math.round(value) + ' 步/分';
  }
  function segmentContents(run) {
    var analysis = (M.runSegments || {})[String(run.id)];
    if (!analysis || !analysis.segments || analysis.segments.length !== 3) {
      return { table: '<p class="empty-note">这场没有完整的逐分钟记录，暂时不能拆分比较。</p>',
        guidance: '数据缺失就留空，不用估算补齐。' };
    }
    var specs = [
      { key: 'pace', label: '配速' },
      { key: 'hr', label: '心率' },
      { key: 'cadence', label: '步频' }
    ];
    var head = '<thead><tr><th>指标</th>' + analysis.segments.map(function (part) {
      return '<th>' + esc(part.label) + '<small>' + esc(Math.round(part.minutes[0]) + '–' + Math.round(part.minutes[1]) + ' 分') + '</small></th>';
    }).join('') + '<th>后程对前程</th></tr></thead>';
    var body = specs.map(function (spec) {
      var cells = analysis.segments.map(function (part) {
        var stat = part[spec.key] || {};
        return '<td><strong>' + esc(formatSegmentValue(spec.key, stat.value)) + '</strong><small>' +
          (stat.n || 0) + ' 个分钟点</small></td>';
      }).join('');
      return '<tr><th scope="row">' + esc(spec.label) + '</th>' + cells +
        '<td class="segment-delta">' + esc((analysis.vsFrontText || {})[spec.key] || '样本不足') + '</td></tr>';
    }).join('');
    return { table: '<table class="segment-table">' + head + '<tbody>' + body + '</tbody></table>',
      guidance: analysis.guidance || '样本不足，暂不判断前后程变化。' };
  }
  function duration(seconds) {
    if (seconds == null || !isFinite(seconds)) return '—';
    var minutes = Math.floor(seconds / 60);
    return minutes >= 60 ? Math.floor(minutes / 60) + ' 小时 ' + minutes % 60 + ' 分'
      : minutes + ' 分 ' + ('0' + Math.round(seconds % 60)).slice(-2) + ' 秒';
  }
  function renderHistory() {
    var all = RUNS.slice().sort(function (a, b) {
      return (b.date + ' ' + (b.time || '')).localeCompare(a.date + ' ' + (a.time || ''));
    });
    var months = Array.from(new Set(all.map(function (run) { return run.date.slice(0, 7); })));
    var monthPicker = el('historyMonth');
    monthPicker.innerHTML = '<option value="">全部月份</option>' + months.map(function (month) {
      return '<option value="' + esc(month) + '">' + esc(month.replace('-', ' 年 ') + ' 月') + '</option>';
    }).join('');
    monthPicker.value = historyMonth;
    var visible = historyMonth ? all.filter(function (run) { return run.date.slice(0, 7) === historyMonth; }) : all;
    el('historyCount').textContent = historyMonth ? '本月 ' + visible.length + ' 场 · 共 ' + all.length + ' 场'
      : '共 ' + all.length + ' 场 · ' + (all.length ? all[0].date + ' 至 ' + all[all.length - 1].date : '等待导入');
    if (!visible.some(function (run) { return String(run.id) === historyRunId; })) {
      historyRunId = visible.length ? String(visible[0].id) : null;
    }
    el('historyList').innerHTML = visible.length ? visible.map(function (run) {
      var active = String(run.id) === historyRunId;
      return '<button class="history-run' + (active ? ' on' : '') + '" type="button" data-history-run="' + esc(run.id) +
        '" aria-pressed="' + active + '"><span class="history-run-date">' + esc(run.date + (run.time ? ' · ' + run.time : '')) +
        '</span><strong>' + esc(number(run.distance, 2)) + ' km</strong><span>' + esc(duration(run.duration)) +
        '</span><span>' + esc(pace(run.pace)) + '/km</span></button>';
    }).join('') : '<p class="empty-note">这个月份没有跑步记录。</p>';
    el('historyList').querySelectorAll('[data-history-run]').forEach(function (button) {
      button.addEventListener('click', function () {
        historyRunId = button.getAttribute('data-history-run');
        renderHistory();
      });
    });
    var run = visible.find(function (item) { return String(item.id) === historyRunId; });
    if (!run) {
      el('historyDetail').innerHTML = '<p class="empty-note">选择一场跑步后可查看详情。</p>';
      return;
    }
    var detail = segmentContents(run);
    el('historyDetail').innerHTML = '<p class="eyebrow">单场记录 · ' + esc(run.date) + '</p><h3>' +
      esc(number(run.distance, 2)) + ' km</h3><p class="history-detail-time">' + esc(run.time || '时间未记录') +
      '</p><dl class="history-facts"><div><dt>用时</dt><dd>' + esc(duration(run.duration)) + '</dd></div>' +
      '<div><dt>平均配速</dt><dd>' + esc(pace(run.pace)) + '/km</dd></div>' +
      '<div><dt>平均心率</dt><dd>' + (run.hr == null ? '—' : esc(run.hr) + ' bpm') + '</dd></div>' +
      '<div><dt>步数</dt><dd>' + (run.steps == null ? '—' : esc(run.steps)) + '</dd></div></dl>' +
      '<section class="segment-panel"><h4>前程 · 中程 · 后程</h4><p class="history-detail-note">按本场时长三等分，只使用已有的逐分钟记录。</p>' +
      '<div class="segment-comparison">' + detail.table + '</div><p class="segment-guidance">' +
      esc(detail.guidance) + '</p></section>';
  }

  function renderWeekly() {
    var timeline = (B.timeline && B.timeline.week && B.timeline.week.rows) || [];
    var weeks = timeline.slice(-5);
    var list = el('weekList');
    var latestWeek = weeks[weeks.length - 1];
    el('weekScope').textContent = latestWeek ? '最近 ' + weeks.length + ' 周 · 跑步数据截至 ' + latestWeek.through
      : '等待导入跑步记录';
    if (!weeks.some(function (row) { return row.key === selectedWeekKey; })) {
      selectedWeekKey = latestWeek ? latestWeek.key : null;
    }
    list.innerHTML = weeks.length ? weeks.slice().reverse().map(function (row) {
      var active = row.key === selectedWeekKey;
      return '<button class="week-row' + (active ? ' on' : '') + '" type="button" data-week-key="' + esc(row.key) +
        '" aria-pressed="' + active + '"><span>' + esc(row.from.slice(5) + ' – ' + (row.partial ? row.through : row.to).slice(5)) +
        '</span><strong>' + esc(number(row.km, 1)) +
        ' km</strong><span>' + row.n + ' 场</span></button>';
    }).join('') : '<p class="empty-note">暂无可回看的周记录。</p>';
    list.querySelectorAll('[data-week-key]').forEach(function (button) {
      button.addEventListener('click', function () {
        selectedWeekKey = button.getAttribute('data-week-key');
        renderWeekly();
      });
    });
    var row = weeks.find(function (item) { return item.key === selectedWeekKey; });
    if (!row) {
      el('weekDetail').innerHTML = '<p class="empty-note">导入后，这里会显示每周的跑步记录。</p>';
      return;
    }
    var comparison = row.compare && row.compare.previous;
    var previousIndex = timeline.findIndex(function (item) { return item.key === row.key; }) - 1;
    var previousRow = previousIndex >= 0 ? timeline[previousIndex] : null;
    var evidence = comparison && comparison.enough
      ? '与上一周相比，跑量' + (comparison.kmDelta > 0 ? '多 ' : comparison.kmDelta < 0 ? '少 ' : '相同') +
        (comparison.kmDelta ? number(Math.abs(comparison.kmDelta), 1) + ' km' : '') +
        '。本周 ' + row.n + ' 场，上一周 ' + comparison.n + ' 场' + (row.partial ? '，按相同已过天数对照' : '') + '。'
      : '本周 ' + row.n + ' 场，上一周 ' + (comparison ? comparison.n : 0) +
        ' 场；两边各至少 3 场才比较变化。';
    var paceComparison = comparison && comparison.pace && comparison.pace.ok
      ? '同距离、同强度的 ' + comparison.pace.n + ' 对可比场次：配速' +
        (comparison.pace.seconds > 0 ? '慢 ' + comparison.pace.seconds + ' 秒/公里' :
          comparison.pace.seconds < 0 ? '快 ' + Math.abs(comparison.pace.seconds) + ' 秒/公里' : '接近') + '。'
      : '同类跑配速样本不足，不作判断。';
    var runs = runsForRow(row).reverse();
    var previousRuns = runsForRow(previousRow).reverse();
    function runButtons(items) {
      return items.map(function (run) {
        return '<button type="button" data-week-run="' + esc(run.id) + '"><span>' + esc(run.date + (run.time ? ' · ' + run.time : '')) +
          '</span><strong>' + esc(number(run.distance, 2)) + ' km</strong><span>' + esc(pace(run.pace)) + '/km</span></button>';
      }).join('');
    }
    el('weekDetail').innerHTML = '<p class="eyebrow">所选周 · ' + esc(row.from + ' 至 ' + (row.partial ? row.through : row.to)) +
      '</p><h3>' + esc(number(row.km, 1)) + ' km</h3><dl class="week-facts"><div><dt>跑步次数</dt><dd>' + row.n +
      ' 场</dd></div><div><dt>最长一场</dt><dd>' + (row.longestKm == null ? '—' : esc(number(row.longestKm, 2)) + ' km') +
      '</dd></div></dl><p class="week-evidence">' + esc(evidence) + '</p><p class="week-evidence">' +
      esc(paceComparison) + '</p><h4>这一周的场次</h4><div class="week-runs">' +
      (runs.length ? runButtons(runs) : '<p class="empty-note">这一周没有跑步记录。</p>') + '</div>' +
      (previousRow ? '<details class="week-baseline"><summary>查看上一周的 ' + previousRuns.length +
        ' 场对照记录</summary><div class="week-runs">' +
        (previousRuns.length ? runButtons(previousRuns) : '<p class="empty-note">上一周没有跑步记录。</p>') +
        '</div></details>' : '');
    el('weekDetail').querySelectorAll('[data-week-run]').forEach(function (button) {
      button.addEventListener('click', function () {
        openHistoryRun(button.getAttribute('data-week-run'));
      });
    });
  }

  function render() {
    renderHeader();
    renderLatestAnalysis();
    renderInsights();
    renderFitness();
    renderMode();
    renderHistory();
    renderWeekly();
  }

  el('modeSwitch').querySelectorAll('[data-view-mode]').forEach(function (button) {
    button.addEventListener('click', function () {
      viewMode = button.getAttribute('data-view-mode');
      renderMode();
    });
  });
  el('historyMonth').addEventListener('change', function () {
    historyMonth = this.value;
    renderHistory();
  });
  el('latestOpenHistory').addEventListener('click', function () {
    if (RUNS.length) openHistoryRun(RUNS[RUNS.length - 1].id);
  });
  el('openWeekReview').addEventListener('click', function () {
    viewMode = 'week';
    renderMode();
    renderWeekly();
  });
  document.addEventListener('radar-open-run', function (event) { openHistoryRun(event.detail); });
  render();
})();
