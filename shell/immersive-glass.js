/* =============================================================================
 * 沉浸玻璃外壳 · immersive-glass.js
 * -----------------------------------------------------------------------------
 * 职责：把仓库根目录的 index.html（跑步数据看板）等比缩放进一块玻璃画布，
 *      并提供「只读 + 沉浸 + 可缩放查看」的外部约束。**不碰看板的任何代码**。
 *
 * 为什么是 iframe（而不是 Shadow DOM / 直接包一层 div）：
 *   1) 物理隔离：看板的 DOM、CSS、JS 全在自己那份 document 里，与外壳零耦合。
 *      Shadow DOM 做不到这点 —— 把看板节点搬进 shadow root 之后，app.js 里所有
 *      document.querySelector('#kpis') 都会失配，等于改了它的运行环境。
 *   2) 弹窗不会穿透：Chart.js / ECharts 的 tooltip、挂在 body 上的 Modal 都活在
 *      iframe 自己的坐标系里，天然被玻璃容器裁住，不需要 appendTo / getPopupContainer。
 *   3) 鼠标坐标不会错位：scale 加在 iframe 元素上，浏览器自己负责把物理坐标换算成
 *      帧内坐标，图表的 hover / 点击不需要任何补偿。
 *   4) 代价：跨源时读不到帧内 DOM，只影响"隐藏内层滚动条"这类美化项（见 CAPABILITY 节）；
 *      以及滚轮/拖拽事件被帧吃掉 —— 所以缩放要靠"接管层"，见 ZOOM 节。
 *
 * 两种运行形态：
 *   A. 同源（推荐）：双击根目录「沉浸看板.cmd」→ 本地 http 服务 → 全功能。
 *   B. 跨源降级：直接双击本文件 → file:// → 看板照常显示，只是"注入类"能力关闭。
 * ============================================================================= */
