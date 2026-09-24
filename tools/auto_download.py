# -*- coding: utf-8 -*-
"""用脚本自己的浏览器把小米的导出包下载下来（自动下载那一跳）。

小米那个下载链接认 `account.xiaomi.com` 的登录态，裸 HTTP 请求会被重定向到登录页
（实测）。这里用一个**独立的浏览器 profile**（`local/browser-profile/`，已 gitignore）：
你在里面登录一次，登录态就归它所有，之后每次导出它都能自己下载。
脚本不存账号密码 —— 密码由你在窗口里自己输。

    python tools/auto_download.py --url <下载页> --dest <保存目录>

进度打到 stdout，最后一行 `SAVED=<完整路径>`，调用方靠它拿文件。

三条别改回去的结论（都实测过）：
1. **别改回「等 Playwright 的 download 事件」** —— 包 2 秒就下好了，那个事件 92 秒
   都不发，老版本卡在这儿（等满 300 秒判失败）。现在点击的同一瞬间截下签名直链，
   用 `context.request.get` 取回（直链认 cookie，裸 urllib 是 403）。
2. **别再加 `about:blank` 兜底页 / 别开第二个窗口** —— 用户看到的就是「浏览器瞎开」。
3. **别默认开可见窗口** —— 有头启动会抢前台。默认无头静默跑，只有登录态过期时
   才换有头全屏并置前（无头里没法扫码），那是唯一会打扰用户的一次。
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.parse

PROJECT_DIR = Path(__file__).resolve().parent.parent
PROFILE_DIR = PROJECT_DIR / "local" / "browser-profile"
LOGIN_HOST = "account.xiaomi.com"

# 只用 Edge（用户日常就用它登录小米），**不做 Chrome 回退** ——
# 回退的代价是出错时又弹一个浏览器出来。
BROWSER_CHANNEL = "msedge"
# 需要露面的那种跑法。`no_viewport` 必须一起给：否则页面仍按 1280x720 渲染，
# 全屏窗口里四周留白，看着像没全屏。
FULLSCREEN_ARGS = ("--start-fullscreen", "--no-first-run", "--no-default-browser-check")
WINDOWED_ARGS = ("--window-size=1600,1000", "--window-position=40,20")

# 点完之后最多试几次、每次给它多久出反应（总上限 6 × 1.2 秒）。
CLICK_ATTEMPTS = 6
CLICK_SETTLE_SECONDS = 1.2

# 找「文件 #1」按钮的探测脚本。实拍 DOM：`<div class="file_button"><div>文件 #1</div>…`
# —— 是带 JS 处理的 div，不是 <a>/<button>，而且必须点**外层 .file_button**：
# 点里层那个文字节点什么都不会发生（事件处理要 `e.target` 就是按钮本身）。
# 找到后打个标记再按标记定位，比返回选择器重新匹配稳（选择器可能命中多个元素）。
_PROBE_JS = """() => {
  const vis = el => !!el && el.getClientRects().length > 0;
  document.querySelectorAll('[data-ra-target]').forEach(e => e.removeAttribute('data-ra-target'));
  const mark = (el, why) => { el.setAttribute('data-ra-target', '1'); return why; };
  const bySelector = ['.file_button', '[class*="file_button"]', 'a[href*="download"]', 'button'];
  for (const s of bySelector) {
    for (const el of document.querySelectorAll(s)) {
      if (vis(el)) return mark(el, s);
    }
  }
  for (const el of document.querySelectorAll('div, span, a, button')) {
    const t = (el.textContent || '').trim();
    if (t.startsWith('文件 #') && vis(el)) {
      return mark(el.closest('[class]') || el, '文字匹配：' + t.slice(0, 12));
    }
  }
  return null;
}"""


def say(msg: str = "") -> None:
    print(msg, flush=True)


_START = time.time()


def say_step(msg: str) -> None:
    """带累计耗时的进度行。"""
    say("  [%5.1fs] %s" % (time.time() - _START, msg))


def brief(exc) -> str:
    """异常只报第一行 —— Playwright 的错误里带着整段请求/响应头，
    原样打进日志就是十几行噪声，真正的原因反而被埋掉。"""
    text = str(exc) or type(exc).__name__
    return text.splitlines()[0][:160]


def on_login_page(url: str) -> bool:
    return LOGIN_HOST in urllib.parse.urlparse(url).netloc


def is_package_url(url: str) -> bool:
    """是不是那个 zip 的下载地址。"""
    try:
        return urllib.parse.urlparse(url).path.lower().endswith(".zip")
    except Exception:
        return False


def find_trigger(page, timeout: float = 15.0, quiet: bool = False):
    """找到「文件 #1」按钮，返回按标记定位的 locator。

    轮询而不是死等固定时长：按钮一出现就返回（正常 0.2 秒内）。
    `quiet=True`：找不到返回 None，不报错不截图（只想问一下它在不在时用）。
    """
    deadline = time.time() + timeout
    last_error = None
    while True:
        try:
            hit = page.evaluate(_PROBE_JS)
        except Exception as exc:       # 页面还在导航时会说执行上下文没了
            hit, last_error = None, exc
        if hit:
            if not quiet:
                say_step("找到下载入口：%s" % hit)
            return page.locator('[data-ra-target="1"]').first
        if time.time() >= deadline:
            break
        time.sleep(0.15)
    if quiet:
        return None
    dump_page(page, "no-button")
    raise RuntimeError(
        "等了 %d 秒没找到「文件 #1」按钮（导出还没生成，或页面改版）。"
        "已把截图存下来。最后错误：%s（如果是静默跑的，可以加 --show 用可见窗口再试）"
        % (int(timeout), last_error)
    )


def dump_page(page, tag: str) -> None:
    """截图 + 列出页面上可点元素的文字，排查「为什么找不到按钮」全靠它。

    截图放 %TEMP%\\run-analytics\\，不进仓库。
    """
    out_dir = Path(tempfile.gettempdir()) / "run-analytics"
    out_dir.mkdir(parents=True, exist_ok=True)
    shot = out_dir / ("download-page-%s.png" % tag)
    try:
        page.screenshot(path=str(shot), full_page=True)
        say("  页面截图已存: %s" % shot)
        say("SHOT=%s" % shot)
    except Exception as exc:
        say("  截图失败：%s" % exc)
        return
    try:
        say("  页面标题: %s" % page.title())
        say("  页面地址: %s" % page.url)
        texts = [t.strip() for t in page.locator("a, button").all_inner_texts() if t and t.strip()]
        if texts:
            say("  页面上 a/button 的文字（前 40 个）：%s" % " | ".join(texts[:40]))
        else:
            say("  （页面上没有 a / button 元素）")
    except Exception as exc:
        say("  枚举页面元素失败：%s" % exc)


def _profile_pids():
    """还在用我们这个 profile 的浏览器进程 pid（按命令行匹配）。"""
    if os.name != "nt":
        return []
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='chrome.exe'\""
        " | Where-Object { $_.CommandLine -like '*%s*' }"
        " | Select-Object -ExpandProperty ProcessId" % PROFILE_DIR
    )
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return []
    return [line.strip() for line in (done.stdout or "").splitlines() if line.strip().isdigit()]


def close_lingering_browser() -> None:
    """关掉上一次遗留的、用我们这个 profile 的浏览器，并等它真的退干净。

    不等就会踩 Chromium 的单例机制：新进程把请求转交给残留实例后自己退出，
    Playwright 报「Target page, context or browser has been closed」。

    匹配只看命令行里有没有我们这个 profile 路径，**绝不按进程名扫** ——
    那会把用户自己开的 Edge 一起杀掉。
    """
    pids = _profile_pids()
    if not pids:
        return
    say_step("上一次的浏览器还开着，先关掉（%d 个进程）…" % len(pids))
    for pid in pids:
        try:
            subprocess.run(
                ["taskkill", "/PID", pid, "/T", "/F"],
                capture_output=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass
    deadline = time.time() + 10
    while time.time() < deadline and _profile_pids():
        time.sleep(0.15)
    say_step("profile 已空出来。")


def _open_context(p, headless: bool, windowed: bool = False):
    """用系统里已经装好的 Edge 起一个带独立 profile 的浏览器（不下载 150 MB 的浏览器）。

    默认无头：屏幕上不出现窗口，也不抢前台，实测比有头还快。
    `headless=False` 只在**登录态过期**时用 —— 无头里没法扫码。
    """
    if headless:
        say_step("静默跑：系统 Edge（无头，不弹窗口）")
        return p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            channel=BROWSER_CHANNEL,
            headless=True,
            accept_downloads=True,
            args=["--no-first-run", "--no-default-browser-check"],
        )
    say_step("可见窗口：系统 Edge%s（这次需要你操作）"
             % ("1600x1000" if windowed else "全屏"))
    try:
        return p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            channel=BROWSER_CHANNEL,
            headless=False,
            accept_downloads=True,
            # 见 FULLSCREEN_ARGS 的说明：全屏必须配 no_viewport
            no_viewport=True,
            args=list(WINDOWED_ARGS if windowed else FULLSCREEN_ARGS),
        )
    except Exception as exc:
        message = str(exc)
        if "Executable doesn't exist" in message or "channel" in message.lower():
            raise RuntimeError(
                "没找到系统里的 Edge。装一个，或跑一次 `playwright install msedge`。"
                "原始错误：%s" % message
            ) from exc
        raise


def _launch_browser(p, headless: bool, windowed: bool = False):
    """起浏览器；失败一次就清一遍残留再试。

    为什么值得重试：profile 被上一个实例占住时，Chromium 会**以 exitCode=21 直接退出**，
    而 Playwright 报到上层的是「Target page, context or browser has been closed」——
    看不出真正原因（实测踩过：一次失败被这句掩盖，白查了一轮）。
    """
    try:
        return _open_context(p, headless, windowed)
    except Exception as exc:
        say_step("起不来（%s），清一遍残留的浏览器再试…" % brief(exc))
        close_lingering_browser()
        time.sleep(0.5)
        return _open_context(p, headless, windowed)


def _open_page(context, page_url: str):
    """在 context 里开一个页面并打开下载页（context 里本来就有且只有一个）。"""
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(page_url, wait_until="domcontentloaded", timeout=60_000)
    return page


def _filename_for(url: str, headers) -> str:
    """文件叫什么：优先响应头，其次地址末段。"""
    disposition = headers.get("content-disposition") or ""
    if "filename=" in disposition:
        name = disposition.split("filename=", 1)[1].strip().strip('"').strip("'")
        if name:
            return Path(name).name
    return Path(urllib.parse.urlparse(url).path).name or "MiFitness_export.zip"


def _fetch_package(context, url: str, dest_dir: Path, timeout_seconds: int) -> Path:
    """用浏览器上下文自己的请求器取文件（带着登录 cookie）。

    包是 10 MB 量级，一次性读进内存再落盘够用。
    """
    say_step("取文件中…")
    response = context.request.get(url, timeout=max(60, timeout_seconds) * 1000)
    if response.status != 200:
        raise RuntimeError("取文件返回 HTTP %d（直链可能已过期）。" % response.status)
    length = response.headers.get("content-length")
    if length:
        say_step("服务端 200，共 %.1f MB，下载中…" % (int(length) / 1048576))
    body = response.body()
    target = Path(dest_dir) / _filename_for(url, response.headers)
    if target.exists():          # 同名旧包先清掉，不留两份
        target.unlink()
    target.write_bytes(body)
    say_step("已保存 %s（%.1f MB）" % (target.name, len(body) / 1048576))
    return target


def _save_download(context, page, dest_dir: Path, timeout_seconds: int) -> Path:
    """点「文件 #1」，把包存到 dest_dir。

    主路：`context.route` 拦下那个 `.zip` 请求，记地址 + 掐掉浏览器自己那一份，
    再用 `context.request.get` 取回。备路：万一拦不到，退回等 `download` 事件。
    """
    zip_urls: list[str] = []
    caught: list = []

    def on_route(route):
        url = route.request.url
        try:
            if is_package_url(url):
                if url not in zip_urls:
                    zip_urls.append(url)
                    say_step("截到下载直链")
                route.abort()
            else:
                route.continue_()
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass

    context.route(is_package_url, on_route)
    context.on("download", lambda item: caught.append(item))

    # 点击没反应时的唯一线索。各限 200 条，别让它无限涨。
    console_lines: list[str] = []
    failed_requests: list[str] = []

    def note_console(msg):
        if len(console_lines) < 200:
            console_lines.append("%s: %s" % (msg.type, msg.text))

    def note_failure(req):
        if len(failed_requests) < 200:
            failed_requests.append("%s %s (%s)"
                                   % (req.method, req.url.split("?")[0], req.failure))

    page.on("console", note_console)
    page.on("requestfailed", note_failure)

    locator = find_trigger(page)

    # 这页的点击处理是后挂上的。**点完立刻看反应，没反应再点** ——
    # 别改回「等 networkidle 再死等 2 秒」那种赌法。
    for attempt in range(1, CLICK_ATTEMPTS + 1):
        try:
            locator.click(timeout=8_000)
        except Exception as exc:
            # 点击报错不代表没发出去，继续等结果
            say_step("第 %d 次点击时出了点状况（%s），继续看结果…"
                     % (attempt, type(exc).__name__))
        deadline = time.time() + CLICK_SETTLE_SECONDS
        while time.time() < deadline and not zip_urls and not caught:
            time.sleep(0.05)
        if zip_urls or caught:
            break
    else:
        say_step("点了 %d 次都没反应。" % CLICK_ATTEMPTS)

    if zip_urls:
        try:
            return _fetch_package(context, zip_urls[0], dest_dir, timeout_seconds)
        except Exception as exc:
            say_step("自己取文件失败（%s），退回等浏览器下载事件…" % brief(exc))
            try:
                context.unroute(is_package_url)
                locator.click(timeout=8_000)
            except Exception:
                pass
            deadline = time.time() + timeout_seconds
            while time.time() < deadline and not caught:
                time.sleep(0.1)

    if caught:
        item = caught[0]
        target = Path(dest_dir) / (item.suggested_filename or "MiFitness_export.zip")
        if target.exists():
            target.unlink()
        item.save_as(str(target))
        say_step("已保存 %s（%.1f MB）" % (target.name, target.stat().st_size / 1048576))
        return target

    try:
        pages = list(context.pages)
    except Exception:            # 浏览器已经没了，看不了页面
        pages = []
    say("  没拿到下载结果。context 里的页面：")
    for extra in pages:
        try:
            say("    %s" % extra.url)
        except Exception:
            pass
    say("  控制台输出（末尾 12 条）：")
    for line in console_lines[-12:]:
        say("    %s" % line[:200])
    say("  失败/出错的请求：")
    for line in failed_requests[-12:]:
        say("    %s" % line[:200])
    if not failed_requests:
        say("    （没有失败请求 —— 说明点击压根没发出请求）")
    try:
        dump_page(context.pages[0], "no-download")
    except Exception:
        pass
    raise RuntimeError(
        "点了「文件 #1」，既没截到直链也没等到下载事件。"
        "页面可能改版了，或者下载被拦住了 —— 把上面的记录发我。"
    )


def download(page_url: str, dest_dir: Path, wait_login_seconds: int,
             download_timeout_seconds: int, windowed: bool = False,
             show: bool = False) -> Path:
    """默认全程静默（无头，不出现任何窗口）。

    例外只有一个：进下载页发现被弹到登录页（登录态过期）→ 无头里没法扫码，
    换成有头全屏窗口并置前。`show=True`（`--show`）可强制一开始就要窗口。
    """
    from playwright.sync_api import sync_playwright

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    close_lingering_browser()

    with sync_playwright() as p:
        context = _launch_browser(p, headless=not show, windowed=windowed)
        try:
            page = _open_page(context, page_url)
            if on_login_page(page.url) and not show:
                say_step("登录态过期 —— 这次需要你露个面，换成可见窗口。")
                try:
                    context.close()
                except Exception:
                    pass
                context = _launch_browser(p, headless=False, windowed=windowed)
                page = _open_page(context, page_url)
            if on_login_page(page.url):
                say_step("请在窗口里登录：账号密码，或切到微信扫码。")
                _tick_agreement(page)
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                # 自己轮询而不是 wait_for_url：要每 10 秒报一次还剩多久，
                # 安静地等 5 分钟看起来就是「卡死」。
                deadline = time.time() + wait_login_seconds
                last_note = time.time()
                while time.time() < deadline:
                    try:
                        if not on_login_page(page.url):
                            break
                    except Exception:
                        break
                    time.sleep(1)
                    if time.time() - last_note >= 10:
                        last_note = time.time()
                        say_step("还在等你登录…（剩 %d 秒）"
                                 % max(0, int(deadline - time.time())))
                else:
                    raise RuntimeError(
                        "等了 %d 秒还没登录成功。请在窗口里登录后重试。"
                        % wait_login_seconds
                    )
                say_step("登录完成，继续。")
                # 有些流程登录完不会自动回到下载页：按钮不在就再进一次
                if find_trigger(page, timeout=2.0, quiet=True) is None:
                    say_step("登录后没停在下载页，重新进一次…")
                    page = _open_page(context, page_url)
            return _save_download(context, page, dest_dir, download_timeout_seconds)
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            # 这句 Playwright 错误有两种来源：人把可见窗口关了，或 profile 被残留实例占住
            # （后者 Chromium 会以 exitCode=21 退出）—— 所以别只写「窗口被关掉了」。
            if "has been closed" in message or "Target page" in message:
                raise RuntimeError(
                    "浏览器被关掉或起不来（可见窗口别关；也可能是上一次的实例还占着 profile）。"
                    "直接再跑一次；如果一直这样，删掉 local/browser-profile 重新登录一次。"
                ) from exc
            raise
        finally:
            try:
                context.close()
            except Exception:
                pass


def _tick_agreement(page) -> None:
    """小米登录页要先勾「已阅读并同意」，微信扫码才点得动。尽力勾，勾不上也不拦。

    刻意**不点协议文字本身** —— 那串文字里带链接，点上去会跳走；只碰复选框和 label。
    """
    for selector in (
        'input[type="checkbox"]:not(:checked)',
        'label:has-text("已阅读并同意")',
    ):
        try:
            box = page.locator(selector).first
            if not box.is_visible():
                continue
            box.click(timeout=3_000)
            say("  已自动勾选「已阅读并同意」。")
            return
        except Exception:
            continue
    say("  没找到可勾的协议框 —— 请手动勾一下。")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="小米导出包自动下载（脚本自己的 Edge）")
    parser.add_argument("--url", required=True, help="邮件里的下载页地址")
    parser.add_argument("--dest", required=True, help="保存目录")
    parser.add_argument("--wait-login", type=int, default=300,
                        help="等你登录最多等多少秒（默认 300）")
    parser.add_argument("--download-timeout", type=int, default=300,
                        help="取文件最多等多少秒（默认 300）")
    parser.add_argument("--show", action="store_true",
                        help="强制用看得见的窗口跑（默认静默无头）")
    parser.add_argument("--windowed", action="store_true",
                        help="可见时用 1600x1000 的小窗，不全屏")
    args = parser.parse_args(argv)

    try:
        target = download(args.url, Path(args.dest), args.wait_login,
                          args.download_timeout, args.windowed, args.show)
    except Exception as exc:
        say("自动下载失败：%s" % brief(exc))
        return 1
    print("SAVED=%s" % target, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
