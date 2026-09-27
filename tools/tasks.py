# -*- coding: utf-8 -*-
"""导入链路的业务层：四个动作，与界面无关。

为什么单独抽出来
    原先这四个动作的实现都长在 `import_gui.py`（Tkinter 窗口）里，逻辑和控件缠在
    一起 —— 想给看板工具轨复用，就只能把窗口一起端过去。现在拆成这一层：
    窗口和本地桥（`tools/serve.py`）都调它，**同一条链路只有一份实现**。

四个动作（对齐 Tkinter 窗口那四个按钮）
    1. `refresh()`    → 从邮箱取包导入（长任务，产出流式日志）
    2. `login()`      → 唤起系统浏览器登录小米（长任务，产出流式日志）
    3. `versions()`   → 数据版本时间线 + 与上一版差异
    4. `cleanup_*()`  → 清理下载的包和记录（预演 / 真删分离）

**两条硬约束照搬窗口，别改**：
    * 登录的浏览器和自动下载用的浏览器**同一个 profile**。`auto_download.py` 一启动
      就 `close_lingering_browser()`，所以登录窗口开着时，刷新和清理都必须锁住
      （见 `guard()` 与 `busy_state()`）。「数据版本」不碰浏览器、不改文件，任何时候可用。
    * 删数据不可逆：`cleanup_preview()` 只列不删，`cleanup_apply()` 才真删。
      界面层负责「先给用户看清单，再让他确认」。看板数据 `data.js` 不在范围内。

这里**不 import tkinter**，也不碰任何界面状态 —— 纯函数 + 子进程编排。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cleanup  # noqa: E402

PROJECT_DIR = Path(__file__).resolve().parent.parent
TOOLS_DIR = Path(__file__).resolve().parent

FETCH_SCRIPT = TOOLS_DIR / "fetch_from_mail.py"
LOGIN_SCRIPT = TOOLS_DIR / "xiaomi_login.py"
FETCH_LAUNCHER = PROJECT_DIR / "从邮箱导入.cmd"
LOGIN_LAUNCHER = PROJECT_DIR / "登录小米账号.cmd"

DATA_JS = PROJECT_DIR / "data.js"
DATA_PREV = PROJECT_DIR / "data.prev.js"

# 长任务上限。窗口里的取值照搬过来，别各写一套。
REFRESH_TIMEOUT = 1800       # 30 分钟：等邮件 + 下载 + 解析
LOGIN_TIMEOUT = 7200         # 2 小时：够走完短信 + 申请导出


# ---------------------------------------------------------------- 忙标志
# 进程级单例。窗口和本地桥可能同时活着（双路径并存），所以标志放在模块里：
# 无论从哪边发起，另一边都能看见「已经有一条链路在跑」。
_LOCK = threading.Lock()
_BUSY = {"refresh": False, "login": False}


def busy_state() -> dict:
    """当前哪条链路在跑。给状态查询用。"""
    with _LOCK:
        return dict(_BUSY)


def _acquire(kind: str) -> bool:
    """占用某条链路。已经有任何一条在跑就拒绝 —— 两条链路不能并行（共用 profile）。"""
    with _LOCK:
        if _BUSY["refresh"] or _BUSY["login"]:
            return False
        _BUSY[kind] = True
        return True


def _release(kind: str) -> None:
    with _LOCK:
        _BUSY[kind] = False


def guard(kind: str) -> str:
    """能不能发起某个动作。返回空串 = 放行；否则是拒绝理由（人话）。

    锁定逻辑照搬 `import_gui._sync_buttons`：
        * 登录进行中 → 锁「刷新数据」和「清理下载包」（会杀用户正在操作的窗口）
        * 刷新进行中 → 锁「登录小米」和「清理下载包」
        * 「数据版本」任何时候都放行（不碰浏览器、不改文件）
    """
    with _LOCK:
        if kind == "versions":
            return ""
        if _BUSY["login"]:
            return "隐私中心的浏览器窗口还开着。先关掉那个窗口，再试这里。"
        if _BUSY["refresh"]:
            return "邮箱导入正在跑，等它结束。"
        return ""


# ---------------------------------------------------------------- ① 状态
def _read_data_payload(path: Path):
    """读 `data.js` / `data.prev.js` 的 JSON 体。读不动返回 None。"""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    if "=" not in text:
        return None
    body = text.split("=", 1)[1].rstrip().rstrip(";")
    try:
        return json.loads(body)
    except Exception:
        return None


def data_summary(path: Path = DATA_JS) -> dict:
    """一份数据的概况：场次数 / 首末日期 / 生成时间 / 文件大小。"""
    if not path.is_file():
        return {}
    payload = _read_data_payload(path)
    stat = path.stat()
    info = {
        "file": path.name,
        "bytes": stat.st_size,
        "mtime": int(stat.st_mtime),
    }
    if payload:
        runs = payload.get("runs") or []
        dates = [r.get("date") for r in runs if r.get("date")]
        info.update({
            "generated": payload.get("generated") or "",
            "runs": len(runs),
            "first": min(dates) if dates else "",
            "last": max(dates) if dates else "",
        })
    else:
        info.update({"generated": "", "runs": 0, "first": "", "last": ""})
    return info


def dashboard_meta() -> str:
    """给「已有历史数据」那句提示用的一句话摘要（窗口和工具轨共用）。"""
    info = data_summary(DATA_JS)
    if not info or not info.get("last"):
        return ""
    text = "共 %d 场 · 截至 %s" % (info["runs"], info["last"])
    if info.get("generated"):
        text += " · 生成于 %s" % info["generated"]
    return text


def status() -> dict:
    """工具轨初始化时拉一次：数据概况 + 两次导入的关系 + 忙标志 + 小米登录态。

    这是「一次请求把工具轨画出来」的接口，别让它去逐个问。
    """
    busy = busy_state()
    current = data_summary(DATA_JS)
    previous = data_summary(DATA_PREV)
    return {
        "ok": True,
        "data": current,
        "previous": previous,
        "hasData": bool(current),
        "canRollback": DATA_PREV.is_file() and DATA_JS.is_file(),
        "busy": busy,
        "idle": not (busy["refresh"] or busy["login"]),
        "locks": {
            "refresh": guard("refresh"),
            "login": guard("login"),
            "cleanup": guard("cleanup"),
            "versions": "",
        },
        "xiaomi": xiaomi_state(),
        "cleanup": cleanup_summary(),
    }


def xiaomi_state() -> dict:
    """小米登录态：按浏览器 profile 里有没有登录痕迹判断，只做「角标绿点」这种粗判断。

    不解析 cookie 内容（那是登录脚本的事），只看 profile 目录在不在、有多大 ——
    够回答「大概登录过没有」，判错也不会造成后果。
    """
    profile = PROJECT_DIR / "local" / "browser-profile"
    if not profile.is_dir():
        return {"loggedIn": False, "reason": "还没有浏览器 profile"}
    cookies = list(profile.rglob("Cookies")) + list(profile.rglob("cookies.sqlite"))
    if not cookies:
        return {"loggedIn": False, "reason": "profile 里还没有 cookie"}
    newest = max(c.stat().st_mtime for c in cookies)
    return {
        "loggedIn": True,
        "reason": "profile 里有登录痕迹",
        "ageDays": round((time.time() - newest) / 86400, 1),
    }


def cleanup_summary() -> dict:
    """清理范围概况（**只统计不删**），给工具轨的二次确认弹窗用。"""
    packages, state = cleanup.collect()
    items = []
    total = 0
    for path in packages:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        total += size
        items.append({"kind": "package", "path": str(path), "name": path.name, "bytes": size})
    if state is not None:
        items.append({
            "kind": "state",
            "path": str(state),
            "name": state.name,
            "bytes": state.stat().st_size if state.is_file() else 0,
            "detail": cleanup.describe_state(state),
        })
    return {
        "count": len(items),
        "bytes": total,
        "items": items,
        "note": "看板数据（data.js）不会被动。",
    }


# ---------------------------------------------------------------- 长任务执行
class Runner:
    """跑一个子进程，逐行收集输出；可被查询进度、可被中止。

    为什么要这个类：窗口那边是把输出投进队列、主线程定时抽；本地桥那边是要把输出
    推给 HTTP 客户端（轮询增量拉取）。两者要的是同一份「有谁在跑、跑到哪一行了」。
    所以这里做成一个简单的缓冲区 + 游标：`poll(since)` 返回从第 since 行起的新输出。
    """

    def __init__(self, kind: str, argv: list, timeout: int) -> None:
        self.kind = kind
        self.argv = argv
        self.timeout = timeout
        self.lines: list = []
        self.exit_code = None
        self.started = time.time()
        self.ended = None
        self.error = ""
        self._proc = None
        self._thread = None

    # ---- 生命周期
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["RUN_ANALYTICS_NO_PAUSE"] = "1"
        try:
            self._proc = subprocess.Popen(
                self.argv,
                cwd=str(PROJECT_DIR),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                               if os.name == "nt" else 0),
            )
            for raw in self._proc.stdout or []:
                self.lines.append(raw.rstrip("\n"))
            code = self._proc.wait(timeout=self.timeout)
            self.exit_code = code
        except subprocess.TimeoutExpired:
            if self._proc is not None:
                self._proc.kill()
            self.lines.append("超过 %d 秒还没结束，已中止。" % self.timeout)
            self.exit_code = -1
        except Exception as exc:
            self.lines.append("启动失败：%s: %s" % (type(exc).__name__, exc))
            self.exit_code = -1
        finally:
            self.ended = time.time()
            _release(self.kind)

    def abort(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.kill()
            except Exception:
                pass

    # ---- 查询
    @property
    def alive(self) -> bool:
        return self.ended is None

    def poll(self, since: int = 0) -> dict:
        """返回从第 since 行起的新输出 + 是否结束。"""
        new = self.lines[since:]
        return {
            "kind": self.kind,
            "from": since,
            "cursor": len(self.lines),
            "lines": new,
            "alive": self.alive,
            "exitCode": self.exit_code,
            "elapsed": round((self.ended or time.time()) - self.started, 1),
            "error": self.error,
        }


# 当前在跑的那个（一次只允许一条链路，所以单槽够用）
_CURRENT: dict = {"refresh": None, "login": None}


def current_runner(kind: str):
    return _CURRENT.get(kind)


def _spawn(kind: str, argv: list, timeout: int) -> Runner:
    if not _acquire(kind):
        raise RuntimeError(guard(kind) or "已经有一条链路在跑")
    runner = Runner(kind, argv, timeout)
    _CURRENT[kind] = runner
    runner.start()
    return runner


def _launcher_argv(launcher: Path, script: Path, args=None) -> list:
    """Run through the same launcher a user double-clicks.

    On Windows the `.cmd` file selects the shared Playwright venv and owns the
    Python fallback order. Calling the Python file with `sys.executable` here
    bypassed that choice, so the desktop button could exit with code 1 even
    while the matching launcher worked. The launchers skip their interactive
    `pause` only for this child process and return the real script exit code.
    """
    args = list(args or [])
    if os.name == "nt":
        if not launcher.is_file():
            raise RuntimeError("找不到启动脚本：%s" % launcher)
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "call",
                launcher.name] + args
    return [sys.executable, "-u", str(script)] + args


# ---------------------------------------------------------------- ② 刷新数据
def refresh(argv_extra=None) -> Runner:
    """从邮箱取包导入。长任务，返回值可轮询输出。

    取密码 → 下载 → 导入；完成后由看板自动重载已打开的数据页。
    """
    reason = guard("refresh")
    if reason:
        raise RuntimeError(reason)
    if not FETCH_SCRIPT.is_file():
        raise RuntimeError("找不到 %s" % FETCH_SCRIPT)
    argv = _launcher_argv(FETCH_LAUNCHER, FETCH_SCRIPT, argv_extra)
    return _spawn("refresh", argv, REFRESH_TIMEOUT)


def refresh_finished_ok(runner: Runner, before_mtime) -> tuple:
    """导入结束后的核对：**不能只信返回码**。

    返回码 0 只代表「fetch_from_mail 说导入了」。必须亲眼核对 `data.js` 真的被重写过，
    否则我们看到的还是上一次的看板。真出过这个 bug（窗口里那段注释就是从这来的）。

    返回 (是否成功, 要说的话)。`before_mtime` 是发起前记下的 mtime。
    """
    if runner.exit_code == 0:
        try:
            now = DATA_JS.stat().st_mtime if DATA_JS.is_file() else None
        except OSError:
            now = None
        if before_mtime is not None and now is not None and now <= before_mtime:
            return False, "返回码说成功，但看板文件没有被改动 —— 按未导入处理。"
        info = data_summary(DATA_JS)
        return True, "导入完成，当前共 %d 场。" % info.get("runs", 0)
    if runner.exit_code == 9:
        return False, "没有新的导出邮件，什么都没导入 —— 看板没有变动。"
    if runner.exit_code == 8:
        return False, "自动下载没完成，什么都没导入 —— 看板没有变动。"
    return False, "导入未完成（返回码 %s），看板没有变动。" % runner.exit_code


def data_mtime():
    try:
        return DATA_JS.stat().st_mtime if DATA_JS.is_file() else None
    except OSError:
        return None


# ---------------------------------------------------------------- ③ 登录小米
def login() -> Runner:
    """唤起系统浏览器登录小米（自动勾协议 + 填表）。

    **必须用真浏览器**：跨站、要过短信，内嵌 WebView 做不了（工具轨那侧也是
    「唤起系统浏览器」而不是内嵌）。脚本只做到登录为止 —— 隐私中心、申请导出由人做。
    窗口关掉子进程才结束，所以是长任务。
    """
    reason = guard("login")
    if reason:
        raise RuntimeError(reason)
    if not LOGIN_SCRIPT.is_file():
        raise RuntimeError("找不到 %s" % LOGIN_SCRIPT)
    argv = _launcher_argv(LOGIN_LAUNCHER, LOGIN_SCRIPT)
    return _spawn("login", argv, LOGIN_TIMEOUT)


LOGIN_MESSAGES = {
    0: ("ok", ["登录完成，窗口交给你了。",
               "接下来你自己来：进隐私中心 → 过短信 → 申请导出，然后关掉窗口。",
               "等小米把导出邮件发来（通常几小时），再点「刷新数据」。"]),
    2: ("err", ["要存密码得先有账号：加 --account 你的小米账号。",
                "不过不存密码也能用：再点一次「登录小米」，在窗口里登录即可。"]),
    3: ("err", ["登录没成功（等超时了）。再点一次，在窗口里登完即可。"]),
    5: ("err", ["浏览器没起来或被中途关掉了。再点一次即可。"]),
}


def login_message(code) -> tuple:
    """登录结束后的收尾话术。**绝不报「导入成功」** —— 这一步一个字节都没动。"""
    if code in LOGIN_MESSAGES:
        return LOGIN_MESSAGES[code]
    return ("err", ["这一步没完成（返回码 %s），看板数据没有任何改动。" % code])


# ---------------------------------------------------------------- ④ 数据版本
def _journal_path() -> Path:
    return PROJECT_DIR / "local" / "import_journal.json"


def _load_journal() -> list:
    path = _journal_path()
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return payload if isinstance(payload, list) else []


def _save_journal(entries: list) -> None:
    path = _journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(entries[-50:], ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except OSError:
        pass


def record_import() -> None:
    """导入成功后记一笔，供时间线用。**记的是事实**（当场核对的 mtime + 场次数）。"""
    info = data_summary(DATA_JS)
    if not info:
        return
    entries = _load_journal()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(info.get("mtime") or time.time()))
    if entries and entries[-1].get("mtime") == info.get("mtime"):
        return                                     # 同一次导入不重复记
    entries.append({
        "at": stamp,
        "mtime": info.get("mtime"),
        "runs": info.get("runs"),
        "first": info.get("first"),
        "last": info.get("last"),
        "generated": info.get("generated"),
        "bytes": info.get("bytes"),
    })
    _save_journal(entries)


def _diff(before: dict, after: dict) -> dict:
    """两份数据的差异：场次增减 + 新增日期区间 + 文件大小变化。"""
    if not before or not after:
        return {}
    return {
        "runsDelta": (after.get("runs") or 0) - (before.get("runs") or 0),
        "bytesDelta": (after.get("bytes") or 0) - (before.get("bytes") or 0),
        "firstBefore": before.get("first") or "",
        "firstAfter": after.get("first") or "",
        "lastBefore": before.get("last") or "",
        "lastAfter": after.get("last") or "",
        "generatedBefore": before.get("generated") or "",
        "generatedAfter": after.get("generated") or "",
    }


def versions() -> dict:
    """数据版本抽屉的数据：当前版 + 上一版 + 差异 + 时间线。

    时间线优先读导入日志（`local/import_journal.json`）；日志还没建立时，
    用「当前版 + 上一版」两条兜底 —— 至少让抽屉不是空的。
    """
    current = data_summary(DATA_JS)
    previous = data_summary(DATA_PREV)
    journal = _load_journal()

    timeline = list(journal)
    if not timeline:
        for info in (previous, current):
            if info:
                timeline.append({
                    "at": info.get("generated") or "",
                    "mtime": info.get("mtime"),
                    "runs": info.get("runs"),
                    "first": info.get("first"),
                    "last": info.get("last"),
                    "bytes": info.get("bytes"),
                })
    timeline.sort(key=lambda e: e.get("mtime") or 0)

    # 每条时间线上挂「与上一条的差异」
    for i, entry in enumerate(timeline):
        if i == 0:
            entry["diff"] = {}
            continue
        prev = timeline[i - 1]
        entry["diff"] = {
            "runsDelta": (entry.get("runs") or 0) - (prev.get("runs") or 0),
            "bytesDelta": (entry.get("bytes") or 0) - (prev.get("bytes") or 0),
        }

    return {
        "ok": True,
        "current": current,
        "previous": previous,
        "diff": _diff(previous, current),
        "timeline": timeline,
        "canRollback": bool(previous and current),
        "currentIsPrev": False,
    }


def rollback(dry_run: bool = True) -> dict:
    """回滚到上一版（`data.prev.js` → `data.js`）。

    **先把当前版存成 data.prev.js 再覆盖**，这样回滚是可逆的（能来回切）。
    `dry_run=True` 只说明要做什么，不动文件 —— 界面层先预演再二次确认。
    """
    if not DATA_PREV.is_file():
        return {"ok": False, "error": "没有上一版（data.prev.js 不存在），无法回滚。"}
    if not DATA_JS.is_file():
        return {"ok": False, "error": "当前没有 data.js，无法回滚。"}

    current = data_summary(DATA_JS)
    previous = data_summary(DATA_PREV)
    plan = {
        "ok": True,
        "dryRun": dry_run,
        "from": {"runs": current.get("runs"), "last": current.get("last"),
                 "generated": current.get("generated")},
        "to": {"runs": previous.get("runs"), "last": previous.get("last"),
               "generated": previous.get("generated")},
        "note": "回滚后当前版会被存成上一版，可以再切回来。",
    }
    if dry_run:
        return plan

    # 真回滚。顺序是硬的：**两份内容都先读进内存，再落地**。
    # 一旦先写其中一个，另一个的原始内容就没了 —— 先复制再写会把两版写成同一份。
    try:
        prev_text = DATA_PREV.read_text(encoding="utf-8", errors="ignore")
        cur_text = DATA_JS.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return {"ok": False, "error": "读取版本文件失败：%s" % exc}

    try:
        DATA_PREV.write_text(cur_text, encoding="utf-8")   # 当前版 → 上一版
        DATA_JS.write_text(prev_text, encoding="utf-8")    # 上一版 → 当前版
    except OSError as exc:
        # 写第二份失败时尽量把第一份还原，别留半拉状态
        try:
            DATA_PREV.write_text(prev_text, encoding="utf-8")
        except OSError:
            pass
        return {"ok": False, "error": "回滚写入失败（已尝试还原）：%s" % exc}

    _record_rollback(plan)
    return {"ok": True, "swapped": True,
            "now": data_summary(DATA_JS).get("runs"),
            "note": "已回滚。当前版与上一版已互换，可再切回来。"}


def _record_rollback(plan: dict) -> None:
    """把回滚也记进时间线 —— 否则用户回滚完看不到自己干了什么。"""
    entries = _load_journal()
    info = data_summary(DATA_JS)
    entries.append({
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mtime": info.get("mtime"),
        "runs": info.get("runs"),
        "first": info.get("first"),
        "last": info.get("last"),
        "bytes": info.get("bytes"),
        "kind": "rollback",
        "note": "回滚到上一版",
    })
    _save_journal(entries)


# ---------------------------------------------------------------- ⑤ 清理
def cleanup_preview() -> dict:
    """清理预演：**只列不删**。删数据不可逆，界面层必须先把它给用户看。"""
    return cleanup_summary()


def cleanup_apply() -> dict:
    """真删。调用方必须先让用户确认过 preview 里的清单。"""
    reason = guard("cleanup")
    if reason:
        return {"ok": False, "error": reason}
    packages, state = cleanup.collect()
    if not packages and state is None:
        return {"ok": True, "removed": [], "failed": [],
                "note": "没有需要清理的东西。"}
    removed, failed = cleanup.apply(packages, state)
    return {"ok": True, "removed": removed, "failed": failed,
            "note": "看板数据 data.js 没有被改动。"}


# ---------------------------------------------------------------- 自检
def _selftest() -> int:
    """离线自检：不联网、不动文件，只验纯逻辑。"""
    fails = []

    def ok(name, cond):
        if not cond:
            fails.append(name)

    # 锁定逻辑：登录中该锁谁、不锁谁
    with _LOCK:
        _BUSY["login"] = True
    ok("登录中锁刷新", guard("refresh") != "")
    ok("登录中锁清理", guard("cleanup") != "")
    ok("登录中不锁版本", guard("versions") == "")
    ok("登录中锁登录", guard("login") != "")
    with _LOCK:
        _BUSY["login"] = False
        _BUSY["refresh"] = True
    ok("刷新中锁登录", guard("login") != "")
    ok("刷新中锁清理", guard("cleanup") != "")
    ok("刷新中不锁版本", guard("versions") == "")
    with _LOCK:
        _BUSY["refresh"] = False
        _BUSY["login"] = False
    ok("空闲时全放行", guard("refresh") == "" and guard("login") == ""
       and guard("cleanup") == "" and guard("versions") == "")

    # 数据概况
    info = data_summary(DATA_JS)
    ok("能读出 data.js 概况", bool(info))
    ok("场次数是正数", (info.get("runs") or 0) > 0)
    ok("有首末日期", bool(info.get("first")) and bool(info.get("last")))
    ok("file 名对", info.get("file") == "data.js")

    # 版本时间线
    v = versions()
    ok("版本有 current", bool(v.get("current")))
    ok("版本有 diff 键", "diff" in v)
    ok("时间线非空", len(v.get("timeline") or []) >= 1)
    ok("canRollback 是布尔", isinstance(v.get("canRollback"), bool))

    # 回滚预演（不真回滚）
    plan = rollback(dry_run=True)
    ok("回滚预演有 ok 字段", "ok" in plan)

    # 清理预演：只列不删
    before = cleanup.collect()
    preview = cleanup_preview()
    after = cleanup.collect()
    ok("清理预演不改变现场", before == after)
    ok("清理预演有 count", "count" in preview)
    ok("预演里写明不动 data.js", "data.js" in (preview.get("note") or ""))

    # 登录话术
    ok("邮箱命令启动器存在", FETCH_LAUNCHER.is_file())
    ok("小米登录命令启动器存在", LOGIN_LAUNCHER.is_file())
    ok("邮箱 Python 入口存在", FETCH_SCRIPT.is_file())
    ok("小米登录 Python 入口存在", LOGIN_SCRIPT.is_file())
    kind, lines = login_message(0)
    ok("登录返回 0 是 ok", kind == "ok")
    ok("登录话术不误导成导入成功", not any("导入成功" in x for x in lines))
    kind, lines = login_message(9)
    ok("未知返回码归 err", kind == "err")

    # 导入核对：返回码 0 但文件没被改 → 必须判失败
    class FakeRunner:
        exit_code = 0
    mtime = data_mtime()
    good, msg = refresh_finished_ok(FakeRunner(), mtime)
    ok("返回码 0 但文件没变 → 判失败", good is False)
    ok("并说明原因", "没有" in msg or "未" in msg or "改动" in msg)

    class FailRunner:
        exit_code = 9
    good, msg = refresh_finished_ok(FailRunner(), mtime)
    ok("返回码 9 → 判失败", good is False)
    ok("9 的话术是「没有新邮件」", "邮件" in msg)

    if fails:
        print("=== TASKS SELFTEST FAIL: %d 项 ===" % len(fails))
        for name in fails:
            print("    - %s" % name)
        return 1
    print("=== TASKS SELFTEST PASS ===")
    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="导入链路业务层（自检 / 手动触发）")
    parser.add_argument("--selftest", action="store_true", help="离线自检")
    parser.add_argument("--status", action="store_true", help="打印状态 JSON")
    parser.add_argument("--versions", action="store_true", help="打印版本 JSON")
    args = parser.parse_args()

    if args.selftest:
        raise SystemExit(_selftest())
    if args.status:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        raise SystemExit(0)
    if args.versions:
        print(json.dumps(versions(), ensure_ascii=False, indent=2))
        raise SystemExit(0)
    parser.print_help()
    raise SystemExit(0)
