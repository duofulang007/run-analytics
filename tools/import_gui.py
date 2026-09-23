# -*- coding: utf-8 -*-
"""跑步数据导入窗：把小米导出的 zip 拖进来，输入解压密码，一键导入。

三个要点
1. 拖拽用 Windows 原生 `WM_DROPFILES`（ctypes 子类化窗口过程），不依赖任何第三方库。
   万一子类化失败（非 Windows / 权限异常），「选择文件」按钮仍然可用。
2. **密码不落盘**：只在本进程内存在，通过环境变量交给子进程，用完即弃。
   小米的导出密码会变，存下来只会变成「上次的密码」这种误导。
3. 导入在后台线程里跑，输出实时灌进窗口，界面不会卡死。
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

PROJECT_DIR = Path(__file__).resolve().parent.parent
IMPORT_SCRIPT = Path(__file__).resolve().parent / "import_zip.py"
DASHBOARD = PROJECT_DIR / "index.html"
# inbox / latest 收在项目内（2026-09-23 起）：项目打包时自动带走，不再依赖 D:\RunningData。
SEARCH_DIRS = (
    PROJECT_DIR / "inbox",
    PROJECT_DIR / "latest",
    Path(r"D:\网页下载\新建文件夹"),
    Path.home() / "Downloads",
)

BG = "#ffffff"
CARD = "#f6f7f9"
BORDER = "#dcdfe5"
TEXT = "#1f2328"
MUTED = "#6b7280"
ACCENT = "#2f6feb"
OK_COLOR = "#1a7f37"
ERR_COLOR = "#c0392b"

WINDOW_W = 620
WINDOW_H = 520


def dashboard_meta() -> str:
    """摘取 data.js 的概况，给「历史看板」入口当副标题。

    注意看的是 **data.js**（数据本体），不是 DASHBOARD（index.html 入口页）。
    只用正则摘字段，不解析整个 JSON：data.js ~157KB，摘 generated / 场次数 /
    最大日期这三样足够。多弗朗时不时会点开看板，多数时候并不想走一遍导入——
    这个入口就是给「只想看看」的日子用的。
    """
    data_file = PROJECT_DIR / "data.js"
    if not data_file.is_file():
        return ""
    text = data_file.read_text(encoding="utf-8", errors="ignore")
    dates = re.findall(r'"date"\s*:\s*"(\d{4}-\d{2}-\d{2})"', text)
    if not dates:
        return ""
    runs = len(re.findall(r'"id"\s*:', text))
    gen = re.search(r'"generated"\s*:\s*"([^"]+)"', text)
    return f"共 {runs} 场 · 截至 {max(dates)}" + (f" · 生成于 {gen.group(1)}" if gen else "")


# ------------------------------------------------------------------ 拖拽
WM_DROPFILES = 0x0233
GWLP_WNDPROC = -4


class WindowsDropTarget:
    """把窗口变成文件拖放目标，命中后回调路径列表。

    Tk 的顶层窗口在 Windows 上有两个 HWND：外层的「框架窗口」和里面的「客户区」。
    投递文件时系统只会发给注册过 DragAcceptFiles 的那个句柄，而不同 Tk 版本 /
    主题下到底落在哪一个上并不固定，所以这里两个都注册一遍，谁收到都能处理。
    """

    def __init__(self, root: tk.Misc, callback) -> None:
        self.callback = callback
        self.ok = False
        self.reason = "非 Windows"
        self._old_procs: dict[int, int] = {}
        if os.name != "nt":
            return
        try:
            self.user32 = ctypes.windll.user32
            self.shell32 = ctypes.windll.shell32

            handles: list[int] = []
            for getter in (lambda: int(root.winfo_id()), lambda: int(root.frame(), 16)):
                try:
                    handle = getter()
                except Exception:
                    continue
                if handle and handle not in handles:
                    handles.append(handle)
            if not handles:
                self.reason = "拿不到窗口句柄"
                return

            proto = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
                ctypes.c_size_t, ctypes.c_ssize_t,
            )
            self._proto = proto
            self._proc = proto(self._handler)

            if hasattr(self.user32, "SetWindowLongPtrW"):
                setter = self.user32.SetWindowLongPtrW
                setter.restype = ctypes.c_ssize_t
                setter.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
            else:
                setter = self.user32.SetWindowLongW
                setter.restype = ctypes.c_long
                setter.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]

            self.user32.CallWindowProcW.restype = ctypes.c_ssize_t
            self.user32.CallWindowProcW.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                ctypes.c_size_t, ctypes.c_ssize_t,
            ]
            self.user32.DefWindowProcW.restype = ctypes.c_ssize_t
            self.user32.DefWindowProcW.argtypes = [
                ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t,
            ]

            new_proc = ctypes.cast(self._proc, ctypes.c_void_p).value
            for handle in handles:
                old = setter(ctypes.c_void_p(handle), GWLP_WNDPROC, new_proc)
                if not old:
                    continue
                self._old_procs[handle] = old
                self.shell32.DragAcceptFiles(ctypes.c_void_p(handle), True)
            self.ok = bool(self._old_procs)
            if not self.ok:
                self.reason = "窗口过程替换失败"
        except Exception as exc:  # pragma: no cover - 取决于运行环境
            self.reason = f"{type(exc).__name__}: {exc}"

    def _handler(self, hwnd, msg, wparam, lparam):
        if msg == WM_DROPFILES:
            try:
                drop = ctypes.c_void_p(wparam)
                count = self.shell32.DragQueryFileW(drop, 0xFFFFFFFF, None, 0)
                paths = []
                for index in range(count):
                    length = self.shell32.DragQueryFileW(drop, index, None, 0)
                    buffer = ctypes.create_unicode_buffer(length + 1)
                    self.shell32.DragQueryFileW(drop, index, buffer, length + 1)
                    paths.append(buffer.value)
                self.shell32.DragFinish(drop)
                if paths:
                    self.callback(paths)
            except Exception:
                pass
            return 0
        old_proc = self._old_procs.get(int(hwnd or 0))
        if old_proc:
            return self.user32.CallWindowProcW(
                ctypes.c_void_p(old_proc), hwnd, msg, wparam, lparam
            )
        return self.user32.DefWindowProcW(hwnd, msg, wparam, lparam)


# ------------------------------------------------------------------ 窗口
def newest_zip() -> Path | None:
    best, best_mtime = None, -1.0
    for folder in SEARCH_DIRS:
        if not folder.is_dir():
            continue
        for item in folder.glob("*.zip"):
            if "MiFitness" not in item.name and "MiWear" not in item.name:
                continue
            try:
                mtime = item.stat().st_mtime
            except OSError:
                continue
            if mtime > best_mtime:
                best, best_mtime = item, mtime
    return best


class ImportWindow:
    def __init__(self, root: tk.Tk, preset: Path | None) -> None:
        self.root = root
        self.zip_path = preset or newest_zip()
        self.busy = False
        self.outcome: dict | None = None
        self.lines: queue.Queue[str] = queue.Queue()

        root.title("跑步数据导入 · run-analytics")
        root.minsize(560, 460)
        root.configure(bg=BG)
        self._center_on_screen()

        self._build()
        self.drop = WindowsDropTarget(root, self._on_drop)
        self._refresh_selection()
        self._hint_drop_state()
        root.after(120, self._pump)

    # -------------------------------------------------- 布局
    def _center_on_screen(self) -> None:
        """居中弹出。

        不写死 `geometry("WxH")`，那样窗口会落在系统随便挑的位置（通常是左上角一路叠下去）。
        屏幕尺寸要先 `update_idletasks()` 才准；多显示器下 Tk 给的是主屏尺寸，就按主屏居中。
        """
        self.root.update_idletasks()
        x = max(0, (self.root.winfo_screenwidth() - WINDOW_W) // 2)
        y = max(0, (self.root.winfo_screenheight() - WINDOW_H) // 2)
        self.root.geometry(f"{WINDOW_W}x{WINDOW_H}+{x}+{y}")

    def _build(self) -> None:
        pad = {"padx": 18}
        tk.Label(self.root, text="跑步数据导入", bg=BG, fg=TEXT,
                 font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w", pady=(16, 2), **pad)
        tk.Label(self.root, text="把小米导出的 zip 拖进下面的框里，输入密码后点开始。",
                 bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(anchor="w", **pad)

        zone = tk.Frame(self.root, bg=CARD, highlightbackground=BORDER,
                        highlightthickness=1)
        zone.pack(fill="x", pady=(12, 8), **pad)
        self.zone_title = tk.Label(zone, text="拖压缩包到这里", bg=CARD, fg=TEXT,
                                   font=("Microsoft YaHei UI", 11))
        self.zone_title.pack(pady=(18, 4))
        self.zone_name = tk.Label(zone, text="", bg=CARD, fg=MUTED,
                                  font=("Microsoft YaHei UI", 9), wraplength=520)
        self.zone_name.pack(pady=(0, 6))
        tk.Button(zone, text="选择文件…", command=self._pick, relief="flat",
                  bg="#e8eaee", fg=TEXT, activebackground="#dde0e6",
                  font=("Microsoft YaHei UI", 9), padx=12, pady=3,
                  cursor="hand2").pack(pady=(0, 16))

        row = tk.Frame(self.root, bg=BG)
        row.pack(fill="x", **pad)
        tk.Label(row, text="解压密码", bg=BG, fg=TEXT,
                 font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.password = tk.Entry(row, show="●", relief="flat", bg=CARD, fg=TEXT,
                                 insertbackground=TEXT, font=("Consolas", 10))
        self.password.pack(side="left", fill="x", expand=True, padx=(10, 0), ipady=4)
        tk.Label(self.root, text="密码只用于本次导入，不会写进任何文件。",
                 bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(
                     anchor="w", pady=(4, 0), **pad)

        actions = tk.Frame(self.root, bg=BG)
        actions.pack(fill="x", pady=(12, 8), **pad)
        self.start_button = tk.Button(actions, text="开始导入", command=self._start,
                                      relief="flat", bg=ACCENT, fg="#ffffff",
                                      activebackground="#2559c0", activeforeground="#ffffff",
                                      font=("Microsoft YaHei UI", 10, "bold"),
                                      padx=20, pady=6, cursor="hand2")
        self.start_button.pack(side="left")
        self.open_button = tk.Button(actions, text="打开历史看板", command=self._open_dashboard,
                                     relief="flat", bg="#e8eaee", fg=TEXT,
                                     activebackground="#dde0e6",
                                     font=("Microsoft YaHei UI", 10),
                                     padx=16, pady=6, cursor="hand2",
                                     state="disabled")
        self.open_button.pack(side="left", padx=(10, 0))
        self.hist_hint = tk.Label(self.root, text="", bg=BG, fg=MUTED,
                                  font=("Microsoft YaHei UI", 8))
        self.hist_hint.pack(anchor="w", padx=18)

        self.log = tk.Text(self.root, height=11, relief="flat", bg=CARD, fg=TEXT,
                           font=("Consolas", 9), wrap="word", padx=10, pady=8)
        self.log.pack(fill="both", expand=True, padx=18, pady=(4, 16))
        self.log.configure(state="disabled")

        self.password.focus_set()
        self.root.bind("<Return>", lambda _event: self._start())
        self._refresh_history()

    def _refresh_history(self) -> None:
        """data.js 已存在时，「打开历史看板」直接可用——看板是本地静态文件，
        大多数打开只是想看看，不该逼着用户先走一遍导入。"""
        meta = dashboard_meta()
        if meta:
            self.open_button.configure(state="normal", text="打开历史看板")
            self.hist_hint.configure(text="已有一份历史数据（" + meta + "），不导入也能直接看；要更新才需要导入。")
        else:
            self.open_button.configure(state="disabled", text="打开历史看板")
            self.hist_hint.configure(text="还没有历史数据：先导入一次，之后随时可以直接打开。")

    def _say(self, text: str, color: str = TEXT) -> None:
        self.log.configure(state="normal")
        tag = f"c{color}"
        self.log.tag_configure(tag, foreground=color)
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _hint_drop_state(self) -> None:
        if not self.drop.ok:
            self.zone_title.configure(text="拖拽不可用，请点下面的「选择文件…」")
            self.zone_name.configure(text=f"（原因：{self.drop.reason}）")

    def _refresh_selection(self) -> None:
        if self.zip_path and self.zip_path.is_file():
            size = self.zip_path.stat().st_size / 1024 / 1024
            self.zone_title.configure(text="已选压缩包" if self.drop.ok else "已选压缩包（拖拽不可用）")
            self.zone_name.configure(text=f"{self.zip_path.name}   {size:.1f} MB")
        else:
            if self.drop.ok:
                self.zone_title.configure(text="拖压缩包到这里")
            self.zone_name.configure(text="未找到导出包，请拖入或点「选择文件…」")

    # -------------------------------------------------- 事件
    def _on_drop(self, paths: list[str]) -> None:
        def apply() -> None:
            for raw in paths:
                candidate = Path(raw)
                if candidate.suffix.lower() == ".zip" and candidate.is_file():
                    self.zip_path = candidate
                    self.log.configure(state="normal")
                    self.log.delete("1.0", "end")
                    self.log.configure(state="disabled")
                    self._refresh_selection()
                    self._say(f"已接收：{candidate.name}", OK_COLOR)
                    return
            self._say("拖进来的不是 .zip 文件，已忽略。", ERR_COLOR)

        self.root.after(0, apply)

    def _pick(self) -> None:
        initial = str(SEARCH_DIRS[0]) if SEARCH_DIRS[0].is_dir() else str(Path.home())
        chosen = filedialog.askopenfilename(
            title="选择小米导出的压缩包",
            initialdir=initial,
            filetypes=[("压缩包", "*.zip"), ("所有文件", "*.*")],
        )
        if chosen:
            self.zip_path = Path(chosen)
            self._refresh_selection()
            self._say(f"已选择：{self.zip_path.name}", OK_COLOR)

    def _open_dashboard(self) -> None:
        if DASHBOARD.is_file():
            os.startfile(str(DASHBOARD))  # type: ignore[attr-defined]

    # -------------------------------------------------- 导入
    def _start(self) -> None:
        if self.busy:
            return
        if not self.zip_path or not self.zip_path.is_file():
            messagebox.showwarning("还没有压缩包", "请先把 zip 拖进窗口，或点「选择文件…」。")
            return
        password = self.password.get()
        if not password:
            messagebox.showwarning("还没有密码", "请输入小米导出包的解压密码。")
            self.password.focus_set()
            return

        self.busy = True
        self.outcome = None
        self.start_button.configure(state="disabled", text="导入中…", bg="#9aa7bd")
        self.open_button.configure(state="disabled")
        self._say("", TEXT)
        self._say(f"开始导入：{self.zip_path.name}", ACCENT)
        # 密码只在内存与子进程环境里走一趟；导入结束就随进程消失，不写文件、不留痕。
        self.password.delete(0, "end")

        threading.Thread(
            target=self._run_import, args=(self.zip_path, password), daemon=True
        ).start()

    def _run_import(self, zip_path: Path, password: str) -> None:
        env = dict(os.environ)
        env["MIFITNESS_ZIP_PASSWORD"] = password
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            proc = subprocess.run(
                [sys.executable, str(IMPORT_SCRIPT), str(zip_path), "--no-prompt"],
                cwd=str(PROJECT_DIR),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
                timeout=900,
            )
            for line in (proc.stdout or "").splitlines():
                self.lines.put(line)
            self.lines.put(f"__EXIT__{proc.returncode}")
        except subprocess.TimeoutExpired:
            self.lines.put("导入超时（超过 900 秒），已中止。")
            self.lines.put("__EXIT__-1")
        except Exception as exc:
            self.lines.put(f"启动导入器失败：{type(exc).__name__}: {exc}")
            self.lines.put("__EXIT__-1")

    def _pump(self) -> None:
        try:
            while True:
                line = self.lines.get_nowait()
                if line.startswith("__EXIT__"):
                    self._finish(int(line.replace("__EXIT__", "")))
                else:
                    self._say(line)
        except queue.Empty:
            pass
        self.root.after(120, self._pump)

    def _finish(self, code: int) -> None:
        self.busy = False
        self.start_button.configure(state="normal", text="开始导入", bg=ACCENT)
        if code == 0:
            self._say("导入完成。", OK_COLOR)
            self.outcome = {"ok": True}
            self._refresh_history()
            self._say("已生成看板数据，点「打开历史看板」查看。", OK_COLOR)
            try:
                payload = json.loads(
                    (PROJECT_DIR / "data.js").read_text(encoding="utf-8").split("=", 1)[1]
                    .rstrip().rstrip(";")
                )
                self._say(f"当前共 {len(payload.get('runs') or [])} 场跑步记录。", MUTED)
            except Exception:
                pass
        else:
            self._say("导入未完成，请按上面的提示处理。", ERR_COLOR)
            # 失败也要把按钮恢复成「看历史」的状态：历史数据还在，不该被一次失败的导入锁住
            self._refresh_history()


def main() -> int:
    preset = None
    for raw in sys.argv[1:]:
        candidate = Path(raw)
        if candidate.suffix.lower() == ".zip":
            preset = candidate
            break
    root = tk.Tk()
    try:
        root.iconbitmap(default="")  # 某些环境没有默认图标，忽略失败
    except Exception:
        pass
    ImportWindow(root, preset)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
