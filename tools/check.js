/**
 * run-analytics 自检脚本 —— 改完 app.js / index.html / styles.css / metrics.py 之后跑一遍
 *
 *     node tools/check.js            # 逻辑自检（无依赖，纯 Node）
 *     node tools/check.js --browser  # 输出经总控台手动核验的项目，不启动浏览器
 *
 * 为什么要有两轨：
 *   1) 逻辑自检用 vm + 轻量 DOM 桩运行 app.js 和离线工具轨，覆盖页面证据跳转及工具入口。它可以抓运行错误和数据联动问题，但不代表真实浏览器布局已验收。
 *   2) 真实页面的尺寸、响应式布局和工具轨悬浮行为需从 APEX Console 手动查看。
 *
 * 旧版的检查项（6 个页签、日历格子、月份两两组合、子页签切换）已随页面改版移除。
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..");
const wantBrowser = process.argv.includes("--browser");

const errors = [];
function ok(name, cond, detail) {
  if (!cond) errors.push(name + (detail ? " :: " + detail : ""));
}

// ------------------------------------------------------------------ DOM 桩
function makeEl(id) {
  const el = {
    id, _html: "", _text: "", className: "", value: "", dataset: {}, options: [],
    attrs: {}, _queries: {},
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
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k]; },
    appendChild(c) { return c; },
    removeChild(c) { return c; },
    querySelectorAll(selector) {
      if (this._queries[selector]) return this._queries[selector];
      const matches = [];
      const rx = /<button\b([^>]*?)>([\s\S]*?)<\/button\s*>/gi;
      let m;
      while ((m = rx.exec(this.innerHTML))) {
        const tag = "button", attrs = {};
        const ar = /([^\s=]+)="([^"]*)"/g;
        let a;
        while ((a = ar.exec(m[1]))) attrs[a[1]] = a[2];
        const isMatch = selector === "button" ? tag === "button"
          : selector === "[data-history-run]" ? tag === "button" && attrs["data-history-run"] != null
                    : selector === "[data-evidence-run]" ? tag === "button" && attrs["data-evidence-run"] != null
                    : selector === "[data-week-key]" ? tag === "button" && attrs["data-week-key"] != null
                      : selector === "[data-week-run]" ? tag === "button" && attrs["data-week-run"] != null
                      : selector === "[data-fitness-run]" ? tag === "button" && attrs["data-fitness-run"] != null
                  : selector === "[data-view-mode]" ? tag === "button" && attrs["data-view-mode"] != null : false;
        if (!isMatch) continue;
        const node = makeEl(tag);
        node.attrs = attrs;
        node.className = attrs.class || "";
        Object.keys(attrs).forEach(k => {
          if (k.indexOf("data-") === 0) node.dataset[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = attrs[k];
        });
        node.textContent = (m[2] || "").replace(/<[^>]*>/g, "");
        node.click = function () {
          const event = { key: "", preventDefault() { } };
          if (typeof this.onclick === "function") this.onclick.call(this, event);
          (this.handlers.click || []).forEach(fn => fn.call(this, event));
        };
        node.parentNode = this;
        matches.push(node);
      }
      this._queries[selector] = matches;
      return matches;
    },
    querySelector() { return null; },
    closest() { return null; },
    getContext() { return { __el: id }; },
    getBoundingClientRect() { return { width: 600, height: 300, top: 0, left: 0 }; },
    clientWidth: 1340,
    hidden: false,
    scrollIntoView() { },
    parentNode: null
  };
  Object.defineProperty(el, "innerHTML", { get() { return this._html; }, set(v) { this._html = String(v); this._queries = {}; }, configurable: true });
  Object.defineProperty(el, "textContent", { get() { return this._text; }, set(v) { this._text = String(v); }, configurable: true });
  return el;
}

const els = {};
// 取值一律走这个：app.js 一旦中途抛错，某些元素根本没被创建，
// 直接读 els.x.innerHTML 会让自检自己崩掉，把真正的错误盖住（踩过）。
const html = id => (els[id] ? els[id].innerHTML : "");
const text = id => (els[id] ? els[id].textContent : "");
// 标题和统计说明使用 textContent，也纳入检查。
const any = id => html(id) + text(id);

function stubContext() {
  const doc = {
    body: makeEl("body"),
    getElementById(id) { return els[id] || (els[id] = makeEl(id)); },
    createElement(tag) { return makeEl(tag); },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    addEventListener() { }
  };
  const ctx = {
    window: null, document: doc, console, setTimeout, clearTimeout,
    /* 进度卡的秒表用 setInterval。桩里让它空转：真定时器会让 node 的事件循环
       一直挂着不退，而这里只关心状态（文本由 updateProgress 直接写），
       不关心那个秒表跳不跳。 */
    setInterval() { return 0; }, clearInterval() { },
    location: { hash: "" }, history: { replaceState() { } },
    getComputedStyle() { return { fontFamily: "Segoe UI", getPropertyValue() { return "3px"; } }; }
  };
  ctx.window = ctx;
  ctx.window.addEventListener = function () { };
  vm.createContext(ctx);
  return ctx;
}

