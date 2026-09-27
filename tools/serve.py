# -*- coding: utf-8 -*-
"""本地桥：一个进程、一个端口，既 serve 静态看板又提供导入链路 API。

为什么要有它
    看板本身是纯静态的（双击 `index.html` 就能开，README 明文承诺，不能破坏）。
    但「刷新数据 / 登录小米 / 清理下载包」要跑子进程、碰文件系统 —— 网页做不到。
    所以额外起一个本机服务，**同一端口**既发静态文件又收这几个动作，网页同源直调，
    不用 `file://` 那套跨源降级。

安全（这是本机服务，边界要划清）
    * 只绑 `127.0.0.1` —— 局域网里别的机器连不到。
    * 启动时生成**随机 token**，注入到 `index.html` 里；所有 `/api/*` 都校验它。
      防止「你正开着的别的网页」偷偷往 localhost 发请求（CSRF）。
    * 只允许 GET/POST，不做 OPTIONS 跨源放行。

接口
    GET  /                      → 看板本体，token 已注入（悬浮工具轨就在这一页里）
    GET  /api/status            → 数据概况 + 忙标志 + 锁定原因 + 登录态 + 清理范围
    POST /api/refresh           → 开跑邮箱导入（返回 kind，之后轮询 /api/log）
    POST /api/login             → 唤起浏览器登录小米
    POST /api/cleanup/preview   → 清理预演（**只列不删**）
    POST /api/cleanup/apply     → 真删（调用方必须先展示过 preview）
    GET  /api/log?kind=&from=   → 增量拉取长任务输出（游标式）
    POST /api/abort?kind=       → 中止长任务
    GET  /api/versions          → 数据版本时间线 + 差异
    POST /api/rollback?dryRun=  → 回滚（预演 / 真回滚）
    POST /api/quit              → 关掉这个服务（工具轨里的「退出」）

起法（别在工具调用里后台起 —— 会话一结束子进程就被回收）：
    双击 `打开看板.cmd`，或手动 `python tools/serve.py`
"""
from __future__ import annotations

import json
from pathlib import Path
import secrets
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tasks  # noqa: E402

PROJECT_DIR = Path(__file__).resolve().parent.parent

# 只发这些后缀的静态文件。白名单而非黑名单 —— 别把 local/（里面是浏览器 profile、
# 邮箱凭证、日志）顺手发出去了。
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}

# 静态服务的白名单。**根目录只放下面这几个文件**（看板三件套 + 数据 + 图标），
# 不是整个项目 —— `local/` 里是浏览器 profile、邮箱凭证、导入日志，`tools/` 里是脚本，
# 都不能被网页取到。子目录一个都不开放。
ROOT_FILES = {
    "index.html", "styles.css", "app.js", "toolrail.js",
    "data.js", "data.prev.js",
    "favicon.ico",
}
SERVE_DIRS = ()

# 只有这一页要注入 token，也只改它一条路由（`/` 与 `/index.html`）。
INJECT_FILE = "index.html"
# 注入锚点：index.html 头部留的一个注释，服务端把它替成 <script>。
BOOTSTRAP_ANCHOR = "<!--BRIDGE_BOOTSTRAP-->"

