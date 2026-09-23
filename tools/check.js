/**
 * run-analytics 自检脚本 —— 改完 app.js / index.html / styles.css 之后跑一遍
 *
 *     node tools/check.js            # 逻辑自检（无依赖，纯 Node）
 *     node tools/check.js --browser  # 额外跑真实 Chrome 截图（需要 playwright-core）
 *
 * 为什么要有两轨：
 *   1) 逻辑自检用 vm + DOM 桩把 app.js 真跑一遍，覆盖「所有页签 × 所有年份 ×
 *      所有月份两两组合 × 抽样的每一天」。它能抓到抛异常、拿不到元素、
 *      渲染出空内容这类问题，而且秒级完成、不需要浏览器。
 *   2) 但它**看不见 CSS**。实测踩过一次：`.cal button { background: … }` 的优先级
 *      (0,1,1) 压过了 `.heat1` 的 (0,1,0)，于是日历所有格子都是同一个颜色 ——
 *      桩里一切正常，真实浏览器里整片日历是空白的。所以颜色/尺寸这类问题
 *      必须靠 --browser 抽样 computed style。
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..");
const wantBrowser = process.argv.includes("--browser");

// ------------------------------------------------------------------ DOM 桩
function makeEl(id) {
  const el = {
    id, _html: "", _text: "", className: "", value: "", dataset: {}, options: [],
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); },
      remove(c) { this._s.delete(c); },
      toggle(c, f) { if (f === undefined) f = !this._s.has(c); f ? this._s.add(c) : this._s.delete(c); return f; },
      contains(c) { return this._s.has(c); }
    },
    style: { cssText: "", setProperty(k, v) { this[k] = v; } },
    handlers: {},
    addEventListener(t, fn) { (this.handlers[t] = this.handlers[t] || []).push(fn); },
    appendChild(c) { return c; },
    removeChild(c) { return c; },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    closest() { return null; },
    getContext() { return { __el: id }; },
    getBoundingClientRect() { return { width: 600, height: 300, top: 0, left: 0 }; },
    clientWidth: 1340,
    parentNode: null
  };
  Object.defineProperty(el, "innerHTML", { get() { return this._html; }, set(v) { this._html = String(v); }, configurable: true });
  Object.defineProperty(el, "textContent", { get() { return this._text; }, set(v) { this._text = String(v); }, configurable: true });
  return el;
}

const els = {};
const chartCfgs = [];
const errors = [];

function stubContext() {
  const TABS = ["overview", "daily", "gait", "single", "compare", "table"];
  const tabBtns = TABS.map(t => { const b = makeEl("tab-" + t); b.dataset = { tab: t }; return b; });
  const panels = TABS.map(t => makeEl("panel-" + t));

  const doc = {
    body: makeEl("body"),
    getElementById(id) { return els[id] || (els[id] = makeEl(id)); },
    createElement(tag) { return makeEl(tag); },
    querySelector(sel) {
      if (sel === ".calflex") return els.calflex || (els.calflex = makeEl("calflex"));
      if (sel === ".calwrap") return els.calwrap || (els.calwrap = makeEl("calwrap"));
      return null;
    },
    querySelectorAll(sel) {
      if (sel === ".tabs button") return tabBtns;
      if (sel === ".panel") return panels;
      return [];
    },
    addEventListener() { }
  };

  function Chart(el, cfg) { this.el = el; this.cfg = cfg; chartCfgs.push(cfg); }
  Chart.prototype.destroy = function () { };
  Chart.prototype.resize = function () { };
  Chart.defaults = { font: {}, color: "" };

  const ctx = {
    window: null, document: doc, Chart, console, setTimeout, clearTimeout,
    location: { hash: "" }, history: { replaceState() { } },
    getComputedStyle() { return { fontFamily: "Segoe UI", getPropertyValue() { return "3px"; } }; }
  };
  ctx.window = ctx;
  ctx.window.addEventListener = function () { };
  vm.createContext(ctx);
  return ctx;
}

function main() {
  const ctx = stubContext();
  const run = (name, fn) => {
    try { fn(); } catch (e) {
      errors.push(name + " :: " + String(e && e.stack ? e.stack.split("\n").slice(0, 2).join(" | ") : e));
    }
  };

  run("load data.js", () => vm.runInContext(fs.readFileSync(path.join(ROOT, "data.js"), "utf8"), ctx, { filename: "data.js" }));
  run("load app.js", () => vm.runInContext(fs.readFileSync(path.join(ROOT, "app.js"), "utf8"), ctx, { filename: "app.js" }));

  const D = ctx.window.RUNNER_DATA;
  if (!D) { console.error("data.js 没有产出 window.RUNNER_DATA"); process.exit(1); }

  const fire = (id, type, ev) => els[id].handlers[type][0].call(els[id], ev);
  const byData = (obj) => ({ target: { closest: () => obj } });

  const setYear = (y) => {
    const b = makeEl("y"); b.dataset = { year: y };
    run("year " + y, () => fire("yearSeg", "click", byData(b)));
  };
  const showTab = (t) => {
    const b = makeEl("t"); b.dataset = { tab: t };
    run("tab " + t, () => fire("tabs", "click", byData(b)));
  };

  ["2026", "2025", "all"].forEach(y => {
    setYear(y);
    ["overview", "daily", "gait", "single", "compare", "table"].forEach(showTab);
  });

  // --- 日历：每一个有记录的日子都点一遍，再抽查休息日与超范围日期
  setYear("2026");
  showTab("daily");
  const gridHtml = els.calGrid.innerHTML;
  const uniqDays = Array.from(new Set(D.runs.map(r => r.date))).sort();

  uniqDays.forEach(d => {
    const b = makeEl("d"); b.dataset = { day: d };
    run("day " + d, () => fire("calGrid", "click", byData(b)));
    if (!els.dayKpis.innerHTML) errors.push("有记录的日子没有渲染 KPI: " + d);
    if (els.dayNotes.innerHTML.indexOf("强度结构") < 0) errors.push("有记录的日子缺强度结构: " + d);
  });
  ["2026-09-16", "2026-09-21"].forEach(d => {          // 已知的休息日
    const b = makeEl("d"); b.dataset = { day: d };
    run("rest " + d, () => fire("calGrid", "click", byData(b)));
    if (els.dayNotes.innerHTML.indexOf("休息日") < 0) errors.push("休息日说明缺失: " + d);
    if (els.dayKpis.innerHTML) errors.push("休息日不该有 KPI: " + d);
  });
  run("pre-range", () => {
    const b = makeEl("d"); b.dataset = { day: "2000-01-01" };
    fire("calGrid", "click", byData(b));
    if (els.dayNotes.innerHTML.indexOf("数据范围") < 0) errors.push("早于数据范围的提示缺失");
  });

  // --- 月度对比：所有月份两两组合
  const months = Array.from(new Set(D.runs.map(r => r.month))).sort();
  let pairs = 0;
  months.forEach(a => months.forEach(b => {
    els.cmpMonth.value = a; els.cmpBase.value = b;
    run("cmp " + a + "/" + b, () => fire("cmpMonth", "change", {}));
    els.cmpBase.value = b;
    run("cmpBase " + b, () => fire("cmpBase", "change", {}));
    pairs++;
  }));
  els.cmpMonth.value = months[months.length - 1];
  run("cmp same", () => fire("cmpMonth", "change", {}));
  const sameGuard = els.mCmpNotes.innerHTML.indexOf("基准月与本期相同") >= 0;
  if (!sameGuard) errors.push("选了同一个月的提示缺失");

  // --- 对比页子页签：两个子页切换（月度 / 跨年与赛季）
  run("cmp subtab season", () => {
    const b = makeEl("sb"); b.dataset = { sub: "season" };
    fire("cmpSubTabs", "click", byData(b));
    if (!els["sub-season"].classList.contains("on")) errors.push("子页 season 没被点亮");
    if (els["sub-month"].classList.contains("on")) errors.push("子页 month 没被熄灭");
  });
  run("cmp subtab month", () => {
    const b = makeEl("sb2"); b.dataset = { sub: "month" };
    fire("cmpSubTabs", "click", byData(b));
    if (!els["sub-month"].classList.contains("on")) errors.push("子页 month 没被切回");
  });

  // --- 单次复盘：每一场都过一遍
  showTab("single");
  D.runs.forEach(r => {
    els.runPick.value = String(r.id);
    run("single " + r.id, () => fire("runPick", "change", {}));
  });

  // --- 总览建议条
  showTab("overview");
  if (els.ovAdvice.innerHTML.indexOf("日视图") < 0) errors.push("总览的今日建议条没渲染");
  run("advice click", () => fire("ovAdvice", "click", {}));

  // --- 日视图：今日建议大卡 + 依据列表
  showTab("daily");
  if (els.advCard.innerHTML.indexOf("class=\"advcard") < 0) errors.push("日视图的今日建议大卡没渲染");
  const advFacts = (els.advFacts.innerHTML.match(/class="fact"/g) || []).length;
  if (advFacts < 3) errors.push("日视图的依据列表过于单薄（" + advFacts + " 行）");

  // --- 总览：负荷状态三块 + CTL/ATL/TSB 图
  showTab("overview");
  const loadStats = (els.loadStat.innerHTML.match(/class="stat/g) || []).length;
  if (loadStats < 3) errors.push("训练负荷状态块缺失（" + loadStats + " 块）");

  // --- 跑姿：体检表
  showTab("gait");
  const gaitRows = (els.gaitCheck.innerHTML.match(/<tr>/g) || []).length;
  if (gaitRows < 4) errors.push("跑姿体检表行数不足（" + gaitRows + " 行）");

  const report = {
    errors,
    runs: D.runs.length,
    days: uniqDays.length,
    calendarCells: (gridHtml.match(/<button/g) || []).length,
    monthPairs: pairs,
    sameMonthGuard: sameGuard,
    adviceFacts: advFacts,
    loadStats: loadStats,
    gaitRows: gaitRows,
    charts: chartCfgs.length,
    emptyCharts: chartCfgs.filter(c => (c.data.datasets || []).every(d => !d.data.length)).length
  };
  return report;
}

const report = main();

// ------------------------------------------------------------------ 可选：真实浏览器
async function browserPass(report) {
  let pw;
  try { pw = require("playwright-core"); } catch (e) {
    report.browser = "skipped (找不到 playwright-core，设 NODE_PATH 指向含它的 node_modules)";
    return;
  }
  // 注意：require("playwright-core") 返回整个模块，浏览器类型在 .chromium 上。
  // 必须保留 this（chromium.launch(...)），把 launch 解构出来单独调用会丢 this，
  // 报 "Cannot read properties of undefined (reading '_playwright')"（踩过）。
  const chromium = pw.chromium;
  if (!chromium || typeof chromium.launch !== "function") {
    report.browser = "skipped (playwright-core 里没有可用的 chromium.launch)";
    return;
  }
  const candidates = [
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
  ];
  const exe = candidates.find(p => fs.existsSync(p));
  if (!exe) { report.browser = "skipped (没找到 Chrome / Edge)"; return; }

  const browser = await chromium.launch({ executablePath: exe });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1024 } });
  const consoleErrs = [];
  page.on("pageerror", e => consoleErrs.push("PAGEERROR " + e.message));
  page.on("console", m => { if (m.type() === "error") consoleErrs.push("CONSOLE " + m.text()); });

  await page.goto("file:///" + path.join(ROOT, "index.html").replace(/\\/g, "/"));
  await page.waitForTimeout(800);

  const out = { consoleErrors: consoleErrs };

  // 必须先切到日视图 —— 日历只在渲染日视图时才生成，
  // 在总览页抽样会拿到空对象，守卫条件恒为假，检查静默失效（踩过）。
  await page.click('button[data-tab="daily"]');
  await page.waitForTimeout(500);

  // 关键：抽查 computed style，桩看不见但会真出问题的地方
  out.heatColors = await page.evaluate(() => {
    const seen = {};
    document.querySelectorAll("#calGrid button").forEach(b => {
      const cls = Array.from(b.classList).find(c => /^heat[0-5]$/.test(c));
      if (cls && seen[cls] === undefined) seen[cls] = getComputedStyle(b).backgroundColor;
    });
    return seen;
  });
  const heats = out.heatColors;
  const found = Object.keys(heats);
  if (found.length < 3) {
    errors.push("热力色阶抽样只拿到 " + found.length + " 档（" + found.join(",") + "），检查本身失效了");
  } else {
    const distinct = new Set(found.map(k => heats[k]));
    if (distinct.size !== found.length) {
      errors.push("日历热力色阶有重复颜色（多半是 CSS 优先级被 .cal button 压过）：" + JSON.stringify(heats));
    }
  }

  // 日历是否铺满、是否溢出、格子是否方的
  out.calendar = await page.evaluate(() => {
    const grid = document.getElementById("calGrid");
    const wrap = grid.closest(".calwrap");
    const c = grid.querySelector("button").getBoundingClientRect();
    return {
      cells: grid.querySelectorAll("button").length,
      cell: Math.round(c.width),
      square: Math.abs(c.width - c.height) < 1,
      gridW: Math.round(grid.getBoundingClientRect().width),
      wrapW: Math.round(wrap.clientWidth),
      overflowing: wrap.scrollWidth > wrap.clientWidth
    };
  });
  if (!out.calendar.square) errors.push("日历格子不是正方形");

  // 各页签轮一遍
  for (const t of ["overview", "daily", "gait", "single", "compare", "table"]) {
    await page.click(`button[data-tab="${t}"]`);
    await page.waitForTimeout(300);
  }
  await page.click('button[data-tab="overview"]');
  await page.waitForTimeout(300);
  out.adviceBar = await page.$eval("#ovAdvice", el => el.className + " :: " + el.innerText.replace(/\s+/g, " ").trim().slice(0, 120));

  // 结构化建议卡与体检表：桩验证不了「真的有字」，这里抽查实际文本
  out.loadStat = await page.$eval("#loadStat", el => el.innerText.replace(/\s+/g, " ").trim());
  await page.click('button[data-tab="daily"]');
  await page.waitForTimeout(300);
  out.advCard = await page.$eval("#advCard", el => el.innerText.replace(/\s+/g, " ").trim().slice(0, 80));
  out.advFacts = await page.$eval("#advFacts", el => el.innerText.replace(/\s+/g, " ").trim().slice(0, 80));
  await page.click('button[data-tab="gait"]');
  await page.waitForTimeout(300);
  out.gaitCheck = await page.$eval("#gaitCheck", el => el.innerText.replace(/\s+/g, " ").trim().slice(0, 120));
  const gib = await page.$eval("#gaitCheck", el => el.getBoundingClientRect().width);
  if (!out.gaitCheck) errors.push("浏览器里跑姿体检表是空的");
  if (gib < 200) errors.push("浏览器里跑姿体检表宽度异常：" + Math.round(gib));

  await browser.close();
  report.browser = out;
}

(async () => {
  if (wantBrowser) await browserPass(report);
  console.log(JSON.stringify(report, null, 2));
  if (errors.length) { console.error("\n失败 " + errors.length + " 项"); process.exit(1); }
  console.log("\nALL_PASS");
})();
