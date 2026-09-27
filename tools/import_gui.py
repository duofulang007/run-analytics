# -*- coding: utf-8 -*-
"""跑步数据导入窗：一键把小米导出的跑步数据更新到看板。

窗口是**两步一条链路 + 两个工具**：

    第一步「登录小米账号」    → 自动勾协议、自动登录（**到此为止**）
    第二步「从邮箱导入」      → 邮件到了之后取包、导入（密码自动取、静默下载）
    两个工具：打开历史看板 / 清理下载的包和记录

**两步的先后是硬的**：不在小米那边申请导出，就没有导出邮件，也就没有包可抓。
所以登录那个按钮放在「从邮箱导入」左边 —— 读起来就是从左往右走一遍。
中间那段（进隐私中心、过短信、申请导出）由人做，脚本不掺和。

2026-09-24 多弗朗：「把本地导入压缩包和导入密码的功能删掉，现在只用这个功能」——
所以拖拽区、选择文件、密码输入框、开始导入按钮**全部去掉了**，解压密码只在内存里
从邮件取一趟，用完即弃（见 tools/import_zip.py 的密码来源说明）。
`import_zip.py` 引擎本身没变，手动导入仍可用命令行那一套。

样式对齐总控台（apex-console 的 styles.css 设计令牌）：
品牌色 #E8402A、墨色 #1B1D22、幽灵按钮、以及日志用的深板岩终端面板 #2E3440
（连 oke/warn/err 的取值都取自它的 `.term*` 规则）。
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox

# 清理功能在同目录的 cleanup.py 里（预演 / 真删分离，命令行也能单独跑）
# 四个动作的业务实现都在 tasks.py —— 窗口和本地桥（serve.py）共用同一份，
# 避免「同一件事两处各实现一遍，改一处忘一处」。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cleanup  # noqa: E402
import tasks  # noqa: E402

PROJECT_DIR = Path(__file__).resolve().parent.parent
# 自动链路：从邮箱取最新一封的密码 → 静默下载包 → 导入 → 弹通知
FETCH_SCRIPT = Path(__file__).resolve().parent / "fetch_from_mail.py"
# 链路第一步：开小米隐私中心（自动关 cookie 条 + 勾协议 + 登录 + 跳到隐私中心）。
# 它是「从邮箱导入」的前置 —— 不在小米那边申请导出，就不会有导出邮件。
# 两者**共用同一个浏览器 profile**（local/browser-profile），所以不能同时跑（见 _sync_buttons）。
LOGIN_SCRIPT = Path(__file__).resolve().parent / "xiaomi_login.py"
# 「打开历史看板」的落点是裸看板本体。独立外壳页（shell/ 下的极简 iframe 壳与更早的
# 玻璃壳）已废弃删除：工具轨现在住在 index.html 自己身上（悬浮侧边），套壳只会多一层。
# 这个窗口只管「导入链路」；日常看板走根目录的「打开看板.cmd」（起本地桥 → 工具轨激活）。
DASHBOARD = PROJECT_DIR / "index.html"

# ---------------------------------------------------------------- 设计令牌
# 取值来源：apex-console/styles.css 的 :root 与 .terminal（rgba 的在这里换算成实色，
# Tk 不支持透明通道）。改配色只动这一段，组件里不写死颜色。
BRAND = "#E8402A"          # --brand
BRAND_DARK = "#C9331F"     # --brand-dark（按下态；也是全窗唯一的"红字"色）
BRAND_SOFT = "#FCEDEA"     # --brand-soft：soft 按钮的底
BRAND_SOFT_2 = "#F8DBD3"   # soft 按钮的悬停一档（比 BRAND_SOFT 深半档）
BRAND_PALE = "#F6D5CE"     # 主按钮禁用态的底
BRAND_DEEP = "#A8412C"     # 主按钮禁用态的字
ON_BRAND = "#FFFFFF"       # --on-brand
INK = "#1B1D22"            # --ink
INK_2 = "#5C6270"          # --ink-2
INK_3 = "#8A909C"          # --ink-3
LINE = "#E6E7EB"           # --line（卡片描边）
BORDER_SOFT = "#D9DBE2"    # 幽灵按钮的描边：比 LINE 深半档，不然按钮看着像没描边
CARD = "#FFFFFF"           # --card-strong
BG = "#F7F6FA"             # --bg 的中间调（渐变在 Tk 里做不了，取它最中间那一档）
HOVER = "#F1F2F5"          # --hover 落在白底上的实色
ERR = "#D93025"            # --err
ERR_SOFT = "#FBEAE9"       # --err-bg 落在白底上的实色
TERM_BG = "#2E3440"        # --term-bg：日志终端底板
TERM_INK = "#D7DBE3"       # .terminal 正文色
TERM_OK = "#5BE38A"
TERM_WARN = "#E8B45B"
TERM_ERR = "#FF8A76"
TERM_DIM = "#7C8494"
TERM_CMD = "#7FB2FF"

FONT = "Microsoft YaHei UI"     # 总控台字体栈里的中文字面
MONO = "Consolas"

WINDOW_W = 420
WINDOW_H = 452
PAD = 16                 # 窗口外边距
CARD_PAD = 14            # 卡片内边距（--r-inner 那一档的手感）
# 卡片内容宽度：卡片宽 - 1px 边框 ×2 - 内边距 ×2。**按钮必须按这个算** ——
# 按窗口宽算会超出去 28px，Canvas 会被裁掉（实测：请求 386 只拿到 357，
# 右端圆角整个被切平）。
INNER_W = (WINDOW_W - PAD * 2) - 2 - CARD_PAD * 2

STEP_H, STEP_R = 40, 14                                           # --ctl-h:40px：第一/第二步那两个控件
GHOST_H, GHOST_R = 32, 11                                         # --r-sm:10px 一档
# 半宽：卡片里并排两个控件的统一宽度。**上下两行共用同一个算式** ——
# 「隐私中心 / 邮箱导入」那一行必须和「看板 / 清理」那一行左右严格对齐，
# 各留一份算式，就等着它们哪天对不齐（INNER_W 一改就露馅）。
HALF_W = (INNER_W - 10) // 2
TERM_R = 14                                                       # --r-inner:14px


def dashboard_meta() -> str:
    """摘取 data.js 的概况，给「历史看板」入口当副标题。

    实现在 `tasks.dashboard_meta()`（本地桥的工具轨也用同一句摘要，别各写一份）。
    """
    return tasks.dashboard_meta()


# ---------------------------------------------------------------- 圆角控件
def round_rect_points(x1, y1, x2, y2, r, steps=20):
    """圆角矩形的多边形顶点。Tk 没有圆角，只能自己描点（smooth 样条会走形，用采样点）。

    采样数别省：Tk 的多边形不做抗锯齿，steps=8 时 12px 的圆角会看出台阶（截图量过）。
    """
    pts = []
    for cx, cy, start in ((x1 + r, y1 + r, 180), (x2 - r, y1 + r, 270),
                          (x2 - r, y2 - r, 0), (x1 + r, y2 - r, 90)):
        for i in range(steps + 1):
            ang = math.radians(start + 90 * i / steps)
            pts.extend((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return pts


class RoundedButton(tk.Canvas):
    """画布实现的圆角按钮（品牌实心 / 幽灵 / 危险色），带 hover 与禁用态。

    **边框不用描边画**，而是「外圈一个圆角色块 + 内圈再填一个浅 1px 的圆角色块」：
    Tk 的多边形描边是尖角接合，弧上点一密、夹角一浅就甩出小刺（截图量到过四个角上的
    「小钩子」）。双层填充没有这个问题，而且和填充同一套光栅化，边缘一致。
    """

    KINDS = {
        # 禁用（导入中）用「浅品牌底 + 深品牌字」：比白字压浅底清楚，也还看得出是品牌色
        "primary": dict(border=None, fill=BRAND, fg=ON_BRAND, hover=BRAND_DARK,
                        active=BRAND_DARK, disabled_fill=BRAND_PALE, disabled_fg=BRAND_DEEP),
        # 第一步（登录小米账号）：浅品牌底 + 深品牌字。和主按钮同一个色系但轻一档，
        # 让上排读起来是"一对"，而不是"一个红的 + 一个灰的"。
        # border=None 让外圈跟着内芯一起变色，禁用时才能整颗干净地褪成白。
        "soft": dict(border=None, fill=BRAND_SOFT, fg=BRAND_DARK, hover=BRAND_SOFT_2,
                     active=BRAND_SOFT_2, disabled_fill=CARD, disabled_fg=INK_3),
        "ghost": dict(border=BORDER_SOFT, fill=CARD, fg=INK, hover=HOVER, active="#E8E9EE",
                      disabled_fill=CARD, disabled_fg=INK_3),
        # 危险动作只借用品牌这一种红（BRAND_DARK），全窗就一种红，不会再冒出第二种红字
        "danger": dict(border=BORDER_SOFT, fill=CARD, fg=BRAND_DARK, hover=ERR_SOFT,
                       active="#F6DAD7", disabled_fill=CARD, disabled_fg=INK_3),
    }

    def __init__(self, parent, text, command, kind="ghost", width=120, height=GHOST_H,
                 radius=GHOST_R, font=None, surface=CARD):
        super().__init__(parent, width=width, height=height, bg=surface,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.command = command
        self.style = self.KINDS[kind]
        self.enabled = True
        self.text = text
        self._border = self.create_polygon(
            round_rect_points(0.5, 0.5, width - 0.5, height - 0.5, radius),
            fill=self.style["border"] or self.style["fill"], outline="")
        self._fill = self.create_polygon(
            round_rect_points(1.5, 1.5, width - 1.5, height - 1.5, max(1, radius - 1)),
            fill=self.style["fill"], outline="")
        self._label = self.create_text(width / 2, height / 2 + 1, text=text,
                                       fill=self.style["fg"], font=font)
        self.bind("<Enter>", lambda _e: self._paint("hover"))
        self.bind("<Leave>", lambda _e: self._paint("fill"))
        self.bind("<Button-1>", lambda _e: self._paint("active"))
        self.bind("<ButtonRelease-1>", self._release)

    def _paint(self, state, fill=None, fg=None):
        color = fill or self.style[state]
        self.itemconfigure(self._fill, fill=color)
        if self.style["border"] is None:      # 品牌实心：外圈跟着一起变色
            self.itemconfigure(self._border, fill=color)
        if fg:
            self.itemconfigure(self._label, fill=fg)

    def _release(self, event):
        if not self.enabled:
            return
        inside = 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
        self._paint("hover" if inside else "fill")
        if inside and self.command:
            self.command()

    def configure_state(self, enabled=None, text=None):
        """统一的禁用/改字入口（导入中要把主按钮锁住并改成「邮箱导入中…」）。"""
        if text is not None:
            self.text = text
            self.itemconfigure(self._label, text=text)
        if enabled is not None:
            self.enabled = enabled
            self.configure(cursor="hand2" if enabled else "arrow")
            if enabled:
                self._paint("fill", fg=self.style["fg"])
            else:
                self._paint("fill", fill=self.style["disabled_fill"],
                            fg=self.style["disabled_fg"])


# ---------------------------------------------------------------- 窗口
class ImportWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        # 两个忙标志分开：它们锁的东西不一样。
        # busy = 邮箱导入在跑；login_busy = 隐私中心的浏览器窗口还开着。
        self.busy = False
        self.login_busy = False
        self.data_js_mtime = None
        self.lines: queue.Queue[str] = queue.Queue()

        root.title("跑步数据导入 · run-analytics")
        root.configure(bg=BG)
        root.resizable(False, False)
        self._center_on_screen()
        self._build()
        root.after(120, self._pump)

    def _center_on_screen(self) -> None:
        """居中弹出（不写死位置，否则会一路叠在左上角）。"""
        self.root.update_idletasks()
        x = max(0, (self.root.winfo_screenwidth() - WINDOW_W) // 2)
        y = max(0, (self.root.winfo_screenheight() - WINDOW_H) // 2)
        self.root.geometry(f"{WINDOW_W}x{WINDOW_H}+{x}+{y}")

    def _build(self) -> None:
        tk.Label(self.root, text="跑步数据导入", bg=BG, fg=INK,
                 font=(FONT, 15, "bold")).pack(anchor="w", padx=PAD, pady=(PAD, 1))
        tk.Label(self.root, text="左侧登录小米 → 自己去申请导出 → 右侧取包导入 · 密码自动取、静默下载",
                 bg=BG, fg=INK_3, font=(FONT, 9)).pack(anchor="w", padx=PAD)

        card = tk.Frame(self.root, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        card.pack(fill="x", padx=PAD, pady=(12, 10))

        # 第一步 + 第二步并排。**顺序就是左 → 右**：先在小米那边申请导出，
        # 等导出邮件到了，再点右边取包导入。左按钮走幽灵态（它的作用是"去做个前置动作"），
        # 右按钮保留品牌实心（这条链路真正一键完成的那一下是它）。
        steps = tk.Frame(card, bg=CARD)
        steps.pack(padx=CARD_PAD, pady=(CARD_PAD, 8))
        self.privacy_button = RoundedButton(
            steps, "登录小米账号", self._start_login_flow, kind="soft",
            width=HALF_W, height=STEP_H, radius=STEP_R,
            font=(FONT, 10), surface=CARD)
        self.privacy_button.pack(side="left")
        self.mail_button = RoundedButton(
            steps, "从邮箱导入", self._start_mail_flow, kind="primary",
            width=HALF_W, height=STEP_H, radius=STEP_R,
            font=(FONT, 10, "bold"), surface=CARD)
        self.mail_button.pack(side="left", padx=(10, 0))

        row = tk.Frame(card, bg=CARD)
        row.pack(padx=CARD_PAD, pady=(0, 12))
        self.open_button = RoundedButton(row, "打开历史看板", self._open_dashboard,
                                         width=HALF_W, height=GHOST_H, radius=GHOST_R,
                                         font=(FONT, 9), surface=CARD)
        self.open_button.pack(side="left")
        self.clean_button = RoundedButton(row, "清理下载的包和记录", self._cleanup,
                                          kind="danger", width=HALF_W, height=GHOST_H,
                                          radius=GHOST_R, font=(FONT, 9), surface=CARD)
        self.clean_button.pack(side="left", padx=(10, 0))

        self.hist_hint = tk.Label(card, text="", bg=CARD, fg=INK_3,
                                  font=(FONT, 8), wraplength=INNER_W - 6, justify="left")
        self.hist_hint.pack(anchor="w", padx=CARD_PAD, pady=(0, CARD_PAD))

        # 日志：总控台的终端面板（深底 + 等宽浅字），下掉一块空白就撑在这里
        self.term = tk.Canvas(self.root, width=WINDOW_W - PAD * 2,
                              height=WINDOW_H - 200, bg=BG, highlightthickness=0, bd=0)
        self.term.pack(padx=PAD, pady=(0, PAD))
        tw, th = WINDOW_W - PAD * 2, WINDOW_H - 200
        self.term.create_polygon(round_rect_points(0.5, 0.5, tw - 0.5, th - 0.5, TERM_R),
                                 fill=TERM_BG, outline="")
        self.log = tk.Text(self.term, bg=TERM_BG, fg=TERM_INK, bd=0, relief="flat",
                           highlightthickness=0, font=(MONO, 9), wrap="word",
                           padx=8, pady=6, insertwidth=0)
        self.term.create_window(10, 8, window=self.log, anchor="nw",
                                width=tw - 20, height=th - 16)
        self.log.configure(state="disabled")

        self._refresh_history()

    def _refresh_history(self) -> None:
        """data.js 存在时「打开历史看板」直接可用 —— 看板是本地静态文件。"""
        meta = dashboard_meta()
        self.open_button.configure_state(enabled=bool(meta))
        self.hist_hint.configure(
            text="已有一份历史数据（" + meta + "），不导入也能直接看。" if meta
            else "还没有历史数据：先从邮箱导入一次，之后随时可以直接打开。")

    def _say(self, text: str, color: str = TERM_INK) -> None:
        self.log.configure(state="normal")
        tag = "c" + color
        self.log.tag_configure(tag, foreground=color)
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _open_dashboard(self) -> None:
        """打开历史看板（裸看板本体，file:// 直开）。

        纯静态，双击即可，所以这里不起服务、不留常驻进程 —— 也就不存在
        "服务被回收导致打不开"的老问题。代价是 file:// 下拿不到桥，
        工具轨降级成灰按钮（刷新/登录/清理要点根目录的「打开看板.cmd」）。
        """
        if not DASHBOARD.is_file():
            self._say("找不到看板文件：%s" % DASHBOARD, TERM_ERR)
            return
        self._say("已打开历史看板：%s" % DASHBOARD.name, TERM_OK)
        self._say("刷新/登录/清理要点根目录的「打开看板.cmd」（本机 http 才有桥）。", TERM_DIM)
        os.startfile(str(DASHBOARD))  # type: ignore[attr-defined]

    # -------------------------------------------------- 邮箱自动导入
    def _start_mail_flow(self) -> None:
        """一键链路：取最新一封的密码 → 静默下载 → 导入 → 弹通知。

        密码、找包、试错全在子进程里；这里只把它的输出灌进日志窗，并在跑的时候
        把按钮锁住，避免同时开两条导入线。
        """
        if self.busy or self.login_busy:
            if self.login_busy:
                # 隐私中心那个浏览器还开着 —— 两边用的是**同一个 profile**，
                # auto_download.py 起来第一件事就是 close_lingering_browser()，
                # 会把那个窗口连同用户正在做的申请一起杀掉。所以必须挡住，并说清原因。
                self._say("隐私中心的浏览器窗口还开着。先关掉那个窗口，再点这里。", TERM_ERR)
            return
        self.busy = True
        # 记下看板文件的修改时间：回来时要**核对它真的被重写过**，不能只信返回码 ——
        # 「返回码说成功、文件却没变」意味着我们看到的还是上一次的看板（真出过这个 bug）。
        try:
            self.data_js_mtime = (PROJECT_DIR / "data.js").stat().st_mtime
        except OSError:
            self.data_js_mtime = None
        self.open_button.configure_state(enabled=False)
        self._sync_buttons()
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        # 一行交代清楚「要做什么 + 什么时候才需要你」，别用两行说同一件事
        self._say("取密码 → 静默下载 → 导入看板；只有登录态过期才需要你操作一次。", TERM_CMD)
        threading.Thread(target=self._run_mail_flow, daemon=True).start()

    def _run_mail_flow(self) -> None:
        """跑邮箱导入。业务的启动参数在 `tasks.refresh()` 里（工具轨也用同一份）。

        这里只负责「把它的输出一行行投进日志窗」：这条链路可能要在浏览器里登录、
        跑几分钟，等它全跑完再一次性显示，用户看到的就是「卡住不动」。所以用 tasks
        给的 Runner 流式读，而不是自己 Popen 一份。
        """
        runner = None
        try:
            runner = tasks.refresh()
            self._runner = runner
            cursor = 0
            while True:
                chunk = runner.poll(cursor)
                for line in chunk["lines"]:
                    self.lines.put(line)
                cursor = chunk["cursor"]
                if not chunk["alive"]:
                    self.lines.put("__EXIT__%s" % chunk["exitCode"])
                    break
                time.sleep(0.12)
        except Exception as exc:
            self.lines.put(f"启动邮箱导入流程失败：{type(exc).__name__}: {exc}")
            self.lines.put("__EXIT__-1")

    # -------------------------------------------------- 隐私中心（第一步）
    def _sync_buttons(self) -> None:
        """按两个忙标志统一刷新按钮可用性。

        这里有一条硬约束：**隐私中心的浏览器和自动下载用的浏览器是同一个 profile**。
        `auto_download.py` 一启动就先 `close_lingering_browser()`，所以只要隐私中心
        那个窗口还开着，点「从邮箱导入」就会把用户正在操作的窗口杀掉 ——
        于是 login_busy 期间必须同时锁住「从邮箱导入」和「清理」。

        「打开历史看板」不碰浏览器、也不改任何文件，所以**任何情况下都保持可用**：
        等小米发邮件的那几个小时里，用户随时想看看板都不该被挡住。
        """
        mail_free = not self.busy and not self.login_busy
        self.privacy_button.configure_state(enabled=not self.busy and not self.login_busy)
        self.mail_button.configure_state(
            enabled=mail_free, text="邮箱导入中…" if self.busy else "从邮箱导入")
        self.clean_button.configure_state(enabled=mail_free)
    def _start_login_flow(self) -> None:
        """第一步：登录小米账号（自动关 cookie 条 + 勾协议 + 填表 + 点登录）。

        出去的是**一个真的 Edge 窗口**：脚本把登录这件事做完，然后把窗口交给用户。
        **到此为止** —— 隐私中心、短信二次验证、申请导出都由人做
        （多弗朗 2026-09-25 明确「做到帮我登录就行了」）。**窗口关掉，子进程才结束。**
        """
        if self.busy or self.login_busy:
            self._say("已经有一条链路在跑，等它结束。", TERM_ERR)
            return
        if not LOGIN_SCRIPT.is_file():
            self._say("找不到 %s" % LOGIN_SCRIPT, TERM_ERR)
            return
        self.login_busy = True
        self.privacy_button.configure_state(enabled=False, text="登录窗口开着…")
        self._sync_buttons()
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._say("开一个浏览器窗口去小米登录页（自动勾协议；有配置就自动登录）。", TERM_CMD)
        self._say("脚本只做到登录为止；隐私中心、申请导出由你自己来。", TERM_DIM)
        threading.Thread(target=self._run_login_flow, daemon=True).start()

    def _run_login_flow(self) -> None:
        """跑登录脚本，逐行灌进日志窗。启动参数在 `tasks.login()` 里（工具轨共用）。

        这个子进程会一直活到用户关掉浏览器（可能几十分钟），所以必须是后台线程 +
        流式读取：一次性 `communicate()` 会让界面在这几十分钟里一句话都不说，
        看起来就是"卡死"。
        """
        try:
            runner = tasks.login()
            self._login_runner = runner
            cursor = 0
            while True:
                chunk = runner.poll(cursor)
                for line in chunk["lines"]:
                    self.lines.put(line)
                cursor = chunk["cursor"]
                if not chunk["alive"]:
                    self.lines.put("__LOGIN_EXIT__%s" % chunk["exitCode"])
                    break
                time.sleep(0.12)
        except Exception as exc:
            self.lines.put(f"启动隐私中心脚本失败：{type(exc).__name__}: {exc}")
            self.lines.put("__LOGIN_EXIT__-1")

    def _finish_login(self, code: int) -> None:
        """第一步收尾。**绝不报「导入成功」** —— 这一步一个字节的数据都没动。

        话术在 `tasks.login_message()` 里（工具轨说同一套话，别各写一份）。
        """
        self.login_busy = False
        self.privacy_button.configure_state(enabled=True, text="登录小米账号")
        kind, lines = tasks.login_message(code)
        color = {"ok": TERM_OK, "warn": TERM_WARN, "err": TERM_ERR}.get(kind, TERM_DIM)
        for line in lines:
            self._say(line, color)
        self._sync_buttons()

    # -------------------------------------------------- 清理
    def _cleanup(self) -> None:
        """删掉下载的包 + 已处理邮件记录。**先预演给他看，再让他确认。**

        删数据不可逆：先把要删的逐条列进日志窗，再用模态框要一次明确确认。
        看板数据（data.js）不在范围内。范围统计与真删都走 `tasks`（工具轨共用）。
        """
        if self.busy or self.login_busy:
            self._say("已经有一条链路在跑，等它结束再清理。", TERM_ERR)
            return

        preview = tasks.cleanup_preview()
        items = preview.get("items") or []
        if not items:
            self._say("没有需要清理的东西：没有小米导出包，也没有已处理邮件记录。", TERM_OK)
            return

        self._say("即将删除下面这些（看板数据 data.js 不会被动）：", TERM_WARN)
        n_pkg = 0
        n_state = 0
        for item in items:
            if item["kind"] == "package":
                n_pkg += 1
                self._say("    [包]   %s（%.1f MB）" % (item["path"], item["bytes"] / 1024 / 1024),
                          TERM_DIM)
            else:
                n_state += 1
                self._say("    [记录] %s（%s）" % (item["path"], item.get("detail") or ""), TERM_DIM)

        prompt = "\n".join([
            "将删除 %d 项：" % len(items),
            "",
            "  · 小米导出包 %d 个" % n_pkg,
            "  · 已处理邮件记录 %s" % ("1 个" if n_state else "无"),
            "",
            "看板数据（data.js）不会被动。",
            "删掉记录后旧邮件可以重新被处理，但包需要重新下载。",
            "",
            "确认删除？",
        ])
        if not messagebox.askyesno("确认清理", prompt, icon="warning", default="no"):
            self._say("已取消，什么都没删。", TERM_DIM)
            return

        result = tasks.cleanup_apply()
        if not result.get("ok"):
            self._say(result.get("error") or "清理失败。", TERM_ERR)
            return
        removed = result.get("removed") or []
        failed = result.get("failed") or []
        self._say("删掉 %d 项。" % len(removed), TERM_OK)
        for path in removed:
            self._say("    %s" % path, TERM_DIM)
        if failed:
            self._say("有 %d 项删不掉（可能被占用），已跳过：" % len(failed), TERM_ERR)
            for path in failed:
                self._say("    %s" % path, TERM_DIM)
        self._refresh_history()

    # -------------------------------------------------- 收尾
    def _pump(self) -> None:
        try:
            while True:
                line = self.lines.get_nowait()
                if line.startswith("__LOGIN_EXIT__"):
                    self._finish_login(int(line.replace("__LOGIN_EXIT__", "")))
                elif line.startswith("__EXIT__"):
                    self._finish(int(line.replace("__EXIT__", "")))
                else:
                    self._say(line)
        except queue.Empty:
            pass
        self.root.after(120, self._pump)

    def _finish(self, code: int) -> None:
        self.busy = False
        self._sync_buttons()
        if code == 0:
            # code 0 只代表「fetch_from_mail 说导入了」。**亲眼核对一次**：
            # 看板文件必须真的被重写过。没变就宁可不报成功，也不用上一次的看板充数。
            try:
                now = (PROJECT_DIR / "data.js").stat().st_mtime
            except OSError:
                now = None
            if self.data_js_mtime is not None and now is not None and now <= self.data_js_mtime:
                self._say("返回码说成功，但看板文件没有被改动 —— 按未导入处理。", TERM_ERR)
                self._say("看板仍是上一次的内容，没有变化。", TERM_ERR)
                self._refresh_history()
                return
            self._say("导入完成。", TERM_OK)
            self._refresh_history()
            self._say("已生成看板数据，点「打开历史看板」查看。", TERM_OK)
            try:
                payload = json.loads(
                    (PROJECT_DIR / "data.js").read_text(encoding="utf-8").split("=", 1)[1]
                    .rstrip().rstrip(";")
                )
                self._say(f"当前共 {len(payload.get('runs') or [])} 场跑步记录。", TERM_DIM)
            except Exception:
                pass
        elif code == 9:
            self._say("没有新的导出邮件，什么都没导入 —— 看板没有变动。", TERM_WARN)
            self._refresh_history()
        else:
            if code == 8:
                self._say("自动下载没完成，什么都没导入 —— 看板没有变动。", TERM_ERR)
            else:
                self._say("导入未完成（返回码 %d），看板没有变动。请按上面的提示处理。" % code,
                          TERM_ERR)
            # 失败也要把「看历史」恢复可用：历史数据还在，不该被一次失败的导入锁住
            self._refresh_history()


def main() -> int:
    root = tk.Tk()
    try:
        root.iconbitmap(default="")  # 某些环境没有默认图标，忽略失败
    except Exception:
        pass
    ImportWindow(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