// ------------------------------------------------------------------ 页面与数据自检
function main() {
  const ctx = stubContext();
  const markup = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");
  const css = fs.readFileSync(path.join(ROOT, "styles.css"), "utf8");
  ctx.document.getElementById("modeSwitch").innerHTML =
    '<button data-view-mode="after">跑后</button>' +
    '<button data-view-mode="fitness">体能变化</button>' +
    '<button data-view-mode="week">周复盘</button>' +
    '<button data-view-mode="history">历史</button>';
  function run(name, fn) {
    try { fn(); } catch (e) { errors.push(name + " :: " + String(e)); }
  }
  run("load data.js", () => vm.runInContext(fs.readFileSync(path.join(ROOT, "data.js"), "utf8"), ctx));
  run("load app.js", () => vm.runInContext(fs.readFileSync(path.join(ROOT, "app.js"), "utf8"), ctx));
  const D = ctx.window.RUNNER_DATA || {};
  const M = D.metrics || {};
  const B = M.board || {};
  const runs = D.runs || [];
  const weeks = (((B.timeline || {}).week || {}).rows || []);
  const fitness = B.fitness || {};
  const click = (parent, selector, predicate) => {
    const target = parent.querySelectorAll(selector).find(predicate || (() => true));
    if (!target) { errors.push("找不到交互目标 " + selector); return false; }
    target.click();
    return true;
  };

  ok("跑步记录或周汇总缺失", runs.length > 0 && weeks.length >= 5);
  ok("首页缺最新一场与近况", markup.includes('id="afterView"') &&
    markup.includes('id="latestVerdict"') && markup.includes('id="recentEvidence"'));
  ok("周复盘或历史入口缺失", markup.includes('id="weekView"') &&
    markup.includes('id="historyView"') && markup.includes('id="historyMonth"'));
  ok("首页近期跑量与预计算不一致", text("recentKm") ===
    B.insights.recent28.km.toFixed(1) + " km");
  ok("体能变化入口或图表缺失", markup.includes('id="fitnessView"') &&
    html("fitnessChart").includes("fitness-line-base") && html("fitnessChart").includes("fitness-line-recent"));
  ok("训练基础与近期负荷数据不完整", fitness.series && fitness.series.length > 20 &&
    fitness.runs && fitness.runs.length === runs.length);
  ok("每场表现列表未覆盖全部记录",
    (html("fitnessRunList").match(/data-fitness-run=/g) || []).length === runs.length);
  ok("最新一场体能摘要与预计算不一致",
    html("fitnessSummary").includes(fitness.base.toFixed(1)) &&
    html("fitnessSummary").includes(fitness.recent.toFixed(1)));
  ok("逐场比较误用了未来跑步", fitness.runs.every(item =>
    (item.comparableIds || []).every(id => {
      const source = runs.find(run => String(run.id) === String(id));
      return source && source.date < item.date;
    })));
  ok("近期变化没有原始场次入口", html("recentEvidence").includes("data-evidence-run="));
  ok("最新一场对照场次不可追溯",
    B.insights.last.comparable.ids.length === B.insights.last.comparable.n);
  ok("近况场次不可追溯", B.insights.recent28.ids.length === B.insights.recent28.n);
  ok("历史没有列出全部场次",
    (html("historyList").match(/data-history-run=/g) || []).length === runs.length);
  ok("单场分段没有显示", html("historyDetail").includes("segment-table"));

  const evidence = els.recentEvidence.querySelectorAll("[data-evidence-run]")[0];
  if (evidence) {
    evidence.click();
    const runId = evidence.getAttribute("data-evidence-run");
    const source = runs.find(r => String(r.id) === runId);
    ok("结论无法打开原始场次", source && !els.historyView.hidden &&
      html("historyDetail").includes(source.date));
  }

  click(els.modeSwitch, "[data-view-mode]", b => b.getAttribute("data-view-mode") === "fitness");
  ok("体能变化页面没有显示", !els.fitnessView.hidden && els.afterView.hidden);
  const improved = fitness.runs.find(item => item.signal === "improved");
  if (improved) {
    click(els.fitnessRunList, "[data-fitness-run]",
      b => b.getAttribute("data-fitness-run") === String(improved.id));
    ok("提升线索没有比较依据", html("fitnessRunDetail").includes("秒/公里") &&
      html("fitnessEvidence").includes("data-evidence-run="));
  }

  click(els.modeSwitch, "[data-view-mode]", b => b.getAttribute("data-view-mode") === "week");
  ok("周复盘没有显示", !els.weekView.hidden && els.afterView.hidden);
  ok("最近五周列表缺失",
    (html("weekList").match(/data-week-key=/g) || []).length === 5);
  const current = weeks[weeks.length - 1];
  const previous = weeks[weeks.length - 2];
  ok("周复盘数值与预计算不一致",
    html("weekDetail").includes(current.km.toFixed(1) + " km"));
  ok("周复盘场次不可追溯",
    (html("weekDetail").match(/data-week-run=/g) || []).length ===
    current.ids.length + previous.ids.length);
  if (current.ids.length) {
    click(els.weekDetail, "[data-week-run]",
      b => b.getAttribute("data-week-run") === String(current.ids[0]));
    ok("周复盘无法打开单场", !els.historyView.hidden &&
      html("historyDetail").includes("segment-table"));
  }

  click(els.modeSwitch, "[data-view-mode]", b => b.getAttribute("data-view-mode") === "history");
  const earliest = runs[0];
  click(els.historyList, "[data-history-run]",
    b => b.getAttribute("data-history-run") === String(earliest.id));
  ok("历史单场没有显示", html("historyDetail").includes(earliest.date));
  els.historyMonth.value = earliest.date.slice(0, 7);
  els.historyMonth.handlers.change[0].call(els.historyMonth);
  const monthCount = runs.filter(r => r.date.slice(0, 7) === els.historyMonth.value).length;
  ok("月份筛选没有生效",
    (html("historyList").match(/data-history-run=/g) || []).length === monthCount);
  els.latestOpenHistory.handlers.click[0]();
  ok("最新一场入口没有打开最新记录",
    html("historyDetail").includes(runs[runs.length - 1].date));
  click(els.modeSwitch, "[data-view-mode]", b => b.getAttribute("data-view-mode") === "after");
  ok("跑后首页没有显示", !els.afterView.hidden && els.weekView.hidden && els.historyView.hidden);

  const originalInsights = B.insights;
  B.insights = { last: originalInsights.last,
    recent28: { n: 1, km: 5, longestKm: 5 }, previous28: { n: 0, km: 0 },
    delta28: {}, paired: [], periodEnough: false };
  run("少样本页面重绘", () => vm.runInContext(
    fs.readFileSync(path.join(ROOT, "app.js"), "utf8"), ctx));
  ok("少样本仍在判断变化", text("recentBasis").includes("各至少 3 场") &&
    text("recentChange") === "先看这段时间实际跑过的记录。");
  B.insights = originalInsights;

  ["beforeView", "extraTrends", "trendSection", "timelineChart", "planKmRange",
    "scopeChip", "foot"].forEach(id => {
    ok("已删区域仍在页面：" + id, !markup.includes('id="' + id + '"'));
  });
  ["charts.js", "plan-ui.js"].forEach(file => {
    ok("页面仍加载旧脚本：" + file, !markup.includes('src="' + file + '"'));
  });
  ok("重复说明仍在页面", !markup.includes("mode-note"));
  ok("跑前或趋势样式残留", !css.includes(".plan-") && !css.includes(".extra-trends") &&
    !css.includes(".timeline-chart"));
  ["toolrail", "trRefresh", "trLogin", "trCleanup", "trProgress",
    "trModal", "trDegrade", "trProgressStop"].forEach(id => {
    ok("工具入口缺失：" + id, markup.includes('id="' + id + '"'));
  });
  run("load toolrail.js", () => vm.runInContext(
    fs.readFileSync(path.join(ROOT, "toolrail.js"), "utf8"), ctx));
  ok("静态直开时提示不该自动弹出", els.trDegrade.dataset.open === "false");
  ok("静态直开时工具按钮应能解释原因", els.trRefresh.disabled === false &&
    els.trRefresh.getAttribute("aria-disabled") === "true");
  if (els.trHot.handlers.click && els.trRefresh.handlers.click) {
    els.trHot.handlers.click[0]();
    ok("边缘入口不能打开工具", els.toolrail.dataset.open === "true" &&
      els.trHot.getAttribute("aria-expanded") === "true");
    els.trRefresh.handlers.click[0]();
    ok("离线操作没有给出说明", els.trDegrade.dataset.open === "true");
  }
  ok("清理没有先预演",
    fs.readFileSync(path.join(ROOT, "toolrail.js"), "utf8").includes("/cleanup/preview"));

  /* 停止出口：2026-09-28 之前**根本不存在**。任务一卡住（典型：登录态过期，
     脚本开着浏览器干等），三个按钮全被 busy 锁死、进度卡无限转圈，唯一的自救是
     关掉整座桥。下面四条钉住它：接口接线、后端判据的消费、判据不许前端另算、
     以及「跑的时候能停、收起时跟着收」这个行为。 */
  const railSrc = fs.readFileSync(path.join(ROOT, "toolrail.js"), "utf8");
  ok("停止按钮没有接中止接口", railSrc.includes("/abort?kind="));
  ok("停止按钮走的是 POST", /abort\?kind='[\s\S]{0,80}method:\s*'POST'/.test(railSrc));
  ok("没有消费后端的停止原因", railSrc.includes("stopReason"));
  ok("没有画后端的卡死判据", railSrc.includes("stalled"));
  /* 卡死阈值只许有一份（tools/tasks.py 的 STALL_HINT / STALL_KILL）。
     前端自己写个秒数去比 = 两端必然会漂，最后是「界面说没事、其实早停了」。
     注意断言只看**拿数字自己比**这种写法，注释里提常量名是允许的
     （第一版就是被自己注释里的 STALL_HINT 撞红的）。 */
  ok("卡死阈值被前端硬编码了（该直接用后端给的 stalled）",
    !/idleSeconds\s*[><]=?\s*\d/.test(railSrc) && !/\b(600|180)\s*000\b/.test(railSrc));
  /* 进度卡本体是 pointer-events:none（当年是纯状态卡，故意不挡操作）。
     给它加按钮时**必须把按钮自己的指针事件放回来** —— 少这一行，
     按钮外观完全正常、就是点不到（2026-09-28 真实浏览器轨抓到的真 bug）。 */
  ok("停止按钮没有开指针事件（整张卡是 none，会连按钮一起被略过）",
    /\.tr-progress-stop\s*\{[^}]*pointer-events:\s*auto/.test(css));
  /* 失败必须能说出**原因**。2026-09-28 多弗朗看到的那句「没完成（1）」背后是一条
     Python traceback，而它在轮询里被逐批取走、没人留底，于是界面只能说「没完成」。
     这两条钉住：留底 + 带出来。 */
  ok("失败时没把任务最后那句话留底", railSrc.includes("rememberLines(kind, d.lines)"));
  ok("失败时没把原因带出来（只剩「没完成（x）」）", railSrc.includes("reasonFrom(kind)"));
  ok("提示条不允许换行（长原因会横着铺到屏幕外）",
    /\.tr-tip\s*\{[^}]*white-space:\s*normal/.test(css));

  /* 批处理文件必须是 CRLF。2026-09-28 就是这条把「登录」弄坏的 ——
     cmd 找标签（goto）时按「CRLF = 2 字节」算偏移，LF 文件每行只占 1 字节，
     跳转落点于是按行数漂移：落行首侥幸没事，落行中间就把注释内容当命令执行
     （退出码 9009；或误调 python 报 `can't open file 'block-buffers'`）。
     实测同一份内容 LF 坏 / CRLF 好，所以谁把行尾改回 LF，这里必须红。 */
  fs.readdirSync(ROOT).filter(f => f.endsWith(".cmd")).forEach(f => {
    const text = fs.readFileSync(path.join(ROOT, f)).toString("latin1");
    const crlf = (text.match(/\r\n/g) || []).length;
    const bareLf = (text.match(/(^|[^\r])\n/g) || []).length;
    ok("批处理不是 CRLF（cmd 的 goto 会跳错位置）：" + f, crlf > 0 && bareLf === 0);
    ok("批处理里混进了非 ASCII（该类文件约定 ASCII-only）：" + f,
      !/[^\x00-\x7f]/.test(text));
  });
  ok("没有 .gitattributes 钉住 cmd 的行尾（改一次就回归）",
    fs.existsSync(path.join(ROOT, ".gitattributes")) &&
    /\*\.cmd\s+text\s+eol=crlf/.test(
      fs.readFileSync(path.join(ROOT, ".gitattributes"), "utf8")));

  /* 工具轨的失败态必须能**自己走出去**。2026-09-27 多弗朗报的「侧边栏三个按钮
     坏了、用不了」根因就是这个：err 态永久粘住 —— 一次失败之后按钮永远停在那句
     文案上，点几次、等多久都一样，唯一的自救是刷新页面（而没人会想到）。
     这条断言把保鲜期钉住：谁把 TTL 改回无限（= 永久粘住），这里就会红。 */
  const rail = ctx.window.__TOOLRAIL__;
  rail.setBtn('login', 'err', '没完成（1）');
  ok("失败提示在保鲜期内不被轮询冲掉", rail.isAlert('login') === true);
  ok("失败提示有保鲜期，不会永久粘住", rail.alertTTL > 0 && rail.alertTTL <= 300000);
  rail.setBtn('login', 'idle', '登录小米账号');
  ok("回到 idle 之后不再算告警", rail.isAlert('login') === false);

  /* 停止出口的行为断言（不只是「源码里有这个词」）。 */
  rail.showProgress(null, null, 'refresh');
  ok("刷新在跑时停止按钮要能点",
    rail.stopState() === 'ready' && els.trProgressStop.disabled === false &&
    els.trProgressStop.hidden === false);
  ok("进度卡默认不是卡死态", rail.stallShown() === false);
  rail.updateProgress(
    { alive: true, stalled: true, idleSeconds: 600, elapsed: 600, lines: [] }, 'refresh');
  ok("卡住时进度卡要转成琥珀提示",
    rail.stallShown() === true && text("trProgressStep").includes("没有新进展"));
  rail.hideProgress();
  ok("收起进度卡时停止按钮跟着收",
    rail.stopState() === 'idle' && els.trProgressStop.hidden === true &&
    rail.stallShown() === false);

  return { errors, runs: runs.length, weeks: weeks.length,
    metricsKB: Math.round(JSON.stringify(M).length / 1024) };
}

const report = main();
if (wantBrowser) {
  report.browser = "宽窄屏、静态直开与工具轨行为仍需在真实页面目视核验。";
}
console.log(JSON.stringify(report, null, 2));
if (errors.length) process.exit(1);
console.log("ALL_PASS");
