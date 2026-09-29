# -*- coding: utf-8 -*-
"""从 QQ 邮箱取解压密码，把本地已下载的小米导出包导入看板。

它替代的是这段人工动作：
    盯着邮箱 → 开 wx.mail.qq.com → 找到邮件 → 抄解压密码
    → 去 D:\\网页下载 找文件 → 拖进导入窗口 → 输密码

现在的分工：
    人：登录小米 → 申请导出 → 收到邮件 → 点链接 → 点「文件 #1」（包落到 D:\\网页下载）
    脚本：从邮件里取密码 → 捡起本地最新的包 → 导入看板数据

为什么「点文件 #1」这一步不能自动化：那个下载链接认 account.xiaomi.com 的登录态，
裸 HTTP 请求会被重定向到登录页（2026-09-24 实测，最终地址是 /fe/service/login）。
详见设计要点 10。

设计要点
1. **走 IMAP，不走网页。** imap.qq.com:993 + 授权码（不是 QQ 登录密码）。
   网页版 `wx.mail.qq.com` 是本项目刻意避开的路径：它需要人登录、会改版、
   还会把「抄密码」这一步变成手艺活。IMAP 只读邮件正文，稳定得多。
2. **凭证不进仓库。** 优先读环境变量，其次读 `local/mail_config.json`，
   两者都由 `.gitignore` 排除。授权码是长期凭证，和「每次会变的解压密码」不是一类东西，
   所以这里的存放策略和 import_zip.py「密码不落盘」并不冲突。
3. **解压密码仍然不落盘。** 从邮件里解析出来后只在本进程内存里，经环境变量传给
   导入器子进程，用完即弃。和导入窗口的行为完全一致。
4. **幂等。** 已处理过的邮件 UID 记在 `local/mail_state.json`，同一封不会重复下载导入。
   即使状态丢了，重导同一份包也只是把 data.js 重写一遍（导入本身是幂等的），
   而且 import_zip.py 有「包比看板旧就拒绝写入」的兜底（按最新一场的日期比），不会吞掉历史。
5. **没事可做时安静成功。** 没有新邮件时打印一行、返回 0 —— 它要被计划任务反复调用，
   不能因为「今天还没导出」就报错刷屏；也不会弹任何通知窗。
6. **两段式取信，别白拉流量。** 先用「只取 FROM/SUBJECT/DATE 头部」的轻请求挑候选，
   再只对候选拉整封。以前是一封不留地拉整封再本地过滤，邮件一多就是分钟级，附件也白拖。
   已经在状态里记过的 UID **在发请求之前**就跳过，所以定时任务重复跑时几乎是零流量。
7. **只用 PEEK，不留痕迹。** 取正文用 `BODY.PEEK[]` 而不是 `RFC822` —— 后者等价于
   `BODY[]`，会把邮件标记成已读，等于替用户改动了邮箱状态。这个工具只读，不该有副作用。
8. **不弹完成窗。** 看板内发起刷新时由看板展示进度并自动重载；直接运行脚本时
   结果只写在控制台。
9. **只翻收件箱，这是刻意的。** 小米的导出通知落在收件箱，没必要再去别的文件夹翻找。
   会话在 connect() 里 `SELECT INBOX` 一次，之后所有请求都在这个文件夹内。
10. **下载那一跳交给 tools/auto_download.py，本文件只负责调它。** 小米那个下载链接认
    `account.xiaomi.com` 的登录态，无 cookie 的裸请求会被重定向到登录页（实测确认）。
    所以那一步走脚本自己的浏览器（独立的 `local/browser-profile/`，你在里面登录过
    一次就长期有效）。**整条链路只有首次需要人工**：在脚本的窗口里登录。
    它失败时**不再顺手开你的默认浏览器**了 —— 那会让「没下成」变成「屏幕上又多了
    一个浏览器窗口」（多弗朗 2026-09-24 报过），改成把该怎么做写在输出里。
11. **只认最新一封。** 小米每次导出都是**全量**数据，最新的那封就已经是全部历史 ——
    所以只取最新一封的密码，不把旧邮件翻出来逐个试。多封积压时逐个试没有收益，
    反而让人搞不清「现在看板里到底是哪一次的数据」。

用法
    python tools/fetch_from_mail.py --check      # 只连邮箱、列出候选邮件
    python tools/fetch_from_mail.py --peek       # 打印最新一封邮件的结构（密钥打码），用来校准正则
    python tools/fetch_from_mail.py --dry-run    # 只说会用哪个本地包、有几个密码可试
    python tools/fetch_from_mail.py              # 完整跑：取密码 → 找本地包 → 导入 data.js
    python tools/fetch_from_mail.py --all        # 忽略已处理记录，重扫（排查用）

返回码
    **0 只在「真的导入了数据」时返回** —— 界面层拿它当成功判据，绝不能乱用。
    0 导入成功｜2 缺凭证｜3 IMAP 连接或认证失败
    4 没有匹配的导出邮件，或密码不对｜5 解析不出密码｜6 本地没找到包
    7 导入失败（导入器自身的返回码另有含义，见 import_zip.py）
    8 自动下载没完成、需要人工介入 —— 数据未导入
    9 没有新的导出邮件 / 没有需要处理的邮件 —— 数据未导入
    8 和 9 都是「一个字节都没导入」：回 0 会让界面报「导入完成」，
    等于拿上一次的看板数据冒充新结果（2026-09-24 真出过这个 bug）。

凭证怎么给（二选一）
    A) 环境变量（推荐，计划任务用这个）：
         setx QQ_MAIL_USER "你的QQ邮箱@qq.com"
         setx QQ_MAIL_AUTH_CODE "IMAP授权码"
    B) local/mail_config.json（内容：{"user": "...", "auth_code": "..."}）
       授权码在 QQ 邮箱网页版「设置 → 账户 → IMAP/SMTP 服务」里开启并生成。
"""
from __future__ import annotations

import argparse
import datetime
import email
import email.header
import email.utils
import html as html_mod
import imaplib
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import urllib.parse

