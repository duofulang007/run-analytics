# -*- coding: utf-8 -*-
"""「跑步数据分析」模块的执行入口。

职责很窄：在 run-analytics 里起一个**本地桥**（tools/serve.py，单进程同端口同时
serve 看板与导入链路 API），再打开看板。看板右侧会滑出一条**悬浮工具轨**
（刷新数据 / 登录小米 / 数据版本 / 清理下载包），四个动作由那个窗口里的按钮触发 ——
所以总控台点「运行」= 打开看板 + 工具轨就绪，不再开 Tkinter 窗口。

2026-09-26 变更（多弗朗）：原来这里是打开 `tools/import_gui.py`（Tkinter 窗口）。
现在改成起桥开看板 —— 导入入口收敛进看板本体，少一个窗口。Tkinter 窗口仍保留，
可双击 `从邮箱导入.cmd` 单独开（降级入口）。

2026-09-28 变更（多弗朗）：看板改用 **apex-console 的无边框壳**（`ApexShell.exe --url=<地址>`）
打开，而不是系统浏览器 —— 浏览器窗口带地址栏 / 标签页，像「小窗口」而不像独立应用。
壳不可用时退回浏览器（不让看板打不开）。壳的查找见 `shell_exe()`。

契约（与 modules/apex-launcher/runner.py 一致）：
    runner.py --action {run|stop|restart} --root <run-analytics 目录>
              --result-path <结果 json> --log-path <日志>
期望写 {"ok": true, ...} 并退出 0；失败写 {"ok": false, "error": ...} 并退出 1。

自检（离线，不起服务、不开浏览器）：
    runner.py --action run --root <run-analytics> --selftest
              --result-path <任意> --log-path <任意>
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from bridge_version import REVISION_HEADER, source_revision


SERVE_SCRIPT = Path("tools") / "serve.py"         # 本地桥 + 看板
DASHBOARD = "index.html"
# 真正被启动的那个脚本。**它是唯一的启动真相** —— 端口扫描、解释器挑选、
# 等端口应答再开浏览器都在里面，runner 只负责让它在一个不会随调用方被回收的
# 父进程下跑起来。别在 runner.py 里重抄一遍那些逻辑（抄了就会跟它漂移）。
LAUNCHER_SCRIPT = "打开看板.cmd"

# 端口只是「等下要探活哪个口」的期望值，实际以桥自己挑到的为准。
# `打开看板.cmd` 扫的是 8917..8960；这里从 8917 起，与它对齐。
# 避开了 apex-console 后端（8765）与 apex-worker（8766）。
PREFERRED_PORT = 8917
PORT_SCAN_RANGE = 44

# 等桥应答的上限。取 25 秒：桥要 import 一整套依赖再挑端口、开 HTTP 服务，
# 冷启动（文件缓存空）实测最慢见过 8 秒上下；25 秒留足余量又不会让用户在
# 控制台里干等——module.json 的 timeoutSeconds 是 120，这个必须明显小于它，
# 否则我们会先被控制台 kill，日志里只剩「超时」看不到我们自己的诊断。
WAIT_TIMEOUT = 25.0

# 桥进程的 pid 存在系统临时目录（不进任何仓库），供 stop / restart 精确结束它。
# 与旧的导入窗口 pid 同一个 STATE_DIR 口径（%TEMP%\run-analytics\）。
STATE_DIR = Path(tempfile.gettempdir()) / "run-analytics"
SERVE_PID_FILE = STATE_DIR / "serve.pid"
SERVE_PORT_FILE = STATE_DIR / "serve.port"

# 壳属于 ControlPod 仓库。模块不再从自己的文件层级猜总控台在哪；
# ControlPod 调用时显式传 --console-root，独立启动时仍可用 APEX_SHELL_EXE，
# 找不到壳就退回系统浏览器。
SHELL_EXE_RELATIVES = (
    Path("shell") / "bin" / "Release" / "publish" / "ApexShell.exe",
    Path("shell") / "bin" / "x64" / "Debug" / "net8.0-windows10.0.19041.0"
    / "win-x64" / "ApexShell.exe",
)


def write_log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{message}\n")


def write_result(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )


def creation_flags() -> int:
    """不带窗口跑子进程。

    只用于短命的辅助命令（`netstat` / `taskkill`）——它们会闪黑框。
    **不要拿它启桥**：见 spawn_bridge 的说明。
    """
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------- 端口 / 探活
def pick_port(preferred: int = PREFERRED_PORT) -> int:
    """找一个没被占用的端口。与 tools/serve.py 的 _pick_port 同口径。"""
    for offset in range(PORT_SCAN_RANGE):
        port = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"{preferred}–{preferred + PORT_SCAN_RANGE - 1} 都被占用了，起不来本地桥")


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def read_port() -> int | None:
    try:
        return int(SERVE_PORT_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    """判断进程是否还活着。

    **不要用 `tasklist /FI "PID eq N"`** —— 本机实测它会漏报：过滤器匹配不上时
    输出「信息: 没有运行的任务匹配指定标准」（GBK 编码，按 utf-8 解还全是乱码），
    而那个 PID 明明活着。踩过：这个函数永远返回 False，导致「二次运行不复用旧桥、
    反而再开一个」。
    改用**连一下它该在的端口**判活——对常驻服务来说这才是真正的判据，
    「进程还在但端口没服务」和「进程没了」对用户是一回事。
    """
    port = read_port()
    if port is None:
        return False
    return port_serving(port)


def port_serving(port: int, timeout: float = 1.5) -> bool:
    """端口上有东西在正常应答（不是 502/网关错误，那是代理占着）。"""
    url = f"http://127.0.0.1:{port}/api/status"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as exc:
        # 403 = token 校验没过，但服务本身是活的
        return exc.code == 403
    except Exception:
        return False


def bridge_matches(port: int, root: Path) -> bool:
    """An answering port is reusable only if it loaded this backend revision."""
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{port}/", method="HEAD")
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.headers.get(REVISION_HEADER) == source_revision(root)
    except (OSError, urllib.error.URLError):
        return False


def retire_outdated_bridge(root: Path, port: int, log_path: Path) -> None:
    """Gracefully retire our stale bridge; never kill an arbitrary port/PID or active import."""
    base = f"http://127.0.0.1:{port}"
    with urllib.request.urlopen(base + "/", timeout=3) as response:
        html = response.read().decode("utf-8")
    match = re.search(r"<script>window\.__BRIDGE__\s*=\s*([\s\S]*?);</script>", html)
    bootstrap = json.loads(match.group(1)) if match else {}
    if (not bootstrap.get("project") or Path(bootstrap["project"]).resolve() != root.resolve()
            or bootstrap.get("api") != "/api" or not isinstance(bootstrap.get("token"), str)):
        raise RuntimeError("现有端口不是本项目的服务，无法自动更新。")
    headers = {"X-Bridge-Token": bootstrap["token"]}
    request = urllib.request.Request(base + "/api/status", headers=headers)
    with urllib.request.urlopen(request, timeout=3) as response:
        status = json.load(response)
    if any((status.get("busy") or {}).values()) or status.get("idle") is False:
        raise RuntimeError("本地服务需要更新，但刷新或登录仍在运行；完成后再次打开看板。")
    write_log(log_path, "[STEP] 本地服务版本已过期，正常关闭后重新启动")
    request = urllib.request.Request(base + "/api/quit", data=b"{}", headers={**headers, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=3) as response:
        if not json.load(response).get("ok"):
            raise RuntimeError("旧服务未能正常关闭，请在 CPod 重启跑步看板。")
    deadline = time.monotonic() + 5
    while port_serving(port, timeout=.3):
        if time.monotonic() >= deadline:
            raise RuntimeError("旧服务仍在退出，请稍后再次打开看板。")
        time.sleep(.1)


def kill_bridge(log_path: Path) -> str:
    """结束之前起的本地桥；没有就当作无事发生。"""
    pid = read_pid(SERVE_PID_FILE)
    if pid is None:
        write_log(log_path, "[SKIP] 没有记录到本地桥进程")
        return "none"
    if not pid_alive(pid):
        write_log(log_path, f"[SKIP] 本地桥 pid={pid} 已不在")
        SERVE_PID_FILE.unlink(missing_ok=True)
        SERVE_PORT_FILE.unlink(missing_ok=True)
        return "none"
    if os.name == "nt":
        # /T 连子进程一起收（桥会 spawn 导入器子进程）
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=creation_flags(),
        )
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    SERVE_PID_FILE.unlink(missing_ok=True)
    SERVE_PORT_FILE.unlink(missing_ok=True)
    write_log(log_path, f"[STEP] 已关闭本地桥 pid={pid}")
    return "killed"


def bridge_running() -> int | None:
    """若桥已经在跑就返回它的端口，否则 None。

    总控台点两次「运行」不该开出两个桥 —— 端口只有一个，第二个会自己往后找，
    结果是两个进程互不知情。所以先看已经记录的那个端口还通不通。
    """
    port = read_port()
    if port is None:
        return None
    return port if port_serving(port) else None


def spawn_bridge(
    root: Path, port: int, log_path: Path, console_root: Path | None = None
) -> int:
    """把本地桥真正「脱手」起出去，返回反查到的真实 pid（查不到给 0）。

    ★ 为什么不用 `subprocess.Popen(..., DETACHED_PROCESS)` 直接起 python：
      实测那条路起的 pythonw.exe 会**随调用方一起被回收** ——
      表现为「日志写着桥已起来、当场探活 200，过几秒就 502」。
      根因是父进程（总控台后端 / 调试脚本）常处于 Job 对象里，子进程被 Job 连坐；
      `CREATE_BREAKAWAY_FROM_JOB` 只在特定条件下生效，靠不住（踩过）。

    ★ 改用「**调用 `打开看板.cmd` 本体**」，而不是自己拼 `cmd /c start <python>`：
      那个 .cmd 是日常一直在用、已被验证过「关掉它的窗口后服务还活着」的姿势，
      解释器固定使用本项目的 .venv。端口挑选与开浏览器 2026-09-27 起都由桥
      自己负责了（`serve.py` 挑 8917+ 的空口，绑好之后自己 `--open-browser`），
      每件事只有一份实现。里面那句是 `"%PY%" "%ENTRY%" --open-browser`
      —— **前台运行**，所以它需要一个「不会随调用方被回收」的父 cmd：
      外面套 `CREATE_NEW_PROCESS_GROUP` 起的 cmd 就是干这个的（本次调用一结束，
      cmd 与桥一起变孤儿，不再跟着调用方走）。

    ★ 控制台旗标：用 `CREATE_NO_WINDOW`（隐藏控制台），**不要** `DETACHED_PROCESS`。
      DETACHED_PROCESS 下这个 cmd 没有控制台，`start /b` 拉起的 `.cmd` 于是被
      Windows **新分配一个可见控制台** —— 那就是总控台点「运行」时弹出来的黑窗
      （2026-09-27 多弗朗）。CREATE_NO_WINDOW 同样带一个控制台，但**不显示**：
      `start /b` 的子进程继承它，屏幕上干干净净。
      ⚠️ `DETACHED_PROCESS | CREATE_NO_WINDOW` **不能同时给** —— CreateProcess 会
      直接报 invalid argument。以前那句「别用 CREATE_NO_WINDOW」的真相是组合错，
      不是 CREATE_NO_WINDOW 本身有问题。
    """
    launcher = root / LAUNCHER_SCRIPT
    if not launcher.is_file():
        raise RuntimeError(f"看不到启动脚本：{launcher}")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    write_log(log_path, f"[STEP] 启动本地桥（call {LAUNCHER_SCRIPT}，期望端口 {port}）")

    flags = 0
    if os.name == "nt":
        # ★ CREATE_NO_WINDOW，**不是** DETACHED_PROCESS。
        #   DETACHED_PROCESS 下 cmd 拿不到可继承的控制台，Windows 会给它**新分配一个
        #   可见的控制台** —— 总控台点「运行」时那个黑色 cmd 窗口就是这么来的
        #   （2026-09-27 多弗朗：观感难看）。CREATE_NO_WINDOW 同样带一个控制台，
        #   但**不显示**：桥照跑，屏幕上什么都看不见。
        #   ⚠️ 这俩旗标**不能同时给** —— DETACHED_PROCESS | CREATE_NO_WINDOW 会让
        #   CreateProcess 直接报 invalid argument（这就是以前那句「别用 CREATE_NO_WINDOW」
        #   的真相：错的是组合，不是 CREATE_NO_WINDOW 本身）。
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    # `start "" /b` 让 cmd 立刻返回（否则 cmd 会一直等到桥退出），
    # 桥则留在后台继续提供服务。
    # **不能给这个 Popen 挂 stdout/stderr 管道或文件句柄**：桥是孙子进程，
    # 会继承 cmd 的句柄；句柄被我们握着时，桥的输出写不进真终端（`isatty()` 为假，
    # 启动横幅会被缓冲住永不落盘），排查时看不到任何「桥起没起来」的证据。
    # 实测踩过：给 cmd 重定向到 serve.out.log 后，日志里一行自己的输出都没有。
    # 桥的日志改由它自己负责落盘（`打开看板.cmd` 会写 launcher.log，
    # serve.py 的 banner 走它自己的 stdout）。
    command = ["cmd", "/c", "start", "", "/b", "call", str(launcher)]
    bridge_env = os.environ.copy()
    console_shell = shell_exe(console_root)
    if console_shell is not None:
        # serve.py 在子进程里负责首次开窗；把总控台定位结果显式传过去，
        # 避免独立项目反向依赖总控台的固定磁盘路径。
        bridge_env["APEX_SHELL_EXE"] = str(console_shell)
    proc = subprocess.Popen(
        command, cwd=str(root), stdin=subprocess.DEVNULL,
        close_fds=True, creationflags=flags, env=bridge_env,
    )

    # ★ 端口不能认死传入的那个：`打开看板.cmd` 内部会自己扫 8917-8960，
    #   8917 被占它就往后挪。认死端口会「桥明明起来了、我们还在 8917 上空等」，
    #   最后误判成「起不来」。所以在整个扫描区间里找**谁在应答**。
    actual = wait_for_any_bridge(port, root=root)
    if actual is None:
        SERVE_PORT_FILE.unlink(missing_ok=True)
        write_log(log_path, f"[ERROR] {WAIT_TIMEOUT:.0f} 秒内没等到桥在 "
                            f"{port}-{port + PORT_SCAN_RANGE - 1} 上应答")
        return 0

    pid = port_owner(actual) or 0
    SERVE_PORT_FILE.write_text(str(actual), encoding="utf-8")
    SERVE_PID_FILE.write_text(str(pid), encoding="utf-8")
    write_log(
        log_path,
        f"[STEP] 本地桥 pid={pid} 端口={actual}（cmd 启动器 pid={proc.pid}）",
    )
    return pid


def wait_for_any_bridge(port: int, timeout: float = WAIT_TIMEOUT, root: Path | None = None) -> int | None:
    """在 `port` 起的扫描区间里等第一个应答的桥，返回它的真实端口。

    故意扫一整段而不是只试一个：桥的端口由 `打开看板.cmd` 自己挑，
    我们给的只是「起点」。找不到返回 None。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for candidate in range(port, port + PORT_SCAN_RANGE):
            if port_serving(candidate, timeout=0.5) and (root is None or bridge_matches(candidate, root)):
                return candidate
        time.sleep(0.25)
    return None


