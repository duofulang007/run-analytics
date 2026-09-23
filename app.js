(function () {
  "use strict";

  var D = window.RUNNER_DATA;
  if (!D) { document.body.innerHTML = "<p style='padding:24px'>未找到 data.js，请先运行 tools/extract.py 生成数据。</p>"; return; }
  var RUNS = D.runs.slice();

  var C = {
    dist: "#2563eb", pace: "#d97706", hr: "#dc2626", cad: "#0d9488",
    stride: "#7c3aed", td: "#dc2626", va: "#2563eb", vsr: "#0d9488",
    ink3: "#8b9099", line: "#e6e8ec", ink: "#16181d"
  };

  var state = { year: "2026", tab: "overview", sel: null, search: "", sortKey: "id", sortDir: "desc", cmpMonth: null, cmpBase: null, day: null };
  var charts = {};

  // ---------------------------------------------------------------- helpers
  function byYear(y) { return y === "all" ? RUNS.slice() : RUNS.filter(function (r) { return r.year === +y; }); }

  function fmtPace(s) {
    if (!s || !isFinite(s)) return "—";
    s = Math.round(s);
    return Math.floor(s / 60) + "'" + String(s % 60).padStart(2, "0") + '"';
  }
  function fmtDur(s) {
    s = Math.round(s);
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    return h ? h + ":" + String(m).padStart(2, "0") + ":" + String(x).padStart(2, "0")
             : m + ":" + String(x).padStart(2, "0");
  }
  function avg(arr, key) {
    var v = arr.map(key ? function (x) { return +x[key]; } : Number).filter(function (n) { return isFinite(n) && n > 0; });
    return v.length ? v.reduce(function (a, b) { return a + b; }, 0) / v.length : 0;
  }
  function r1(n) { return Math.round(n * 10) / 10; }
  function r2(n) { return Math.round(n * 100) / 100; }
  function paceOf(arr) {
    var d = arr.reduce(function (s, r) { return s + r.duration; }, 0);
    var dd = arr.reduce(function (s, r) { return s + r.distance; }, 0);
    return dd ? d / dd : 0;
  }
  // 居中滑动中位数：跑步过程中的热身 / 间歇 / 停表会让单分钟配速剧烈跳动，
  // 平滑只用于画线，原始值仍保留在数据里。
  function smooth(pairs, win) {
    var w = win || 2;
    return pairs.map(function (p, i) {
      var lo = Math.max(0, i - w), hi = Math.min(pairs.length - 1, i + w), v = [];
      for (var j = lo; j <= hi; j++) v.push(pairs[j][1]);
      v.sort(function (a, b) { return a - b; });
      return [p[0], v[Math.floor(v.length / 2)]];
    });
  }
  function mmdd(s) { return s.slice(5).replace("-", "/"); }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }

  function fit(points) {
    var n = points.length;
    if (n < 3) return null;
    var sx = 0, sy = 0, sxy = 0, sxx = 0, syy = 0;
    points.forEach(function (p) { sx += p[0]; sy += p[1]; sxy += p[0] * p[1]; sxx += p[0] * p[0]; syy += p[1] * p[1]; });
    var d = n * sxx - sx * sx;
    if (!d) return null;
    var b = (n * sxy - sx * sy) / d;
    var a = (sy - b * sx) / n;
    var num = n * sxy - sx * sy;
    var den = Math.sqrt((n * sxx - sx * sx) * (n * syy - sy * sy));
    return { a: a, b: b, r: den ? num / den : 0, min: Math.min.apply(null, points.map(function (p) { return p[0]; })), max: Math.max.apply(null, points.map(function (p) { return p[0]; })) };
  }
  function fitLine(f, color, label) {
    return {
      type: "line", label: label, borderColor: color, borderWidth: 1.5, borderDash: [5, 4],
      pointRadius: 0, fill: false, tension: 0,
      data: [{ x: f.min, y: f.a + f.b * f.min }, { x: f.max, y: f.a + f.b * f.max }]
    };
  }

  function setupChartDefaults() {
    if (!window.Chart || !Chart.defaults || !Chart.defaults.font) return;
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.font.size = 11;
    Chart.defaults.color = C.ink3;
  }

  function mkChart(id, cfg) {
    var el = document.getElementById(id);
    if (!el) return null;
    if (charts[id]) { charts[id].destroy(); delete charts[id]; }
    if (window.Chart) {
      cfg.options = cfg.options || {};
      cfg.options.maintainAspectRatio = false;
      cfg.options.responsive = true;
      charts[id] = new Chart(el.getContext("2d"), cfg);
    } else if (!document.getElementById("noChartLib")) {
      var p = document.createElement("p");
      p.id = "noChartLib"; p.style.cssText = "padding:16px;color:#dc2626";
      p.textContent = "图表库未加载：vendor/chart.umd.min.js 缺失。";
      el.parentNode.appendChild(p);
    }
    return charts[id];
  }

  function axis(title, opts) {
    var o = { title: { display: !!title, text: title || "" }, grid: { color: C.line }, ticks: {} };
    for (var k in (opts || {})) o[k] = opts[k];
    return o;
  }

  // ---------------------------------------------------------------- 建议引擎
  // 设计原则：所有对比最终都要落到一句能执行的建议上，而不是只丢一堆图。
  // 输入 = 距最后一次训练过了多久 + 最近 7 天的负荷形状 + 强度结构；
  // 输出 = 今天该歇还是该跑、跑多少、用什么配速。

  function parseDay(key) {
    var p = String(key).split("-");
    return new Date(+p[0], +p[1] - 1, +p[2]);
  }
  function dayKey(dt) {
    return dt.getFullYear() + "-" + String(dt.getMonth() + 1).padStart(2, "0") + "-" + String(dt.getDate()).padStart(2, "0");
  }
  function shiftDay(key, n) {
    var d = parseDay(key);
    d.setDate(d.getDate() + n);
    return dayKey(d);
  }
  function daySpan(a, b) { return Math.round((parseDay(a) - parseDay(b)) / 86400000); }
  function todayKey() { return dayKey(new Date()); }
  function latestRun() { return RUNS.length ? RUNS[RUNS.length - 1] : null; }
  function runsIn(fromKey, toKey) {
    return RUNS.filter(function (r) { return r.date >= fromKey && r.date <= toKey; });
  }
  function sumBy(arr, key) { return arr.reduce(function (s, r) { return s + (+r[key] || 0); }, 0); }
  function zonesOf(arr) {
    var out = { warm: 0, fat: 0, aer: 0, ana: 0, ext: 0 };
    arr.forEach(function (r) {
      var z = r.zones || {};
      Object.keys(out).forEach(function (k) { out[k] += +z[k] || 0; });
    });
    out.total = out.warm + out.fat + out.aer + out.ana + out.ext;
    // 「低强度」= 热身 + 燃脂 + 有氧，三者都在无氧阈以下。
    // 只算热身 + 燃脂会把占大头的有氧跑误判成高强度（实测最近 7 天有氧占 26%），口径会完全反掉。
    out.easyPct = out.total ? (out.warm + out.fat + out.aer) / out.total * 100 : 0;
    out.hardPct = out.total ? (out.ana + out.ext) / out.total * 100 : 0;
    out.lowPct = out.total ? (out.warm + out.fat) / out.total * 100 : 0;
    out.aerPct = out.total ? out.aer / out.total * 100 : 0;
    return out;
  }
  // 轻松跑参考配速：最近 30 天心率落在 130–155（Zone2 常见区间）的场次加权平均
  function easyPace(rs) {
    var g = rs.filter(function (r) { return r.hr >= 130 && r.hr <= 155 && r.distance >= 3; });
    return g.length >= 2 ? paceOf(g) : 0;
  }
  // 距某场训练开始过了多少小时。用「日期 + 开始时间」拼本地时间，不走 UTC。
  function hoursSince(r) {
    var d = String(r.date).split("-"), t = String(r.time || "0:0").split(":");
    var at = new Date(+d[0], +d[1] - 1, +d[2], +t[0] || 0, +t[1] || 0);
    return (Date.now() - at.getTime()) / 3600000;
  }
  // ---- 结构化呈现：一行一条，比一大段文字好扫读
  // row = { k 标签, v 当前值, d 补充说明, badge 判定词, level 判定色 }
  function factsHtml(rows) {
    return (rows || []).map(function (f) {
      return '<div class="fact"><span class="fk">' + esc(f.k) + "</span>" +
        '<span class="fv">' + esc(f.v) + (f.d ? ' <span class="fd">' + esc(f.d) + "</span>" : "") + "</span>" +
        (f.badge ? '<span class="fb ' + (f.level || "") + '">' + esc(f.badge) + "</span>" : "") +
        "</div>";
    }).join("");
  }

  function statHtml(k, v, d, level) {
    return '<div class="stat ' + (level || "") + '"><div class="sk">' + esc(k) + '</div><div class="sv">' + esc(v) +
      '</div><div class="sd">' + esc(d) + "</div></div>";
  }

  // 今日建议大卡：状态标签 → 一句结论 → 关键数字 → 一句原因。全部控制在一屏内。
  function advCardHtml(a) {
    var lvl = a.level === "rest" ? "rest" : a.level === "warn" ? "warn" : "";
    return '<div class="advcard ' + lvl + '">' +
      '<span class="vtag">' + esc(a.tag) + "</span>" +
      '<div class="head">' + esc(a.headline) + "</div>" +
      (a.chips && a.chips.length
        ? '<div class="chips">' + a.chips.map(function (c) { return '<span class="chip">' + esc(c) + "</span>"; }).join("") + "</div>"
        : "") +
      '<p class="why">' + esc(a.why) + "</p></div>";
  }

  // ---------------------------------------------------------------- 训练负荷模型
  // CTL 体能（42 天指数加权）· ATL 疲劳（7 天）· TSB 状态 = CTL − ATL。
  // 跑圈通行的负荷-恢复模型（TrainingPeaks / Garmin 口径），这里用小米给出的
  // train_load 当每日负荷，**休息日按 0 计入**（这是关键：只按「有跑的日子」平均，
  // 得到的会是单次平均负荷 ~71，而不是 ATL ~45，两者差一个数量级的含义）。
  // 前 7 天用均值起算，否则曲线要从 0 慢慢爬上来，前两个月都是假的。
  // **绝对值只跟自己的历史比**，跨设备没有可比性；模型的价值在形状
  // （CTL 涨得慢、ATL 来得快去得也快）和 TSB 落在哪一段。
  function loadModel() {
    var per = {};
    RUNS.forEach(function (r) { per[r.date] = (per[r.date] || 0) + (+r.load || 0); });
    var keys = Object.keys(per).sort();
    if (!keys.length) return null;
    var days = [], ctl = 0, atl = 0, seed = [];
    for (var k = keys[0]; k <= todayKey(); k = shiftDay(k, 1)) {
      var L = per[k] || 0;
      if (seed.length < 7) {
        // 前 7 天用均值起算，否则曲线要从 0 慢慢爬上来，前两个月都是假的
        seed.push(L);
        ctl = atl = seed.reduce(function (a, b) { return a + b; }, 0) / seed.length;
      } else {
        ctl += (L - ctl) / 42;
        atl += (L - atl) / 7;
      }
      days.push({ k: k, L: L, ctl: ctl, atl: atl, tsb: ctl - atl });
    }
    var last = days[days.length - 1];
    var back = days.length > 28 ? days[days.length - 29] : days[0];
    return { days: days, last: last, ctlPrev: back.ctl };
  }

  // TSB 分区用通行阈值。在「休息日按 0 计入」的口径下，本数据近 70 天 TSB 落在
  // −33.7 ~ −0.3（长期处在生产性疲劳区，偶尔触到警戒），量级与通行口径吻合，
  // 所以阈值可以直接用，不需要另外标定。
  function tsbState(tsb) {
    if (tsb < -30) return { level: "bad", word: "负荷偏重", tip: "疲劳明显高于体能，先减量" };
    if (tsb < -10) return { level: "warn", word: "生产性疲劳", tip: "正常的高峰训练区间，别再加量，把恢复跟上" };
    if (tsb < 5) return { level: "good", word: "节奏平稳", tip: "强度与恢复平衡，适合正常训练" };
    if (tsb < 25) return { level: "good", word: "状态新鲜", tip: "适合做质量课或测一次试" };
    return { level: "warn", word: "偏休息", tip: "体能正在往下掉，该恢复训练量了" };
  }

  // ---------------------------------------------------------------- 跑姿体检
  // 参考区间取跑表圈的通行口径：步频 170–180 理想；触地 <240 良好 / 240–280 一般 /
  // >300 偏长；垂直振幅 5–8 优秀 / 8–12 常见；垂直步幅比 <7 理想、业余常见 8–12%。
  // 每项都带一个**具体动作**，这是「体检」和「看一眼数字」的区别。
  var GAIT_REF = [
    { k: "步频", key: "cadence", unit: "步/分", fmt: function (v) { return String(Math.round(v)); },
      band: "170–180 理想 · 160–170 略低 · <160 偏低",
      judge: function (v) { return v >= 170 ? ["good", "理想"] : v >= 160 ? ["warn", "略低"] : ["bad", "偏低"]; },
      tip: function (v) {
        return v >= 180
          ? "已经够高，别再往上加，把注意力放到稳定性上"
          : "用节拍器，从 " + (Math.round(v / 5) * 5 + 5) + " 起步，每周 +2~3 步；别刻意迈大步";
      } },
    { k: "触地时间", key: "td", unit: "ms", fmt: function (v) { return String(Math.round(v)); },
      band: "<240 良好 · 240–280 一般 · >300 偏长",
      judge: function (v) { return v < 240 ? ["good", "良好"] : v <= 280 ? ["warn", "一般"] : ["bad", "偏长"]; },
      tip: function () { return "跳绳 100 次 ×3 组、踝跳（pogo jump），练肌腱弹性回收；先把步频提上去，它通常跟着变短"; } },
    { k: "垂直振幅", key: "va", unit: "cm", fmt: r1,
      band: "5–8 优秀 · 8–12 常见 · >12 偏大",
      judge: function (v) { return v < 8 ? ["good", "优秀"] : v <= 12 ? ["warn", "常见"] : ["bad", "偏大"]; },
      tip: function () { return "核心平板支撑 + 步频 +2；跑时想着「头顶有天花板」，把力往前送而不是往上跳"; } },
    { k: "垂直步幅比", key: "vsr", unit: "%", fmt: r1,
      band: "<7 理想（精英 5–6%）· 业余常见 8–12%",
      judge: function (v) { return v < 7 ? ["good", "理想"] : v <= 12 ? ["warn", "可改善"] : ["bad", "偏高"]; },
      tip: function () { return "这是前两项的合成结果：步频上去、振幅下来，它自然跟着降，不用单独盯着它练"; } }
  ];

  function gaitCheck() {
    var win = RUNS.slice(-20);                 // 用最近 20 场，比按天数切更稳
    if (!win.length) return null;
    return {
      n: win.length, from: win[0].date, to: win[win.length - 1].date,
      rows: GAIT_REF.map(function (ref) {
        var v = avg(win, ref.key), j = ref.judge(v);
        return { k: ref.k, v: ref.fmt(v) + " " + ref.unit, band: ref.band, word: j[1], level: j[0], tip: ref.tip(v) };
      })
    };
  }

  // 返回 { level, tag, headline, chips:[], why, facts:[{k,v,d,badge,level}] }
  function buildAdvice() {
    var last = latestRun();
    if (!last) {
      return { level: "info", tag: "今日建议", headline: "还没有任何跑步记录", chips: [],
        why: "先导入一次小米导出包，这里就会给出每天该练什么的建议。", facts: [] };
    }
    var today = todayKey();
    var stale = daySpan(today, last.date);
    var w1 = runsIn(shiftDay(today, -6), today);
    var w0 = runsIn(shiftDay(today, -13), shiftDay(today, -7));
    var d1 = sumBy(w1, "distance"), d0 = sumBy(w0, "distance");
    var rise = d0 ? (d1 - d0) / d0 * 100 : 0;
    var seen = {};
    w1.forEach(function (r) { seen[r.date] = 1; });
    var nDay = Object.keys(seen).length;
    var z = zonesOf(w1);
    var ep = easyPace(runsIn(shiftDay(today, -29), today));
    var hrs = hoursSince(last);
    var remain = (+last.recover || 0) - hrs;
    var lm = loadModel();
    var tsb = lm ? lm.last.tsb : 0;
    var ts = tsbState(tsb);
    var paceChip = ep ? "配速 " + fmtPace(ep) + " 上下" : "";

    var out;
    if (remain > 12) {
      out = { level: "rest", tag: "建议休息", headline: "今天别跑了，先把恢复补上",
        chips: ["还差 " + Math.round(remain) + " 小时", "上一场 " + mmdd(last.date) + " " + last.time],
        why: "上一场 " + r2(last.distance) + " km 的建议恢复是 " + Math.round(last.recover) +
          " 小时，现在只过了 " + Math.round(hrs) + " 小时，硬跑只会把疲劳带进下周。" };
    } else if (remain > 0) {
      out = { level: "rest", tag: "轻微活动", headline: "今天走一走，或慢跑 20–30 分钟",
        chips: ["还差 " + Math.round(remain) + " 小时", "心率 <140"],
        why: "恢复窗口还没走完。这几天不上强度，后面的质量课才跑得出来。" };
    } else if (nDay >= 6) {
      out = { level: "rest", tag: "建议休息", headline: "今天安排一个完全休息日",
        chips: ["7 天跑了 " + nDay + " 天", "0 km"],
        why: "一周里几乎没有恢复日。疲劳会先在跑姿上露出来（触地变长、步频掉），歇一天比硬顶更有价值。" };
    } else if (tsb < -30) {
      out = { level: "warn", tag: "负荷偏重", headline: "今天降一档：跑短、跑慢",
        chips: ["TSB " + r1(tsb), "距离减半", paceChip].filter(Boolean),
        why: "疲劳明显高于体能（TSB 已进 −30 警戒区）。这时加量不会涨体能，只会把伤病概率抬上去。" };
    } else if (rise > 10) {
      out = { level: "warn", tag: "控制跑量", headline: "今天轻松跑，别再加量",
        chips: ["本周 +" + r1(rise) + "%", "距离减两成", paceChip].filter(Boolean),
        why: "最近 7 天 " + r1(d1) + " km，比前 7 天的 " + r1(d0) + " km 涨了 " + r1(rise) + "%，超过常说的 10% 安全线。" };
    } else if (z.total && z.easyPct < 70) {
      out = { level: "easy", tag: "补有氧", headline: "今天做一次纯轻松跑，全程不看表速",
        chips: ["心率 <145", paceChip, "6–8 km"].filter(Boolean),
        why: "最近 7 天有 " + Math.round(z.hardPct) + "% 的时间在中高强度，无氧阈以下只占 " +
          Math.round(z.easyPct) + "%。有氧底座补不上来，速度就一直是硬撑出来的。" };
    } else {
      out = { level: "info", tag: "可以训练", headline: "今天可以按计划跑",
        chips: ["状态 " + ts.word, ep ? "配速 " + fmtPace(ep) + " 起" : "", "先热身 1–2 km"].filter(Boolean),
        why: "恢复窗口已过，最近 7 天 " + r1(d1) + " km / " + w1.length + " 场，负荷节奏正常。" };
    }

    // ---- 依据：逐行结构，代替原来的长段落
    var facts = [];
    if (stale > 3) {
      facts.push({ k: "数据", v: last.date + " 之后没有新记录", d: "距今 " + stale + " 天",
        badge: "建议重新导入", level: "warn" });
    }
    if (stale < 0) {
      facts.push({ k: "数据", v: "末场 " + last.date, d: "晚于今天", badge: "核对设备时间", level: "warn" });
    }
    facts.push({ k: "恢复", v: "已过 " + Math.round(hrs) + " 小时", d: "需要 " + Math.round(last.recover) + " 小时",
      badge: remain > 0 ? "还差 " + Math.round(remain) + " 小时" : "已恢复", level: remain > 0 ? "warn" : "good" });
    facts.push({ k: "训练负荷", v: "CTL " + r1(lm ? lm.last.ctl : 0) + " · ATL " + r1(lm ? lm.last.atl : 0),
      d: "TSB " + r1(tsb), badge: ts.word, level: ts.level });
    facts.push({ k: "强度结构", v: "无氧阈下 " + Math.round(z.easyPct) + "%", d: "参考 ≥70%",
      badge: z.easyPct >= 70 ? "达标" : "偏低", level: z.easyPct >= 70 ? "good" : "warn" });
    if (w1.length) {
      facts.push({ k: "最近 7 天", v: w1.length + " 场 · " + r1(d1) + " km",
        d: "配速 " + fmtPace(paceOf(w1)) + " · 心率 " + Math.round(avg(w1, "hr")) + " bpm",
        badge: !d0 ? "" : rise > 10 ? "比前 7 天 +" + r1(rise) + "%" : rise < -30 ? "比前 7 天 " + r1(rise) + "%" : "与前 7 天持平",
        // 只有「涨太快」才值得标色。跑量掉下来多半是主动减量，标成告警会误导（踩过）。
        level: rise > 10 ? "warn" : "" });
    }
    if (ep) {
      facts.push({ k: "轻松跑配速", v: fmtPace(ep), d: "最近 30 天心率 130–155 场次", badge: "Zone2 落点", level: "" });
    }
    out.facts = facts;
    return out;
  }

  // ---------------------------------------------------------------- daily
  var CAL_WEEKS = 26;

  function heatLevel(km) {
    if (!(km > 0)) return 0;
    if (km < 5) return 1;
    if (km < 7) return 2;
    if (km < 9) return 3;
    if (km < 11) return 4;
    return 5;
  }

  // 日历格子大小按可用宽度算：26 列方块要么铺满卡片，要么小到能放下，
  // 而不是固定 22px 让右边空一半。窗口变窄时由 .calwrap 横向滚动兜底。
  function sizeCalendar() {
    var flex = document.querySelector(".calflex");
    var wrap = document.querySelector(".calwrap");
    if (!flex || !wrap) return;
    var gap = parseFloat(getComputedStyle(wrap).getPropertyValue("--cal-gap")) || 3;
    var avail = wrap.clientWidth - 40;            // 减去左侧星期标签与间隙
    var size = Math.floor((avail - (CAL_WEEKS - 1) * gap) / CAL_WEEKS);
    size = Math.max(18, Math.min(40, size));
    flex.style.setProperty("--cal-cell", size + "px");
  }

  function renderDaily() {
    var adv = buildAdvice();
    document.getElementById("advCard").innerHTML = advCardHtml(adv);
    document.getElementById("advFacts").innerHTML = factsHtml(adv.facts);

    var last = latestRun();
    var tKey = todayKey();
    var endKey = last && last.date > tKey ? last.date : tKey;
    var end = parseDay(endKey);
    var dow = (end.getDay() + 6) % 7;
    var startKey = shiftDay(endKey, -dow - (CAL_WEEKS - 1) * 7);

    var byDay = {};
    RUNS.forEach(function (r) { (byDay[r.date] || (byDay[r.date] = [])).push(r); });

    var cells = [];
    for (var i = 0; i < CAL_WEEKS * 7; i++) {
      var key = shiftDay(startKey, i);
      if (key > tKey) { cells.push('<button type="button" class="blank" tabindex="-1"></button>'); continue; }
      var g = byDay[key] || [];
      var km = sumBy(g, "distance");
      var tip = key + (g.length ? " · " + r2(km) + " km / " + g.length + " 场" : " · 休息");
      cells.push('<button type="button" class="heat' + heatLevel(km) +
        (key === tKey ? " today" : "") + (key === state.day ? " sel" : "") +
        '" data-day="' + key + '" title="' + esc(tip) + '"></button>');
    }
    document.getElementById("calGrid").innerHTML = cells.join("");
    document.getElementById("calSub").textContent =
      "最近 " + CAL_WEEKS + " 周（" + mmdd(startKey) + " – " + mmdd(endKey) +
      "）。颜色越深里程越大，空心方块是休息日；这一页不看顶部的年份筛选，始终反映最近的状态。";
    document.getElementById("calLegend").innerHTML =
      "<span>休息</span>" + [0, 1, 2, 3, 4, 5].map(function (k) { return '<i class="heat' + k + '"></i>'; }).join("") +
      "<span>里程更多</span>";

    sizeCalendar();
    renderDay(state.day || (last ? last.date : tKey));
  }

  function renderDay(key) {
    state.day = key;
    var tKey = todayKey();
    var g = RUNS.filter(function (r) { return r.date === key; });
    var back = daySpan(tKey, key);
    var rel = back === 0 ? "今天" : back === 1 ? "昨天" : back > 1 ? back + " 天前" : "未来";
    document.getElementById("dayTitle").textContent = key + "（" + rel + "）";
    document.getElementById("dayHint").textContent = g.length ? g.length + " 场" : "休息日";

    var kpiEl = document.getElementById("dayKpis");
    var noteEl = document.getElementById("dayNotes");

    if (key < RUNS[0].date) {
      kpiEl.innerHTML = "";
      noteEl.innerHTML = factsHtml([{ k: "数据范围", v: "现有记录从 " + RUNS[0].date + " 开始",
        d: "更早的日期没有数据，无法判断那天有没有跑", badge: "无数据", level: "warn" }]);
      return;
    }

    if (!g.length) {
      kpiEl.innerHTML = "";
      var nb = runsIn(shiftDay(key, -1), shiftDay(key, -1));
      var na = runsIn(shiftDay(key, 1), shiftDay(key, 1));
      var nbTxt = nb.length ? r1(sumBy(nb, "distance")) + " km" : "也没跑";
      var naTxt = na.length ? r1(sumBy(na, "distance")) + " km" : "也没跑";
      noteEl.innerHTML = factsHtml([
        { k: "这天", v: "休息日（0 km）", badge: "没跑", level: "" },
        { k: "前一天", v: nbTxt, d: mmdd(shiftDay(key, -1)), badge: nb.length ? "有跑" : "连续休息", level: nb.length ? "" : "warn" },
        { k: "次日", v: naTxt, d: mmdd(shiftDay(key, 1)), badge: na.length ? "有跑" : "连续休息", level: na.length ? "" : "warn" },
        { k: "提示", v: "休息日就是训练的一部分，不必补跑", d: "连着休息两天以上才需要留意节奏" }
      ]);
      return;
    }

    var km = sumBy(g, "distance");
    var sec = sumBy(g, "duration");
    var rec = sumBy(g, "recover");
    kpiEl.innerHTML = kpiHtml([
      { k: "距离", v: r2(km), u: "km" },
      { k: "时长", v: fmtDur(sec) },
      { k: "平均配速", v: fmtPace(km ? sec / km : 0) },
      { k: "平均心率", v: Math.round(avg(g, "hr")), u: "bpm" },
      { k: "平均步频", v: Math.round(avg(g, "cadence")), u: "步/分" },
      { k: "触地时间", v: Math.round(avg(g, "td")), u: "ms" },
      { k: "垂直振幅", v: r1(avg(g, "va")), u: "cm" },
      { k: "训练负荷", v: Math.round(sumBy(g, "load")) },
      { k: "建议恢复", v: Math.round(rec), u: "小时" }
    ]);
    noteEl.innerHTML = factsHtml(buildDayFacts(g, key));
  }

  // 一组训练（同一天可能跑两次）的评价与建议。日视图和单次复盘共用，保证口径一致。
  // 返回 facts 行：{ k 标签, v 当前值, d 补充, badge 判定词, level 判定色 }
  function buildDayFacts(g, key) {
    var km = sumBy(g, "distance"), sec = sumBy(g, "duration");
    var z = zonesOf(g), rec = sumBy(g, "recover");
    var out = [];
    out.push({ k: "强度结构", v: "无氧阈下 " + Math.round(z.easyPct) + "%",
      d: "热身+燃脂 " + Math.round(z.lowPct) + "% · 有氧 " + Math.round(z.aerPct) + "% · 无氧+极限 " + Math.round(z.hardPct) + "%",
      badge: z.easyPct >= 70 ? "打底有氧课" : "偏硬", level: z.easyPct >= 70 ? "good" : "warn" });

    var t0 = String(g[g.length - 1].time || "00:00").split(":");
    var base = parseDay(key);
    base.setHours(+t0[0] || 0, +t0[1] || 0, 0, 0);
    var done = new Date(base.getTime() + (+rec || 0) * 3600000);
    var doneAt = dayKey(done) + " " + String(done.getHours()).padStart(2, "0") + ":" + String(done.getMinutes()).padStart(2, "0");
    var overdue = done.getTime() < Date.now();
    out.push({ k: "恢复窗口", v: "建议 " + Math.round(rec) + " 小时",
      d: "训练负荷 " + Math.round(sumBy(g, "load")) + " · 从开跑算起，到 " + doneAt + " 结束",
      badge: overdue ? "已结束" : "进行中", level: overdue ? "good" : "warn" });

    var band = RUNS.filter(function (r) {
      return r.date !== key && r.date >= shiftDay(key, -30) && r.date < key && Math.abs(r.distance - km) <= 1;
    });
    if (band.length >= 3) {
      var gHr = avg(g, "hr") - avg(band, "hr");
      var gTd = avg(g, "td") - avg(band, "td");
      out.push({ k: "同距离对照",
        v: "心率 " + Math.round(avg(g, "hr")) + " · 配速 " + fmtPace(km ? sec / km : 0),
        d: "近 30 天 " + band.length + " 场同距离（" + r1(km - 1) + "–" + r1(km + 1) + " km）：心率 " +
          Math.round(avg(band, "hr")) + " · 配速 " + fmtPace(paceOf(band)),
        badge: Math.abs(gHr) <= 3 ? "基本一致" : gHr < 0 ? "心率更低" : "心率更高",
        level: Math.abs(gHr) <= 3 ? "" : gHr < 0 ? "good" : "warn" });
      out.push({ k: "触地对比", v: (gTd <= 0 ? "短 " : "长 ") + Math.abs(Math.round(gTd)) + " ms",
        d: "本场 " + Math.round(avg(g, "td")) + " ms · 同距离均值 " + Math.round(avg(band, "td")) + " ms",
        badge: gTd <= 0 ? "步态更紧" : "步态略散", level: gTd <= 0 ? "good" : "warn" });
    }
    return out;
  }

  // ---------------------------------------------------------------- KPIs
  function kpiHtml(list) {
    return list.map(function (k) {
      return '<div class="kpi"><div class="k">' + k.k + '</div><div class="v">' + k.v +
        (k.u ? '<small>' + k.u + '</small>' : '') + '</div>' +
        (k.d ? '<div class="d">' + k.d + '</div>' : '') + '</div>';
    }).join("");
  }

  function renderOverview() {
    var adv = buildAdvice();
    var bar = document.getElementById("ovAdvice");
    if (bar) {
      bar.className = "advbar " + (adv.level === "rest" || adv.level === "warn" ? adv.level : "");
      bar.innerHTML = '<span class="tag">' + esc(adv.tag) + "</span><div><strong>" + esc(adv.headline) +
        "</strong><p>" + esc(adv.why) + '</p></div><span class="more">日视图 ›</span>';
    }
    var rs = byYear(state.year);
    var dist = rs.reduce(function (a, b) { return a + b.distance; }, 0);
    var dur = rs.reduce(function (a, b) { return a + b.duration; }, 0);
    var cal = rs.reduce(function (a, b) { return a + b.calories; }, 0);

    document.getElementById("kpis").innerHTML = kpiHtml([
      { k: "总里程", v: r1(dist), u: "km", d: rs.length + " 次跑步" },
      { k: "总时长", v: Math.round(dur / 3600 * 10) / 10, u: "小时", d: "平均 " + fmtDur(dur / rs.length) + "/次" },
      { k: "平均配速", v: fmtPace(dur / dist), d: "全程加权" },
      { k: "平均每次距离", v: r2(dist / rs.length), u: "km" },
      { k: "平均步频", v: Math.round(avg(rs, "cadence")), u: "步/分", d: "最大 " + Math.max.apply(null, rs.map(function (r) { return r.cadenceMax; })) },
      { k: "平均心率", v: Math.round(avg(rs, "hr")), u: "bpm", d: "峰值 " + Math.max.apply(null, rs.map(function (r) { return r.hrMax; })) },
      { k: "总卡路里", v: Math.round(cal), u: "kcal" },
      { k: "平均触地时间", v: Math.round(avg(rs, "td")), u: "ms" }
    ]);

    var labels = rs.map(function (r) { return mmdd(r.date); });

    mkChart("cDistPace", {
      type: "bar",
      data: {
        labels: labels,
        datasets: [
          { type: "bar", label: "距离", data: rs.map(function (r) { return r.distance; }), backgroundColor: "rgba(37,99,235,.55)", borderRadius: 3, yAxisID: "y", order: 3 },
          { type: "line", label: "平均配速", data: rs.map(function (r) { return r.pace; }), borderColor: C.pace, backgroundColor: C.pace, pointRadius: rs.length > 40 ? 0 : 2.5, borderWidth: 1.8, tension: .25, yAxisID: "y1", order: 1 }
        ]
      },
      options: {
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { position: "top", align: "end", labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true } },
          tooltip: { callbacks: { label: function (c) { return c.dataset.label + "：" + (c.dataset.yAxisID === "y1" ? fmtPace(c.parsed.y) : r2(c.parsed.y) + " km"); } } }
        },
        scales: {
          x: axis("", { ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 14 } }),
          y: axis("距离 km", { beginAtZero: true }),
          y1: axis("配速", { position: "right", reverse: true, grid: { drawOnChartArea: false }, ticks: { callback: function (v) { return fmtPace(v); } } })
        }
      }
    });

    var months = D.months.filter(function (m) { return state.year === "all" || m.month.slice(0, 4) === state.year; });
    mkChart("cMonth", {
      type: "bar",
      data: {
        labels: months.map(function (m) { return m.month.replace("-", "/"); }),
        datasets: [{ label: "当月里程", data: months.map(function (m) { return m.distance; }), backgroundColor: "rgba(37,99,235,.6)", borderRadius: 4 }]
      },
      options: {
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: {
            label: function (c) { var m = months[c.dataIndex]; return [r1(m.distance) + " km · " + m.count + " 次", "平均配速 " + fmtPace(m.pace)]; }
          } }
        },
        scales: { x: axis(""), y: axis("km", { beginAtZero: true }) }
      }
    });

    var zl = ["热身", "燃脂", "有氧", "无氧", "极限"];
    var zk = ["warm", "fat", "aer", "ana", "ext"];
    var zc = ["#93c5fd", "#6ee7b7", "#34d399", "#fbbf24", "#f87171"];
    mkChart("cZones", {
      type: "bar",
      data: {
        labels: months.map(function (m) { return m.month.replace("-", "/"); }),
        datasets: zk.map(function (k, i) {
          return {
            label: zl[i], backgroundColor: zc[i], borderRadius: 2,
            data: months.map(function (m) {
              var g = rs.filter(function (r) { return r.month === m.month; });
              return Math.round(g.reduce(function (a, r) { return a + (r.zones[k] || 0); }, 0) / 60);
            })
          };
        })
      },
      options: {
        plugins: { legend: { position: "bottom", labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true } } },
        scales: { x: axis("", { stacked: true }), y: axis("分钟", { stacked: true, beginAtZero: true }) }
      }
    });

    // ---- 训练负荷与状态：CTL / ATL / TSB 三条线，取代原来的「负荷 + 恢复」柱线
    var lm = loadModel();
    var statEl = document.getElementById("loadStat");
    var hintEl = document.getElementById("loadHint");
    if (lm) {
      var L = lm.last, ts = tsbState(L.tsb);
      statEl.innerHTML =
        statHtml("体能 CTL", r1(L.ctl), "42 天负荷均值", "good") +
        statHtml("疲劳 ATL", r1(L.atl), "7 天负荷均值", "warn") +
        statHtml("状态 TSB", (L.tsb >= 0 ? "+" : "") + r1(L.tsb), ts.word, ts.level);
      hintEl.textContent = ts.tip + "。负荷取自小米单次 train_load 按日累加；CTL 涨得慢、ATL 来得快去得也快，" +
        "两者相减得到 TSB。这套数值只跟自己的历史纵向比，跨设备没有可比性。";

      var win = lm.days.slice(-182);            // 半年足够看趋势，再长就糊成一片
      var tmin = Math.min.apply(null, win.map(function (d) { return d.tsb; }));
      var tmax = Math.max.apply(null, win.map(function (d) { return d.tsb; }));
      mkChart("cLoad", {
        type: "line",
        data: {
          labels: win.map(function (d) { return mmdd(d.k); }),
          datasets: [
            { type: "line", label: "体能 CTL", data: win.map(function (d) { return d.ctl; }),
              borderColor: C.dist, backgroundColor: C.dist + "1a", fill: "start", pointRadius: 0, borderWidth: 2.4, tension: .3 },
            { type: "line", label: "疲劳 ATL", data: win.map(function (d) { return d.atl; }),
              borderColor: C.hr, backgroundColor: "transparent", pointRadius: 0, borderWidth: 1.6, tension: .3 },
            { type: "line", label: "状态 TSB", data: win.map(function (d) { return d.tsb; }),
              borderColor: C.stride, backgroundColor: C.stride, pointRadius: 0, borderWidth: 1.6, borderDash: [5, 4], tension: .3 }
          ]
        },
        options: {
          interaction: { mode: "index", intersect: false },
          plugins: {
            legend: { position: "top", align: "end", labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true } },
            tooltip: { callbacks: { label: function (c) { return c.dataset.label + "：" + r1(c.parsed.y); } } }
          },
          scales: {
            x: axis("", { ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 12 } }),
            y: axis("负荷", {})
          }
        }
      });
      document.getElementById("cLoadSub").textContent = "CTL 体能（涨得慢）· ATL 疲劳（来得快、去得也快）· TSB 状态 = CTL − ATL。近半年 TSB 区间 " +
        r1(tmin) + " ~ " + r1(tmax) + "，当前 " + (L.tsb >= 0 ? "+" : "") + r1(L.tsb) + "（" + ts.word + "）";
    } else {
      statEl.innerHTML = "";
      hintEl.textContent = "还没有足够的数据来算训练负荷模型。";
    }
  }

  // ---------------------------------------------------------------- gait
  function bandChart(id, rs, key, minK, maxK, title, color, unit) {
    var labels = rs.map(function (r) { return mmdd(r.date); });
    return mkChart(id, {
      type: "line",
      data: {
        labels: labels,
        datasets: [
          { type: "line", label: "当场上限", data: rs.map(function (r) { return r[maxK] || null; }), borderColor: "transparent", backgroundColor: color + "22", pointRadius: 0, fill: "+1", tension: .25, order: 3 },
          { type: "line", label: "当场下限", data: rs.map(function (r) { return r[minK] || null; }), borderColor: "transparent", backgroundColor: "transparent", pointRadius: 0, tension: .25, order: 3 },
          { type: "line", label: "平均" + title, data: rs.map(function (r) { return r[key] || null; }), borderColor: color, backgroundColor: color, pointRadius: rs.length > 40 ? 0 : 2.5, borderWidth: 1.8, tension: .25, order: 1 }
        ]
      },
      options: {
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { position: "top", align: "end", labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true, filter: function (i) { return i.text === "平均" + title; } } },
          tooltip: { callbacks: { label: function (c) { return c.dataset.label + "：" + r1(c.parsed.y) + unit; } } }
        },
        scales: { x: axis("", { ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 12 } }), y: axis(unit, {}) }
      }
    });
  }

  function scatter(id, pts, xL, yL, color, xTitle, yTitle, unit) {
    var f = fit(pts);
    var ds = [{ type: "scatter", label: yL, data: pts, backgroundColor: color + "cc", pointRadius: 3.5, pointHoverRadius: 5 }];
    if (f) ds.push(fitLine(f, color, "趋势 r=" + r2(f.r)));
    return mkChart(id, {
      type: "scatter",
      data: { datasets: ds },
      options: {
        plugins: {
          legend: { position: "top", align: "end", labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true } },
          tooltip: { callbacks: {
            label: function (c) {
              if (c.dataset.type === "line") return "拟合趋势";
              var p = c.raw; return xTitle + " " + (unit === "pace" ? fmtPace(p.x) : r1(p.x)) + " · " + yTitle + " " + r1(p.y);
            }
          } }
        },
        scales: {
          x: axis(xTitle, unit === "pace" ? { reverse: true, ticks: { callback: function (v) { return fmtPace(v); } } } : {}),
          y: axis(yTitle, {})
        }
      }
    });
  }

  function renderGait() {
    var rs = byYear(state.year);

    // ---- 跑姿体检表：先用一张表说清「现在什么水平、该练什么」，图表放下面看趋势
    var gc = gaitCheck();
    var tbl = document.getElementById("gaitCheck");
    var gsub = document.getElementById("gaitCheckSub");
    if (gc && tbl) {
      tbl.innerHTML =
        '<thead><tr><th>指标</th><th>最近均值</th><th>参考区间</th><th>判定</th><th>怎么改</th></tr></thead><tbody>' +
        gc.rows.map(function (r) {
          return "<tr><td>" + esc(r.k) + '</td><td class="gv">' + esc(r.v) + '</td><td class="gb">' + esc(r.band) +
            '</td><td><span class="badge ' + r.level + '">' + esc(r.word) + "</span></td><td class=\"gt\">" + esc(r.tip) + "</td></tr>";
        }).join("") + "</tbody>";
      if (gsub) gsub.textContent = "取最近 " + gc.n + " 场（" + gc.from + " – " + gc.to +
        "）的平均值，对照跑圈通行区间；最后一列是可直接照做的改法。";
    }

    bandChart("cTd", rs, "td", "tdMin", "tdMax", "触地时间", C.td, "ms");
    bandChart("cVa", rs, "va", "vaMin", "vaMax", "垂直振幅", C.va, "cm");
    bandChart("cVsr", rs, "vsr", "vsrMin", "vsrMax", "垂直步幅比", C.vsr, "%");

    mkChart("cCadStride", {
      type: "line",
      data: {
        labels: rs.map(function (r) { return mmdd(r.date); }),
        datasets: [
          { type: "line", label: "步频", data: rs.map(function (r) { return r.cadence; }), borderColor: C.cad, backgroundColor: C.cad, pointRadius: rs.length > 40 ? 0 : 2.5, borderWidth: 1.8, tension: .25, yAxisID: "y" },
          { type: "line", label: "步幅", data: rs.map(function (r) { return r.stride; }), borderColor: C.stride, backgroundColor: C.stride, pointRadius: rs.length > 40 ? 0 : 2.5, borderWidth: 1.8, tension: .25, yAxisID: "y1" }
        ]
      },
      options: {
        interaction: { mode: "index", intersect: false },
        plugins: { legend: { position: "top", align: "end", labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true } } },
        scales: {
          x: axis("", { ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 10 } }),
          y: axis("步/分", {}),
          y1: axis("米", { position: "right", grid: { drawOnChartArea: false }, min: 0.7 })
        }
      }
    });

    scatter("cScPaceTd", rs.filter(function (r) { return r.td > 0; }).map(function (r) { return { x: r.pace, y: r.td }; }),
      "触地时间", "触地时间", C.td, "配速", "触地 ms", "pace");

    scatter("cScCadVa", rs.filter(function (r) { return r.cadence > 0; }).map(function (r) { return { x: r.cadence, y: r.va }; }),
      "垂直振幅", "垂直振幅", C.va, "步频", "振幅 cm");

    // ---- 解读：与上面的体检表用同一个窗口（最近 20 场），否则同一页会出现
    // 两套互相打架的数字（"最近 20 场 165" 对 "2026 全年 160"）。这里只留
    // 表格给不出的东西：两个相关性 + 一句「优先改哪一项」。
    var win = RUNS.slice(-20);
    var W = "近 " + win.length + " 场";
    var cadFit = fit(win.filter(function (r) { return r.cadence > 0 && r.td > 0; }).map(function (r) { return [r.cadence, r.td]; }));
    var paceFit = fit(win.filter(function (r) { return r.td > 0; }).map(function (r) { return [r.pace, r.td]; }));
    var rows = [];
    if (cadFit) {
      rows.push({ k: "步频 × 触地", v: "r = " + r2(cadFit.r) + "（" + W + "）",
        d: cadFit.r < -0.3
          ? "负相关成立：步频每提高 1 步/分，触地约减少 " + Math.abs(r1(cadFit.b)) + " ms —— 这就是先练步频的理由"
          : "两者关系不明显。步频与触地的联动在这段数据里没体现出来，别指望一提步频触地就自动变短",
        badge: cadFit.r < -0.3 ? "先练步频收益最大" : "联动不明显", level: cadFit.r < -0.3 ? "good" : "" });
    }
    if (paceFit && paceFit.r > 0.5) {
      rows.push({ k: "配速 × 触地", v: "r = " + r2(paceFit.r) + "（" + W + "）",
        d: "跑得慢触地自然长。看触地趋势必须按配速归一化，否则会把「跑得慢」误读成「跑姿退步」",
        badge: "被配速主导", level: "warn" });
    }
    // 优先改善项：取体检表里最不达标的一项，直接给动作
    if (gc) {
      var order = { bad: 0, warn: 1, "": 2, good: 3 };
      var worst = gc.rows.slice().sort(function (a, b) { return order[a.level] - order[b.level]; })[0];
      rows.push({ k: "优先改善", v: worst.k + "（" + worst.word + "）",
        d: worst.tip, badge: worst.level === "good" ? "暂无短板" : "先动这一项",
        level: worst.level === "good" ? "good" : worst.level || "warn" });
    }
    var gaitNoteEl = document.getElementById("gaitNotes");
    if (gaitNoteEl) gaitNoteEl.innerHTML = factsHtml(rows);
  }

  // ---------------------------------------------------------------- single
  function runSeries(r) { return r && (r.hrSeries.length || r.paceSeries.length || r.cadSeries.length); }

  function fillPick() {
    var sel = document.getElementById("runPick");
    var rs = byYear(state.year).filter(runSeries).sort(function (a, b) { return b.id - a.id; });
    if (!rs.length) { sel.innerHTML = "<option>该范围没有逐分钟数据</option>"; return; }
    if (!state.sel || !rs.some(function (r) { return r.id === state.sel; })) state.sel = rs[0].id;
    sel.innerHTML = rs.map(function (r) {
      return '<option value="' + r.id + '"' + (r.id === state.sel ? " selected" : "") + ">" + r.date + " " + r.time + " · " + r2(r.distance) + " km · " + fmtPace(r.pace) + "</option>";
    }).join("");
  }

  function halfDrift(r) {
    var arr = r.paceSeries;
    if (arr.length < 8) return null;
    var half = Math.floor(arr.length / 2);
    var a = arr.slice(0, half), b = arr.slice(half);
    var pace1 = a.reduce(function (s, p) { return s + p[1]; }, 0) / a.length;
    var pace2 = b.reduce(function (s, p) { return s + p[1]; }, 0) / b.length;
    var h = r.hrSeries;
    var hr1 = null, hr2 = null;
    if (h.length >= 8) {
      var hh = Math.floor(h.length / 2);
      hr1 = h.slice(0, hh).reduce(function (s, p) { return s + p[1]; }, 0) / hh;
      hr2 = h.slice(hh).reduce(function (s, p) { return s + p[1]; }, 0) / (h.length - hh);
    }
    return { pace1: pace1, pace2: pace2, hr1: hr1, hr2: hr2 };
  }

  function renderSingle() {
    fillPick();
    var r = RUNS.filter(function (x) { return x.id === state.sel; })[0];
    if (!r) return;

    var d = halfDrift(r);
    var drift = d ? d.pace2 - d.pace1 : 0;
    document.getElementById("kpisSingle").innerHTML = kpiHtml([
      { k: "距离", v: r2(r.distance), u: "km", d: r.date + " " + r.time },
      { k: "时长", v: fmtDur(r.duration) },
      { k: "平均配速", v: fmtPace(r.pace) },
      { k: "平均步频", v: r.cadence, u: "步/分", d: "最大 " + r.cadenceMax },
      { k: "步幅", v: r2(r.stride), u: "m" },
      { k: "平均心率", v: r.hr, u: "bpm", d: r.hrMin + "–" + r.hrMax },
      { k: "触地时间", v: Math.round(r.td), u: "ms", d: Math.round(r.tdMin) + "–" + Math.round(r.tdMax) },
      { k: "垂直振幅", v: r1(r.va), u: "cm", d: "步幅比 " + r1(r.vsr) + "%" },
      { k: "卡路里", v: r.calories, u: "kcal" },
      { k: "后半程配速漂移", v: (drift >= 0 ? "+" : "") + Math.round(drift), u: "秒/km", d: drift > 3 ? "后半程掉速" : "前后基本持平" }
    ]);

    // 单次复盘也要给结论，不只是画图：强度结构、恢复窗口、与最近同距离场次的对比
    var singleNotes = document.getElementById("singleNotes");
    if (singleNotes) singleNotes.innerHTML = factsHtml(buildDayFacts([r], r.date));

    var hrs = r.hrSeries, pcs = r.paceSeries, cds = r.cadSeries;
    var maxM = Math.max(
      pcs.length ? pcs[pcs.length - 1][0] : 0,
      hrs.length ? hrs[hrs.length - 1][0] : 0,
      cds.length ? cds[cds.length - 1][0] : 0);
    var labels = [];
    for (var m = 0; m <= Math.ceil(maxM); m++) labels.push(m);
    function seriesToArr(s) { var a = new Array(labels.length).fill(null); s.forEach(function (p) { if (p[0] <= maxM) a[Math.round(p[0])] = p[1]; }); return a; }

    var pv = pcs.map(function (p) { return p[1]; }).sort(function (a, b) { return a - b; });
    var paceLo = 380, paceHi = 520;
    if (pv.length >= 5) {
      paceLo = Math.floor(pv[Math.floor(pv.length * 0.05)] / 15) * 15 - 15;
      paceHi = Math.ceil(pv[Math.floor(pv.length * 0.95)] / 15) * 15 + 15;
      if (paceHi - paceLo < 60) { paceHi = paceLo + 60; }
    }

    mkChart("cSingle", {
      type: "line",
      data: {
        labels: labels.map(function (v) { return v + "′"; }),
        datasets: [
          { type: "line", label: "配速（原始）", data: seriesToArr(pcs), borderColor: C.pace + "55", backgroundColor: "transparent", pointRadius: 0, borderWidth: 1, tension: .3, yAxisID: "y", spanGaps: true },
          { type: "line", label: "配速（5 分钟中位平滑）", data: seriesToArr(smooth(pcs, 2)), borderColor: C.pace, backgroundColor: C.pace, pointRadius: 0, borderWidth: 2.2, tension: .3, yAxisID: "y", spanGaps: true },
          { type: "line", label: "心率", data: seriesToArr(hrs), borderColor: C.hr, backgroundColor: C.hr, pointRadius: 0, borderWidth: 1.8, tension: .3, yAxisID: "y1", spanGaps: true, stepped: true },
          { type: "line", label: "步频（平滑）", data: seriesToArr(smooth(cds, 2)), borderColor: C.cad, backgroundColor: C.cad, pointRadius: 0, borderWidth: 1.5, borderDash: [4, 3], tension: .3, yAxisID: "y2", spanGaps: true }
        ]
      },
      options: {
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { position: "top", align: "end", labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true } },
          tooltip: { callbacks: { label: function (c) { return c.dataset.label + "：" + (c.dataset.label.indexOf("配速") === 0 ? fmtPace(c.parsed.y) : r1(c.parsed.y)); } } }
        },
        scales: {
          x: axis("分钟", { ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 16 } }),
          y: axis("配速", { reverse: true, min: paceLo, max: paceHi, ticks: { callback: function (v) { return fmtPace(v); } } }),
          y1: axis("心率 bpm", { position: "right", grid: { drawOnChartArea: false } }),
          y2: axis("步频", { position: "right", grid: { drawOnChartArea: false }, ticks: { display: false } })
        }
      }
    });

    var bins = {};
    pcs.forEach(function (p) { var b = Math.floor(p[1] / 10) * 10; bins[b] = (bins[b] || 0) + 1; });
    var keys = Object.keys(bins).map(Number).sort(function (a, b) { return a - b; });
    mkChart("cHist", {
      type: "bar",
      data: {
        labels: keys.map(function (k) { return fmtPace(k); }),
        datasets: [{ label: "分钟数", data: keys.map(function (k) { return bins[k]; }), backgroundColor: "rgba(217,119,6,.6)", borderRadius: 3 }]
      },
      options: {
        plugins: { legend: { display: false } },
        scales: { x: axis("配速区间", { ticks: { maxRotation: 60 } }), y: axis("分钟", { beginAtZero: true }) }
      }
    });

    if (d && d.hr1) {
      var oldMsg = document.getElementById("driftMsg");
      if (oldMsg && oldMsg.parentNode) oldMsg.parentNode.removeChild(oldMsg);
      mkChart("cDrift", {
        type: "bar",
        data: {
          labels: ["平均配速", "平均心率"],
          datasets: [
            { label: "前半程", data: [d.pace1, d.hr1], backgroundColor: "rgba(37,99,235,.55)", borderRadius: 3 },
            { label: "后半程", data: [d.pace2, d.hr2], backgroundColor: "rgba(220,38,38,.55)", borderRadius: 3 }
          ]
        },
        options: {
          plugins: {
            legend: { position: "top", align: "end", labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true } },
            tooltip: { callbacks: { label: function (c) { return c.dataset.label + "：" + (c.dataIndex === 0 ? fmtPace(c.parsed.y) : r1(c.parsed.y) + " bpm"); } } }
          },
          scales: { x: axis(""), y: axis("（两种单位不同，仅示意升降）", { beginAtZero: true }) }
        }
      });
    } else {
      if (charts["cDrift"]) { charts["cDrift"].destroy(); delete charts["cDrift"]; }
      var el = document.getElementById("cDrift");
      if (el && el.parentNode && !document.getElementById("driftMsg")) {
        var msg = document.createElement("p");
        msg.id = "driftMsg";
        msg.style.cssText = "padding:12px;color:#8b9099";
        msg.textContent = "该场次缺少足够的心率点，无法计算半程漂移。";
        el.parentNode.appendChild(msg);
      }
    }
  }

  // ---------------------------------------------------------------- month cmp
  // 「本月 vs 上月」。两个月的跑量常常差很多（2026-09 单次均 7.4 km 对 2026-08 的 5.7 km），
  // 所以除了月份均值本身，另给两张控制变量的图：
  //   · 配速—心率散点  → 控制强度，看同样配速下的心率
  //   · 同距离档位心率 → 控制跑量，看同样距离下的心率
  function monthsOf() {
    var m = {};
    RUNS.forEach(function (r) { m[r.month] = 1; });
    return Object.keys(m).sort();
  }
  function prevMonth(m) {
    var y = +m.slice(0, 4), mo = +m.slice(5, 7) - 1;
    if (mo < 1) { mo = 12; y -= 1; }
    return y + "-" + (mo < 10 ? "0" : "") + mo;
  }
  function mLabel(m) { return +m.slice(0, 4) + " 年 " + (+m.slice(5, 7)) + " 月"; }

  // dir: "up" / "down" 标明哪个方向算变好；null 表示该项只作对照，不判好坏
  var M_KEYS = [
    { k: "场次", u: "场", dir: null, f: function (a) { return a.length; }, fmt: function (v) { return v; } },
    { k: "总里程", u: "km", dir: null, f: function (a) { return a.reduce(function (s, r) { return s + r.distance; }, 0); }, fmt: r1 },
    { k: "单次均距", u: "km", dir: null, f: function (a) { return a.length ? a.reduce(function (s, r) { return s + r.distance; }, 0) / a.length : 0; }, fmt: r2 },
    { k: "平均配速", u: "", dir: null, f: paceOf, fmt: fmtPace },
    { k: "平均心率", u: "bpm", dir: "down", f: function (a) { return avg(a, "hr"); }, fmt: function (v) { return Math.round(v); } },
    { k: "平均步频", u: "步/分", dir: "up", f: function (a) { return avg(a, "cadence"); }, fmt: function (v) { return Math.round(v); } },
    { k: "平均步幅", u: "米", dir: null, f: function (a) { return avg(a, "stride"); }, fmt: r2 },
    { k: "触地时间", u: "ms", dir: "down", f: function (a) { return avg(a, "td"); }, fmt: function (v) { return Math.round(v); } },
    { k: "垂直振幅", u: "cm", dir: "down", f: function (a) { return avg(a, "va"); }, fmt: r1 },
    { k: "垂直步幅比", u: "%", dir: "down", f: function (a) { return avg(a, "vsr"); }, fmt: r1 },
    { k: "最大摄氧量", u: "ml/kg", dir: "up", f: function (a) { return avg(a, "vo2"); }, fmt: r1 }
  ];

  function dropChart(id) { if (charts[id]) { charts[id].destroy(); delete charts[id]; } }

  function renderMonthCmp() {
    var ms = monthsOf();
    var selM = document.getElementById("cmpMonth");
    var selB = document.getElementById("cmpBase");
    var want = ms.slice().reverse();
    // 选项只在月份集合变化时重建，否则每次渲染都会重置下拉状态
    var have = Array.prototype.map.call(selM.options || [], function (o) { return o.value; });
    if (have.join(",") !== want.join(",")) {
      var opts = want.map(function (m) { return '<option value="' + m + '">' + esc(mLabel(m)) + "</option>"; }).join("");
      selM.innerHTML = opts;
      selB.innerHTML = opts;
    }
    if (!state.cmpMonth || ms.indexOf(state.cmpMonth) < 0) state.cmpMonth = ms[ms.length - 1];
    // 基准月默认取本期的上一月，但允许任选另一月（比如 6 月对 9 月）
    if (!state.cmpBase || ms.indexOf(state.cmpBase) < 0) {
      var dflt = prevMonth(state.cmpMonth);
      state.cmpBase = ms.indexOf(dflt) >= 0 ? dflt : ms[0];
    }
    selM.value = state.cmpMonth;
    selB.value = state.cmpBase;

    var cur = state.cmpMonth, pre = state.cmpBase;
    var A = RUNS.filter(function (r) { return r.month === cur; });
    var B = RUNS.filter(function (r) { return r.month === pre; });
    var kA = mLabel(cur), kB = mLabel(pre);
    // 每边至少 3 场才算变化率 / 做同档位比较：单场或两场之间的百分比只是噪声，
    // 曾出现过「最大摄氧量 +101.5%」这种由 1 场样本算出来的荒唐数字。
    var MINN = 3;
    var enough = A.length >= MINN && B.length >= MINN;
    function km(a) { return a.reduce(function (s, r) { return s + r.distance; }, 0); }

    document.getElementById("mCmpHint").textContent =
      kB + "（" + B.length + " 场，单次 " + (B.length ? r2(km(B) / B.length) : "—") + " km）  对比  " +
      kA + "（" + A.length + " 场，单次 " + (A.length ? r2(km(A) / A.length) : "—") + " km）";

    var notes = [];
    if (!A.length || !B.length || cur === pre) {
      notes.push(["warn", cur === pre ? "基准月与本期相同" : "缺少可比月份",
        cur === pre
          ? "两个下拉选到了同一个月，看不出变化。把「基准月」换成另一个有记录的月份。"
          : kB + " 或 " + kA + " 没有跑步记录，无法对比。"]);
      document.getElementById("mCmpNotes").innerHTML = notes.map(function (n) {
        return '<div class="note ' + n[0] + '"><h3>' + esc(n[1]) + '</h3><p>' + esc(n[2]) + "</p></div>";
      }).join("");
      document.getElementById("mCmpTbl").innerHTML = "";
      document.getElementById("mScSub").textContent = cur === pre ? "请选两个不同的月份" : "所选月份缺少数据";
      document.getElementById("mBandSub").textContent = cur === pre ? "请选两个不同的月份" : "所选月份缺少数据";
      document.getElementById("mKpiSub").textContent = cur === pre ? "请选两个不同的月份" : "所选月份缺少数据";
      ["cMonthSc", "cMonthBand", "cMonthKpi"].forEach(dropChart);
      return;
    }

    // ---- 配速与心率散点（控制强度）
    var ptsP = B.map(function (r) { return { x: r.pace, y: r.hr }; });
    var ptsC = A.map(function (r) { return { x: r.pace, y: r.hr }; });
    var fP = fit(ptsP.map(function (p) { return [p.x, p.y]; }));
    var fC = fit(ptsC.map(function (p) { return [p.x, p.y]; }));
    var sds = [
      { type: "scatter", label: kB, data: ptsP, backgroundColor: C.ink3 + "99", pointRadius: 3 },
      { type: "scatter", label: kA, data: ptsC, backgroundColor: C.cad + "cc", pointRadius: 3.5 }
    ];
    if (fP) sds.push(fitLine(fP, C.ink3, kB + " 趋势"));
    if (fC) sds.push(fitLine(fC, C.cad, kA + " 趋势"));
    mkChart("cMonthSc", {
      type: "scatter",
      data: { datasets: sds },
      options: {
        plugins: {
          legend: { position: "top", align: "end", labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true } },
          tooltip: {
            callbacks: {
              label: function (c) {
                if (c.dataset.type === "line") return c.dataset.label;
                return "配速 " + fmtPace(c.raw.x) + " · 心率 " + c.raw.y + " bpm";
              }
            }
          }
        },
        scales: { x: axis("平均配速", { reverse: true, ticks: { callback: function (v) { return fmtPace(v); } } }), y: axis("平均心率 bpm", {}) }
      }
    });
    document.getElementById("mScSub").textContent = kB + " 与 " + kA + " 的每场记录，虚线为各自趋势";

    // 同配速心率：取两月共同覆盖的配速区间里的评价点，避免外推
    var evPace = 0, hrPre = 0, hrCur = 0, gapHr = 0, hasFit = !!(fP && fC);
    if (hasFit) {
      var lo = Math.max(fP.min, fC.min), hi = Math.min(fP.max, fC.max);
      evPace = hi > lo ? (lo + hi) / 2 : (avg(B, "pace") + avg(A, "pace")) / 2;
      hrPre = fP.a + fP.b * evPace;
      hrCur = fC.a + fC.b * evPace;
      gapHr = hrPre - hrCur;   // 正数 = 本月心率更低 = 变好
    }

    // ---- 同距离档位心率（控制跑量）
    // 每边至少 MINN 场才纳入：单场对单场比出来的差异只是噪声，不能当成趋势。
    var bands = [[0, 5, "5 km 内"], [5, 7, "5–7 km"], [7, 9, "7–9 km"], [9, 999, "9 km 以上"]];
    var bl = [], bDiff = [], bCol = [], bTip = [], pairs = [], skipped = [];
    bands.forEach(function (b) {
      var gp = B.filter(function (r) { return r.distance >= b[0] && r.distance < b[1]; });
      var gc = A.filter(function (r) { return r.distance >= b[0] && r.distance < b[1]; });
      if (!gp.length && !gc.length) return;
      if (gp.length < MINN || gc.length < MINN) {
        skipped.push(b[2] + "（" + gp.length + " / " + gc.length + " 场）");
        return;
      }
      var hp = avg(gp, "hr"), hc = avg(gc, "hr"), d = r1(hc - hp);
      pairs.push({ b: b, gp: gp, gc: gc, hp: hp, hc: hc, d: d });
      bl.push(b[2] + "  " + (d > 0 ? "+" : "") + d.toFixed(1) + " bpm");
      bDiff.push(d);
      bCol.push(d < -1 ? "rgba(13,148,136,.65)" : d > 1 ? "rgba(220,38,38,.6)" : "rgba(139,144,153,.5)");
      bTip.push(b[2] + "：" + kB + " " + gp.length + " 场（配速 " + fmtPace(paceOf(gp)) + "，心率 " + Math.round(hp) +
        "）→ " + kA + " " + gc.length + " 场（配速 " + fmtPace(paceOf(gc)) + "，心率 " + Math.round(hc) + "）");
    });
    var bandSubBase = "同一距离档位内的心率差，负值代表本月更省力；只收录两月都至少 " + MINN + " 场的档位";
    if (bl.length) {
      mkChart("cMonthBand", {
        type: "bar",
        data: {
          labels: bl,
          datasets: [{
            label: kA + " 心率 − " + kB + " 心率",
            data: bDiff,
            backgroundColor: bCol,
            borderRadius: 4
          }]
        },
        options: {
          indexAxis: "y",
          plugins: {
            legend: { display: false },
            tooltip: { callbacks: { label: function (c) { return bTip[c.dataIndex]; } } }
          },
          scales: { x: axis("心率差 bpm（负值 = 本月更低 = 更省力）", {}), y: axis("", { ticks: { font: { size: 10 } } }) }
        }
      });
      document.getElementById("mBandSub").textContent =
        bandSubBase + (skipped.length ? "；已排除样本不足的 " + skipped.join("、") : "");
    } else {
      dropChart("cMonthBand");
      document.getElementById("mBandSub").textContent = skipped.length
        ? "两月没有样本都足够的共同档位（" + skipped.join("、") + "），无法做同跑量比较"
        : "两月没有共同的距离档位，无法做同跑量比较";
    }

    // ---- 关键指标变化（方向化：正数 = 变好）
    var mk = M_KEYS.filter(function (c) { return c.dir; });
    var kl = [], kv = [], kc = [], ktip = [];
    mk.forEach(function (c) {
      var vp = c.f(B), vc = c.f(A);
      var raw = vp ? (vc - vp) / vp * 100 : 0;
      var gain = c.dir === "down" ? -raw : raw;
      var g = r1(gain);
      kv.push(g);
      kc.push(g > 1 ? "rgba(13,148,136,.65)" : g < -1 ? "rgba(220,38,38,.6)" : "rgba(139,144,153,.5)");
      kl.push(c.k + "  " + (g >= 0 ? "+" : "") + g.toFixed(1) + "%");
      ktip.push(c.k + "：" + c.fmt(vp) + " → " + c.fmt(vc) + " " + c.u + "（" + (g >= 0 ? "改善 " : "退步 ") + Math.abs(g).toFixed(1) + "%）");
    });
    if (enough) {
      mkChart("cMonthKpi", {
        type: "bar",
        data: {
          labels: kl,
          datasets: [{ label: kA + " 相对 " + kB + " 的变化", data: kv, backgroundColor: kc, borderRadius: 4 }]
        },
        options: {
          indexAxis: "y",
          plugins: {
            legend: { display: false },
            tooltip: { callbacks: { label: function (c) { return ktip[c.dataIndex]; } } }
          },
          scales: { x: axis("变化 %（正向 = 改善）", {}), y: axis("", { ticks: { font: { size: 10 } } }) }
        }
      });
      document.getElementById("mKpiSub").textContent = "只收录方向明确的指标；配速与步幅见下方表格";
    } else {
      dropChart("cMonthKpi");
      document.getElementById("mKpiSub").textContent =
        "两月各需至少 " + MINN + " 场才计算变化率（当前 " + B.length + " / " + A.length + " 场），样本不足，请看下方表格的原始均值";
    }

    // ---- 结论（文案要短：对比页要一屏读完，每条只留结论句，过程数字看表格/图）
    if (pairs.length) {
      // 主档位取样本量最大的那个，不取「改善最明显」的，避免挑好看的写结论
      var primary = pairs.slice().sort(function (a, b) {
        return (b.gp.length + b.gc.length) - (a.gp.length + a.gc.length);
      })[0];
      var others = pairs.filter(function (p) { return p !== primary; });
      var tail = "";
      if (others.length) {
        var list = others.map(function (p) { return p.b[2] + " " + (p.d > 0 ? "+" : "") + p.d.toFixed(1) + " bpm"; }).join("、");
        // 方向是否一致要真算，不能写死
        var sameDir = others.every(function (p) { return (p.d <= 0) === (primary.d <= 0); });
        tail = sameDir ? "其余档位方向一致。"
          : "但 " + list + " 方向不同，样本偏少时先不作为结论。";
      }
      notes.push([primary.d < -1 ? "good" : primary.d > 1 ? "bad" : "warn", "同距离档位心率",
        "样本最足的 " + primary.b[2] + "：心率 " + Math.round(primary.hp) + " → " + Math.round(primary.hc) +
        " bpm（配速都落在 " + fmtPace(paceOf(primary.gp)) + " 上下），心率" + (primary.d < 0 ? "降 " : "升 ") +
        Math.abs(primary.d) + " bpm，同样距离" + (primary.d < 0 ? "更省力了" : "更吃力了") + "。" + tail]);
    }
    if (hasFit) {
      notes.push([gapHr > 2 ? "good" : gapHr < -2 ? "bad" : "warn", "同配速心率",
        fmtPace(evPace) + " 配速下：" + Math.round(hrPre) + " → " + Math.round(hrCur) + " bpm，" +
        (gapHr > 0 ? "同配速心率更低，有氧在变好" : gapHr < 0 ? "同配速心率更高，需要留意" : "基本持平") + "。"]);
    }
    var dPre = km(B) / B.length, dCur = km(A) / A.length;
    notes.push([Math.abs(dCur - dPre) / dPre > .15 ? "warn" : "good", "跑量前提",
      "单次均 " + r2(dPre) + " → " + r2(dCur) + " km（" + (dCur >= dPre ? "+" : "") + r1((dCur - dPre) / dPre * 100) +
      "%）。差得多时均值不能直接比，要看「同距离档位」与「同配速」两张控制变量的图。"]);
    if (!enough) {
      notes.push(["warn", "样本不足",
        "两月各需至少 " + MINN + " 场才算变化率；当前 " + B.length + " / " + A.length + " 场，只展示原始均值。"]);
    }
    if (enough) {
      var gCad = (avg(A, "cadence") - avg(B, "cadence")) / avg(B, "cadence") * 100;
      var gVa = (avg(B, "va") - avg(A, "va")) / avg(B, "va") * 100;
      notes.push([gCad > 1 && gVa > 1 ? "good" : "warn", "跑姿变化",
        "步频 " + Math.round(avg(B, "cadence")) + " → " + Math.round(avg(A, "cadence")) + " 步/分，振幅 " +
        r1(avg(B, "va")) + " → " + r1(avg(A, "va")) + " cm。" +
        (gCad > 1 && gVa > 1 ? "步频升、振幅降，跑姿在往省力方向走。" : "方向不一致，先看趋势别急着下结论。")]);
    }
    document.getElementById("mCmpNotes").innerHTML = notes.map(function (n) {
      return '<div class="note ' + n[0] + '"><h3>' + esc(n[1]) + '</h3><p>' + esc(n[2]) + "</p></div>";
    }).join("");

    // ---- 指标表（样本不足时只给原始均值，不给百分比）
    document.getElementById("mCmpTbl").innerHTML =
      "<thead><tr><th>指标</th><th>" + esc(kB) + "</th><th>" + esc(kA) + "</th><th>变化</th></tr></thead><tbody>" +
      M_KEYS.map(function (c) {
        var vp = c.f(B), vc = c.f(A);
        var raw = vp ? (vc - vp) / vp * 100 : 0;
        var s = r1(raw);
        var col = c.dir ? (s * (c.dir === "down" ? -1 : 1) > 0 ? "#0d9488" : "#dc2626") : "#8b9099";
        var cell = enough ? '<span style="color:' + col + '">' + (s >= 0 ? "+" : "") + s.toFixed(1) + "%</span>" : "—";
        return "<tr><td>" + c.k + "</td><td class='num'>" + c.fmt(vp) + (c.u ? " " + c.u : "") +
          "</td><td class='num'>" + c.fmt(vc) + (c.u ? " " + c.u : "") + "</td><td class='num'>" + cell + "</td></tr>";
      }).join("") + "</tbody>";
  }

  // ---------------------------------------------------------------- compare
  function renderCompare() {
    renderMonthCmp();
    var rs26 = RUNS.filter(function (r) { return r.year === 2026; });
    var rs25 = RUNS.filter(function (r) { return r.year === 2025; });

    // 散点数据保留：cmpNotes 的跨年结论要算「同配速下心率差」。
    // 但散点图本身不再画——它和月度对比里的 cMonthSc 功能重复，
    // 一页两份散点挤不掉（多弗朗要求对比页一屏读完），文字结论足够。
    var pts26 = rs26.map(function (r) { return { x: r.pace, y: r.hr }; });
    var pts25 = rs25.map(function (r) { return { x: r.pace, y: r.hr }; });
    var f26 = fit(pts26.map(function (p) { return [p.x, p.y]; }));
    var f25 = fit(pts25.map(function (p) { return [p.x, p.y]; }));

    var cmpKeys = [
      { k: "平均配速", s: "配速", f: function (a) { var d = a.reduce(function (s, r) { return s + r.duration; }, 0), dd = a.reduce(function (s, r) { return s + r.distance; }, 0); return dd ? d / dd : 0; }, inv: true, unit: "", fmt: fmtPace },
      { k: "平均心率", s: "心率", f: function (a) { return avg(a, "hr"); }, inv: true, unit: "bpm", fmt: function (v) { return Math.round(v); } },
      { k: "平均步频", s: "步频", f: function (a) { return avg(a, "cadence"); }, inv: false, unit: "步/分", fmt: function (v) { return Math.round(v); } },
      { k: "平均步幅", s: "步幅", f: function (a) { return avg(a, "stride"); }, inv: false, unit: "米", fmt: function (v) { return r2(v); } },
      { k: "触地时间", s: "触地时间", f: function (a) { return avg(a, "td"); }, inv: true, unit: "ms", fmt: function (v) { return Math.round(v); } },
      { k: "垂直振幅", s: "垂直振幅", f: function (a) { return avg(a, "va"); }, inv: true, unit: "cm", fmt: function (v) { return r1(v); } },
      { k: "垂直步幅比", s: "垂直步幅比", f: function (a) { return avg(a, "vsr"); }, inv: true, unit: "%", fmt: function (v) { return r1(v); } },
      { k: "最大摄氧量", s: "摄氧量", f: function (a) { return avg(a, "vo2"); }, inv: false, unit: "ml/kg", fmt: function (v) { return r1(v); } }
    ];

    var pct = [], txt = [];
    cmpKeys.forEach(function (c) {
      var v25 = c.f(rs25), v26 = c.f(rs26);
      var p = v25 ? (v26 - v25) / v25 * 100 : 0;
      if (c.inv) p = -p;
      pct.push(r1(p));
      txt.push(c.k + " " + c.fmt(v25) + " → " + c.fmt(v26) + " " + c.unit + "（" + (p > 0 ? "改善 " : "退步 ") + Math.abs(r1(p)) + "%）");
    });

    // 与「赛季内对比」保持同一套读法：横向条形，数值写进标签，省掉竖排中文
    mkChart("cYearCmp", {
      type: "bar",
      data: {
        labels: cmpKeys.map(function (c, i) { return c.s + "  " + (pct[i] >= 0 ? "+" : "") + pct[i].toFixed(1) + "%"; }),
        datasets: [{
          label: "相对 2025 的变化",
          data: pct,
          backgroundColor: pct.map(function (v) { return v >= 0 ? "rgba(13,148,136,.65)" : "rgba(220,38,38,.6)"; }),
          borderRadius: 4
        }]
      },
      options: {
        indexAxis: "y",
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: function (c) { return txt[c.dataIndex]; } } }
        },
        scales: { x: axis("变化 %（正向 = 改善）", {}), y: axis("", { ticks: { font: { size: 10 } } }) }
      }
    });

    var buckets = [[0, 3, "3 km 内"], [3, 5, "3–5 km"], [5, 7, "5–7 km"], [7, 9, "7–9 km"], [9, 99, "9 km 以上"]];
    var bl = [], bp = [], bh = [], bc = [];
    buckets.forEach(function (b) {
      var g = rs26.filter(function (r) { return r.distance >= b[0] && r.distance < b[1]; });
      // 标签传数组 = 两行显示：第一行档位、第二行场次数。单行写「5–7 km（30）」
      // 在窄卡里会把 x 轴挤成一团（多弗朗截图反馈过），拆行后每个标签只要一半宽度。
      bl.push([b[2], "（" + g.length + " 场）"]);
      bp.push(g.length ? r1(g.reduce(function (s, r) { return s + r.pace; }, 0) / g.length) : null);
      bh.push(g.length ? r1(avg(g, "hr")) : null);
      bc.push(g.length ? r1(avg(g, "cadence")) : null);
    });
    mkChart("cBucket", {
      type: "bar",
      data: {
        labels: bl,
        datasets: [
          // 配速不能用柱：反向轴上柱基线会被 clamp 到轴边缘，五根柱全部满高，
          // 档位间差异完全看不见（多弗朗截图反馈「看不清」的第二根源）。三条线各走各的轴。
          { type: "line", label: "平均配速", data: bp, borderColor: C.pace, backgroundColor: C.pace, borderWidth: 2.2, pointRadius: 4, tension: .25, yAxisID: "y" },
          { type: "line", label: "平均心率", data: bh, borderColor: C.hr, backgroundColor: C.hr, borderWidth: 1.8, pointRadius: 3, yAxisID: "y1" },
          { type: "line", label: "平均步频", data: bc, borderColor: C.cad, backgroundColor: C.cad, borderWidth: 1.4, borderDash: [4, 3], pointRadius: 3, yAxisID: "y2" }
        ]
      },
      options: {
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { position: "top", align: "end", labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true } },
          tooltip: { callbacks: { label: function (c) { return c.dataset.label + "：" + (c.dataset.yAxisID === "y" ? fmtPace(c.parsed.y) : r1(c.parsed.y)); } } }
        },
        scales: {
          x: axis("", { ticks: { maxRotation: 0, autoSkip: false, font: { size: 10 } } }),
          y: axis("配速（越低越快）", { reverse: true, ticks: { callback: function (v) { return fmtPace(v); } } }),
          y1: axis("心率", { position: "right", grid: { drawOnChartArea: false } }),
          y2: axis("步频", { position: "right", grid: { drawOnChartArea: false }, ticks: { display: false } })
        }
      }
    });

    var wk = {};
    rs26.forEach(function (r) {
      var dt = new Date(r.date + "T00:00:00");
      var day = (dt.getDay() + 6) % 7;
      dt.setDate(dt.getDate() - day);
      // 用本地年月日拼接。不能用 toISOString()：它会转成 UTC，在东八区会把周一整体退到上周日。
      var key = dt.getFullYear() + "-" + String(dt.getMonth() + 1).padStart(2, "0") + "-" + String(dt.getDate()).padStart(2, "0");
      var o = wk[key] || (wk[key] = { d: 0, t: 0, n: 0 });
      o.d += r.distance; o.t += r.duration; o.n++;
    });
    var wks = Object.keys(wk).sort();
    mkChart("cWeek", {
      type: "bar",
      data: {
        labels: wks.map(function (k) { return mmdd(k); }),
        datasets: [
          { type: "bar", label: "周里程", data: wks.map(function (k) { return r1(wk[k].d); }), backgroundColor: "rgba(37,99,235,.55)", borderRadius: 3, yAxisID: "y" },
          { type: "line", label: "周平均配速", data: wks.map(function (k) { return wk[k].d ? r1(wk[k].t / wk[k].d) : null; }), borderColor: C.pace, backgroundColor: C.pace, borderWidth: 1.8, pointRadius: 3, tension: .25, yAxisID: "y1" }
        ]
      },
      options: {
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { position: "top", align: "end", labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true } },
          tooltip: { callbacks: { label: function (c) { return c.dataset.label + "：" + (c.dataset.yAxisID === "y1" ? fmtPace(c.parsed.y) : r1(c.parsed.y) + " km"); } } }
        },
        scales: {
          x: axis("周起始日", { ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 14 } }),
          y: axis("km", { beginAtZero: true }),
          y1: axis("配速", { position: "right", reverse: true, grid: { drawOnChartArea: false }, ticks: { callback: function (v) { return fmtPace(v); } } })
        }
      }
    });

    // ---- 2026 赛季内对比：把距离锁在 5–7 km，只比较时间前后，避免跑量变量污染
    var inBand = function (r) { return r.distance >= 5 && r.distance < 7; };
    var sEarly = rs26.filter(function (r) { return inBand(r) && r.month <= "2026-07"; });
    var sLate = rs26.filter(function (r) { return inBand(r) && r.month >= "2026-08"; });

    // 只画「方向明确」的指标，且用横向条形图：卡片只有 330px 宽，6 个中文标签竖排
    // 是唯一能读清楚的做法。配速与步幅不进图，改在下方结论里作对照说明 —— 距离锁死
    // 后配速变化等于强度变化而非成绩，步幅收窄本就是「小步快跑」的正确方向，
    // 把它们标成红或绿都会误导。
    // good 字段标明「哪个方向算变好」。
    var sKeys = [
      { k: "心率", f: function (a) { return avg(a, "hr"); }, good: "down", unit: "bpm", fmt: function (v) { return Math.round(v); } },
      { k: "步频", f: function (a) { return avg(a, "cadence"); }, good: "up", unit: "步/分", fmt: function (v) { return Math.round(v); } },
      { k: "触地时间", f: function (a) { return avg(a, "td"); }, good: "down", unit: "ms", fmt: function (v) { return Math.round(v); } },
      { k: "垂直振幅", f: function (a) { return avg(a, "va"); }, good: "down", unit: "cm", fmt: r1 },
      { k: "垂直步幅比", f: function (a) { return avg(a, "vsr"); }, good: "down", unit: "%", fmt: r1 },
      { k: "最大摄氧量", f: function (a) { return avg(a, "vo2"); }, good: "up", unit: "ml/kg", fmt: r1 }
    ];

    var sGain = [], sTip = [], sCol = [], sLabels = [];
    sKeys.forEach(function (c) {
      var ve = c.f(sEarly), vl = c.f(sLate);
      var raw = ve ? (vl - ve) / ve * 100 : 0;          // 原始变化率，正数代表数值上升
      var gain = c.good === "down" ? -raw : raw;        // 统一成「正数 = 变好」
      var g = r1(gain);
      sGain.push(g);
      sCol.push(g > 1 ? "rgba(13,148,136,.65)" : g < -1 ? "rgba(220,38,38,.6)" : "rgba(139,144,153,.5)");
      // 数值直接写进标签，静态看板不用悬停也能读数；统一保留一位小数
      sLabels.push(c.k + "  " + (g >= 0 ? "+" : "") + g.toFixed(1) + "%");
      sTip.push(c.k + "：" + c.fmt(ve) + " → " + c.fmt(vl) + " " + c.unit +
        "（" + (g >= 0 ? "改善 " : "退步 ") + Math.abs(g).toFixed(1) + "%）");
    });

    mkChart("cSeasonCmp", {
      type: "bar",
      data: {
        labels: sLabels,
        datasets: [{
          label: "后期（8 月后）相对早期（7 月前）的变化",
          data: sGain,
          backgroundColor: sCol,
          borderRadius: 4
        }]
      },
      options: {
        indexAxis: "y",
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: function (c) { return sTip[c.dataIndex]; },
              afterLabel: function (c) {
                return c.parsed.x > 1 ? "同期更省力" : c.parsed.x < -1 ? "同期略退步" : "基本持平";
              }
            }
          }
        },
        scales: {
          x: axis("变化 %（正向 = 改善）", {}),
          y: axis("", { ticks: { font: { size: 10 } } })
        }
      }
    });

    // ---- 同比结论
    var notes = [];
    if (f26 && f25) {
      var evalPace = 400; // 6'40"/km
      var hr26 = f26.a + f26.b * evalPace, hr25 = f25.a + f25.b * evalPace;
      var gap = hr25 - hr26;
      notes.push([gap > 2 ? "good" : gap < -2 ? "bad" : "warn",
        "同配速心率对比",
        fmtPace(evalPace) + " 配速下心率 " + Math.round(hr25) + " → " + Math.round(hr26) + " bpm，" +
        (gap > 0 ? "同等配速更省力，有氧在变好" : "同等配速更吃力") + "。"]);
    }
    if (sEarly.length && sLate.length) {
      // 用同距离档位做同期比较：距离被锁死后，剩下的变化才能归因到身体状态
      var gHr = (avg(sEarly, "hr") - avg(sLate, "hr")) / avg(sEarly, "hr") * 100;
      notes.push([gHr > 2 ? "good" : gHr < -2 ? "bad" : "warn", "赛季内同距离对比",
        "同为 5–7 km（配速差 " + r1(Math.abs((paceOf(sLate) - paceOf(sEarly)) / paceOf(sEarly) * 100)) + "%）：心率 " +
        Math.round(avg(sEarly, "hr")) + " → " + Math.round(avg(sLate, "hr")) + " bpm、触地 " +
        Math.round(avg(sEarly, "td")) + " → " + Math.round(avg(sLate, "td")) + " ms，" +
        (gHr > 0 ? "同样距离用更低的心肺负荷就能完成" : "同样距离更吃力了") + "。"]);
    }
    // ---- 自然周对比：以最后一次训练所在的自然周（周一起）为基准，看它自己、前一周、前 4 周均值
    var anchor = RUNS[RUNS.length - 1].date;
    var anchorStart = shiftDay(anchor, -((parseDay(anchor).getDay() + 6) % 7));
    var weeks = [];
    for (var wi = 0; wi < 6; wi++) {
      var ws = shiftDay(anchorStart, -wi * 7);
      weeks.push(runsIn(ws, shiftDay(ws, 6)));
    }
    var wkCur = weeks[0], wkPrev = weeks[1];
    var wkBase = weeks.slice(2).filter(function (a) { return a.length; });
    if (wkCur.length || wkPrev.length) {
      var curKm = sumBy(wkCur, "distance"), prevKm = sumBy(wkPrev, "distance");
      var baseKm = wkBase.length
        ? wkBase.reduce(function (t, a) { return t + sumBy(a, "distance"); }, 0) / wkBase.length : 0;
      var delta = prevKm ? (curKm - prevKm) / prevKm * 100 : 0;
      notes.push([prevKm && delta > 10 ? "warn" : prevKm && delta < -20 ? "warn" : "info", "自然周对比",
        "本周 " + wkCur.length + " 场 / " + r1(curKm) + " km，上周 " + wkPrev.length + " 场 / " + r1(prevKm) + " km" +
        (baseKm ? "（近 4 周周均 " + r1(baseKm) + " km）" : "") + "。" +
        (prevKm && delta > 10 ? "一周涨超 10% 是常见受伤诱因，下周把量压回去。" :
         prevKm && delta < -20 ? "掉量超过两成，检查是不是节奏被打断了。" :
         "周间波动在 ±10% 内就是可持续节奏。")]);
    }
    notes.push(["info", "样本说明",
      "同比基于 2025 年 " + rs25.length + " 场 vs 2026 年 " + rs26.length + " 场。2026 跑量近乎翻倍（单次均 3.5 → 5.4 km），" +
      "所以均值类对比只作参考，结论以锁定了距离的「赛季内对比」为准。"]);
    document.getElementById("cmpNotes").innerHTML = notes.map(function (n) {
      return '<div class="note ' + n[0] + '"><h3>' + esc(n[1]) + '</h3><p>' + esc(n[2]) + "</p></div>";
    }).join("");

    document.getElementById("cmpTbl").innerHTML =
      "<thead><tr><th>指标</th><th>2025</th><th>2026</th><th>变化</th></tr></thead><tbody>" +
      cmpKeys.map(function (c, i) {
        var v25 = c.f(rs25), v26 = c.f(rs26);
        var p = pct[i];
        return "<tr><td>" + c.k + "</td><td class='num'>" + c.fmt(v25) + (c.unit ? " " + c.unit : "") +
          "</td><td class='num'>" + c.fmt(v26) + (c.unit ? " " + c.unit : "") + "</td><td class='num' style='color:" + (p >= 0 ? "#0d9488" : "#dc2626") + "'>" + (p >= 0 ? "+" : "") + p.toFixed(1) + "%</td></tr>";
      }).join("") + "</tbody>";
  }

  // ---------------------------------------------------------------- table
  var COLS = [
    { k: "date", t: "日期" }, { k: "time", t: "开始" }, { k: "distance", t: "距离 km" },
    { k: "duration", t: "时长", f: fmtDur }, { k: "pace", t: "配速", f: fmtPace },
    { k: "cadence", t: "步频" }, { k: "stride", t: "步幅 m", f: function (v) { return r2(v); } },
    { k: "hr", t: "心率" }, { k: "hrMax", t: "最大心率" },
    { k: "td", t: "触地 ms", f: function (v) { return Math.round(v); } },
    { k: "va", t: "垂直 cm", f: function (v) { return r1(v); } },
    { k: "vsr", t: "步幅比 %", f: function (v) { return r1(v); } },
    { k: "calories", t: "卡路里" }, { k: "load", t: "负荷" }, { k: "recover", t: "恢复 h" }
  ];

  function currentRows() {
    var rs = byYear(state.year);
    if (state.search.trim()) {
      var q = state.search.trim();
      rs = rs.filter(function (r) { return r.date.indexOf(q) >= 0; });
    }
    var k = state.sortKey, dir = state.sortDir === "asc" ? 1 : -1;
    return rs.slice().sort(function (a, b) {
      var x = a[k], y = b[k];
      if (typeof x === "string") return x.localeCompare(y) * dir;
      return (x - y) * dir;
    });
  }

  function renderTable() {
    var rs = currentRows();
    document.getElementById("rowHint").textContent = "共 " + rs.length + " 场";
    document.getElementById("runTbl").innerHTML =
      "<thead><tr>" + COLS.map(function (c) {
        var cls = "sortable " + (state.sortKey === c.k ? state.sortDir : "");
        return '<th class="' + cls.trim() + '" data-key="' + c.k + '">' + c.t + "</th>";
      }).join("") + "</tr></thead><tbody>" +
      rs.map(function (r) {
        return "<tr>" + COLS.map(function (c) {
          var v = r[c.k];
          var s = c.f ? c.f(v) : (typeof v === "number" ? (Math.abs(v) >= 100 ? Math.round(v) : r2(v)) : v);
          return "<td class='num'>" + s + "</td>";
        }).join("") + "</tr>";
      }).join("") + "</tbody>";
  }

  // ---------------------------------------------------------------- wiring
  var TABS = ["overview", "daily", "gait", "single", "compare", "table"];

  function tabFromHash() {
    try {
      var h = String((typeof location !== "undefined" && location.hash) || "").replace(/^#/, "");
      return TABS.indexOf(h) >= 0 ? h : "overview";
    } catch (e) { return "overview"; }
  }

  function syncTabs(name) {
    Array.prototype.forEach.call(document.querySelectorAll(".tabs button"), function (b) {
      b.classList.toggle("on", b.dataset.tab === name);
    });
    Array.prototype.forEach.call(document.querySelectorAll(".panel"), function (p) {
      p.classList.toggle("on", p.id === "panel-" + name);
    });
  }

  function showTab(name) {
    state.tab = name;
    syncTabs(name);
    try {
      if (typeof history !== "undefined" && history.replaceState) history.replaceState(null, "", "#" + name);
    } catch (e) { }
    render();
  }

  function render() {
    if (state.tab === "overview") renderOverview();
    else if (state.tab === "daily") renderDaily();
    else if (state.tab === "gait") renderGait();
    else if (state.tab === "single") renderSingle();
    else if (state.tab === "compare") renderCompare();
    else if (state.tab === "table") renderTable();
  }

  document.getElementById("tabs").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-tab]");
    if (b) showTab(b.dataset.tab);
  });

  document.getElementById("yearSeg").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-year]");
    if (!b) return;
    state.year = b.dataset.year;
    Array.prototype.forEach.call(this.querySelectorAll("button"), function (x) { x.classList.toggle("on", x === b); });
    state.sel = null;
    render();
  });

  document.getElementById("cmpMonth").addEventListener("change", function () {
    var oldBase = prevMonth(state.cmpMonth);
    state.cmpMonth = this.value;
    // 基准月若没被手动改过（仍等于原先的上一月），就跟着本期一起走，否则尊重手动选择
    if (state.cmpBase === oldBase) state.cmpBase = prevMonth(this.value);
    renderMonthCmp();
  });
  document.getElementById("cmpBase").addEventListener("change", function () {
    state.cmpBase = this.value;
    renderMonthCmp();
  });

  // 对比页的子页签。关键点：藏在 display:none 里的 canvas 尺寸是 0，
  // 切回来时必须对里面的图逐个 resize，否则会得到一张 0×0 的空图（Chart.js 的经典坑）。
  // 自检桩里的 Chart 没有 getChart，typeof 守卫让桩环境安静跳过。
  function showCmpSub(key) {
    ["month", "season", "detail"].forEach(function (k) {
      var p = document.getElementById("sub-" + k);
      if (p) p.classList.toggle("on", k === key);
    });
    var tabs = document.getElementById("cmpSubTabs");
    if (tabs) {
      Array.prototype.forEach.call(tabs.querySelectorAll("button"), function (x) {
        x.classList.toggle("on", x.dataset.sub === key);
      });
    }
    Array.prototype.forEach.call(document.querySelectorAll("#sub-" + key + " canvas"), function (cv) {
      if (typeof Chart.getChart === "function") {
        var c = Chart.getChart(cv);
        if (c) c.resize();
      }
    });
  }
  document.getElementById("cmpSubTabs").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-sub]");
    if (b) showCmpSub(b.dataset.sub);
  });

  document.getElementById("calGrid").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-day]");
    if (!b) return;
    renderDay(b.dataset.day);
    Array.prototype.forEach.call(this.querySelectorAll("button"), function (x) {
      x.classList.toggle("sel", x.dataset.day === state.day);
    });
  });

  document.getElementById("ovAdvice").addEventListener("click", function () { showTab("daily"); });

  document.getElementById("runPick").addEventListener("change", function () {
    state.sel = +this.value;
    renderSingle();
  });
  document.getElementById("btnPrev").addEventListener("click", function () {
    var rs = byYear(state.year).filter(runSeries).sort(function (a, b) { return b.id - a.id; });
    var i = rs.findIndex(function (r) { return r.id === state.sel; });
    if (i < rs.length - 1) { state.sel = rs[i + 1].id; renderSingle(); }
  });
  document.getElementById("btnNext").addEventListener("click", function () {
    var rs = byYear(state.year).filter(runSeries).sort(function (a, b) { return b.id - a.id; });
    var i = rs.findIndex(function (r) { return r.id === state.sel; });
    if (i > 0) { state.sel = rs[i - 1].id; renderSingle(); }
  });

  document.getElementById("runTbl").addEventListener("click", function (e) {
    var th = e.target.closest("th[data-key]");
    if (!th) return;
    var k = th.dataset.key;
    if (state.sortKey === k) state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
    else { state.sortKey = k; state.sortDir = k === "date" || k === "time" ? "desc" : "desc"; }
    renderTable();
  });

  document.getElementById("search").addEventListener("input", function () {
    state.search = this.value;
    renderTable();
  });

  document.getElementById("genAt").textContent = "数据生成于 " + D.generated;

  window.addEventListener("resize", function () {
    Object.keys(charts).forEach(function (k) { if (charts[k]) charts[k].resize(); });
    if (state.tab === "daily") sizeCalendar();
  });

  state.tab = tabFromHash();
  syncTabs(state.tab);
  setupChartDefaults();
  render();
})();
