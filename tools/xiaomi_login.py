# -*- coding: utf-8 -*-
"""打开小米账号登录页 → 自动勾选协议 → 登录。**到此为止**。

它替代的是这段人工动作：
    打开 account.xiaomi.com → 勾「已阅读并同意」→ 输账号密码 → 点登录

**登录之后的一切都由人做**：进隐私中心、过短信二次验证、申请导出 ——
那些是小米的敏感操作，脚本不该也不能替你过。多弗朗 2026-09-25 明确：
「做到帮我登录就行了，隐私中心什么的我自己做」。

设计要点
1. **和 auto_download.py 共用同一个浏览器 profile**（`local/browser-profile/`）。
   这是整个改动里最重要的一条。以前是「申请在你自己日常的 Edge 里、下载在脚本的
   profile 里」两套 cookie，于是隐私中心的短信授权存在 A、下载登录态存在 B，
   两边各自过期、各自要重来。现在全部收进同一个 profile：登录一次，
   申请和下载都复用，短信授权的有效期也一起续在同一条会话上。
2. **不抄 auto_download 的浏览器启动代码，直接 import。** profile 路径、Edge channel、
   残留进程清理、起不来时清一遍重试 —— 这些逻辑抄一份就多一处要同步的坑
   （仓库里已经为 SEARCH_DIRS 踩过一次）。
3. **账号和密码都不是必须的；密码默认不落盘。** 多弗朗 2026-09-25 反馈：
   「用 edge 打开网页就有默认的账号密码了」—— 那是浏览器的自动填充，不是配置文件。
   所以这里不强求任何配置：`local/xiaomi_config.json` 里写了就帮着填，没写就把表单
   留给窗口里的人（自动填充 / 手动敲都行）。**表单不全时代码绝不代点「登录」** ——
   点了是空提交，还会搅乱浏览器要弹的「保存密码」提示。反正登录态常驻 profile，
   输密码是一次性的。真要存密码：显式 `--save-password`，写入前用 **Windows DPAPI
   加密**（绑定当前这台机器的当前 Windows 用户，别的用户拿到文件也解不开）——
   与 fetch_from_mail.py「长期凭证才存」的口径一致。
4. **不假设登录一定一次过。** 点完登录可能弹「小米账号安全验证」（选安全手机 / 安全邮箱
   → 输验证码），也可能直接进账号首页。所以这里不做「一步到位」的假设，
   而是**轮询到离开登录页为止**，期间把该做什么打在控制台上。隐私中心那一跳同理。
5. **窗口归你，用完关掉就行。** 到了隐私中心脚本不抢跑也不自动关窗：保持打开，
   等你自己关掉浏览器后进程才结束 —— 因为接下来是你要做短信验证和申请导出。
6. **不抢前台这条规矩这里是有意放松的一次。** 平时所有自动化都静默无窗口；
   但这一次是人明确要求「给我开个登录页」，所以要露面、并且要置前，否则窗口开在
   后面等于没开。窗口也**只有一个**。

用法
    python tools/xiaomi_login.py                  # 开窗 → 勾协议 → 登录（到此为止，隐私中心自己点）
    python tools/xiaomi_login.py --no-fill        # 只填账号与勾协议，密码你自己敲（不落盘）
    python tools/xiaomi_login.py --qr            # 走微信/支付宝扫码，完全不碰密码
    python tools/xiaomi_login.py --save-password  # 把密码用 DPAPI 加密存进 local/xiaomi_config.json
    python tools/xiaomi_login.py --goto-only      # 已登录过，只要跳到隐私中心
    python tools/xiaomi_login.py --probe          # 导出当前页结构（诊断「找不到按钮」用）
    python tools/xiaomi_login.py --fullscreen     # 用全屏而不是 1600x1000 小窗

返回码
    0 登录完成（或本来就已是登录态）｜2 只在 --save-password 没给账号时出现
    3 登录没成功（超时）｜5 浏览器起不来或中途被关掉
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.wintypes as wt
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
import time

# 兄弟脚本 auto_download.py 是「用哪个浏览器 profile、怎么起浏览器」这条事的唯一权威。
# 直接 import 而不是再抄一份 —— 理由见模块开头设计要点 2。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import auto_download as ad  # noqa: E402

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "local" / "xiaomi_config.json"

LOGIN_URL = "https://account.xiaomi.com/"

# 实测（2026-09-25 无头抓 DOM，登录页 https://account.xiaomi.com/fe/service/login/password）：
#   账号框  <input name="account" aria-label="邮箱/手机号码/小米ID" class="mi-input__input">
#   密码框  <input name="password" type="password" aria-label="密码" class="mi-input__input">
#   协议框  <input type="checkbox" class="ant-checkbox-input">，祖先 label.ant-checkbox-wrapper
#           → div.mi-accept-terms；label 文字「已阅读并同意小米账号使用协议和小米账号隐私政策」
#   登录键  <button type="submit">登录</button>
#   扫码键  <button aria-label="扫码登录">
#   干扰项  <button class="mi-cookie-banner__button">同意并关闭提示</button>
#           —— **这个 cookie 提示条会盖在表单上**，不先关掉，点登录可能被它拦下。
#           这是实测才发现的，别删。
ACCOUNT_SEL = 'input[name="account"]'
PASSWORD_SEL = 'input[name="password"]'
TERMS_INPUT_SEL = 'input[type="checkbox"].ant-checkbox-input'
TERMS_LABEL_SEL = 'label:has-text("已阅读并同意")'
SUBMIT_SEL = 'button[type="submit"]'
QR_BUTTON_SEL = 'button[aria-label="扫码登录"]'
COOKIE_BANNER_SEL = 'button.mi-cookie-banner__button'

# 登录页的判定：URL 路径里带 /login。注意不能用 auto_download.on_login_page ——
# 那个是看主机名，而登录成功之后主机名仍然是 account.xiaomi.com，判不出来。
LOGIN_PATH_MARKERS = ("/login", "/fe/service/login", "/pass/forgetPassword")


def say(msg: str = "") -> None:
    print(msg, flush=True)


def say_step(msg: str) -> None:
    ad.say_step(msg)


class NeedLogin(RuntimeError):
    """没登录成 —— 带上该怎么做的一句话。"""


# ------------------------------------------------------------------ DPAPI
# 用 ctypes 直接调 crypt32，不引入 pywin32：本机只有 venv 里有 playwright，
# 装第三方包只为加密一个密码不值得。DPAPI 的密文绑定「当前机器 + 当前 Windows 用户」，
# 同一个文件拷到别的电脑或别的用户下解不开 —— 这正是我们要的性质。
CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _to_blob(data: bytes) -> _BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _from_blob(blob: _BLOB) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _crypt32():
    lib = ctypes.WinDLL("crypt32", use_last_error=True)
    lib.CryptProtectData.argtypes = [
        ctypes.POINTER(_BLOB), wt.LPCWSTR, ctypes.POINTER(_BLOB),
        ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(_BLOB)]
    lib.CryptProtectData.restype = wt.BOOL
    lib.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_BLOB), ctypes.POINTER(wt.LPWSTR), ctypes.POINTER(_BLOB),
        ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(_BLOB)]
    lib.CryptUnprotectData.restype = wt.BOOL
    return lib


def dpapi_available() -> bool:
    return os.name == "nt"


def dpapi_protect(text: str) -> str:
    """明文 → base64 密文。"""
    lib = _crypt32()
    blob_in = _to_blob(text.encode("utf-8"))
    blob_out = _BLOB()
    if not lib.CryptProtectData(ctypes.byref(blob_in), None, None, None, None,
                               CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return base64.b64encode(_from_blob(blob_out)).decode("ascii")
    finally:
        ctypes.WinDLL("kernel32").LocalFree(blob_out.pbData)


def dpapi_unprotect(cipher_b64: str) -> str:
    """base64 密文 → 明文。换机器或换用户会失败，这是设计如此。"""
    lib = _crypt32()
    raw = base64.b64decode(cipher_b64)
    blob_in = _to_blob(raw)
    blob_out = _BLOB()
    if not lib.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None,
                                  CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return _from_blob(blob_out).decode("utf-8")
    finally:
        ctypes.WinDLL("kernel32").LocalFree(blob_out.pbData)


# ------------------------------------------------------------------ 配置
def load_config() -> dict:
    if not CONFIG_PATH.is_file():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        say("读 %s 失败（%s），当没有配置处理。" % (CONFIG_PATH, exc))
        return {}


def save_config(payload: dict) -> None:
    """写配置。落盘前把文件权限收紧一点（尽力而为，失败不拦）。"""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if os.name == "nt":
        try:
            # 只给当前用户读写：DPAPI 已经加密了密码，这是第二道。
            os.system('icacls "%s" /inheritance:r /grant:r "%%USERNAME%%:F" >nul 2>&1'
                      % CONFIG_PATH)
        except Exception:
            pass


def resolve_password(config: dict, override: str):
    """返回 (密码, 说明)。没有可用的就返回 (None, 原因)。

    DPAPI 密文优先于明文。明文只在手改过配置的情况下才可能出现，会明确警告 ——
    不能让人以为「我写进去了就等于安全」。
    """
    if override:
        return override, "命令行参数"
    cipher = (config.get("password_dpapi") or "").strip()
    if cipher:
        try:
            return dpapi_unprotect(cipher), "DPAPI 加密（绑定本机当前用户）"
        except Exception as exc:
            say("配置里的密码解不开（%s）。" % (str(exc).splitlines()[0][:120]))
            say("换过电脑或换过 Windows 用户就会这样 —— 重跑一次 --save-password 即可。")
    plain = (config.get("password") or "").strip()
    if plain:
        say("⚠ 配置里存的是**明文**密码（password 字段）。建议改成 --save-password 加密存。")
        return plain, "明文（不推荐）"
    return None, None


# ------------------------------------------------------------------ 页面动作
def dismiss_cookie_banner(page) -> None:
    """关掉登录页底部的 cookie 提示条。

    **不关会挡住表单**（实测：`button.mi-cookie-banner__button`「同意并关闭提示」）。
    这里刻意是「尽力而为」：小米去掉这个条之后，找不到就安静跳过，不能因为一个
    可选元素把整条路堵死。
    """
    try:
        btn = page.locator(COOKIE_BANNER_SEL).first
        if btn.count() and btn.is_visible():
            btn.click(timeout=3_000)
            say("  已关掉 cookie 提示条。")
    except Exception:
        pass


def is_login_url(url: str) -> bool:
    low = (url or "").lower()
    return any(marker.lower() in low for marker in LOGIN_PATH_MARKERS)


def tick_terms(page) -> str:
    """勾「已阅读并同意」。

    只碰复选框和它的 label —— **绝不点协议文字本身**，那串文字里带链接，点上去就跳走了。
    点完回读 checked 状态：有些框架的点击会被二次渲染吃掉，读一次才算数。

    （auto_download.py 里有个更宽松的 `_tick_agreement`，是登录态过期那条路上用的；
    这里选择器更精确，因为这一页是主角、值得写死。两处统一留作后续。）
    """
    for selector, why in ((TERMS_INPUT_SEL, "精确选择器"),
                          (TERMS_LABEL_SEL, "按文字匹配 label")):
        try:
            box = page.locator(selector).first
            if not box.count() or not box.is_visible():
                continue
            box.click(timeout=5_000)
            time.sleep(0.2)
            state = page.evaluate(
                "() => { const c = document.querySelector('input[type=\"checkbox\"]');"
                " return c ? !!c.checked : null; }")
            if state:
                return "已勾选「已阅读并同意」（%s）" % why
        except Exception:
            continue
    return "没能勾上协议框 —— 请在窗口里手动勾一下"


def fill_form(page, account: str, password: str):
    """填账号 / 密码。返回实际填了哪些。**空值直接跳过** ——
    把空串填进输入框还报「已填：账号」是在撒谎，而且会把浏览器自动填充
    正要带出来的值清掉。
    """
    filled = []
    if account:
        try:
            acc = page.locator(ACCOUNT_SEL).first
            if acc.count() and acc.is_visible():
                acc.fill(account, timeout=5_000)
                filled.append("账号")
        except Exception as exc:
            say("  填账号失败：%s" % ad.brief(exc))
    if password:
        try:
            pwd = page.locator(PASSWORD_SEL).first
            if pwd.count() and pwd.is_visible():
                pwd.fill(password, timeout=5_000)
                filled.append("密码")
        except Exception as exc:
            say("  填密码失败：%s" % ad.brief(exc))
    return filled


def click_submit(page) -> bool:
    try:
        page.locator(SUBMIT_SEL).first.click(timeout=8_000)
        return True
    except Exception as exc:
        say("  点「登录」失败：%s" % ad.brief(exc))
        return False


def click_qr_tab(page) -> bool:
    """切到扫码登录。不碰密码 —— 对「密码不落盘」这条路来说这是最干净的一种。"""
    try:
        page.locator(QR_BUTTON_SEL).first.click(timeout=8_000)
        return True
    except Exception as exc:
        say("  切扫码登录失败：%s" % ad.brief(exc))
        return False


def wait_off_login(page, timeout: float, hint: str) -> bool:
    """轮询到离开登录页为止。

    自己轮询而不是 wait_for_url：要每 10 秒报一次状态。
    安静地等 5 分钟，看起来就是「卡死了」—— 这个文件旁边的 auto_download.py 里
    也是同样的理由和同样的写法。
    """
    deadline = time.time() + timeout
    last_note = 0.0
    while time.time() < deadline:
        try:
            if not is_login_url(page.url):
                return True
            if page.is_closed():
                raise NeedLogin("浏览器窗口被关掉了。")
        except NeedLogin:
            raise
        except Exception:
            return True          # 页面在导航中，够不上判定；下一轮再看
        if time.time() - last_note >= 10:
            last_note = time.time()
            say_step("%s（还剩 %d 秒）" % (hint, max(0, int(deadline - time.time()))))
        time.sleep(1)
    return False


def _run_ps(script: str) -> str:
    """跑一段 PowerShell，把 stdout 原样带回。失败 / 超时都返回空串。

    本机 PowerShell 的 stdout 是 GBK，按 UTF-8 解码会在读线程里直接抛
    UnicodeDecodeError（实测踩过）；这里只读 ASCII（数字和标记），
    所以 errors="replace" 就够，不会炸。
    """
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=25,
            encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return ""
    return done.stdout or ""


def _profile_pids() -> list:
    """挂着我们 profile 的浏览器进程 pid。慢查询，只在进入等待前做一次。"""
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='chrome.exe'\""
        " | Where-Object { $_.CommandLine -like '*" + str(ad.PROFILE_DIR) + "*' }"
        " | ForEach-Object { [int]$_.ProcessId }"
    )
    out = _run_ps(script)
    return [int(line) for line in out.split() if line.strip().isdigit()]


def _visible_windows(pid_csv: str) -> int:
    """这些进程里还有几个带着**可见主窗口**。

    实测（2026-09-25 探针，用 CloseMainWindow 模拟用户点 X 关窗）三个信号里
    只有一个会翻转：
        可见窗口      1 -> 0   ← 就是它
        profile 进程  9 -> 7   Edge 的启动加速把进程留在后台，永远不归零
        Playwright    pages=2、is_closed=False，从头到尾一动不动
    所以"网站关掉没有"只能数可见窗口；进程数和 Playwright 状态都是幻觉。
    返回 -1 表示这次没问到（超时等），调用方按"继续等"处理，绝不误判成已关闭。
    """
    if not pid_csv:
        return -1
    script = (
        "$ids = @(" + pid_csv + ")\n"
        "$win = @(Get-Process msedge,chrome -ErrorAction SilentlyContinue"
        " | Where-Object { $_.MainWindowHandle -ne 0 -and $ids -contains [int]$_.Id })\n"
        "Write-Output $win.Count"
    )
    out = _run_ps(script).strip()
    return int(out) if out.isdigit() else -1


def keep_open_until_closed(context, page) -> None:
    """不自动关窗：等你自己关掉浏览器。

    接下来是你要做的那一段（进隐私中心、过短信、申请导出），脚本抢着关窗只会碍事。
    判定"你关了"只认一个信号：**屏幕上还有没有这个网站的可见窗口**（每 2 秒数一次）。
    实测 Playwright 的页面状态和进程数在关窗后都不翻转，唯有可见窗口会 1 → 0。
    """
    say("")
    say("窗口交给你了。接下来在窗口里做事：")
    say("  · 如果弹出「小米账号安全验证」，按提示选安全手机 / 安全邮箱并输入验证码；")
    say("  · 隐私中心、申请导出这些由你自己来 —— 本脚本只做到登录为止；")
    say("  · **做完直接把浏览器窗口关掉**，这个脚本就会自己结束。")
    say("")
    say("（注意：登录态和这次的短信授权都会留在脚本自己的 profile 里 —— "
        "local/browser-profile/。下次再跑通常就不用重新验证了。）")
    pids = _profile_pids()
    pid_csv = ",".join(str(v) for v in pids)
    if not pids:
        say_step("系统里已经没有挂本 profile 的浏览器进程了 —— 直接收尾。")
        say("浏览器已关闭，脚本结束。")
        return
    say_step("每 2 秒看一眼这个窗口；你把它关掉，我就收尾。")
    deadline = time.time() + 3600
    seen_window = False
    last_poll = 0.0
    while time.time() < deadline:
        try:
            if not context.pages or page.is_closed():
                break                       # Playwright 这条路要是通了，走它更快
        except Exception:
            break
        if time.time() - last_poll >= 2:
            last_poll = time.time()
            visible = _visible_windows(pid_csv)
            if visible > 0:
                seen_window = True          # 先确认它真的出现过，才谈得上"消失"
            elif visible == 0 and seen_window:
                say_step("窗口已经关了（可见窗口归零）——收尾。")
                break
        time.sleep(0.4)
    say("浏览器已关闭，脚本结束。")


# ------------------------------------------------------------------ 诊断
def probe(page) -> None:
    """把当前页面的可交互元素导出来 —— 排查「找不到某按钮」全靠它。

    与 fetch_from_mail.py 的 --peek 是同一个思路：能安全外发的结构描述。
    """
    try:
        data = page.evaluate("""() => {
          const cls = el => (el.className || '').toString().slice(0, 80);
          const vis = el => !!el && el.getClientRects().length > 0;
          return {
            url: location.href,
            title: document.title,
            inputs: [...document.querySelectorAll('input')].map(e => ({
              name: e.name, type: e.type, aria: e.getAttribute('aria-label'),
              visible: vis(e) })),
            buttons: [...document.querySelectorAll('button')].map(e => ({
              text: (e.innerText || '').trim().slice(0, 24),
              aria: e.getAttribute('aria-label'), type: e.type, visible: vis(e) })),
            clickableTexts: [...document.querySelectorAll('a, li, [role="button"]')]
              .filter(e => vis(e) && (e.innerText || '').trim())
              .map(e => (e.innerText || '').trim().slice(0, 24)).slice(0, 40)
          };
        }""")
    except Exception as exc:
        say("导出页面结构失败：%s" % ad.brief(exc))
        return
    say("")
    say("当前页结构（可安全外发）：")
    say("  URL  : %s" % data.get("url"))
    say("  标题 : %s" % data.get("title"))
    say("  输入框：")
    for item in data.get("inputs") or []:
        say("    name=%-12s type=%-10s aria=%-18s 可见=%s"
            % (item.get("name") or "-", item.get("type") or "-",
               item.get("aria") or "-", item.get("visible")))
    say("  按钮：")
    for item in data.get("buttons") or []:
        if item.get("visible"):
            say("    文字=%-16s aria=%-12s type=%s"
                % (item.get("text") or "-", item.get("aria") or "-", item.get("type") or "-"))
    say("  可点文字（前 40 个）：")
    say("    %s" % " | ".join(data.get("clickableTexts") or []))


# ------------------------------------------------------------------ 主流程
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="一键进小米隐私中心（自动勾协议 + 登录）")
    parser.add_argument("--no-fill", action="store_true",
                        help="只勾协议、只填账号，密码你在窗口里自己敲（不读配置里的密码）")
    parser.add_argument("--qr", action="store_true",
                        help="切到扫码登录（微信/支付宝/QQ），完全不碰密码")
    parser.add_argument("--password", help="本次使用的密码（不落盘；配 --save-password 才存）")
    parser.add_argument("--save-password", action="store_true",
                        help="把密码用 Windows DPAPI 加密后存进 local/xiaomi_config.json")
    parser.add_argument("--account", help="小米账号（默认读 local/xiaomi_config.json）")
    parser.add_argument("--goto-only", action="store_true",
                        help="已经登录过：只把窗口打开，不碰登录表单")
    parser.add_argument("--fullscreen", action="store_true",
                        help="用全屏窗口（默认 1600x1000）")
    parser.add_argument("--wait-login", type=int, default=300,
                        help="等你登录最多等多少秒（默认 300）")
    parser.add_argument("--probe", action="store_true",
                        help="到页面后导出结构（诊断用），不点任何东西")
    args = parser.parse_args(argv)

    config = load_config()
    account = (args.account or config.get("account") or "").strip()

    # --save-password 要在开浏览器之前做完：加密、落盘、然后照常继续跑。
    if args.save_password:
        secret = args.password or getpass.getpass("小米账号密码（输入不回显，只用于加密保存）：")
        if not secret:
            say("没拿到密码，什么都没存。")
            return 2
        if not account:
            say("存密码前得先有账号：加 --account 你的小米账号，或先写进 %s" % CONFIG_PATH)
            return 2
        try:
            config["account"] = account
            config["password_dpapi"] = dpapi_protect(secret)
            config.pop("password", None)      # 存了密的就清掉明文
            save_config(config)
        except Exception as exc:
            say("加密保存失败：%s" % ad.brief(exc))
            return 2
        say("已用 DPAPI 加密保存到 %s（绑定本机当前 Windows 用户）。" % CONFIG_PATH)
        say("")

    # 账号**不是必须的**：浏览器的自动填充可能已经把账号密码存进这个 profile 了
    # （第一次在窗口里登录时让浏览器记住即可）。配置里有就帮着填，没有就留给人。
    password, password_from = (None, None)
    if not args.no_fill and not args.goto_only:
        password, password_from = resolve_password(config, args.password)

    from playwright.sync_api import sync_playwright  # 延迟导入，--help 不该要求装了它

    ad.PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    ad.close_lingering_browser()

    with sync_playwright() as p:
        context = ad._launch_browser(p, headless=False, windowed=not args.fullscreen)
        try:
            page = ad._open_page(context, LOGIN_URL)
            # 开窗就要置前一次：不然窗口开在后面，等于没开。
            # （这是全局「静默不抢前台」规矩里唯一有意放松的一处，因为这次是人主动要的。）
            try:
                page.bring_to_front()
            except Exception:
                pass

            say_step("已打开小米账号页面。")
            dismiss_cookie_banner(page)

            if args.probe:
                probe(page)
                keep_open_until_closed(context, page)
                return 0

            if args.goto_only or not is_login_url(page.url):
                if args.goto_only:
                    say_step("--goto-only：跳过登录。")
                else:
                    say_step("这个 profile 已经是登录态，不用再登。")
            else:
                say_step("在登录页。开始自动填表…")
                say("  %s" % tick_terms(page))

                if args.qr:
                    if click_qr_tab(page):
                        say_step("已切到扫码登录 —— 用微信/支付宝扫窗口里的码。")
                else:
                    filled = fill_form(page, account, password or "")
                    if filled:
                        say("  已填：%s" % "、".join(filled))
                    if password:
                        say("  密码来源：%s" % password_from)
                    if account and password:
                        say_step("账号密码都齐了，点「登录」…")
                        click_submit(page)
                    else:
                        # 表单不全（浏览器自动填充会来补，或者用户自己敲）——
                        # **绝不代点「登录」**：点了是空提交，还可能搅乱浏览器
                        # 要弹的「保存密码」提示。剩下的事交给窗口里的人。
                        say_step("表单没填全，剩下的交给你：在窗口里补完，然后点「登录」。")
                        say("  （这个 profile 是脚本专用的：第一次在这里登录时让浏览器")
                        say("   记住密码，以后这个窗口就会自己把账号密码带出来。）")

                say_step("等你完成登录（如果弹出安全验证，请在窗口里选手机/邮箱并输码）…")
                if not wait_off_login(page, args.wait_login, "还在等你登录"):
                    say("")
                    say("等了 %d 秒还没离开登录页。" % args.wait_login)
                    say("窗口还开着，你可以继续在里面登录 —— 登录成功后本脚本会自己往下走。")
                    if not wait_off_login(page, 600, "继续等你登录"):
                        raise NeedLogin("登录超时（又等了 600 秒）。")
                say_step("登录完成。")

            # 到此为止：登录这件事脚本做，隐私中心那一串由人做（见模块 docstring）。
            keep_open_until_closed(context, page)
            return 0
        except NeedLogin as exc:
            say("")
            say("没登录成：%s" % exc)
            return 3
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            if "has been closed" in message or "Target page" in message:
                say("")
                say("浏览器被关掉或起不来（可见窗口别中途关；也可能是上一次的实例还占着 profile）。")
                say("直接再跑一次；如果一直这样，删掉 local/browser-profile 重新登录一次。")
                return 5
            say("")
            say("出错了：%s" % ad.brief(exc))
            return 5
        finally:
            try:
                context.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
