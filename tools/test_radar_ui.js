/* Exercise the real radar event handlers and stale-local-page recovery without personal data. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '..', 'radar.js'), 'utf8');

function harness(connected) {
  const nodes = {};
  function node(id) {
    if (nodes[id]) return nodes[id];
    const n = nodes[id] = { value: '', checked: false, hidden: false, disabled: false, open: false,
      textContent: '', handlers: {}, options: [], tagName: 'SELECT',
      addEventListener(type, fn) { this.handlers[type] = fn; },
      setAttribute() {}, getAttribute() {},
      querySelectorAll(selector) { return selector === 'option' ? this.options : []; },
      fire(type) { this.handlers[type].call(this); } };
    let html = '';
    Object.defineProperty(n, 'innerHTML', { get: () => html, set(value) {
      html = value;
      if (id === 'radarTarget' || id === 'radarSingle') {
        n.options = [...value.matchAll(/<option value="([^"]+)"/g)].map(m => ({ value: m[1] }));
        n.value = n.options[0]?.value || '';
      }
    } });
    return n;
  }
  node('radarDays').options = ['28', '56', '90', '180', 'custom'].map(value => ({ value }));
  const runs = [1, 2, 3, 4].map(i => ({ id: String(i), date: `2026-09-0${i}`, time: '18:00', distance: 6 }));
  const bridge = { api: '/api', token: 'synthetic-test-token' };
  const requests = [];
  const result = body => ({ target: body.target, mode: body.mode || 'auto', from: body.start || '2026-08-01',
    through: body.end || '2026-09-04', ids: body.ids || ['1', '2', '3'], candidateIds: ['1', '2', '3'],
    historyN: 3, score: null, status: '暂不评分', reason: 'synthetic',
    axes: ['pace', 'retention', 'variation', 'hr', 'duration', 'high'].map((key, i) => ({
      key, label: key, unit: '%', scored: i < 3, plot: 50, baselinePlot: 50,
      value: 50, baseline: 50, delta: 0, n: 1 })) });
  const ctx = { console, setTimeout, clearTimeout, AbortController,
    document: { getElementById: node, addEventListener() {} },
    location: { protocol: 'http:', hostname: '127.0.0.1' },
    RUNNER_DATA: { runs, metrics: { radar: { presets: { '4': { '56': result({ target: '4' }) } } } } },
    fetch: async (url, options) => {
      requests.push({ url, options });
      if (url === '/index.html') return { ok: true, text: async () => `<script>window.__BRIDGE__ = ${JSON.stringify(bridge)};</script>` };
      const body = JSON.parse(options.body);
      return { ok: true, json: async () => ({ ok: true, comparison: result(body) }) };
    } };
  ctx.window = ctx;
  if (connected) ctx.__BRIDGE__ = bridge;
  vm.runInNewContext(source, ctx);
  return { nodes, node, requests };
}
const flush = () => new Promise(resolve => setImmediate(resolve));

(async () => {
  const recovered = harness(false);
  await flush();
  assert.ok(recovered.requests.some(r => r.url === '/index.html'), 'stale localhost page must reconnect');
  assert.equal(recovered.node('radarMode').disabled, false, 'reconnected mode must unlock');
  assert.equal(recovered.node('radarDays').options.at(-1).disabled, false, 'custom range must unlock');
  assert.equal(recovered.node('radarConnection').hidden, true);

  const h = harness(true);
  await flush();
  for (const days of ['28', '56', '90', '180']) {
    h.node('radarDays').value = days; h.node('radarDays').fire('change'); await flush();
    assert.equal(JSON.parse(h.requests.at(-1).options.body).days, Number(days));
  }
  h.node('radarMode').value = 'single'; h.node('radarMode').fire('change'); await flush();
  assert.equal(h.node('radarSingleBox').hidden, false);
  assert.deepEqual(JSON.parse(h.requests.at(-1).options.body).ids, ['3']);
  h.node('radarSingle').value = '2'; h.node('radarSingle').fire('change'); await flush();
  assert.deepEqual(JSON.parse(h.requests.at(-1).options.body).ids, ['2']);

  h.node('radarMode').value = 'manual'; h.node('radarMode').fire('change'); await flush();
  assert.equal(h.node('radarPickBox').open, true, 'manual list must open when selected');
  assert.ok(h.node('radarMessage').textContent.includes('勾选对照场次'));

  h.node('radarMode').value = 'auto'; h.node('radarMode').fire('change'); await flush();
  h.node('radarDays').value = 'custom'; h.node('radarDays').fire('change'); await flush();
  assert.equal(h.node('radarDates').hidden, false);
  h.node('radarStart').value = '2026-09-02'; h.node('radarStart').fire('change');
  h.node('radarEnd').value = '2026-09-03'; h.node('radarEnd').fire('change');
  h.node('radarApplyDates').fire('click'); await flush();
  const body = JSON.parse(h.requests.at(-1).options.body);
  assert.equal(body.start, '2026-09-02'); assert.equal(body.end, '2026-09-03');
  assert.ok(h.node('radarSelection').textContent.includes('2026-09-02 至 2026-09-03'));
  assert.ok(h.requests.filter(r => r.options.method === 'POST').every(r => r.options.headers['X-Bridge-Token'] === 'synthetic-test-token'));
  console.log('RADAR_UI_PASS: reconnect, modes, preset ranges, custom dates, token headers');
})().catch(error => { console.error(error.message); process.exitCode = 1; });