def port_owner(port: int) -> int | None:
    """反查监听某端口的进程 pid。用 netstat（本机实测 tasklist /FI 不可靠）。"""
    try:
        out = subprocess.run(
            ["netstat", "-ano"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            creationflags=creation_flags(), timeout=15,
        ).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if f":{port} " in line and "LISTENING" in line.upper():
            parts = line.split()
            if parts and parts[-1].isdigit():
                return int(parts[-1])
    return None


def shell_exe(console_root: Path | None = None) -> Path | None:
    """找到 ApexShell.exe（无边框壳）。找不到返回 None —— 调用方退回系统浏览器。

    两个查找位置，按优先级：
    ① 环境变量 `APEX_SHELL_EXE`（给别的机器 / 打包后留的出口）；
    ② 调用方传入的 ControlPod 仓库中，优先 Release 日常入口，再找 Debug。
    不从 run-analytics 的目录结构推算 ControlPod 的位置。
    """
    override = os.environ.get("APEX_SHELL_EXE")
    if override:
        candidate = Path(override)
        return candidate if candidate.is_file() else None
    if console_root is None:
        return None
    for relative in SHELL_EXE_RELATIVES:
        candidate = console_root / relative
        if candidate.is_file():
            return candidate
    return None


def open_dashboard(
    port: int, root: Path, log_path: Path, console_root: Path | None = None
) -> str:
    """打开看板根地址（走 http，工具轨才激活）。

    ★ 优先用 apex-console 的无边框壳（ApexShell.exe --url=<看板地址>）打开：
      这样看板是一个无地址栏 / 无标签页的独立窗口，与总控台观感一致，并且
      窗口位置尺寸会被壳记住（壳侧 WindowStateStore 按 role=dashboard 分文件存）。
      壳不可用时**退回系统浏览器**（os.startfile）—— 不能让看板打不开。

    ★ 无论走哪条路，都**不能**开 `file://.../index.html`：那样拿不到 window.__BRIDGE__，
      工具轨会降级成灰按钮。必须开桥的地址。

    壳进程**故意不等待**：它是常驻窗口，占着模块运行位没有意义（同 spawn_bridge 的理由）。
    """
    url = f"http://127.0.0.1:{port}/"

    exe = shell_exe(console_root)
    if exe is not None:
        try:
            # DETACHED_PROCESS：壳不随 runner 退出而退出（runner 是短命进程）。
            # 静默启动，不弹控制台窗口。
            detached = 0
            if os.name == "nt":
                detached = getattr(subprocess, "DETACHED_PROCESS", 0) | \
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            subprocess.Popen(
                [str(exe), "--url", url],
                cwd=str(exe.parent),
                creationflags=detached,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            write_log(log_path, f"[STEP] 已用壳打开看板 {url}（{exe}）")
            return url
        except OSError as exc:
            # 壳启动失败不是致命错误：退回浏览器，并如实记因
            write_log(log_path, f"[WARN] 壳启动失败（{exc}），退回系统浏览器")
    else:
        write_log(log_path, "[WARN] 未找到 ApexShell.exe，退回系统浏览器")

    if os.name == "nt":
        os.startfile(url)  # type: ignore[attr-defined]
    else:
        import webbrowser
        webbrowser.open(url)
    write_log(log_path, f"[STEP] 已用系统浏览器打开看板 {url}")
    return url


# ---------------------------------------------------------------- 动作
def launch_bridge(
    root: Path, log_path: Path, console_root: Path | None = None
) -> dict[str, Any]:
    """起本地桥（若已在跑则复用），等它可应答，然后开看板。

    故意不等待桥退出 —— 它是常驻服务，占着模块运行位没有意义。
    """
    script = root / SERVE_SCRIPT
    if not script.is_file():
        raise RuntimeError(f"run-analytics 本地桥不存在：{script}")
    index = root / DASHBOARD
    if not index.is_file():
        raise RuntimeError(f"看板文件不存在：{index}")

    existing = bridge_running()
    if existing is not None and not bridge_matches(existing, root):
        retire_outdated_bridge(root, existing, log_path)
        existing = None
    if existing is not None:
        write_log(log_path, f"[SKIP] 本地桥已在跑（端口 {existing}），直接开看板")
        url = open_dashboard(existing, root, log_path, console_root)
        return {"exit_code": 0, "pid": read_pid(SERVE_PID_FILE), "port": existing,
                "url": url, "reused": True}

    # 旧记录可能指向一个已经死掉的桥（崩过 / 被回收）；清掉记录再起新的。
    # 注意：**不能在这里盲目 kill** —— 端口不通不代表进程该杀（可能刚起来还没 listen），
    # 但记录已经不可用了，清掉记录即可，让新桥去抢端口。
    write_log(log_path, "[STEP] 没有在跑的本地桥，起一个新的")
    SERVE_PID_FILE.unlink(missing_ok=True)
    SERVE_PORT_FILE.unlink(missing_ok=True)
    # pick_port() 这里只是**给出期望的起点端口**，不是最终端口 ——
    # 真正挑端口的是 `打开看板.cmd` 内部的 netstat 扫描（8917-8960），
    # 8917 被占它会自己往后挪。它挑到几号，以桥的应答为准（下面 actual）。
    port = pick_port()

    pid = spawn_bridge(root, port, log_path, console_root)
    # spawn_bridge 已经把「真实端口 / pid」写进记录文件了；这里只做最后一道确认，
    # 而且**要认它探到的那个端口**，不是认我们给的那个 —— 两者可能不同
    # （8917 被占时 .cmd 会往后挪）。认错端口会「桥稳稳在跑，却报起不来」。
    actual = read_port() or port
    if not port_serving(actual):
        serve_log = STATE_DIR / "serve.out.log"
        raise RuntimeError(
            f"本地桥没能起来（端口 {actual}）。看 {serve_log} 排查；"
            f"也可双击 run-analytics\\{LAUNCHER_SCRIPT} 手动起。")

    # ★ 新起的桥：**由这里统一开看板**（走 open_dashboard → 优先无边框壳）。
    #   历史：以前新桥那条路靠 serve.py 自己 `--open-browser`（系统浏览器），只有
    #   「复用已有桥」才走 runner；结果就是「第一次运行开浏览器小窗、第二次才开壳」，
    #   观感前后不一致（2026-09-28 多弗朗）。现在两个分支都收敛到 open_dashboard()，
    #   全链路只有一个「开看板」的决策点，壳/浏览器兜底逻辑不会分叉。
    url = open_dashboard(actual, root, log_path, console_root)
    return {"exit_code": 0, "pid": pid, "port": actual, "url": url, "reused": False}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", choices=("run", "stop", "restart"))
    parser.add_argument("--root", type=Path, help="run-analytics 项目目录")
    parser.add_argument("--console-root", type=Path, help="ControlPod 仓库目录（可选）")
    parser.add_argument("--result-path", type=Path)
    parser.add_argument("--log-path", type=Path)
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="离线自检：不联网、不开浏览器、不起桥，只验常量与端口选择逻辑。",
    )
    args = parser.parse_args(argv)
    # 控制台调用时才要求那四个位置参数；自检没有它们也不该报 usage。
    # （把 required=True 直接写死在 add_argument 上的后果：`--selftest` 单独跑会
    #  先被 argparse 用缺参怼回来，自检永远跑不到——踩过。）
    if not args.selftest:
        missing = [
            name
            for name in ("action", "root", "result_path", "log_path")
            if getattr(args, name) is None
        ]
        if missing:
            parser.error(
                "缺少参数: " + ", ".join("--" + m.replace("_", "-") for m in missing)
            )
    return args


def selftest(root: Path | None = None) -> int:
    """离线自检。**只验不需要真的起服务的东西**，避免自检本身去开一堆窗口。

    真链路（起桥 + 开看板 + 工具轨四动作）由它自己的自检覆盖：
    `tools/serve.py --selftest`、`tools/tasks.py --selftest`、`tools/check.js --browser`。
    """
    fails: list[str] = []

    def _spawn_bridge_source() -> str:
        """只取 spawn_bridge 的函数体，去掉文档字符串与注释 —— 用来断言它没走回老路。

        必须剥掉注释：那段「为什么不用某某姿势」的说明里正提到了被弃用的常量名，
        直接搜 `in source` 会被自己的文档判成失败（第一次写就踩了）。
        """
        source = Path(__file__).read_text(encoding="utf-8")
        body = source.split("def spawn_bridge", 1)[1].split("\ndef ", 1)[0]
        lines = []
        in_doc = False
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.count('"""') == 1:
                in_doc = not in_doc
                continue
            if in_doc or stripped.startswith("#"):
                continue
            code = line.split("#", 1)[0]
            if code.strip():
                lines.append(code)
        return "\n".join(lines)

    def ok(name: str, cond: bool) -> None:
        if not cond:
            fails.append(name)

    ok("启动脚本常量指向 .cmd（不是直接起 python）", LAUNCHER_SCRIPT.endswith(".cmd"))
    ok("桥脚本常量在 tools/ 下", SERVE_SCRIPT == Path("tools") / "serve.py")
    ok("期望端口避开 8765/8766", PREFERRED_PORT not in (8765, 8766))
    ok("端口扫描范围为正", PORT_SCAN_RANGE > 0)
    ok("pid / port 记录落在系统临时目录",
       STATE_DIR == Path(tempfile.gettempdir()) / "run-analytics")
    spawn_body = _spawn_bridge_source()
    # 方向别写反：`ok(name, cond)` 的语义是「cond 为假才报错」，所以断言里要写
    # **应该成立的事实**，不是失败条件。
    #   CREATE_NO_WINDOW  → 桥跑在隐藏控制台里，总控台点「运行」不会弹黑窗
    #   DETACHED_PROCESS  → 会给 cmd 新分配一个**可见**控制台，那就是黑窗
    ok("起桥用 CREATE_NO_WINDOW（隐藏控制台，不弹黑窗）",
       "CREATE_NO_WINDOW" in spawn_body)
    ok("起桥不再用 DETACHED_PROCESS（它会新分配可见控制台）",
       "DETACHED_PROCESS" not in spawn_body)

    # 开看板走壳（2026-09-28）：断言开窗函数里真的优先用壳、且保留了浏览器兜底。
    # 剥注释的原因同 _spawn_bridge_source —— 文档里正提到了 os.startfile 这个降级路径，
    # 不剥就会把「说明存在」误判成「代码里还有」。
    def _open_dashboard_source() -> str:
        source = Path(__file__).read_text(encoding="utf-8")
        body = source.split("def open_dashboard", 1)[1].split("\ndef ", 1)[0]
        lines = []
        in_doc = False
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.count('"""') == 1:
                in_doc = not in_doc
                continue
            if in_doc or stripped.startswith("#"):
                continue
            code = line.split("#", 1)[0]
            if code.strip():
                lines.append(code)
        return "\n".join(lines)

    def _launch_bridge_source() -> str:
        """只取 launch_bridge 的函数体（同样剥掉文档 / 注释，理由同上）。"""
        source = Path(__file__).read_text(encoding="utf-8")
        body = source.split("def launch_bridge", 1)[1].split("\ndef ", 1)[0]
        lines = []
        in_doc = False
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.count('"""') == 1:
                in_doc = not in_doc
                continue
            if in_doc or stripped.startswith("#"):
                continue
            code = line.split("#", 1)[0]
            if code.strip():
                lines.append(code)
        return "\n".join(lines)

    open_body = _open_dashboard_source()
    ok("开看板优先用壳（shell_exe + --url）",
       "shell_exe(console_root)" in open_body and '"--url"' in open_body)
    ok("开看板保留浏览器兜底（壳不可用时不让看板打不开）",
       "os.startfile" in open_body or "webbrowser.open" in open_body)
    ok("壳进程不阻塞 runner（不 wait，常驻窗口）",
       "wait(" not in open_body)
    source = Path(__file__).read_text(encoding="utf-8")
    ok("总控台壳路径不依赖模块文件层级猜测",
       "def shell_exe(console_root" in source and "console_root / relative" in source
       and "--console-root" in source)
    ok("桥进程继承显式传入的壳路径",
       "bridge_env[\"APEX_SHELL_EXE\"]" in spawn_body and "env=bridge_env" in spawn_body)

    # ★ 「新起的桥也要走壳」这条契约必须有断言守着：
    #   历史 bug 是新桥那条路只让 serve.py 自己开浏览器，于是「第一次运行开浏览器小窗、
    #   第二次才开壳」。断言 launch_bridge 里确实调了 open_dashboard，而不是自己拼 URL。
    lb_body = _launch_bridge_source()
    ok("新起的桥也走 open_dashboard（不再只靠 serve.py 开浏览器）",
       "open_dashboard(" in lb_body)
    ok("launch_bridge 不再自己拼看板 URL 就算完（必须经过 open 决策点）",
       'url = f"http://127.0.0.1:{actual}/"' not in lb_body)

    picked = pick_port()
    ok("挑到一个可用端口", isinstance(picked, int) and 1024 < picked < 65536)

    if root is not None:
        ok(f"{LAUNCHER_SCRIPT} 存在", (root / LAUNCHER_SCRIPT).is_file())
        ok("serve.py 存在", (root / SERVE_SCRIPT).is_file())
        ok("index.html 存在", (root / DASHBOARD).is_file())

    if fails:
        print(f"=== RUNNER-LAB SELFTEST FAIL: {len(fails)} 项 ===")
        for name in fails:
            print(f"    - {name}")
        return 1
    print("=== RUNNER-LAB SELFTEST PASS ===")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # 自检要在 root 解析之前短路：自检没有 --root 也不该去碰它。
    if args.selftest:
        return selftest(args.root.resolve() if args.root else None)
    root = args.root.resolve()
    try:
        if args.action == "stop":
            status = kill_bridge(args.log_path)
            write_result(
                args.result_path,
                {"ok": True, "action": "stop",
                 "status": "stopped" if status == "killed" else "noop",
                 # ★ 总控台只认 result.status.state 的 {running, stopped} 两个值，
                 #   其它一律退化成 unknown。老版本这里没有 state，所以「点关闭后
                 #   卡片永久卡在关闭中」—— 关掉之后必须如实写 stopped。
                 "result": {"status": {"state": "stopped"}}},
            )
            return 0
        if args.action == "restart":
            kill_bridge(args.log_path)
        console_root = args.console_root.resolve() if args.console_root else None
        result = launch_bridge(root, args.log_path, console_root)
        write_log(args.log_path, "[OK] 看板已打开，右侧工具轨就绪")
        write_result(
            args.result_path,
            # ★ result.status.state=running 让卡片红点亮起来；
            #   result.status.pid 是**交叉校验用**的 —— 桥被 taskkill /F 强杀时
            #   状态文件会残留 running，后端拿这个 pid 验一下进程是否还活着，
            #   才能把红点灭掉（详见 backend 的 /runtime 实现）。
            {"ok": True, "action": args.action, "status": "dashboard-opened",
             "result": {"status": {"state": "running", "pid": result.get("pid")}},
             **result},
        )
        return 0
    except Exception as exc:
        message = str(exc) or type(exc).__name__
        write_log(args.log_path, f"[ERROR] {message}")
        write_result(args.result_path, {"ok": False, "error": message})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