# 兄弟脚本 import_zip.py 是「导出包放在哪」这件事的唯一权威（SEARCH_DIRS / find_latest_zip）。
# 这里直接 import 而不是再抄一份目录列表 —— 之前两份 SEARCH_DIRS 分开维护，
# 结果改了一处漏了另一处，包就一直找不到。抄一份就等于埋一次同样的坑。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import import_zip  # noqa: E402

PROJECT_DIR = Path(__file__).resolve().parent.parent
IMPORT_SCRIPT = Path(__file__).resolve().parent / "import_zip.py"
AUTO_DOWNLOAD_SCRIPT = Path(__file__).resolve().parent / "auto_download.py"
# 自动下载的落点放项目内：import_zip.py 找包时它排第一，而且不会把看板数据
# 和你在 D:\网页下载 里的其它下载混在一起。
INBOX = PROJECT_DIR / "inbox"
CONFIG_PATH = PROJECT_DIR / "local" / "mail_config.json"
STATE_PATH = PROJECT_DIR / "local" / "mail_state.json"

IMAP_HOST = "imap.qq.com"
IMAP_PORT = 993
# 连接必须显式设超时：socket.getdefaulttimeout() 默认是 None，网络卡住时
# 这个进程会永久挂住 —— 对计划任务来说就是「跑了但永远不结束」，而且不会报错。
# 实测本机 imaplib 在 Python 3.12 / 3.13 都没有 IDLE（hasattr(IMAP4,"idle") 为 False），
# 所以只能靠短连接轮询，超时就是唯一的止损手段。
IMAP_TIMEOUT = 30

# 候选邮件的判定特征 —— 2026-09-24 从真实邮件确认（多弗朗提供列表截图）：
#   发件人  Xiaomi Corporation <noreply@notice.xiaomi.com>
#   主题    小米运动健康-个人数据副本
#   正文    「您在小米运动健康的个人数据文件已准备就绪。」
#           「请通过以下链接下载副本。该副本将在 7 天后过期。」
#           「您的数据文件已加密。 密码如下：」
# 判定**从严**：发件人与主题都要命中（发件人是最难模仿的特征，没理由不用它把门 ——
# 之前是「或」，任何主题里带「数据」的邮件都能混进来），正文特征再交叉确认一次。
# 小米改版时只需要动这三个元组。
SENDER_HINTS = ("xiaomi.com", "mi.com", "小米")
SUBJECT_HINTS = ("小米运动健康-个人数据副本", "个人数据副本", "运动健康")
BODY_HINTS = ("个人数据文件已准备就绪", "数据文件已加密", "密码如下")
PASSWORD_HINTS = ("解压密码", "压缩包密码", "文件密码", "密码", "password")

# 默认只看最近这么多天的邮件，避免把整个邮箱拉下来
DEFAULT_SINCE_DAYS = 30
MAX_CANDIDATES = 8
# 最多回看多少封。配合 --since-days 一起收窄，两层都别放太宽。
SCAN_LIMIT = 120


def say(msg: str = "") -> None:
    """打一行进度。**必须 flush，别去掉**（2026-09-28 修）。

    工具轨是通过**管道**读这个进程的 stdout 的，而 Python 对非终端输出用块缓冲
    （8KB 一吐）。不 flush 的话，几十行进度会一直卡在缓冲区里 —— 界面就停在最初
    那句「正在连接邮箱，检查新的导出邮件」上，计时器一直涨而什么都不动。
    多弗朗 2026-09-28 报的「卡在那里了一直在刷新」就是它。

    更贵的一处：连「还在等你登录…（剩 N 秒）」也是被这条堵死的 —— 脚本明明正开着
    浏览器等他登录，界面上却一个字都没有，所以他只能猜「是不是账号不能登录了」。

    启动器那边同时加了 `-u`（见 `从邮箱导入.cmd`）。两道保险分工明确：这条管住
    所有经过 `say()` 的输出（包括从子进程转发回来的行），`-u` 管住别处的裸 print。
    """
    print(msg, flush=True)


# ------------------------------------------------------------------ 凭证
def _clean(value) -> str:
    """去掉首尾空白，并剥掉误包在外面的成对引号。

    环境变量里混进引号是个很常见的坑：cmd 的 `setx` **只认双引号，不认单引号** ——
    `setx QQ_MAIL_USER 'a@qq.com'` 会把那两个单引号一起存进去，于是邮箱地址变成
    `'a@qq.com'`，登录时报的错完全看不出原因。粘贴时带上尾随空格同理。
    这里顺手清掉，比让人对着一个看不见的引号排查半天强。
    """
    text = str(value or "").strip()
    for quote in ("'", '"'):
        if len(text) >= 2 and text.startswith(quote) and text.endswith(quote):
            text = text[1:-1].strip()
    return text


def _registry_user_env(name: str):
    """从 HKCU\\Environment 读用户级环境变量（`setx` 写的就是这里）。

    为什么需要它：**已经在跑的进程读不到 setx 的新值** —— 进程的环境块在启动那一刻
    就固定了。总控台后端是开机自启的，通常早于 setx，于是从总控台点「运行项目」
    拉起的整条链路都拿不到新设的凭证，表现为「明明 setx 过了却提示缺凭证」。
    读一次注册表就把这个洞补上，不用为了两个变量重启整个控制台。
    （本机 reg.exe 被安全策略拉黑，但 Python 的 winreg 不受影响。）
    """
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return _clean(value) or None


def load_credentials(args):
    """返回 (user, auth_code, 来源说明)；缺任何一项就返回 (None, None, 原因)。"""
    if args.user and args.auth_code:
        return _clean(args.user), _clean(args.auth_code), "命令行参数"
    env_user = os.environ.get("QQ_MAIL_USER") or _registry_user_env("QQ_MAIL_USER")
    env_code = os.environ.get("QQ_MAIL_AUTH_CODE") or _registry_user_env("QQ_MAIL_AUTH_CODE")
    if env_user and env_code:
        return _clean(env_user), _clean(env_code), "环境变量"
    if CONFIG_PATH.is_file():
        try:
            payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            user = _clean(payload.get("user") or env_user)
            code = _clean(payload.get("auth_code") or env_code)
            if user and code:
                return user, code, str(CONFIG_PATH)
        except Exception as exc:
            return None, None, "读 %s 失败：%s" % (CONFIG_PATH, exc)
    missing = []
    if not (args.user or env_user):
        missing.append("邮箱地址（QQ_MAIL_USER）")
    if not (args.auth_code or env_code):
        missing.append("IMAP 授权码（QQ_MAIL_AUTH_CODE）")
    return None, None, "缺 " + "、".join(missing)