(function () {
  'use strict';

  /* 主题/动效/模糊最先落地（解析期执行），避免"深色用户先闪一下白底"这类跳变：
       ?theme=dark|light  ?motion=on  ?blur=0..40 */
  (function applyEarlyFlags() {
    var q = new URLSearchParams(location.search);
    var root = document.documentElement;
    var t = q.get('theme');
    if (t === 'dark' || t === 'light') root.setAttribute('data-theme', t);
    // 玻璃背后的动画默认关掉：backdrop-filter 每帧重算模糊，动起来就是卡顿
    if (q.get('motion') === 'on') root.setAttribute('data-motion', 'on');
    var b = q.get('blur');
    if (b !== null && b !== '' && !isNaN(+b)) root.style.setProperty('--glass-blur', (+b) + 'px');
  })();

  /* ===========================================================================
   * 1. 配置：要调的东西几乎都在这里（外加 CSS 顶部 :root 里的视觉令牌）
   * ======================================================================== */
  var CONFIG = {
    /* 虚拟视口（iframe 内部布局用的尺寸）—— 两种模式：
       自适应（默认）：按可用区实时推导，让看板铺满画布、四周不留白。
         旧版固定 1600×900 在小窗口只能缩到 51%（字看不清、四周还全是空的）；
         自适应下 1080p 直接铺满 1760×1016，等于同屏多出约 1/4 的内容。
       固定：?design=1600x900 或 ?mode=fixed —— 版式在任何屏幕上完全一致（旧行为）。
       注意：自适应模式下下面两个值会被 layout() 按实测推导值覆盖。 */
    designWidth: 1600,
    designHeight: 900,
    adaptive: true,      // 是否按可用区自适应虚拟视口
    /* 密度：1 = 原始尺寸（最锐利）；0.9 = 小 10%（眼睛几乎看不出）但同屏多约 11% 内容。
       默认取 0.9：既不像旧版那样缩到看不清，也不浪费屏幕。想按原始尺寸就 ?density=1。 */
    density: 0.9,
    designMinW: 1024,    // 下限压在看板自己的响应式断点(900px)之上，保住桌面版式
    designMaxW: 2000,    // 上限：再宽卡片被拉得太开，密度反而下降
    /* 高度上下限只当"刹车"，正常屏幕都碰不到：
       下限太大反而会在矮窗口里塞不下、又变回留白（踩过：620 卡在 540 可用高度上）；
       上限放宽到 1600 —— 视口越高 = 同屏看到的内容越多，这正是想要的方向。 */
    designMinH: 480,
    designMaxH: 1600,

    fitFactor: 1.0,     // <1 表示四周再留一点余量
    maxScale: 1.6,      // 适配（整幅）时的放大上限，防糊
    minScale: 0.2,

    /* 查看细节时的缩放：以"整幅适配"为 100%，最多放到 500% */
    maxZoom: 5,
    wheelStep: 1.12,    // 每一格滚轮的缩放倍率

    /* 跨源时给 iframe 多加的宽度（滚动条宽度），右侧这一条会被裁掉 ——
       等于在拿不到帧内 DOM 的情况下"隐藏"内层滚动条。见 CAPABILITY 节。 */
    scrollbarGutter: 17,

    /* 只读化：命中这些选择器的元素会被隐藏（相当于"编辑按钮"）。
       本看板里没有 .edit / .toolbar，所以现在是空转；
       写在这里是为了将来把它套到别的看板上时零改代码。 */
    hideSelectors: ['.edit', '.toolbar', '[data-edit]', '.row-actions', '.no-print'],
    lockSelectors: [],

    blockSelection: true,        // 禁用文本选中（沉浸态不该能拉蓝一片）
    uniformCursor: true,         // 统一光标
    hideInnerScrollbar: true,    // 隐藏内层滚动条（滚轮照常可用）
    blockContextMenu: true,      // 屏蔽右键菜单
    keepTextInputUsable: true,   // 例外：输入框保留文本光标（看板有日期筛选框）

    fullscreenOnDblClick: true,
    hudAutoHideMs: 2600,
    hudMetaSelector: '#genAt',   // 从看板读出"数据生成时间"，塞进 HUD
    appTitle: '跑步数据 · 历史看板'
  };

  /* 运行时开关（URL 参数优先，方便截图 / 分享 / 临时换尺寸）
     ?design=1920x1080  ?zoom=2  ?theme=dark  ?motion=on  ?blur=0
     ?hud=on|off  ?hint=on|off  ?debug=1  ?nolock=1 */
  var QS = new URLSearchParams(location.search);
  (function applyQueryOverrides() {
    var d = (QS.get('design') || '').match(/^(\d{3,5})[xX*](\d{3,5})$/);
    if (d) {                        // 显式给了设计稿尺寸 → 切回固定模式
      CONFIG.designWidth = +d[1];
      CONFIG.designHeight = +d[2];
      CONFIG.adaptive = false;
    }
    if (QS.get('mode') === 'fixed') CONFIG.adaptive = false;
    if (+QS.get('density') > 0.3) { CONFIG.density = +QS.get('density'); CONFIG.adaptive = true; }
    if (+QS.get('scale') > 0) CONFIG.fitFactor = +QS.get('scale');
    if (QS.get('hud') === 'off') CONFIG.hudAutoHideMs = -1;
    if (QS.get('nolock') === '1') {
      CONFIG.hideSelectors = []; CONFIG.lockSelectors = [];
      CONFIG.blockSelection = false; CONFIG.blockContextMenu = false;
    }
  })();

  var DEBUG = QS.get('debug') === '1';

  /* ===========================================================================
   * 2. 元素缓存
   * ======================================================================== */
  var el = {
    stage:    document.getElementById('stage'),
    glass:    document.getElementById('glass'),
    clip:     document.getElementById('clip'),
    scaler:   document.getElementById('scaler'),
    frame:    document.getElementById('frame'),
    panLayer: document.getElementById('panLayer'),
    hud:      document.getElementById('hud'),
    hudMeta:  document.getElementById('hudMeta'),
    hudMode:  document.getElementById('hudMode'),
    hudZoom:  document.getElementById('hudZoom'),
    btnFit:   document.getElementById('btnFit'),
    btnFs:    document.getElementById('btnFs'),
    hint:     document.getElementById('hintFade'),
    boot:     document.getElementById('boot')
  };

  /* 视口状态：
       scale   当前缩放（可能是用户放大后的；等于 fitScale 时就是"整幅适配"）
       fitScale整幅适配的缩放系数
       dx,dy   缩放层在裁剪区内的偏移（CSS px，变换顺序 translate → scale）
     内容坐标 (u,v) 在裁剪区内的落点 = (dx + u*scale, dy + v*scale)，
     下面所有缩放/平移都围绕这个式子做反解。 */
  var state = {
    sameOrigin: false,
    scale: 0,
    fitScale: 1,
    dx: 0,
    dy: 0,
    availW: 0,
    availH: 0,
    inspect: false,     // 检查模式（按住 Ctrl）：接管层铺开，接管滚轮与拖拽
    drag: null,
    announced: false
  };

  var metrics = { padX: 0, padY: 0, innerRadius: 12 };

  function log() {
    if (!DEBUG) return;
    var a = ['[glass]'].concat(Array.prototype.slice.call(arguments));
    console.log.apply(console, a);
  }

  function snapPx(v) {          // 对齐到物理像素：避免 0.5px 造成的文字发虚
    var dpr = window.devicePixelRatio || 1;
    return Math.round(v * dpr) / dpr;
  }

  function clampScale(k) {
    return Math.min(CONFIG.maxZoom, Math.max(state.fitScale, k));
  }

  /* ===========================================================================
   * 3. LAYOUT / PAINT —— 等比缩放 + 居中 + 缩放平移
   * ---------------------------------------------------------------------------
   * 可用区 = 玻璃的客户区 减去 padding（最容易漏的一步：直接用
   * getBoundingClientRect() 会把 padding + border 算进可用宽度，结果永远缩放偏小）。
   * scale  = min(可用宽 / 设计宽, 可用高 / 设计高)
   * 偏移   = (可用尺寸 - 缩放后尺寸) / 2；放大/平移后由 clampPan 收边。
   * ======================================================================== */
  function measureGlass() {
    var cs = getComputedStyle(el.glass);
    metrics.padX = parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight);
    metrics.padY = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
    var r = parseFloat(getComputedStyle(document.documentElement)
                       .getPropertyValue('--glass-inner-radius'));
    metrics.innerRadius = r > 0 ? r : 12;
  }

  function layout() {
    // clientWidth/Height 含 padding，减掉才是真正的可用区
    var availW = Math.max(1, el.glass.clientWidth - metrics.padX);
    var availH = Math.max(1, el.glass.clientHeight - metrics.padY);

    // ---- 虚拟视口：自适应（默认）或固定设计稿 ------------------------------
    if (CONFIG.adaptive) {
      // 让「虚拟视口宽高比」= 可用区宽高比 → 等比缩放后正好铺满，四周不留白。
      // 宽度钳在 1024~1760：低于下限会踩到看板自己的响应式断点(900px)，
      // 高于上限卡片会被拉得太开、信息密度反而下降。
      var aspect = availW / availH;
      var dw = Math.min(CONFIG.designMaxW,
                        Math.max(CONFIG.designMinW, availW / CONFIG.density));
      var dh = Math.min(CONFIG.designMaxH,
                        Math.max(CONFIG.designMinH, Math.round(dw / aspect)));
      // 写回 CONFIG：下面所有几何计算（裁剪 / 复位 / 预设缩放 / 对外接口）
      // 都读这两个值，自适应只要改这一处。
      CONFIG.designWidth = Math.round(dw);
      CONFIG.designHeight = dh;
    }

    // 跨源时 iframe 比虚拟视口宽出 scrollbarGutter，那一条由 .scaler 的 overflow 裁掉，
    // 所以缩放系数仍然只按"设计稿可见尺寸"算。
    var gutter = (state.sameOrigin || !CONFIG.hideInnerScrollbar) ? 0 : CONFIG.scrollbarGutter;
    el.scaler.style.width = CONFIG.designWidth + 'px';
    el.scaler.style.height = CONFIG.designHeight + 'px';
    el.frame.style.width = (CONFIG.designWidth + gutter) + 'px';
    el.frame.style.height = CONFIG.designHeight + 'px';

    var fit = Math.min(availW / CONFIG.designWidth, availH / CONFIG.designHeight) * CONFIG.fitFactor;
    fit = Math.min(CONFIG.maxScale, Math.max(CONFIG.minScale, fit));

    var prev = { w: state.availW, h: state.availH, k: state.scale, dx: state.dx, dy: state.dy, fit: state.fitScale };
    state.availW = availW;
    state.availH = availH;
    state.fitScale = fit;

    if (prev.w > 0 && prev.k > 0) {
      // 窗口尺寸/全屏变了：保住「正在看的那一点」和相对放大倍数，别把视野甩回整幅
      var uCenter = (prev.w / 2 - prev.dx) / prev.k;
      var vCenter = (prev.h / 2 - prev.dy) / prev.k;
      var rel = prev.fit > 0 ? prev.k / prev.fit : 1;
      state.scale = clampScale(fit * rel);
      state.dx = availW / 2 - uCenter * state.scale;
      state.dy = availH / 2 - vCenter * state.scale;
    } else {
      state.scale = fit;
      state.dx = (availW - CONFIG.designWidth * fit) / 2;
      state.dy = (availH - CONFIG.designHeight * fit) / 2;
    }

    clampPan();
    paint();
    log('layout', {
      avail: [availW, availH], design: [CONFIG.designWidth, CONFIG.designHeight],
      fit: +fit.toFixed(4), scale: +state.scale.toFixed(4),
      offset: [+state.dx.toFixed(1), +state.dy.toFixed(1)],
      mode: state.sameOrigin ? 'same-origin' : 'cross-origin'
    });
  }

  /* 放大后把内容"收"在裁剪区内：比区小就居中，比区大就不许拖出空白 */
  function clampPan() {
    var cw = CONFIG.designWidth * state.scale;
    var ch = CONFIG.designHeight * state.scale;
    state.dx = cw <= state.availW
      ? (state.availW - cw) / 2
      : Math.min(0, Math.max(state.availW - cw, state.dx));
    state.dy = ch <= state.availH
      ? (state.availH - ch) / 2
      : Math.min(0, Math.max(state.availH - ch, state.dy));
  }

  function paint() {
    var k = state.scale;
    // 圆角要"视觉上恒为 12px"：缩放层被放大了 k 倍，所以写成 12/k
    el.scaler.style.borderRadius = (metrics.innerRadius / k) + 'px';
    el.scaler.style.transform =
      'translate(' + snapPx(state.dx) + 'px,' + snapPx(state.dy) + 'px) scale(' + k + ')';

    // HUD / 提示条跟着画布一起缩：小窗口里画布缩到 0.6 时，HUD 不能还顶着 12px，
    // 否则就成了"比看板还大"的穿模感。钳在 0.8~1，保证还看得清。
    var ui = Math.max(0.8, Math.min(1, k));
    document.documentElement.style.setProperty('--ui-scale', ui.toFixed(3));

    updateHudZoom();
  }

  function updateHudZoom() {
    var pct = Math.round(state.scale * 100);
    el.hudZoom.textContent = pct + '%';
    el.hudZoom.title = '当前 ' + pct + '% · 整幅适配 ' + Math.round(state.fitScale * 100) + '%';
    el.hud.classList.toggle('zoomed', Math.abs(state.scale - state.fitScale) > 0.004);
  }

  var rafId = 0;
  function scheduleLayout() {
    if (rafId) return;
    rafId = requestAnimationFrame(function () {
      rafId = 0;
      measureGlass();
      layout();
    });
  }

  /* ===========================================================================
   * 4. ZOOM / PAN —— 看细节
   * ---------------------------------------------------------------------------
   * 为什么需要一层"接管层"：iframe 是独立文档，滚轮/拖拽事件在它内部就被处理掉了，
   * 外壳的 window 收不到（事件不跨文档冒泡）。所以按住 Ctrl 时铺一层透明的
   * .pan-layer 盖住画布，把指针事件收回外壳 —— 指针落在接管层上，
   * Ctrl+滚轮才能真正做到"以光标为锚点放大"，拖拽才能平移。
   * 松手（松开 Ctrl）就撤掉，看板照常可点可滚。
   * ======================================================================== */
  function pointerInClip(e) {
    var r = el.clip.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }

  /* 以 (cx,cy)（裁剪区坐标）为锚点缩放：锚点下的那一点内容必须留在原处 */
  function zoomAt(cx, cy, factor) {
    var k0 = state.scale;
    var k1 = clampScale(k0 * factor);
    if (Math.abs(k1 - k0) < 1e-4) return;
    var u = (cx - state.dx) / k0;
    var v = (cy - state.dy) / k0;
    state.scale = k1;
    state.dx = cx - u * k1;
    state.dy = cy - v * k1;
    clampPan();
    paint();
    pokeHud();
  }

  function resetZoom() {
    state.scale = state.fitScale;
    state.dx = (state.availW - CONFIG.designWidth * state.scale) / 2;
    state.dy = (state.availH - CONFIG.designHeight * state.scale) / 2;
    paint();
    pokeHud();
  }

  /* ?zoom=2 → 整幅适配的 2 倍（截图、分享某个局部视角时用） */
  function applyPresetZoom() {
    var z = parseFloat(QS.get('zoom'));
    if (!(z > 0)) return;
    state.scale = clampScale(state.fitScale * z);
    state.dx = state.availW / 2 - (CONFIG.designWidth / 2) * state.scale;
    state.dy = state.availH / 2 - (CONFIG.designHeight / 2) * state.scale;
    clampPan();
    paint();
  }

  function setInspect(on, why) {
    if (state.inspect === on) return;
    state.inspect = on;
    el.panLayer.classList.toggle('on', on);
    if (!on) {
      state.drag = null;
      el.panLayer.classList.remove('dragging');
    }
    log('inspect', on, why || '');
  }

  function onWheel(e) {
    var d = e.deltaMode === 1 ? e.deltaY * 16 : (e.deltaMode === 2 ? e.deltaY * 100 : e.deltaY);
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();                       // 拦住浏览器的整页缩放（我们要的是画布内缩放）
      var p = pointerInClip(e);
      zoomAt(p.x, p.y, d < 0 ? CONFIG.wheelStep : 1 / CONFIG.wheelStep);
      return;
    }
    if (state.inspect) {                        // 检查模式下滚轮 = 平移（像看图那样）
      e.preventDefault();
      if (e.shiftKey) state.dx -= d; else state.dy -= d;
      clampPan();
      paint();
    }
  }

  /* ===========================================================================
   * 5. CAPABILITY —— 同源才有"注入"能力，跨源走视觉降级
   * ======================================================================== */
  function probeSameOrigin() {
    var ok = false;
    try {
      var d = el.frame.contentDocument;          // 跨源时返回 null 或抛 SecurityError
      ok = !!(d && d.documentElement);
    } catch (e) { ok = false; }
    state.sameOrigin = ok;
    el.hudMode.textContent = ok ? '只读' : '只读 · 基础模式';
    el.hudMode.title = ok
      ? '已注入只读样式：隐藏内层滚动条 / 禁选中 / 统一光标'
      : '当前是 file:// 跨源模式，只保证版式与缩放；双击根目录「沉浸看板.cmd」可启用全部只读能力';
    log('sameOrigin =', ok);
    return ok;
  }

  function buildInjectedCSS() {
    return [
      /* 隐藏内层滚动条，但保留滚轮滚动。
         Firefox 走 scrollbar-width，Chromium/WebKit 走 ::-webkit-scrollbar。 */
      'html { scrollbar-width: none !important; }',
      '::-webkit-scrollbar { width: 0 !important; height: 0 !important; display: none !important; }',
      /* 缩放后字体的抗锯齿补偿 */
      'html, body { -webkit-font-smoothing: antialiased !important; text-rendering: optimizeLegibility; }',
      /* 滚动限制在画布内部，不把滚动链传给外层 */
      'html, body { overscroll-behavior: contain !important; }',
      CONFIG.hideSelectors.length
        ? CONFIG.hideSelectors.join(',') + ' { display: none !important; }' : '',
      CONFIG.lockSelectors.length
        ? CONFIG.lockSelectors.join(',') + ' { pointer-events: none !important; opacity: .55 !important; }' : '',
      CONFIG.uniformCursor
        ? 'html, body, div, span, p, h1, h2, h3, table, th, td, canvas, button, select, label, a { cursor: default !important; }'
        : '',
      CONFIG.uniformCursor && CONFIG.keepTextInputUsable
        ? 'input[type="text"], input[type="search"], input:not([type]), textarea { cursor: text !important; }' : '',
      CONFIG.blockSelection
        ? '*:not(input):not(textarea) { user-select: none !important; -webkit-user-select: none !important; }' : ''
    ].filter(Boolean).join('\n');
  }

  function injectReadonly() {
    if (!state.sameOrigin) return;
    var doc;
    try { doc = el.frame.contentDocument; } catch (e) { return; }
    if (!doc || !doc.documentElement) return;

    var style = doc.getElementById('__immersive_glass__');
    if (!style) {
      style = doc.createElement('style');
      style.id = '__immersive_glass__';
      style.setAttribute('data-injected-by', 'immersive-glass');
      (doc.head || doc.documentElement).appendChild(style);
    }
    var css = buildInjectedCSS();
    if (style.textContent !== css) style.textContent = css;

    // 屏蔽帧内右键（沉浸态）
    if (CONFIG.blockContextMenu && !doc.__glassCtxBlocked) {
      doc.__glassCtxBlocked = true;
      doc.addEventListener('contextmenu', function (e) { e.preventDefault(); }, true);
    }
    readMeta(doc);
  }

  function readMeta(doc) {
    // 零侵入读取：只读 DOM，不改 DOM
    try {
      var t = doc.title;
      if (t) document.querySelector('.hud-title').textContent = t;
      var node = doc.querySelector(CONFIG.hudMetaSelector);
      var txt = node && (node.textContent || '').trim();
      // 直接用看板自己的文案（它已经写了"数据生成于 …"），别再套一层前缀
      el.hudMeta.textContent = txt || '数据截至 —';
    } catch (e) { /* 读不到就算了，HUD 保持占位 */ }
  }

  var mo = null, moTimer = 0;
  function watchFrame() {
    if (!state.sameOrigin || typeof MutationObserver === 'undefined') return;
    try {
      if (mo) mo.disconnect();
      mo = new MutationObserver(function () {
        // 看板重绘很频繁：合并到下一帧再处理，别让观察器变成性能负担
        if (moTimer) return;
        moTimer = setTimeout(function () { moTimer = 0; injectReadonly(); }, 250);
      });
      mo.observe(el.frame.contentDocument.documentElement,
                 { childList: true, subtree: true, characterData: true });
    } catch (e) { /* ignore */ }
  }

  /* ===========================================================================
   * 6. 全屏
   * ======================================================================== */
  function toggleFullscreen() {
    if (document.fullscreenElement || document.webkitFullscreenElement) {
      (document.exitFullscreen || document.webkitExitFullscreen).call(document);
      return;
    }
    var root = document.documentElement;
    var req = root.requestFullscreen || root.webkitRequestFullscreen;
    if (!req) return;
    try {
      // navigationUI: 'hide' 让浏览器收起自带的导航条，才是真的沉浸
      var p = req.call(root, { navigationUI: 'hide' });
      if (p && p.catch) p.catch(function () { try { req.call(root); } catch (e) {} });
    } catch (e) {
      try { req.call(root); } catch (e2) {}
    }
  }

  /* ===========================================================================
   * 7. HUD 与提示的自动隐现
   * ======================================================================== */
  var hudTimer = 0;
  /* ?hud=on 常显（拍照、投屏时用）· ?hud=off 永不显示 · 默认鼠标静止 2.6s 隐去 */
  function pokeHud() {
    var mode = QS.get('hud');
    if (mode === 'off') { el.hud.classList.remove('on'); return; }
    el.hud.classList.add('on');
    if (mode === 'on' || CONFIG.hudAutoHideMs <= 0) return;
    clearTimeout(hudTimer);
    hudTimer = setTimeout(function () { el.hud.classList.remove('on'); }, CONFIG.hudAutoHideMs);
  }

  /* ?hint=on 常显 · ?hint=off 不显示 · 默认开工 4.6s 后隐去 */
  function showHint() {
    var mode = QS.get('hint');
    if (mode === 'off') return;
    el.hint.classList.add('on');
    if (mode === 'on') return;
    setTimeout(function () { el.hint.classList.remove('on'); }, 4600);
  }

  /* ===========================================================================
   * 8. 事件绑定
   * ======================================================================== */
  function bind() {
    // 尺寸变化：resize / 全屏切换 / 字体加载完成，都收敛到一次 rAF（layout 会保住当前视野）
    window.addEventListener('resize', scheduleLayout);
    window.addEventListener('fullscreenchange', scheduleLayout);
    window.addEventListener('webkitfullscreenchange', scheduleLayout);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(scheduleLayout);

    // 缩放 / 平移：整个 window 收滚轮（外壳空白处与接管层都会冒泡上来），
    // passive:false 才拦得住浏览器的 Ctrl+滚轮整页缩放。
    window.addEventListener('wheel', onWheel, { passive: false });

    // 接管层：按住 Ctrl 时铺开
    el.panLayer.addEventListener('pointerdown', function (e) {
      if (e.button !== 0) return;
      state.drag = { x: e.clientX, y: e.clientY, dx: state.dx, dy: state.dy };
      el.panLayer.classList.add('dragging');
      try { el.panLayer.setPointerCapture(e.pointerId); } catch (err) {}
      e.preventDefault();
    });
    el.panLayer.addEventListener('pointermove', function (e) {
      if (!state.drag) return;
      state.dx = state.drag.dx + (e.clientX - state.drag.x);
      state.dy = state.drag.dy + (e.clientY - state.drag.y);
      clampPan();
      paint();
    });
    function endDrag(e) {
      if (!state.drag) return;
      state.drag = null;
      el.panLayer.classList.remove('dragging');
      try { el.panLayer.releasePointerCapture(e.pointerId); } catch (err) {}
    }
    el.panLayer.addEventListener('pointerup', endDrag);
    el.panLayer.addEventListener('pointercancel', endDrag);
    // 在接管层上双击 = 复位到整幅。必须 stopPropagation，否则会顺手触发舞台的全屏。
    el.panLayer.addEventListener('dblclick', function (e) {
      e.stopPropagation();
      resetZoom();
    });

    // 双击"背景"（玻璃之外的暗底 + 玻璃的 16px 边框带）切换全屏。
    // 注意：iframe 内部的双击不会冒泡到这里 —— 这正是我们要的，
    // 否则在看板上双击某个图表就会突然全屏。
    el.stage.addEventListener('dblclick', function (e) {
      if (!CONFIG.fullscreenOnDblClick) return;
      toggleFullscreen();
      log('dblclick → fullscreen', e.target && e.target.className);
    });
    el.btnFs.addEventListener('click', toggleFullscreen);
    el.btnFit.addEventListener('click', resetZoom);

    // 键盘：Ctrl(按住) = 检查模式；F 全屏；R 重载；0 复位；? 再看提示
    window.addEventListener('keydown', function (e) {
      pokeHud();
      if (e.key === 'Control' || e.key === 'Meta') { setInspect(true, 'ctrl'); return; }
      if (e.ctrlKey || e.metaKey || e.altKey) {
        if (e.key === '0') { e.preventDefault(); resetZoom(); }   // 别让浏览器把整页缩放重置了
        return;
      }
      var k = e.key.toLowerCase();
      if (k === '0') { e.preventDefault(); resetZoom(); }
      else if (k === 'f') { e.preventDefault(); toggleFullscreen(); }
      else if (k === 'r') { e.preventDefault(); reloadFrame(); }
      else if (k === '?' || k === '/') showHint();
    });
    window.addEventListener('keyup', function (e) {
      if (e.key === 'Control' || e.key === 'Meta') setInspect(false, 'ctrl-up');
    });
    window.addEventListener('blur', function () { setInspect(false, 'blur'); });

    // HUD 隐现
    window.addEventListener('mousemove', function () { pokeHud(); }, { passive: true });

    // 右键：外壳层面屏蔽（帧内由注入的监听器兜住）
    if (CONFIG.blockContextMenu) {
      window.addEventListener('contextmenu', function (e) { e.preventDefault(); }, true);
    }

    // 看板装载完成 → 探测能力、注入、收遮罩
    el.frame.addEventListener('load', onFrameLoad);

    // 兜底：8 秒还没 load 出来，给一个能点开的出口
    setTimeout(function () {
      if (!el.boot.classList.contains('off')) {
        el.boot.innerHTML = '<span class="boot-err">装载超时 —— ' +
          '<a href="../index.html" style="color:#93c5fd">点这里直接打开看板</a></span>';
      }
    }, 8000);

    // 首屏
    measureGlass();
    probeSameOrigin();
    layout();
    applyPresetZoom();
    pokeHud();
    showHint();
  }

  function onFrameLoad() {
    probeSameOrigin();
    injectReadonly();
    watchFrame();
    layout();                     // 同源/跨源切换会改变 scrollbarGutter，重算一次
    el.boot.classList.add('off');
    if (!state.announced) { state.announced = true; pokeHud(); }
    log('frame loaded');
  }

  function reloadFrame() {
    // 重载 iframe 就等于重读 data.js：导入脚本写完数据后按 R 即可看到最新
    el.boot.classList.remove('off');
    el.boot.textContent = '正在重新装载看板…';
    el.frame.contentWindow.location.reload();
  }

  /* ===========================================================================
   * 9. 对外暴露调参入口（控制台里改着看效果，不必改文件）
   *    __glassShell.CONFIG.designWidth = 1920; __glassShell.relayout();
   *    __glassShell.setZoom(2.5);   // 2.5 = 整幅适配的 2.5 倍
   * ======================================================================== */
  window.__glassShell = {
    CONFIG: CONFIG,
    state: state,
    relayout: function () { measureGlass(); layout(); },
    reinject: function () { probeSameOrigin(); injectReadonly(); },
    setZoom: function (mult) {
      state.scale = clampScale(state.fitScale * mult);
      state.dx = state.availW / 2 - (CONFIG.designWidth / 2) * state.scale;
      state.dy = state.availH / 2 - (CONFIG.designHeight / 2) * state.scale;
      clampPan();
      paint();
    },
    resetZoom: resetZoom
  };

  document.readyState === 'loading'
    ? document.addEventListener('DOMContentLoaded', bind)
    : bind();
})();