_TOKEN = secrets.token_urlsafe(24)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------- 处理器
class Handler(BaseHTTPRequestHandler):
    server_version = "run-analytics-bridge/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 基础工具
    def log_message(self, fmt, *args):
        # 默认会往 stderr 打每一行请求；起在窗口后面时太吵。要排查再加 --verbose。
        if getattr(self.server, "verbose", False):
            sys.stderr.write("[serve] " + (fmt % args) + "\n")

    def _send(self, code: int, body: bytes, ctype: str, extra: dict = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 本机服务，只服务自己 —— 明确禁掉被别的源嵌进 iframe
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _error(self, code: int, message: str):
        self._json({"ok": False, "error": message}, code)

    def _token_ok(self, query: dict) -> bool:
        """token 校验。取 header 优先（`X-Bridge-Token`），其次 query（方便 curl 调试）。"""
        given = self.headers.get("X-Bridge-Token") or (query.get("token") or [""])[0]
        return secrets.compare_digest(str(given), _TOKEN)

    def _query(self) -> dict:
        parsed = urllib.parse.urlparse(self.path)
        return parsed, urllib.parse.parse_qs(parsed.query)

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > 1_000_000:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---- 路由
    def do_GET(self):
        parsed, query = self._query()
        route = parsed.path

        if route.startswith("/api/"):
            if not self._token_ok(query):
                return self._error(403, "token 不对（这个接口只能从本机看板页调用）")
            return self._api_get(route, query)

        return self._serve_static(route, query)

    do_HEAD = do_GET

    def do_POST(self):
        parsed, query = self._query()
        route = parsed.path
        body = self._read_body()
        # token 也可能放在 body 里（表单式提交）
        if "token" in body:
            query.setdefault("token", [str(body["token"])])
        if not route.startswith("/api/"):
            return self._error(404, "不认识的地址")
        if not self._token_ok(query):
            return self._error(403, "token 不对（这个接口只能从本机看板页调用）")
        return self._api_post(route, query, body)

    # ---- 静态
    def _serve_static(self, route: str, query: dict):
        if route in ("/", "/index.html"):
            return self._serve_index()

        rel = urllib.parse.unquote(route.lstrip("/"))
        target = (PROJECT_DIR / rel).resolve()

        # 白名单校验：根目录只认列出的那几个文件；子目录只认列出的那几个。
        # 路径穿越（`../local/...`）也在这一步被挡住 —— resolve 之后不再位于
        # 允许的目录里，直接 404。
        rel = target.relative_to(PROJECT_DIR) if _is_within(target, PROJECT_DIR) else None
        if rel is None:
            return self._error(404, "找不到 %s" % route)
        parts = rel.parts
        if len(parts) == 1:
            allowed = parts[0] in ROOT_FILES
        else:
            allowed = parts[0] in SERVE_DIRS
        if not allowed or not target.is_file():
            return self._error(404, "找不到 %s" % route)
        if target.suffix.lower() not in STATIC_TYPES:
            return self._error(403, "这个后缀不对外提供：%s" % target.suffix)

        try:
            body = target.read_bytes()
        except OSError as exc:
            return self._error(500, "读不了文件：%s" % exc)
        self._send(200, body, STATIC_TYPES[target.suffix.lower()])

    def _serve_index(self):
        """发看板本体，把 token 与 API 基址注进去。

        注入而不让前端去别处取：token 不出这一层，别的网页拿不到。
        悬浮工具轨（toolrail.js）就用这个 token 调 `/api/*`。

        **file:// 直开时没有这一步** —— 页面里 `window.__BRIDGE__` 不存在，
        工具轨自动降级成灰按钮。看板本体照常显示。
        """
        target = PROJECT_DIR / INJECT_FILE
        if not target.is_file():
            return self._error(404, "找不到 %s" % INJECT_FILE)
        try:
            html = target.read_text(encoding="utf-8")
        except OSError as exc:
            return self._error(500, "读不了看板页：%s" % exc)
        if BOOTSTRAP_ANCHOR not in html:
            return self._error(500, "index.html 里没有 %s 锚点，注入会静默失效" % BOOTSTRAP_ANCHOR)
        bootstrap = json.dumps({
            "token": _TOKEN,
            "api": "/api",
            "port": self.server.server_address[1],
            "project": str(PROJECT_DIR),
        }, ensure_ascii=False)
        html = html.replace(
            BOOTSTRAP_ANCHOR,
            "<script>window.__BRIDGE__ = %s;</script>" % bootstrap)
        self._send(200, html.encode("utf-8"), STATIC_TYPES[".html"])

    # ---- API: GET
    def _api_get(self, route: str, query: dict):
        if route == "/api/status":
            return self._json(tasks.status())
        if route == "/api/log":
            kind = (query.get("kind") or ["refresh"])[0]
            try:
                since = int((query.get("from") or ["0"])[0])
            except ValueError:
                since = 0
            runner = tasks.current_runner(kind)
            if runner is None:
                return self._json({"ok": True, "kind": kind, "from": since,
                                   "cursor": 0, "lines": [], "alive": False,
                                   "exitCode": None, "elapsed": 0})
            payload = runner.poll(since)
            payload["ok"] = True
            return self._json(payload)
        if route == "/api/versions":
            return self._json(tasks.versions())
        if route == "/api/cleanup/preview":
            return self._json({"ok": True, "preview": tasks.cleanup_preview()})
        return self._error(404, "不认识的接口：%s" % route)

    # ---- API: POST
    def _api_post(self, route: str, query: dict, body: dict):
        if route == "/api/refresh":
            try:
                before = tasks.data_mtime()
                runner = tasks.refresh()
            except RuntimeError as exc:
                return self._json({"ok": False, "error": str(exc)}, 409)
            self.server.pending_mtime = before      # noqa: attribute on server
            return self._json({"ok": True, "kind": runner.kind,
                               "started": runner.started})

        if route == "/api/login":
            try:
                runner = tasks.login()
            except RuntimeError as exc:
                return self._json({"ok": False, "error": str(exc)}, 409)
            return self._json({"ok": True, "kind": runner.kind,
                               "started": runner.started})

        if route == "/api/abort":
            kind = (query.get("kind") or [body.get("kind") or "refresh"])[0]
            runner = tasks.current_runner(kind)
            if runner is None:
                return self._json({"ok": False, "error": "当前没有在跑的任务"})
            runner.abort()
            return self._json({"ok": True, "aborted": kind})

        if route == "/api/cleanup/apply":
            return self._json(tasks.cleanup_apply())

        if route == "/api/rollback":
            dry = (query.get("dryRun") or [str(body.get("dryRun", True))])[0]
            is_dry = str(dry).lower() not in ("false", "0", "no")
            # 真回滚需要第二次确认：body 里得带 confirm=true
            if not is_dry and not body.get("confirm"):
                return self._json({"ok": False,
                                   "error": "真回滚要带 confirm=true（先看预演再确认）"}, 400)
            result = tasks.rollback(dry_run=is_dry)
            return self._json(result)

        if route == "/api/quit":
            self._json({"ok": True, "note": "服务要关了，这个窗口可以关掉。"})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        if route == "/api/heartbeat":
            return self._json({"ok": True, "ts": time.time()})

        return self._error(404, "不认识的接口：%s" % route)


# 端口起点与跨度。**必须与总控台 runner-lab 的 PREFERRED_PORT / PORT_SCAN_RANGE 一致** ——
# runner 起完桥之后就是在这整段里找「谁在应答」，区间对不上会「桥稳稳跑着却报起不来」。
# 8917 起：避开 apex-console 后端 8765 与 apex-worker 8766。
PORT_BASE = 8917
PORT_SPAN = 44


def _occupied(port: int) -> bool:
    """这个端口上是不是已经有人应答。

    **判定只能靠 connect，不能靠 bind。** Windows 的 `SO_REUSEADDR` 语义和 Linux
    不一样：它允许第二个 socket 绑到同一个地址上，所以「能 bind」根本不等于
    「没人用」。2026-09-27 实测被这个坑了整整一轮 —— 一个由别的解释器起的残余桥
    把正常桥挤到了后面，而总控台的 runner 扫到「8917 有人应答」就复用了那个坏的，
    于是页面里三个按钮全部报错（「没完成（1）」）。connect 得通才是唯一可信的判据。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.25)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _pick_port(preferred: int = PORT_BASE) -> int:
    """从 8917 起找一个空端口（占用就往后找）。

    端口由**桥自己**挑，`打开看板.cmd` 不再另跑一遍 netstat 扫描 ——
    那是启动慢的一个来源，而且两处各扫一遍本来就没必要。

    占用判定先 connect（见 `_occupied`），再 bind 复核：**bind 那一关不能带
    `SO_REUSEADDR`**，否则它会把已占用的端口也判成「可用」，等于放劫持进来。
    """
    for offset in range(0, PORT_SPAN):
        port = preferred + offset
        if _occupied(port):
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("%d–%d 都被占用了，起不来" % (preferred, preferred + PORT_SPAN - 1))


def _selftest() -> int:
    """离线自检：只验能离线验的东西（路由、白名单、token 校验），不联网、不真删。"""
    fails = []

    def ok(name, cond):
        if not cond:
            fails.append(name)

    # --- 静态白名单：根目录只认 ROOT_FILES 里那几个，local/tools 必须挡住 ---
    def allowed(rel):
        target = (PROJECT_DIR / rel).resolve()
        if not _is_within(target, PROJECT_DIR):
            return False
        parts = target.relative_to(PROJECT_DIR).parts
        if len(parts) == 1:
            return parts[0] in ROOT_FILES
        return parts[0] in SERVE_DIRS

    ok("看板本体可发", all(allowed(f) for f in
                       ("index.html", "styles.css", "app.js", "toolrail.js", "data.js")))
    ok("local/ 被挡", not allowed("local/mail_state.json"))
    ok("local/browser-profile 被挡", not allowed("local/browser-profile/Cookies"))
    ok("tools 脚本被挡", not allowed("tools/serve.py"))
    ok("仓库文档被挡", not allowed("README.md") and not allowed("PRODUCT_PLAN_V2.md"))
    ok("inbox/ 被挡", not allowed("inbox/x.zip"))
    ok("已删的旧资源不会被服务（计划/图表/旧外壳）",
       not any(allowed(f) for f in ("plan-ui.js", "charts.js",
                                    "vendor/chart.umd.min.js", "shell/minimal.html")))
    ok("路径穿越被挡", not _is_within((PROJECT_DIR / ".." / "x").resolve(), PROJECT_DIR))

    # --- token 校验语义 ---
    ok("token 长度够", len(_TOKEN) >= 20)
    ok("token 每次启动都是新生成", hasattr(secrets, "token_urlsafe"))

    # --- 端口挑选 ---
    # 这段区间必须与总控台 runner-lab 的 PREFERRED_PORT / PORT_SCAN_RANGE 一致，
    # 否则 runner 会在错误的段里找桥，表现成「桥明明起来了却报起不来」。
    ok("端口起点是 8917", PORT_BASE == 8917)
    ok("端口跨度与 runner 一致（44）", PORT_SPAN == 44)
    port = _pick_port()
    ok("挑到一个可用端口", isinstance(port, int) and 1024 < port < 65536)
    ok("挑到的端口落在扫描区间内", PORT_BASE <= port < PORT_BASE + PORT_SPAN)

    # 占用判定必须靠 connect。Windows 的 SO_REUSEADDR 会让 bind 在**已占用**的端口上
    # 假成功 —— 那正是 2026-09-27「桥被残余实例劫持、总控台复用错的那个」的根因。
    # 这条断言就是防止哪天有人把 SO_REUSEADDR 加回来。
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        held = holder.getsockname()[1]
        ok("已经有人应答的端口被判成占用", _occupied(held))
        ok("_pick_port 会绕开已占用的端口", _pick_port(held) != held)
        # 光靠 _pick_port 不够：显式 --port 会绕过它，那时"绑得上"必须等于"没人用"。
        ok("服务端不接受 SO_REUSEADDR", _Bridge.allow_reuse_address is False)
        try:
            _Bridge(("127.0.0.1", held), Handler).server_close()
            ok("已被占用的端口上再起桥会失败（不会顶替旧桥）", False)
        except OSError:
            ok("已被占用的端口上再起桥会失败（不会顶替旧桥）", True)
    ok("没人应答的端口被判成空闲", not _occupied(_pick_port()))

    # --- 注入目标：看板本体 ---
    index = PROJECT_DIR / INJECT_FILE
    ok("index.html 在", index.is_file())
    if index.is_file():
        html = index.read_text(encoding="utf-8")
        ok("index.html 有 token 注入锚点", BOOTSTRAP_ANCHOR in html)
        for hook in ("toolrail", "trHot", "trStack", "trRefresh", "trLogin",
                     "trCleanup", "trProgress", "trModal", "trDegrade"):
            ok("index.html 有 #%s" % hook, ('id="%s"' % hook) in html)
        ok("index.html 引了 toolrail.js", 'src="toolrail.js"' in html)
        # 降级不能靠 JS 兜底 —— 锚点必须在静态文件里，服务端才有东西可替
        ok("index.html 一个字节不依赖服务端改写",
           'href="styles.css"' in html and 'src="app.js"' in html)

    # --- toolrail.js 里的关键约定 ---
    rail = PROJECT_DIR / "toolrail.js"
    ok("toolrail.js 在", rail.is_file())
    if rail.is_file():
        js = rail.read_text(encoding="utf-8")
        ok("toolrail.js 读 __BRIDGE__", "__BRIDGE__" in js)
        ok("toolrail.js 带 token 头", "X-Bridge-Token" in js)
        ok("toolrail.js 不再显示旧版回滚", "/versions/rollback" not in js)
        ok("toolrail.js 清理先预演", "/cleanup/preview" in js)
        ok("toolrail.js 有降级态", "degraded" in js)
        ok("toolrail.js 有轮询游标", "'/log?kind='" in js or "/log?kind=" in js)

    if fails:
        print("=== SERVE SELFTEST FAIL: %d 项 ===" % len(fails))
        for name in fails:
            print("    - %s" % name)
        return 1
    print("=== SERVE SELFTEST PASS ===")
    return 0


def open_browser(url: str) -> None:
    """绑好端口之后，自己开一次系统浏览器。

    以前这一步在 `打开看板.cmd` 里，靠一个 PowerShell 每 250ms 探端口再开 ——
    PowerShell 冷启动本身就要 1–3 秒，而且总控台那条路 `runner.py` 也会开一次，
    浏览器被开成两个标签页。挪进来之后：**零额外进程、零轮询，全链路只开这一次**。
    """
    import os
    try:
        if os.name == "nt":
            os.startfile(url)           # 走默认浏览器，不抢前台
        else:
            import webbrowser
            webbrowser.open(url)
    except Exception:
        pass                            # 开不了浏览器不该拖垮服务，URL 已经打在横幅里了


class _Bridge(ThreadingHTTPServer):
    """本地桥的 HTTP 服务端 —— **故意不接受 `SO_REUSEADDR`**。

    Windows 上 `SO_REUSEADDR` 的语义和 Linux 不同：它允许**第二个 socket 绑到已被
    占用的地址**上，于是新桥能"绑成功"，实际应答的却还是租住在这个端口上的旧桥。
    2026-09-27 实测踩到：显式 `--port 8917` 起桥（上次会话的残余桥还挂在上面），
    横幅一切正常，但带着**正确 token** 的 `/api/status` 依然 403 —— 因为回话的是旧桥，
    它认的是它自己的 token。关掉这个选项，端口被占就直接抛错，不再有"两个桥一个口"。
    """

    allow_reuse_address = False
    daemon_threads = True


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="run-analytics 本地桥（静态 + 导入链路 API）")
    parser.add_argument("--port", type=int, default=0,
                        help="指定端口（默认从 %d 起自动找）" % PORT_BASE)
    parser.add_argument("--verbose", action="store_true", help="打印每条请求")
    parser.add_argument("--print-token", action="store_true", help="只打印 token 后退出（调试）")
    parser.add_argument("--open-browser", action="store_true",
                        help="端口绑好后自己开一次浏览器（打开看板.cmd 走这条）")
    parser.add_argument("--selftest", action="store_true", help="离线自检")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()
    if args.print_token:
        print(_TOKEN)
        return 0

    # 显式指定端口时不猜：已经有人应答就直说，别靠 SO_REUSEADDR 硬绑上去
    # （硬绑的后果是"服务起来了但应答的是别人"，排查起来极难，见 `_Bridge`）。
    if args.port and _occupied(args.port):
        print("端口 %d 已经有人应答了 —— 多半是上一次留下的桥。" % args.port)
        print("先关掉它。或者干脆不给 --port，让桥自己从 %d 起找空口。" % PORT_BASE)
        return 2

    port = args.port or _pick_port()
    server = _Bridge(("127.0.0.1", port), Handler)
    server.verbose = args.verbose

    url = "http://127.0.0.1:%d/" % port
    print("run-analytics 本地桥已起来")
    print("  看板（工具轨已激活）：%s" % url)
    print("  数据版本日志：  %s" % tasks._journal_path())
    # token 打进启动日志：排查时要用它调 API（正常使用不需要看）
    print("  token（调试用）：%s" % _TOKEN)
    print("按 Ctrl+C 关掉。")
    sys.stdout.flush()

    # 端口已经绑好（HTTPServer 构造时就 bind+listen 了），此刻服务一定在应答 ——
    # 不用轮询、不用等待，直接开。
    if args.open_browser:
        open_browser(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已关掉。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
