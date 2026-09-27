/* =============================================================================
   悬浮工具轨 · toolrail.js
   -----------------------------------------------------------------------------
   把常用导入动作融进看板（2026-09-26 多弗朗）。跑步复盘由 app.js 管理 ——
   这个文件只做四件事：

     1. 收纳：常态只见短边缘提示 → 鼠标靠右或点按展开；不随页面滚动
     2. 状态：idle / busy / warn|err；忙时图标换转环
     3. 长任务：起任务 → 游标轮询 /api/log → 更新阶段进度条
     4. 降级：file:// 直开拿不到 window.__BRIDGE__ → 变灰 + 提示 + 复制命令

   一条硬规矩：**删数据的动作永远先预演再确认**（清理要摆清单、回滚要讲清换哪两版）。

   依赖：本地服务（tools/serve.py）注入的 window.__BRIDGE__ = {token, api, port}。
   没有它也能活 —— 就是降级态。**「双击 index.html 就能看」这条承诺不能破坏。**
   ============================================================================= */
(function () {
  'use strict';

  var BRIDGE = window.__BRIDGE__ || null;
  var API = BRIDGE ? (BRIDGE.api || '/api') : '';
  var TOKEN = BRIDGE ? (BRIDGE.token || '') : '';
  var ONLINE = !!BRIDGE;

  function $(id) { return document.getElementById(id); }

  function api(path, options) {
    options = options || {};
    var headers = { 'X-Bridge-Token': TOKEN };
    if (options.body) headers['Content-Type'] = 'application/json';
    return fetch(API + path, {
      method: options.method || 'GET',
      headers: headers,
      body: options.body ? JSON.stringify(options.body) : undefined,
      cache: 'no-store'
    }).then(function (res) {
      return res.json().catch(function () { return { ok: false, error: 'HTTP ' + res.status }; });
    });
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function fmtSize(bytes) {
    if (!bytes) return '0 B';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1048576).toFixed(1) + ' MB';
  }

  /* ============================================================ 收纳 ---- */
  var rail = $('toolrail');
  var hot = $('trHot');
  var stack = $('trStack');
  var hoverTimer = null;

  function openRail() {
    if (hoverTimer) { clearTimeout(hoverTimer); hoverTimer = null; }
    rail.dataset.open = 'true';
    hot.setAttribute('aria-expanded', 'true');
  }
  function closeRail(delay) {
    if (hoverTimer) clearTimeout(hoverTimer);
    hoverTimer = setTimeout(function () {
      rail.dataset.open = 'false';
      hot.setAttribute('aria-expanded', 'false');
      hoverTimer = null;
    }, delay == null ? 160 : delay);
  }
  hot.addEventListener('mouseenter', openRail);
  hot.addEventListener('click', openRail);
  hot.addEventListener('focus', openRail);
  rail.addEventListener('mouseenter', openRail);
  /* 焦点进到按钮里（键盘 Tab）也要展开，否则智能键盘用不了 */
  stack.addEventListener('focusin', openRail);
  rail.addEventListener('mouseleave', function () { closeRail(240); });
  rail.addEventListener('focusout', function (e) {
    if (!rail.contains(e.relatedTarget)) closeRail(240);
  });
  document.addEventListener('pointerdown', function (e) {
    if (rail.dataset.open === 'true' && !rail.contains(e.target)) closeRail(0);
  });

  /* ============================================================ 按钮 ---- */
  var BTNS = {
    refresh:  { el: $('trRefresh'),  tip: $('tipRefresh'),  tipIdle: '刷新数据' },
    login:    { el: $('trLogin'),    tip: $('tipLogin'),    tipIdle: '登录小米账号' },
    cleanup:  { el: $('trCleanup'),  tip: $('tipCleanup'),  tipIdle: '清理下载包和记录' }
  };

  var progress = $('trProgress');
  var progressTitle = $('trProgressTitle');
  var progressElapsed = $('trProgressElapsed');
  var progressStep = $('trProgressStep');
  var progressStartedAt = 0;
  var progressTimer = null;

  function elapsedText(seconds) {
    var total = Math.max(0, Math.floor(seconds || 0));
    var minutes = Math.floor(total / 60);
    var rest = total % 60;
    return String(minutes).padStart(2, '0') + ':' + String(rest).padStart(2, '0');
  }

  function setProgressStep(step) {
    progressStep.textContent = step;
    var bar = progress.querySelector('[role="progressbar"]');
    if (bar) bar.setAttribute('aria-valuetext', step);
  }

  function showProgress(step) {
    progressStartedAt = Date.now();
    progress.dataset.open = 'true';
    progress.setAttribute('aria-hidden', 'false');
    progressTitle.textContent = '正在刷新数据';
    setProgressStep(step || '正在连接邮箱，检查新的导出邮件');
    progressElapsed.textContent = '00:00';
    if (progressTimer) clearInterval(progressTimer);
    progressTimer = setInterval(function () {
      progressElapsed.textContent = elapsedText((Date.now() - progressStartedAt) / 1000);
    }, 500);
  }

  function updateProgress(lines, elapsed) {
    if (progress.dataset.open !== 'true') showProgress();
    if (typeof elapsed === 'number' && isFinite(elapsed)) {
      progressStartedAt = Date.now() - elapsed * 1000;
      progressElapsed.textContent = elapsedText(elapsed);
    }
    (lines || []).forEach(function (line) {
      var text = String(line || '');
      if (/请在窗口里登录|登录态过期|还在等你登录/.test(text)) {
        setProgressStep('需要登录小米账号，完成后会继续下载');
      } else if (/候选邮件|邮箱\s*:\s*/.test(text)) {
        setProgressStep('正在检查邮箱里的新导出邮件');
      } else if (/本地没有更新的包|取文件中|下载中|重新下载/.test(text)) {
        setProgressStep('正在下载新的跑步数据');
      } else if (/已保存 .*MB|已保存 .*KB/.test(text)) {
        setProgressStep('下载完成，正在准备导入');
      } else if (/开始导入|导出包\s*:|密码校验通过|开始解析/.test(text)) {
        setProgressStep('正在解压并读取跑步记录');
      } else if (/逐分钟|正在计算指标/.test(text)) {
        setProgressStep('正在计算跑步指标');
      } else if (/已算好指标|完成：|看板已更新/.test(text)) {
        setProgressStep('正在保存数据并更新看板');
      }
    });
  }

  function hideProgress() {
    progress.dataset.open = 'false';
    progress.setAttribute('aria-hidden', 'true');
    if (progressTimer) { clearInterval(progressTimer); progressTimer = null; }
  }

  function setBtn(kind, state, tip) {
    var b = BTNS[kind];
    if (!b) return;
    /* idle 会把提示文字交还给 tipIdle —— 因为 tipIdle 是「动态默认值」
       （登录态、可清理数都会改它）。传了具体文字就按传的来。 */
    if (state === 'idle') b.tipIdle = tip || b.tipIdle;
    b.el.dataset.state = state;
    b.tip.textContent = tip || b.tipIdle;
    b.el.setAttribute('aria-label', b.tip.textContent);
    /* 记下这个告警是什么时候立的，`isAlert` 靠它算保鲜期 */
    b.alertAt = (state === 'warn' || state === 'err') ? Date.now() : 0;
  }
  function lockBtn(kind, locked) {
    var b = BTNS[kind];
    if (b) b.el.disabled = !!locked;
  }
  function badge(kind, on) {
    var b = BTNS[kind];
    if (b) b.el.dataset.badge = on || '';
  }
  /* 失败 / 待办提示的保鲜期（毫秒） */
  var ALERT_TTL = 60000;
  /* 该按钮当前是不是在报「需要你处理 / 失败了」。
     这种状态不该被 9 秒一次的后台 status 轮询**立刻**冲掉 —— 用户还没看见就被
     抹了，等于没有。保鲜期内轮询只负责「解锁」和「补默认提示」，不碰 warn/err。

     **但保鲜期一过就必须交还给轮询。** 早先是永久粘住的：一次失败之后按钮就永远
     停在那句文案上，用户等多久、点几次都一样，看起来就是「按钮坏了、点了没用」——
     2026-09-27 多弗朗报的正是这个（截图里那个「没完成（1）」）。唯一的自救办法是
     刷新页面，而没人会想到要刷新。现在 60 秒后轮询就能把它拉回 idle。 */
  function isAlert(kind) {
    var b = BTNS[kind];
    if (!b) return false;
    var st = b.el.dataset.state;
    if (st !== 'warn' && st !== 'err') return false;
    if (!b.alertAt || Date.now() - b.alertAt > ALERT_TTL) return false;
    return true;
  }

  /* ============================================================ 状态同步 ---- */
  function applyStatus(s) {
    if (!s || !s.ok) return;
    var locks = s.locks || {};
    if (s.idle) {
      ['refresh', 'login', 'cleanup'].forEach(function (k) { lockBtn(k, false); });
    } else {
      lockBtn('refresh', !!locks.refresh);
      lockBtn('login', !!locks.login);
      lockBtn('cleanup', !!locks.cleanup);
    }
    /* refresh 也得有「回 idle」的路。以前它只在 busy 的时候才被处理，所以一次
       失败就永远停在 err（「没完成（x）」「下载没完成」）—— 和 login 那个坑
       一模一样。不在跑、也不在保鲜期里，就让它回到能点、能看的样子。 */
    if (!(s.busy && s.busy.refresh) && !isAlert('refresh')) {
      setBtn('refresh', 'idle', '刷新数据');
    }
    if (s.xiaomi && s.xiaomi.loggedIn) {
      badge('login', 'on');
      BTNS.login.tipIdle = '登录小米账号（已登录）';
      if (!isAlert('login')) setBtn('login', 'idle', BTNS.login.tipIdle);
    } else {
      badge('login', '');
      BTNS.login.tipIdle = '登录小米账号';
      if (!isAlert('login')) setBtn('login', 'idle', BTNS.login.tipIdle);
    }
    if (s.cleanup && s.cleanup.count > 0) {
      badge('cleanup', 'warn');
      BTNS.cleanup.tipIdle = '清理下载包和记录（' + s.cleanup.count + ' 项）';
      /* 有东西可清 → 那是「待办」不是「报错」，用警告色提示一眼能看见 */
      if (!isAlert('cleanup')) setBtn('cleanup', 'warn', BTNS.cleanup.tipIdle);
    } else {
      badge('cleanup', '');
      BTNS.cleanup.tipIdle = '清理下载包和记录';
      if (!isAlert('cleanup')) setBtn('cleanup', 'idle', BTNS.cleanup.tipIdle);
    }
    /* 页面刚打开时如果任务还在跑（另一个标签页发起的），把状态接上 */
    if (s.busy) {
      if (s.busy.refresh) {
        setBtn('refresh', 'busy', '正在刷新数据…');
        lockBtn('refresh', true);
        showProgress();
        poll('refresh', 0);
      }
      if (s.busy.login) { setBtn('login', 'busy', '正在登录…'); lockBtn('login', true); poll('login', 0); }
    }
  }

  function refreshStatus() {
    if (!ONLINE) return Promise.resolve(null);
    return api('/status').then(function (s) { applyStatus(s); return s; })
      .catch(function () { goOffline('本地服务没有响应'); });
  }

  /* ============================================================ 长任务轮询 ---- */
  var polling = {};

  function poll(kind, from) {
    if (polling[kind]) return;
    polling[kind] = true;
    var cursor = from || 0;

    function tick() {
      api('/log?kind=' + kind + '&from=' + cursor).then(function (d) {
        if (!d) { delete polling[kind]; onTaskEnd(kind, { exitCode: -1 }); return; }
        if (d.cursor != null) cursor = d.cursor;
        if (kind === 'refresh') updateProgress(d.lines, d.elapsed);
        if (!d.alive) { delete polling[kind]; onTaskEnd(kind, d); return; }
        setTimeout(tick, 450);
      }).catch(function () {
        delete polling[kind];
        onTaskEnd(kind, { exitCode: -1 });
      });
    }
    tick();
  }

  function onTaskEnd(kind, d) {
    var code = d.exitCode;
    /* 2026-09-26 多弗朗：**不要回执** —— 成功不必报，结果看一眼板就知道。
       所以这里只在「没成功」时给状态；成功一律直接回 idle（按钮恢复原色、原图标）。
       唯一的例外是 refresh 成功：要整页重载，否则 data.js 换了内容浏览器不会重取。 */
    if (kind === 'refresh') {
      if (code === 0) {
        setProgressStep('更新完成，正在刷新看板');
        reloadDashboard();                 // 成功 → 直接刷新看板，不设 done 态
      } else if (code === 9) {
        // 没有新邮件 = 数据没变。这不算成功也不算失败，更不该弹回执：
        // 看板本来就没变，报「没有新邮件」是多余的噪音。静默回 idle。
        setBtn('refresh', 'idle', '刷新数据');
      } else if (code === 8) {
        setBtn('refresh', 'err', '下载没完成');     // 失败要讲，但要短
      } else if (code === -1) {
        setBtn('refresh', 'err', '连接断了');       // 桥挂了 / 轮询失败
      } else {
        setBtn('refresh', 'err', '没刷成（' + code + '）');
      }
      hideProgress();
      refreshStatus();
      return;
    }
    if (kind === 'login') {
      /* 登录态本来就有角标（右上小蓝点）表达「已登录」，不必再出文字回执。
         只有「需要你手动去点一下」和「失败了」这两种才值得说 ——
         这两种用户不处理就卡住了。 */
      if (code === 0) {
        setBtn('login', 'idle', '登录小米账号（已登录）');
      } else if (code === 2) {
        setBtn('login', 'warn', '要在窗口里手动登录一次');
      } else if (code === 3) {
        setBtn('login', 'warn', '登录超时了，再来一次');
      } else if (code === 5) {
        setBtn('login', 'err', '浏览器没起来');
      } else if (code === -1) {
        setBtn('login', 'err', '连接断了');
      } else {
        setBtn('login', 'err', '没完成（' + code + '）');
      }
      refreshStatus();
    }
  }

  /* 导入成功后看板要重载才显示新数据：data.js 换了内容但同名，浏览器不会自动重取 */
  function reloadDashboard() {
    var t = Date.now();
    location.replace(location.pathname + '?_t=' + t + location.hash);
  }

  /* ============================================================ 侧边动作 ---- */
  function doRefresh() {
    if (!ONLINE) return hintDegrade();
    setBtn('refresh', 'busy', '正在刷新数据…');
    showProgress();
    lockBtn('refresh', true); lockBtn('login', true); lockBtn('cleanup', true);
    api('/refresh', { method: 'POST' }).then(function (d) {
      if (!d.ok) {
        hideProgress();
        setBtn('refresh', 'err', d.error || '起不来');
        refreshStatus();
        return;
      }
      poll('refresh', 0);
    }).catch(function () {
      hideProgress();
      setBtn('refresh', 'err', '请求失败');
      refreshStatus();
    });
  }

  function doLogin() {
    if (!ONLINE) return hintDegrade();
    setBtn('login', 'busy', '正在开浏览器…');
    lockBtn('login', true); lockBtn('refresh', true); lockBtn('cleanup', true);
    api('/login', { method: 'POST' }).then(function (d) {
      if (!d.ok) {
        setBtn('login', 'err', d.error || '起不来');
        refreshStatus();
        return;
      }
      poll('login', 0);
    }).catch(function () {
      setBtn('login', 'err', '请求失败');
      refreshStatus();
    });
  }

  function doCleanup() {
    if (!ONLINE) return hintDegrade();
    api('/cleanup/preview').then(function (d) {
      var p = d && d.preview;
      if (!p || !p.count) {
        confirmBox({
          title: '没有可清理的东西',
          html: '<p>搜索目录里没有小米导出包，也没有已处理邮件记录。</p>',
          okText: null
        });
        return;
      }
      var list = p.items.map(function (it) {
        var tag = it.kind === 'package' ? '包' : '记录';
        return '<li><span class="f">' + escapeHtml(it.name) + '</span> [' + tag + '] · ' +
               fmtSize(it.bytes) + (it.detail ? ' · ' + escapeHtml(it.detail) : '') + '</li>';
      }).join('');
      confirmBox({
        title: '确认清理 ' + p.count + ' 项',
        html: '<p>将删除下面这些：</p><ul>' + list + '</ul>' +
              '<p class="keep">看板数据（data.js）不会被动。</p>' +
              '<p style="color:var(--ink-3)">删掉记录后旧邮件可以重新被处理，但包需要重新下载。</p>',
        okText: '确认删除',
        danger: true,
        onOk: function () {
          setBtn('cleanup', 'busy', '正在清理…');
          api('/cleanup/apply', { method: 'POST' }).then(function (r) {
            if (!r.ok) { setBtn('cleanup', 'err', r.error || '清理失败'); return; }
            /* 成功也不出回执：角标（右上琥珀点）自己会消失，那就是结果。
               只有「删了一半」才值得说 —— 用户需要知道有东西没删掉。 */
            var failed = (r.failed || []).length;
            if (failed) setBtn('cleanup', 'warn', failed + ' 项没删掉');
            else setBtn('cleanup', 'idle', '清理下载包和记录');
            refreshStatus();
          }).catch(function () { setBtn('cleanup', 'err', '请求失败'); });
        }
      });
    }).catch(function () { hintDegrade(); });
  }

  /* ============================================================ 确认框 ---- */
  var modal = $('trModal'), mScrim = $('trModalScrim');
  var mTitle = $('trModalTitle'), mBody = $('trModalBody');
  var mOk = $('trModalOk'), mCancel = $('trModalCancel');
  var modalOnOk = null;
  var modalReturnFocus = null;

  function confirmBox(opts) {
    modalReturnFocus = document.activeElement;
    mTitle.textContent = opts.title || '确认';
    mBody.innerHTML = opts.html || '';
    modalOnOk = opts.onOk || null;
    if (opts.okText) {
      mOk.hidden = false;
      mOk.textContent = opts.okText;
      mOk.className = 'tr-btn-t ' + (opts.danger ? 'tr-btn-danger' : 'tr-btn-main');
      mCancel.style.display = '';
    } else {
      mOk.hidden = true;
      mCancel.textContent = '知道了';
    }
    modal.dataset.open = 'true';
    modal.setAttribute('aria-hidden', 'false');
    mScrim.dataset.open = 'true';
    mCancel.focus();
  }
  function closeModal() {
    modal.dataset.open = 'false';
    modal.setAttribute('aria-hidden', 'true');
    mScrim.dataset.open = 'false';
    modalOnOk = null;
    mCancel.textContent = '取消';
    mCancel.style.display = '';
    if (modalReturnFocus && modalReturnFocus.focus) modalReturnFocus.focus();
    modalReturnFocus = null;
  }
  mOk.addEventListener('click', function () {
    var fn = modalOnOk;
    closeModal();
    if (fn) fn();
  });
  mCancel.addEventListener('click', closeModal);
  mScrim.addEventListener('click', closeModal);
  modal.addEventListener('keydown', function (e) {
    if (e.key !== 'Tab') return;
    var first = mCancel, last = mOk.hidden ? mCancel : mOk;
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault(); last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault(); first.focus();
    }
  });

  /* Esc 关闭当前确认框 */
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    if (modal.dataset.open === 'true') closeModal();
    else if (rail.dataset.open === 'true') closeRail(0);
  });

  /* ============================================================ 降级 ---- */
  var degrade = $('trDegrade');
  var DEGRADE_CMD = 'python tools\\serve.py';
  var DEGRADE_MSG = '当前是离线页面。要使用这些工具，请双击「打开看板.cmd」。';

  function goOffline(reason) {
    ONLINE = false;
    rail.dataset.degraded = 'true';
    ['refresh', 'login', 'cleanup'].forEach(function (k) {
      lockBtn(k, false);
      BTNS[k].el.setAttribute('aria-disabled', 'true');
    });
    $('trDegradeText').textContent = reason ? (reason + '。' + DEGRADE_MSG) : DEGRADE_MSG;
    degrade.dataset.open = reason ? 'true' : 'false';
  }
  function hintDegrade() { degrade.dataset.open = 'true'; }

  $('trDegradeX').addEventListener('click', function () { degrade.dataset.open = 'false'; });
  $('trDegradeCopy').addEventListener('click', function () {
    var btn = this;
    var done = function () { btn.textContent = '已复制'; setTimeout(function () { btn.textContent = '复制命令'; }, 1400); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(DEGRADE_CMD).then(done, function () { fallbackCopy(btn, done); });
    } else { fallbackCopy(btn, done); }
  });
  function fallbackCopy(btn, done) {
    var ta = document.createElement('textarea');
    ta.value = DEGRADE_CMD;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); done(); } catch (e) { btn.textContent = DEGRADE_CMD; }
    document.body.removeChild(ta);
  }

  /* ============================================================ 初始化 ---- */
  BTNS.refresh.el.addEventListener('click', doRefresh);
  BTNS.login.el.addEventListener('click', doLogin);
  BTNS.cleanup.el.addEventListener('click', doCleanup);

  if (ONLINE) {
    refreshStatus();
    /* 别的标签页发起了任务，这边也要能看见 */
    setInterval(function () {
      if (!polling.refresh && !polling.login) refreshStatus();
    }, 9000);
  } else {
    goOffline('');
  }

  /* 暴露给自检（file:// 下拿不到桥，只能靠这个判断工具轨自身状态） */
  window.__TOOLRAIL__ = {
    online: ONLINE,
    setBtn: setBtn,
    lockBtn: lockBtn,
    confirmBox: confirmBox,
    closeModal: closeModal,
    showProgress: showProgress,
    updateProgress: updateProgress,
    hideProgress: hideProgress,
    fmtSize: fmtSize,
    /* 失败态保鲜期的语义（见 isAlert）。暴露出来是给自检用的：断言「保鲜期内
       不放行」+「时长有界」—— 谁要是把 TTL 改回无限，就等于让按钮重新永久
       粘住，测试要拦住。 */
    isAlert: isAlert,
    alertTTL: ALERT_TTL,
    isOpen: function () { return rail.dataset.open === 'true'; },
    degraded: function () { return rail.dataset.degraded === 'true'; }
  };
})();