# ------------------------------------------------------------------ 邮件正文
def decode_header(raw):
    if not raw:
        return ""
    parts = []
    for chunk, charset in email.header.decode_header(raw):
        if isinstance(chunk, bytes):
            try:
                parts.append(chunk.decode(charset or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                parts.append(chunk.decode("utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts)


def strip_html(text: str) -> str:
    """把 HTML 拉平成文本。只做够用的事：去脚本/样式、块级标签换行、反转义实体。"""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|li|h[1-6])\s*>", "\n", text)
    text = re.sub(r"(?i)<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>", r" \1 ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_body(msg) -> str:
    """收集邮件全部文本，plain 与 html 都留，拼在一起交给正则。"""
    chunks = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype not in ("text/plain", "text/html"):
                continue
            try:
                payload = part.get_payload(decode=True)
            except Exception:
                continue
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                text = payload.decode("utf-8", errors="replace")
            chunks.append(strip_html(text) if ctype == "text/html" else text)
    else:
        payload = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            text = payload.decode("utf-8", errors="replace")
        chunks.append(strip_html(text) if msg.get_content_type() == "text/html" else text)
    return "\n".join(chunks)


# ------------------------------------------------------------------ 解析
# URL 里带空格是非法的，但小米的包名「..._data_copy .zip」扩展名前真有个空格，
# 邮件正文的 href 偶尔会原样带过来。通用的 \S+ 会在空格处截断，捞到一个下不动的残链，
# 所以先用这条宽松规则把「含空格且以 .zip 结尾」的整串捞出来，再退回通用规则。
URL_SPACE_RE = re.compile(
    r"https?://[^\s\"'<>（）()\[\]]+(?:\s|%20)[^\s\"'<>（）()\[\]]*\.zip", re.I)
URL_RE = re.compile(r"https?://[^\s\"'<>（）()\[\]]+", re.I)


def looks_like_package_url(url: str) -> bool:
    low = url.lower()
    if low.endswith(".zip"):
        return True
    return any(token in low for token in ("mi-img.com", "data_copy", "datacopy",
                                          "mifitness", "download", "/export"))


def extract_link(body: str):
    """挑出最像导出包下载地址的那一条。"""
    candidates = []
    # 含空格的那条排在前面：它是完整地址，通用规则捞到的只是它的截断残片
    for raw in URL_SPACE_RE.findall(body) + URL_RE.findall(body):
        url = html_mod.unescape(raw).strip().rstrip("。,，、;；")
        if not url:
            continue
        # 空格进 URL 之前必须先编码，否则 urllib 直接拒绝
        url = url.replace(" ", "%20")
        if looks_like_package_url(url):
            candidates.append(url)
    if not candidates:
        return None
    # 优先 .zip 结尾的，其次第一条（小米的直链通常很长且带签名参数）
    zips = [u for u in candidates if u.lower().split("?")[0].endswith(".zip")]
    return (zips or candidates)[0]


# 「密码」这几个字和真正的密码值之间，可能夹着分隔符、连接词、括号、引号。
# 实测小米的邮件是这个形状（2026-09-24 从真实邮件取到）：
#
#     您的数据文件已加密。 密码如下：
#
#     U0VvWki2t6Z0TgdCc
#
# 关键字后面跟的是「如下：」，而值在**隔了一个空行的下一行**、整行只有密码本身。
# 单靠一条「同一行内」的正则无论如何都覆盖不了，所以改成两遍扫：
#   第一遍（行内）：关键字 + 过渡字符 + token —— 覆盖「解压密码：xxx」这类
#   第二遍（跨行）：关键字之后一小段窗口内，某一行**整行就是一个 token** —— 覆盖上面那种
_PW_GAP = r"[\s:：=＝是为即\-—]*[【\[\(（\"'“”]?\s*"
_PW_TOKEN = r"[A-Za-z0-9!@#$%^&*._\-]{4,64}"
# 第二遍「跨行」往下看几行。用**行数**做边界，不用字符数 —— 实测邮件里
# 「密码如下：」和密码行之间隔着一整行长链接行，按字符开窗口会把密码行截在窗外。
_PW_FOLLOW_LINES = 3


def _scan_hint(body: str, hint: str):
    """在正文里扫某个关键字，返回 [(起, 止, token)]，按出现位置排序、去重。

    两遍扫的理由见 `_PW_GAP` 上面的注释。两遍的结果都要收，因为同一封邮件里
    两种排版都可能出现。
    """
    inline = re.compile(r"%s%s(%s)" % (re.escape(hint), _PW_GAP, _PW_TOKEN), re.I)
    own_line = re.compile(r"^[ \t]*(%s)[ \t]*$" % _PW_TOKEN, re.M)
    hint_re = re.compile(re.escape(hint), re.I)

    spans: list[tuple[int, int, str]] = []

    def add(span):
        if span not in spans:
            spans.append(span)

    # 第一遍：同一行内
    for match in inline.finditer(body):
        token = match.group(1)
        # 「密码」后面常跟一句人话，别把纯中文的当密码
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            continue
        add((match.start(1), match.end(1), token))

    # 第二遍：跨行 —— 找「整行只有密码」的行，且它的前几行里出现过关键字。
    # 用**行数**做边界，不用字符数：实测邮件里「密码如下：」和密码行之间
    # 隔着一整行长链接行，按字符开窗口会把真正的密码行截在窗外。
    # 位置直接用 match.start(1)，不做「行号 → 偏移」的手工换算（\r\n 会让加法漂移）。
    for match in own_line.finditer(body):
        line_start = body.rfind("\n", 0, match.start()) + 1
        # 往前数 N 个**非空**行 —— HTML 转文本后段落之间会多出空行，
        # 按原始行数数会把「密码如下：」挤到窗口外。
        window = "\n".join(
            [ln for ln in body[:line_start].splitlines() if ln.strip()][-_PW_FOLLOW_LINES:])
        if not hint_re.search(window):
            continue
        add((match.start(1), match.end(1), match.group(1)))

    spans.sort()
    return spans


def find_password_candidates(body: str):
    """所有关键字命中的候选，按出现位置排序、去重。

    这个函数服务两处：extract_password 取第一个当真密码；
    --peek 则把所有候选都打码 —— 这样正文可以安全外发给我校准正则，
    而不用把真密码交出去。

    去重是必须的：`PASSWORD_HINTS` 里既有「解压密码」又有泛化的「密码」，
    同一句话会被两个关键字各命中一次，落在**完全相同的 token 区间**上。
    不去重的话候选数会翻倍，测试里那个「数一数有几个候选」的断言就废了。
    """
    spans = []
    for hint in PASSWORD_HINTS:
        for span in _scan_hint(body, hint):
            if span not in spans:
                spans.append(span)
    spans.sort()
    return spans


def extract_password(body: str):
    """按关键字优先级取第一个候选：越具体的「解压密码」优先于泛化的「密码」。"""
    for hint in PASSWORD_HINTS:
        found = _scan_hint(body, hint)
        if found:
            return found[0][2]
    return None


def mask_link(url: str) -> str:
    """保留主机与路径结构，打码过长的路径段与整个查询串。

    --peek 要能看清「是不是 .zip / 是不是下载页」，所以路径不能整段抹掉；
    但签名 token 常常就在长路径段或查询串里，那部分必须遮住。
    """
    base, _, query = url.partition("?")
    parts = [seg if len(seg) <= 24 else "<%d字符>" % len(seg) for seg in base.split("/")]
    return "/".join(parts) + ("?<已隐藏>" if query else "")


def mask_for_peek(text: str) -> str:
    """把正文里可能是密钥的东西打码，让 --peek 的输出可以直接外发。

    保留 token 的长度与字符构成 —— 校准正则需要的正是「格式」，不是「值」。
    """
    out = text
    for start, end, token in reversed(find_password_candidates(text)):
        # 构成描述用固定顺序（字母 → 数字 → 符号），否则同一个 token 的说明会随
        # 判断顺序变化，断言和肉眼比对都会莫名其妙对不上。
        kinds = []
        if any(ch.isalpha() for ch in token):
            kinds.append("字母")
        if any(ch.isdigit() for ch in token):
            kinds.append("数字")
        if any(not ch.isalnum() for ch in token):
            kinds.append("符号")
        out = out[:start] + "<已隐藏：%d 位，%s>" % (len(token), "+".join(kinds) or "未知") + out[end:]
    return re.sub(r"(https?://[^\s?]+)\?[^\s\"'<>]*", r"\1?<已隐藏>", out)


# ------------------------------------------------------------------ 状态
def load_state() -> dict:
    if STATE_PATH.is_file():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------------ IMAP
def connect(user: str, auth_code: str):
    context = ssl.create_default_context()
    client = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT,
                               ssl_context=context, timeout=IMAP_TIMEOUT)
    client.login(user, auth_code)
    # 部分国内邮箱要求先发 ID 才肯放行；不支持就忽略
    try:
        client.xatom("ID", '("name" "run-analytics" "version" "1.0")')
    except Exception:
        pass
    client.select("INBOX", readonly=True)
    return client


def _fetch_headers(client, uid):
    """只取 FROM / SUBJECT / DATE 三个头部，不碰正文。

    挑候选只看发件人和主题，整封邮件（可能还带附件）没必要拖下来。
    返回 (头部字节, 邮件大小)；取不到返回 (None, 0)，由调用方决定是否退回整封。
    """
    try:
        status, payload = client.fetch(
            uid, "(RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
    except Exception:
        return None, 0
    if status != "OK" or not payload or not isinstance(payload[0], tuple):
        return None, 0
    raw = payload[0][1]
    if not isinstance(raw, (bytes, bytearray)):
        return None, 0
    size = 0
    match = re.search(rb"RFC822\.SIZE\s+(\d+)", payload[0][0] or b"")
    if match:
        size = int(match.group(1))
    return bytes(raw), size


def _fetch_message(client, uid):
    """取整封邮件。

    用 `BODY.PEEK[]` 而不是 `RFC822`：后者等价于 `BODY[]`，会把邮件标记成**已读** ——
    等于替用户改动了邮箱状态。这个工具只该读不该写，连「已读」这种副作用都不留。
    """
    try:
        status, payload = client.fetch(uid, "(BODY.PEEK[])")
    except Exception:
        return None
    if status != "OK" or not payload or not isinstance(payload[0], tuple):
        return None
    raw = payload[0][1]
    if not isinstance(raw, (bytes, bytearray)):
        return None
    return bytes(raw)


def fetch_candidates(client, since_days: int, skip_uids=None):
    """返回 ([(uid, subject, from, date, body, link, password)], 跳过数)，按时间倒序。

    两段式取信：
      1. 对每封只看头部（几百字节），按发件人 / 主题把候选挑出来；
      2. 只对候选拉全文，再从正文里找下载直链与密码。

    之前是一封不留地拉整封再本地过滤 —— 邮件一多就是分钟级，附件也白拖。
    另外**已处理过的 UID 在发请求之前就跳过**，所以计划任务重复跑时几乎是零流量。

    判定语义与旧版完全一致（发件人或主题命中 + 正文里有像直链的 URL），
    所以这是纯粹省流量的改动，不改变「哪封邮件算候选」。

    只翻收件箱（会话在 connect() 里已经 SELECT 好了）：小米的导出通知就落在这里，
    没必要再去别的文件夹翻找。
    """
    skip = set(skip_uids or ())
    # IMAP 的 SINCE 只认英文月份缩写，且不含时分；用 UTC 减天数再格式化
    # （用 timezone-aware 的 now：utcnow() 在 3.12+ 已废弃，会在日志里刷 DeprecationWarning）
    since_imap = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=since_days)).strftime("%d-%b-%Y")
    try:
        status, data = client.search(None, "SINCE", since_imap)
    except Exception:
        status, data = client.search(None, "ALL")
    if status != "OK":
        return [], 0

    uids = (data[0] or b"").split()
    out = []
    skipped = 0
    for uid in reversed(uids[-SCAN_LIMIT:]):
        key = uid.decode() if isinstance(uid, bytes) else str(uid)
        if key in skip:
            skipped += 1
            continue

        full = None
        head_raw, _size = _fetch_headers(client, uid)
        if head_raw is None:
            # 服务端不认 HEADER.FIELDS：退回整封，慢一点但不漏
            full = _fetch_message(client, uid)
            if full is None:
                continue
            header_msg = email.message_from_bytes(full)
        else:
            header_msg = email.message_from_bytes(head_raw)

        subject = decode_header(header_msg.get("Subject"))
        sender = decode_header(header_msg.get("From"))
        # 判定从严：发件人**和**主题都要命中（理由见 SENDER_HINTS 上面的注释）
        hit_sender = any(h.lower() in sender.lower() for h in SENDER_HINTS)
        hit_subject = any(h in subject for h in SUBJECT_HINTS)
        if not (hit_sender and hit_subject):
            continue

        if full is None:
            full = _fetch_message(client, uid)
            if full is None:
                continue
        body = extract_body(email.message_from_bytes(full))
        # 正文特征再交叉确认一次：得真的是「数据副本已就绪」那封，
        # 而不是碰巧同发件人的别的通知。
        if not any(h in body for h in BODY_HINTS):
            continue
        link = extract_link(body)
        if not link:
            continue
        posted = decode_header(header_msg.get("Date")).strip()
        try:
            ts = email.utils.parsedate_to_datetime(posted).timestamp()
        except Exception:
            ts = 0.0
        out.append({
            "uid": key,
            "subject": subject.strip(),
            "from": sender.strip(),
            "date": posted,
            "ts": ts,
            "body": body,
            "link": link,
            "password": extract_password(body),
        })
        if len(out) >= MAX_CANDIDATES:
            break
    # 「收件最新」必须显式按 Date 头排 —— 不能依赖 UID 顺序（UID 通常等于到达顺序，
    # 但那是 IMAP 的实现细节，不是承诺）。排序口径写在这里才看得见。
    out.sort(key=lambda item: item.get("ts") or 0.0, reverse=True)
    return out, skipped


# ------------------------------------------------------------------ 找包 / 导入
def dashboard_latest_day():
    """data.js 里最新一场的日期；没有 data.js 或读不出来就返回 None。"""
    data_file = PROJECT_DIR / "data.js"
    if not data_file.is_file():
        return None
    text = data_file.read_text(encoding="utf-8", errors="ignore")
    dates = re.findall(r'"date"\s*:\s*"(\d{4}-\d{2}-\d{2})"', text)
    if not dates:
        return None
    try:
        return datetime.date.fromisoformat(max(dates))
    except ValueError:
        return None


def mail_day(candidate):
    """这封通知是哪一天发出的；解析不出来就返回 None（那就当它有效，别误跳）。"""
    try:
        return email.utils.parsedate_to_datetime(candidate.get("date") or "").date()
    except Exception:
        return None


def scan_new_package(baseline_mtime: float, baseline_name: str = ""):
    """在搜索目录里找**内容最新**的小米导出包；返回 (路径, 修改时间)。

    新旧按**文件名里的导出日期**比（小米的包以 `20260923_` 开头，那是内容的新旧），
    文件名里没日期才退回 mtime —— 重下一份**旧**包会让 mtime 变新，拿它当判据就会把
    旧包当成新的。这次改动的原因是多弗朗 2026-09-24 的要求：「对比后再选择解析最新的
    内容，不是根据场数的多少去判断」。

    名字必须匹配 `import_zip.PACKAGE_NAME_RE`（包名带 MiFitness）。这条过滤是**必须**的，
    不是保守 —— 实测踩过（2026-09-24）：清掉状态后 baseline 归零，`~/Downloads` 里一个刚
    下载的无关 zip（archify.zip）因为修改时间最新被当成了「最新的包」，拿去导入当然报
    「这个压缩包里没有找到跑步数据」。口径与 prune_old_packages / cleanup 共用同一常量。
    """
    baseline_stamp = import_zip.name_stamp(baseline_name)
    best, best_key = None, None
    for folder in import_zip.SEARCH_DIRS:
        folder = Path(folder)
        if not folder.is_dir():
            continue
        for item in folder.glob("*.zip"):
            if not import_zip.PACKAGE_NAME_RE.search(item.name):
                continue
            try:
                stamp, mtime = import_zip.package_stamp(item)
            except OSError:
                continue
            if baseline_stamp and stamp:
                if stamp <= baseline_stamp:      # 不比上次导入的那份新，跳过
                    continue
            elif mtime <= baseline_mtime:
                continue
            if best_key is None or (stamp, mtime) > best_key:
                best, best_key = item, (stamp, mtime)
    if best is None:
        return None, float(baseline_mtime)
    return best, best_key[1]


def parse_saved_path(lines):
    """从 auto_download.py 的输出里解析 SAVED=<路径> 那一行，返回 Path。"""
    saved = [line[len("SAVED="):].strip() for line in lines if line.startswith("SAVED=")]
    if not saved:
        raise RuntimeError("自动下载脚本没有报告保存路径（SAVED=），无法继续。")
    path = Path(saved[-1])
    if not path.is_file():
        raise RuntimeError("自动下载说存到了 %s，但那个文件不在。" % path)
    return path


def run_auto_download(page_url: str, dest_dir: Path) -> Path:
    """调用 tools/auto_download.py（Playwright）把包下载下来，返回文件路径。

    放在子进程里跑而不是同进程 import：Playwright 的 sync API 对「跑在哪个线程、
    有没有事件循环」很挑，而导入窗口本来就是在后台线程里调我们的 —— 隔一层子进程
    最稳，也和「导入窗口 → 子进程跑 import_zip.py」是同一套模式。
    """
    try:
        import playwright  # noqa: F401
    except ImportError:
        raise RuntimeError(
            "没装 playwright，自动下载用不了。装一下："
            ".venv\\Scripts\\python.exe -m pip install -r requirements.txt"
        ) from None
    if not AUTO_DOWNLOAD_SCRIPT.is_file():
        raise RuntimeError("auto_download.py 不存在：%s" % AUTO_DOWNLOAD_SCRIPT)

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    lines: list[str] = []
    proc = subprocess.Popen(
        # -u 关掉子进程的缓冲，保证行能及时冒出来（不然要等进程结束才一次性吐）
        [sys.executable, "-u", str(AUTO_DOWNLOAD_SCRIPT),
         "--url", page_url, "--dest", str(dest_dir)],
        cwd=str(PROJECT_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    # **逐行读、当场转出去**，绝不等它跑完再一次性打印：
    # 自动下载可能要在浏览器里登录、要跑几分钟，界面层这几分钟要是一句话都不说，
    # 用户看到的就是「卡住不动」（2026-09-24 多弗朗就是这么判断的）。
    try:
        for raw_line in proc.stdout or []:
            line = raw_line.rstrip("\n")
            lines.append(line)
            # SAVED= 是给机器读的交付行（parse_saved_path 靠它），人已经看过
            # 「已保存 xxx.zip（10.1 MB）」那一行了，别再打印一遍路径。
            if not line.startswith("SAVED="):
                say("  %s" % line)
        code = proc.wait(timeout=900)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError("自动下载超过 900 秒仍未结束，已中止。") from None
    finally:
        if proc.stdout:
            try:
                proc.stdout.close()
            except Exception:
                pass

    if code != 0:
        raise RuntimeError("自动下载脚本退出码 %d，详见上面的输出。" % code)
    return parse_saved_path(lines)


def prune_old_packages(keep: Path):
    """导入成功后只留这一个包，其它小米旧包清掉。返回删掉的路径列表。

    范围**刻意收窄**：只在 import_zip 的搜索目录里找，且文件名必须匹配
    PACKAGE_NAME_RE（小米的包名都带 MiFitness）—— `D:\\网页下载` 是你的下载目录，
    里面还有别的文件，绝不能按「旧的 zip」这种宽口径去删。
    每删一个都打日志；删不掉（文件被占用之类）就留着，不影响使用。
    """
    removed = []
    keep_resolved = keep.resolve()
    for folder in import_zip.SEARCH_DIRS:
        folder = Path(folder)
        if not folder.is_dir():
            continue
        for item in folder.glob("*.zip"):
            try:
                if item.resolve() == keep_resolved:
                    continue
            except OSError:
                continue
            if not import_zip.PACKAGE_NAME_RE.search(item.name):
                continue
            try:
                item.unlink()
                removed.append(str(item))
                say("  清掉旧包: %s" % item)
            except OSError as exc:
                say("  删不掉 %s（%s）—— 先留着，不影响使用。" % (item.name, exc))
    return removed


def try_import(zip_path: Path, passwords):
    """按顺序试密码，返回 (用中的密码, 导入器返回码)。

    邮箱里可能积着好几封导出通知、每封一个密码，而本地这个包是哪一次导出的并不写在
    文件名里。所以**靠试**：导入器返回 4 = 密码错，换下一个接着试。
    其它返回码（比如 6 包比看板旧）是确定性错误，再试也没意义，直接停。
    """
    for password in passwords:
        code = run_import(zip_path, password)
        if code == 0:
            return password, code
        if code != 4:
            return None, code
    return None, 4


def run_import(zip_path: Path, password: str) -> int:
    """调用导入器。密码经环境变量走一趟，不写任何文件。

    逐行读并转出去 —— 导入要解一个近 200 MB 的成员，几十秒到几分钟，进度必须看得见。
    （界面层那层也是流式的，一路传进日志窗。）
    """
    env = dict(os.environ)
    env["MIFITNESS_ZIP_PASSWORD"] = password
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        [sys.executable, "-u", str(IMPORT_SCRIPT), str(zip_path), "--no-prompt"],
        cwd=str(PROJECT_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    try:
        for raw_line in proc.stdout or []:
            say("  %s" % raw_line.rstrip("\n"))
        return proc.wait(timeout=1800)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError("导入器超过 1800 秒仍未结束，已中止。") from None
    finally:
        if proc.stdout:
            try:
                proc.stdout.close()
            except Exception:
                pass


# ------------------------------------------------------------------ 主流程
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="QQ 邮箱小米导出包 → 一键导入 run-analytics")
    parser.add_argument("--user", help="QQ 邮箱地址（默认读 QQ_MAIL_USER）")
    parser.add_argument("--auth-code", help="IMAP 授权码（默认读 QQ_MAIL_AUTH_CODE）")
    parser.add_argument("--since-days", type=int, default=DEFAULT_SINCE_DAYS,
                        help="只看最近几天的邮件，默认 %d" % DEFAULT_SINCE_DAYS)
    parser.add_argument("--check", action="store_true", help="只连邮箱并列出候选邮件")
    parser.add_argument("--peek", action="store_true", help="打印最新一封邮件的结构（密钥打码）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只说会用哪个本地包、有几个密码可试，不导入")
    parser.add_argument("--all", action="store_true", help="忽略已处理记录，重扫全部候选")
    # Keep accepting the old option so existing scheduled commands do not break;
    # successful imports no longer open a separate completion window.
    parser.add_argument("--notify", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    user, auth_code, source = load_credentials(args)
    if not user or not auth_code:
        say("没有拿到邮箱凭证：%s" % source)
        say("")
        say("二选一：")
        say("  A) 设环境变量（计划任务用这个）")
        say('     setx QQ_MAIL_USER "你的QQ邮箱@qq.com"')
        say('     setx QQ_MAIL_AUTH_CODE "IMAP授权码"')
        say("  B) 写 %s" % CONFIG_PATH)
        say('     内容：{"user": "你的QQ邮箱@qq.com", "auth_code": "IMAP授权码"}')
        say("")
        say("授权码在 QQ 邮箱「设置 → 账户 → IMAP/SMTP 服务」里开启后生成。")
        return 2

    say("邮箱  : %s（凭证来源：%s）" % (user, source))
    try:
        client = connect(user, auth_code)
    except imaplib.IMAP4.error as exc:
        say("IMAP 登录失败：%s" % exc)
        say("多半是授权码不对，或者还没在 QQ 邮箱里开启 IMAP 服务。")
        return 3
    except Exception as exc:
        # socket.timeout 在 3.10+ 就是 TimeoutError，不必额外导入 socket
        if isinstance(exc, TimeoutError):
            say("连 %s:%d 超时（%d 秒）—— 网络不通或被防火墙挡住，不是账号问题。"
                % (IMAP_HOST, IMAP_PORT, IMAP_TIMEOUT))
        else:
            say("连不上 %s:%d —— %s: %s" % (IMAP_HOST, IMAP_PORT, type(exc).__name__, exc))
        return 3

    # 先把本地状态读出来：已处理过的 UID 要在「发请求之前」就交给 fetch_candidates 跳过，
    # 否则每次重跑都还在白拉那些早就导过的邮件（计划任务每半小时跑一次，差别很明显）。
    state = load_state()
    handled = set(state.get("handled_uids") or [])
    skip = set() if args.all else handled

    try:
        candidates, skipped = fetch_candidates(client, args.since_days, skip)
    finally:
        try:
            client.logout()
        except Exception:
            pass

    say("候选邮件: %d 封（收件箱，最近 %d 天）" % (len(candidates), args.since_days))
    if skipped:
        say("          另有 %d 封已处理过，未再发起请求（--all 可强制重扫）" % skipped)
    for item in candidates:
        say("  · %s | %s | 密码 %s | 链接 %s"
            % (item["date"][:31], item["subject"][:40],
               "已解析" if item["password"] else "未解析出",
               urllib.parse.urlparse(item["link"]).netloc))
    if not candidates:
        say("")
        say("暂时没有可用的导出邮件。等小米把导出包发过来再跑一次就行。")
        say("这一轮没有导入任何数据，看板没有改动。")
        # 9 = 「没事可做」，不是失败也不是成功 —— 界面层据此显示「未导入」而不是
        # 「导入完成」。返 0 会被当成成功，正是「报假数据」的源头。
        return 9

    if args.check:
        say("")
        say("--check 模式：未下载、未导入。")
        return 0

    if args.peek:
        newest = candidates[0]
        pw_spans = find_password_candidates(newest["body"])
        say("")
        say("最新一封的结构（密钥与签名已打码，这段可以直接外发）：")
        say("  主题: %s" % newest["subject"])
        say("  发件: %s" % newest["from"])
        say("  日期: %s" % newest["date"])
        say("  链接: %s" % mask_link(newest["link"]))
        say("  密码: %s" % ("已解析出 %d 位" % len(newest["password"]) if newest["password"]
                            else "**未解析出** —— 得按下面的格式调 extract_password"))
        say("")
        say("正文里扫到的密码候选（只报格式，不报值）：")
        if pw_spans:
            for _, _, token in pw_spans:
                say("  · %d 位：%s" % (len(token), re.sub(r"[A-Za-z0-9]", "x", token)))
        else:
            say("  （一个候选都没有 —— 说明「密码」这三个字在正文里的写法和我猜的不一样）")
        say("")
        say("正文摘录（前 1500 字，密钥已打码）：")
        say(mask_for_peek(newest["body"])[:1500])
        return 0

    # state / handled 已经在连邮箱之前读好了；这里再挡一道，
    # 防止 --check 之类的路径将来被人改动后绕过跳过逻辑。
    # `--all` 是「忽略已处理记录、重扫」，所以它必须**同时**绕过这一道过滤 ——
    # 老代码只在拉取正文那一步忽略记录，这里照旧按 handled 过滤，于是 --all 什么都重扫不了。
    candidates = [c for c in candidates if args.all or c["uid"] not in handled]
    # 邮箱里积压的旧通知：**邮件日期早于看板里最新一场** → 它的导出不可能带来新数据。
    # 处理它只会白下载一次 10 MB，再被「包更旧」或「密码不对」挡回来（实测：09-22 那封
    # 每点一次就重下一遍，因为它的旧链接现在发的是更新的包、跟它自己的密码对不上）。
    # 直接跳过并标记已处理，免得下一次点击又白等一遍。
    latest_day = dashboard_latest_day()
    if latest_day:
        stale = [c for c in candidates if (mail_day(c) or latest_day) < latest_day]
        if stale:
            skipped = {c["uid"] for c in stale}
            candidates = [c for c in candidates if c["uid"] not in skipped]
            say("")
            say("跳过 %d 封比看板旧的导出通知（看板最新一场 %s），它们不会带来新数据："
                % (len(stale), latest_day))
            for item in stale:
                say("  · %s" % (item["date"] or "")[:31])
                handled.add(item["uid"])
            if not args.dry_run:
                state["handled_uids"] = sorted(handled)
                save_state(state)
    if not candidates:
        say("")
        say("没有需要处理的邮件（候选里的都导过了）。看板没有改动。")
        return 9

    target = candidates[0]
    # 只用最新一封：小米每次导出都是全量数据，最新的那封就已经是全部历史。
    # 多封积压时逐个试密码没有任何收益，反而让人搞不清「现在看板里到底是哪次的数据」。
    newest_password = target["password"]
    passwords = [newest_password] if newest_password else []
    # 上次导入的是哪一份包记在状态里，用来区分「这是新包还是已经导过的旧包」。
    # 主判据是包名里的导出日期（imported_package），mtime 只在名字里没日期时兜底。
    imported_mtime = float(state.get("imported_mtime") or 0.0)
    imported_name = str(state.get("imported_package") or "")
    package, package_mtime = scan_new_package(imported_mtime, imported_name)
    # 这份包是本地捡到的、还是这次刚下载的？密码打不开时的救法不一样（见下面的 code 4）。
    package_from_disk = package is not None

    if args.dry_run:
        say("")
        say("--dry-run：不导入，也不弹浏览器")
        say("  最新邮件: %s" % target["subject"])
        say("  本地新包: %s" % (package if package else "**没有**（比上次导入的新）"))
        say("  可试密码: %d 个" % len(passwords))
        return 0 if (package and passwords) else (5 if not passwords else 0)

    if not passwords:
        say("")
        say("候选邮件里没解析出解压密码，无法导入。")
        say("先跑 --peek 看正文长什么样，再调 SENDER_HINTS / PASSWORD_HINTS 或 extract_password 的正则。")
        return 5

    if package is None:
        # 那一步的链接认登录态，所以交给 auto_download.py 用脚本自己的 Edge（独立 profile，
        # 平时无头静默；只有登录态过期才会开窗要你登一次）。
        say("")
        say("本地没有更新的包，去网上取最新一份…")
        try:
            package = run_auto_download(target["link"], INBOX)
        except Exception as exc:
            say("自动下载失败：%s" % (str(exc) or type(exc).__name__))
            # **失败时不再去开你的默认浏览器。** 以前这里会 webbrowser.open(下载页)，
            # 结果是「自动下载没成，屏幕又莫名其妙多出一个浏览器窗口」——
            # 多弗朗 2026-09-24 报的「浏览器还瞎开」有一半是它。要手动，路子本身很清楚：
            # 邮箱里那封导出邮件点开、点「文件 #1」，不必由脚本再弹一次。
            # 你可能已经手动下好了 —— 看一眼，有就直接用
            package, package_mtime = scan_new_package(imported_mtime, imported_name)
            if package is not None:
                say("  本地已经有新包了 —— 直接用它导入。")
            else:
                say("")
                say("这一轮**没有导入任何数据**，看板还是上一次的内容，没有改动。")
                say("要手动下：打开邮箱里那封导出邮件 → 点里面的下载链接 → 点「文件 #1」，"
                    "然后把 zip 放进项目的 %s，再跑一次本命令（密码会从邮件里取）。"
                    % INBOX)
                # ⚠️ 必须返回**非 0**：这一轮一个字节都没导入。
                # 返回 0 会让界面层报「已生成看板数据」，而那只是上一次的看板 ——
                # 等于拿旧数据当新结果骗人（2026-09-24 被多弗朗抓到过）。
                return 8
        # 自动下载的文件是刚写完的，mtime 就用它，别再靠 scan 去猜
        package_mtime = package.stat().st_mtime

    # 包路径和大小由导入器自己打印（它一定会打），这里不再重复一遍。
    say("")
    say("开始导入（试 %d 个密码）…" % len(passwords))
    used, code = try_import(package, passwords)
    if code == 4 and package_from_disk:
        # 本地那个包打不开：最可能是它被「一封更旧的邮件里的链接」下成了别的导出
        # （那种链接现在会发更新的包），而它的密码不在这批邮件里。**别让一个坏包把整条
        # 链路堵死** —— 实测踩过：本地包被下坏之后，新邮件来了也只会一直报「密码不对」。
        # 直接从这封邮件的下载页重新取一份再试。
        say("")
        say("本地那个包打不开（密码对不上），改成从邮件的下载页重新取一份…")
        try:
            package = run_auto_download(target["link"], INBOX)
            package_from_disk = False
            package_mtime = package.stat().st_mtime
            used, code = try_import(package, passwords)
        except Exception as exc:
            say("重新下载失败：%s" % (str(exc) or type(exc).__name__))
    if code != 0:
        if code == 4:
            say("")
            say("密码不对。这个包多半来自更早的一次导出 —— 那一封的密码不在这里。")
            say("请确认下载的是**最新一封**邮件对应的文件；")
            say("要手动导入某一封的包：python tools/import_zip.py <包路径>，"
                "密码用那封邮件里的（会提示输入，不落盘）。")
        elif code == 6:
            say("")
            say("这一份包比看板里的**旧**（最新的一场比看板还早），导入器拒绝写入 —— 看板数据没动。")
            say("多半是下载到了更早一次导出的包。你现在的看板已经是最新的，")
            say("不用做什么；等小米发来新的导出邮件再跑一次就行。")
            say("（确实要用旧包覆盖：python tools/import_zip.py <包路径> --force）")
            # 这封邮件的包比看板旧，是**确定性**结论：以后每次跑都会下同一个包、被同一句话挡住。
            # 所以标成已处理 —— 否则邮箱里积压的旧通知会让「点击一次」永远白下载 10 MB
            # （实测：09-23 那封每次都要重下一遍才被拒）。密码错（4）之类**不能**这样标，
            # 那可能只是包和邮件对不上，换一封邮件还有救。
            handled.add(target["uid"])
            state["handled_uids"] = sorted(handled)
            save_state(state)
            say("已把这封旧通知标记为处理过，下次不会再为它下载一遍。")
        else:
            say("导入失败，导入器返回码 %d。" % code)
        return 7

    if used is not None:
        # 密码处理到「几次就中」为止，不落盘这件事只在第一次说（见模块开头）
        say("  密码第 %d 次就中" % (passwords.index(used) + 1))

    handled.add(target["uid"])
    state["handled_uids"] = sorted(handled)
    state["last_success"] = target["date"]
    # 记下这次导入的是哪一份：下次跑的时候，比它新的才算「新包」（按包名里的日期比）
    state["imported_mtime"] = package_mtime
    state["imported_package"] = package.name
    save_state(state)
    # 只保留最新一份包，旧的清掉（多弗朗的要求：最终只留一份新文件）
    prune_old_packages(package)
    say("")
    say("看板数据已更新。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
