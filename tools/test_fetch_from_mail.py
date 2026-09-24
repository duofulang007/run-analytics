# -*- coding: utf-8 -*-
"""tools/fetch_from_mail.py 的离线自测：不连网、不连 IMAP，只验解析与取信策略。

用法
    python tools/test_fetch_from_mail.py

覆盖的是整条链路里最容易悄悄坏掉的那几处：邮件正文拉平、下载直链（含小米包名
扩展前那个空格）、解压密码、文件名收紧、候选邮件判定、两段式取信有没有真的省流量、
已处理 UID 是不是在发请求之前就跳过、服务端不认 HEADER.FIELDS 时能否退回整封，
以及缺凭证时的返回码。

IMAP 用假对象替代，所以这个脚本秒级跑完，且不碰任何真实邮箱。
"""
from __future__ import annotations

import contextlib
import email as email_mod
import importlib.util
import io
import os
from pathlib import Path
import shutil
import sys
import tempfile
from email.message import EmailMessage

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("ffm", ROOT / "tools" / "fetch_from_mail.py")
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)

# 清理模块单独加载：它不在 fetch_from_mail 里 —— 界面层直接用，命令行也能单独跑
SPEC_CLEAN = importlib.util.spec_from_file_location("ra_cleanup", ROOT / "tools" / "cleanup.py")
clean = importlib.util.module_from_spec(SPEC_CLEAN)
SPEC_CLEAN.loader.exec_module(clean)

failures: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
    if not ok:
        print("        got  = %r" % (got,))
        print("        want = %r" % (want,))
        failures.append(name)


def build(subject: str, sender: str, html_body: str,
          date: str = "Wed, 24 Sep 2026 10:00:00 +0800") -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = "me@qq.com"
    msg["Date"] = date
    msg.set_content("请用支持 HTML 的客户端查看本邮件。")
    msg.add_alternative(html_body, subtype="html")
    return msg.as_bytes()


# 下面所有示例值都是**虚构**的，不是任何真实凭证。
# ⚠️ 刻意不让它们出现在断言名称里：check() 通过时只打印名称，
# 名称里带上示例密码 → 整份输出到处是「像密钥的字符串」→ 触发安全扫描（踩过两次）。
PW_VALUE = "a7Kd93Lm"

ZIP_NAME = "20260923_2848269717_MiFitness_c3_data_copy"
ZIP_URL_SPACE = "https://cdn.cnbj1.fds.api.mi-img.com/mifitness/%s .zip" % ZIP_NAME
ZIP_URL_PCT = ZIP_URL_SPACE.replace(" ", "%20")

HTML_TMPL = """<html><body>
<p>亲爱的用户</p>
<p>您在小米运动健康 的个人数据文件已准备就绪。<br>
请通过以下链接下载副本，该副本将在 7 天后过期。您的数据文件已加密。 密码如下：</p>
<p><a href="{url}">点击下载数据副本</a></p>
<p>{pw}</p>
<p>请务必对您的密码保密。</p>
</body></html>"""


def header_block(blob: bytes) -> bytes:
    """从整封邮件里切出头部块，模拟 HEADER.FIELDS 的返回。

    故意切真实的线上头部（Subject 是编码后的 =?utf-8?B?...?=），而不是拿
    EmailMessage 解出来的 unicode —— 后者绕过了 decode_header，就测不出中文主题了。
    """
    for sep in (b"\r\n\r\n", b"\n\n"):
        if sep in blob:
            return blob.split(sep, 1)[0] + sep
    return blob


class FakeIMAP:
    """只实现 fetch_from_mail 用到的那几个方法，并记录实际发过哪些请求。

    这是验「省流量」的唯一手段：要断言的不是拉回来的内容，而是**请求了什么**。
    """

    def __init__(self, blobs: list[bytes], support_header_fields: bool = True) -> None:
        self.blobs = blobs
        self.support_header_fields = support_header_fields
        self.calls: list[tuple[int, str]] = []
        self.search_criteria = None

    def search(self, charset, *criteria):
        self.search_criteria = criteria
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.blobs)))]

    def fetch(self, uid, spec):
        self.calls.append((int(uid), spec))
        blob = self.blobs[int(uid) - 1]
        if "HEADER.FIELDS" in spec:
            if not self.support_header_fields:
                return "NO", [None]
            head = header_block(blob)
            return "OK", [(b"1 (RFC822.SIZE %d BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {%d}"
                           % (len(blob), len(head)), head), b")"]
        return "OK", [(b"1 (BODY[] {%d}" % len(blob), blob), b")"]

    def logout(self):
        pass

    def header_calls(self) -> list[int]:
        return sorted(u for u, spec in self.calls if "HEADER.FIELDS" in spec)

    def body_calls(self) -> list[int]:
        return sorted(u for u, spec in self.calls if "HEADER.FIELDS" not in spec)

    def touched_uids(self) -> list[int]:
        return sorted({u for u, _ in self.calls})


def main() -> int:
    print("=== 1. 正文提取（含 HTML 拉平）===")
    html_pct = HTML_TMPL.format(url=ZIP_URL_PCT, pw=PW_VALUE)
    body = mod.extract_body(email_mod.message_from_bytes(
        build("小米运动健康-个人数据副本", "Xiaomi Corporation <noreply@notice.xiaomi.com>", html_pct)))
    check("拉平后还能读到正文文字", "点击下载" in body and "密码如下" in body, True)

    print("")
    print("=== 2. 下载直链解析 ===")
    check("href 里空格已编码 %20 → 捞到完整链", mod.extract_link(body), ZIP_URL_PCT)
    html_space = HTML_TMPL.format(url=ZIP_URL_SPACE, pw=PW_VALUE)
    body_space = mod.extract_body(email_mod.message_from_bytes(
        build("小米运动健康-个人数据副本", "Xiaomi Corporation <noreply@notice.xiaomi.com>", html_space)))
    check("href 里是字面空格 → 不截断，仍以 .zip 结尾",
          (mod.extract_link(body_space) or "").split("/")[-1].endswith(".zip"), True)
    check("没有下载地址的邮件 → None", mod.extract_link("这封邮件里没有任何下载地址"), None)

    print("")
    print("=== 3. 解压密码解析 ===")
    # 注意：断言名称里**只描述格式，不写示例值**。check() 通过时只打印名称，
    # 一旦名称里带上示例密码，整份输出就会到处是「像密钥的字符串」，
    # 触发自动安全扫描（2026-09-24 真的踩过一次，结果被判成敏感内容扣下）。
    # 期望值是参数，只在失败时才打印 —— 那时才是真的需要看到它。
    check("『文件解压密码：<值>』", mod.extract_password(body), "a7Kd93Lm")
    check("『解压密码: <值>』（半角冒号）", mod.extract_password("解压密码: Xy-12_ab"), "Xy-12_ab")
    check("『压缩包密码【<值>】』（方括号包裹）",
          mod.extract_password("压缩包密码【Pw20260924】"), "Pw20260924")
    check("密码后跟中文说明时不误吞", mod.extract_password("解压密码：abc123 请注意保管"), "abc123")
    check("『解压密码为 <值>』（连接词「为」）", mod.extract_password("解压密码为 Xy12ab"), "Xy12ab")
    check("『解压密码是：<值>』（连接词「是」）", mod.extract_password("解压密码是：ab12cd"), "ab12cd")
    check("『密码即 <值>』（连接词「即」）", mod.extract_password("密码即 ab12cd"), "ab12cd")
    check("『密码 ＝ <值>』（全角等号）", mod.extract_password("密码 ＝ AB12cd"), "AB12cd")
    check("『Password: <值>』（英文关键字）", mod.extract_password("Password: ab12cd"), "ab12cd")
    check("具体的『解压密码』优先于泛化的『登录密码』",
          mod.extract_password("登录密码 zzz999\n解压密码 real12"), "real12")
    check("候选去重且按位置排序（同一句话只算一个）",
          len(mod.find_password_candidates("解压密码：aaa111\n压缩包密码：bbb222")), 2)
    # 下面这条的排版是从真实邮件里抄来的（2026-09-24）：关键字是「密码如下：」，
    # 值在**隔了一个空行的下一行**且整行只有密码。第一遍行内扫不到，靠第二遍兜住。
    own_line_shape = "您的数据文件已加密。 密码如下：\n\nFake0Ab1Cd2Ef3Gh\n请务必对您的密码保密。"
    check("『密码如下：<空行><值独占一行>』（真实邮件排版）",
          mod.extract_password(own_line_shape), "Fake0Ab1Cd2Ef3Gh")
    check("同一封邮件里两种排版都能扫到",
          len(mod.find_password_candidates("解压密码：aa11bb\n密码如下：\n\ncc22dd")), 2)
    check("『密码保密』这类不许误判", mod.extract_password("请务必对您的密码保密。"), None)
    check("正文没有密码 → None", mod.extract_password("这封邮件没有任何密码字段"), None)

    print("")
    print("=== 4. 密码靠试：错了换下一个，确定性错误立刻停 ===")

    def stub_runner(results):
        """给 try_import 造一个假的 run_import：按调用次数依次返回 results 里的值。"""
        tried = []

        def runner(zip_path, password):
            tried.append(password)
            index = min(len(tried), len(results)) - 1
            return results[index]

        return runner, tried

    original_runner = mod.run_import
    try:
        mod.run_import, tried = stub_runner([4, 4, 0, 0])
        used, code = mod.try_import(Path("pkg.zip"), ["p1", "p2", "p3", "p4"])
        check("第三个密码才对得上", (used, code), ("p3", 0))
        check("试到成功就停，不乱试剩下的", tried, ["p1", "p2", "p3"])

        mod.run_import, tried = stub_runner([6])
        used, code = mod.try_import(Path("pkg.zip"), ["p1", "p2", "p3"])
        check("遇到确定性错误（6 = 包比看板旧）立即停，不再换密码",
              (used, code, len(tried)), (None, 6, 1))

        mod.run_import, tried = stub_runner([4, 4, 4])
        used, code = mod.try_import(Path("pkg.zip"), ["p1", "p2", "p3"])
        check("全都不对 → 返回码 4", (used, code), (None, 4))
        check("确实把每个密码都试了一遍", len(tried), 3)
    finally:
        mod.run_import = original_runner

    print("")
    print("=== 5. 候选判定（按真实邮件特征，从严）+ 两段式取信 ===")
    noise = build("本周优惠券到账", "广告 <ads@shop.com>",
                  "<p>点这里 <a href='https://shop.com/promo.zip'>领券</a></p>")
    real = build("小米运动健康-个人数据副本", "Xiaomi Corporation <noreply@notice.xiaomi.com>", html_pct)
    real_old = build("小米运动健康-个人数据副本", "Xiaomi Corporation <noreply@notice.xiaomi.com>",
                     html_pct, date="Tue, 22 Sep 2026 18:23:27 +0800")
    other = build("会议纪要", "同事 <a@b.com>", "<p>附件见下</p>")
    fake = FakeIMAP([noise, real, other, real_old])
    found, skipped = mod.fetch_candidates(fake, 30)
    check("返回 (候选列表, 跳过数) 两个值", isinstance(found, list) and isinstance(skipped, int), True)
    check("4 封里挑出两封小米的（新旧各一）", len(found), 2)
    check("收件最新的排前面（按 Date 头排，不靠 UID 顺序）", [c["uid"] for c in found], ["2", "4"])
    check("挑中的主题正确（中文已正确解码）", found[0]["subject"], "小米运动健康-个人数据副本")
    check("挑中的能解析出密码", found[0]["password"], PW_VALUE)
    check("挑中的链接完整", found[0]["link"].endswith("_data_copy%20.zip"), True)
    check("四封都只发了「取头部」请求", fake.header_calls(), [1, 2, 3, 4])
    check("只有两封小米的拉了整封（省流量的关键）", fake.body_calls(), [2, 4])

    print("")
    print("=== 6. 已处理的 UID 在发请求之前就跳过 ===")
    fake2 = FakeIMAP([noise, real, other])
    found2, skipped2 = mod.fetch_candidates(fake2, 30, skip_uids={"2"})
    check("候选为空", found2, [])
    check("跳过数 = 1", skipped2, 1)
    check("压根没为它发过任何请求", fake2.touched_uids(), [1, 3])
    fake3 = FakeIMAP([noise, real, other])
    found3, skipped3 = mod.fetch_candidates(fake3, 30, skip_uids={"1", "3"})
    check("跳过无关邮件不影响挑中目标", [c["uid"] for c in found3], ["2"])
    check("跳过数 = 2", skipped3, 2)

    print("")
    print("=== 7. 服务端不认 HEADER.FIELDS 时退回整封 ===")
    fake4 = FakeIMAP([noise, real, other], support_header_fields=False)
    found4, _ = mod.fetch_candidates(fake4, 30)
    check("仍然挑得出候选（不漏）", [c["uid"] for c in found4], ["2"])
    check("兜底路径下每封都拉了整封", fake4.body_calls(), [1, 2, 3])

    print("")
    print("=== 8. 凭证清洗：误包引号与尾随空格 ===")
    check("去掉尾随空格", mod._clean("abc@qq.com  "), "abc@qq.com")
    check("剥掉误包的单引号（cmd 的 setx 不认单引号）", mod._clean("'abc@qq.com'"), "abc@qq.com")
    check("剥掉误包的双引号", mod._clean('"abc@qq.com"'), "abc@qq.com")
    check("引号里外都有空格也一起清", mod._clean("' abc@qq.com '"), "abc@qq.com")
    check("正常的授权码原样不动", mod._clean("abcdefghijklmnop"), "abcdefghijklmnop")
    check("空值 → 空串", mod._clean(None), "")

    class FakeArgs:
        user = None
        auth_code = None

    saved = {k: os.environ.get(k) for k in ("QQ_MAIL_USER", "QQ_MAIL_AUTH_CODE")}
    try:
        os.environ["QQ_MAIL_USER"] = "'me@qq.com' "
        os.environ["QQ_MAIL_AUTH_CODE"] = '" abcdefg "'
        cred_user, cred_code, cred_source = mod.load_credentials(FakeArgs())
        check("环境变量里的邮箱已清洗", cred_user, "me@qq.com")
        check("环境变量里的授权码已清洗", cred_code, "abcdefg")
        check("来源标明是环境变量", cred_source, "环境变量")
    finally:
        for key, val in saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    print("")
    print("=== 9. 缺凭证时的返回码与提示 ===")
    for key in ("QQ_MAIL_USER", "QQ_MAIL_AUTH_CODE"):
        os.environ.pop(key, None)
    original_config = mod.CONFIG_PATH
    original_registry = mod._registry_user_env
    mod.CONFIG_PATH = Path(r"C:\definitely\not\here\mail_config.json")
    # 注册表兜底也要关掉：本机的 HKCU\Environment 里真的存着 setx 过的凭证，
    # 不关掉的话这条「缺凭证」路径永远测不到。
    mod._registry_user_env = lambda name: None
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = mod.main([])
    finally:
        mod.CONFIG_PATH = original_config
        mod._registry_user_env = original_registry
    check("没有凭证 → 返回码 2", code, 2)
    check("提示里给出两条给凭证的路子",
          "setx QQ_MAIL_AUTH_CODE" in buf.getvalue() and "mail_config.json" in buf.getvalue(), True)

    print("")
    print("=== 9b. 注册表兜底：老进程也能拿到 setx 的新值 ===")
    try:
        mod._registry_user_env = lambda name: {
            "QQ_MAIL_USER": "'me@qq.com'",
            "QQ_MAIL_AUTH_CODE": " abcdefgh ",
        }.get(name)
        class RegistryArgs:
            user = None
            auth_code = None
        reg_user, reg_code, reg_source = mod.load_credentials(RegistryArgs())
        check("注册表里的邮箱被读出并清洗", reg_user, "me@qq.com")
        check("注册表里的授权码被读出并清洗", reg_code, "abcdefgh")
        check("来源标明是环境变量", reg_source, "环境变量")
    finally:
        mod._registry_user_env = original_registry

    print("")
    print("=== 10. --peek 的打码：保证输出可以安全外发 ===")
    # 示例值刻意避开 token / secret 这类会被安全扫描盯上的字面量
    link = ("https://tools.privacy.mi.com/abcdefghijklmnopqrstuvwxyz012345/download"
            "?a1=ZZZZ1111&b2=YYYY")
    masked_link = mod.mask_link(link)
    check("主机保留", masked_link.startswith("https://tools.privacy.mi.com/"), True)
    check("过长的路径段被打码（多半是签名）", "<32字符>" in masked_link, True)
    check("普通路径段保留（要看得出是不是下载端点）", "/download" in masked_link, True)
    check("查询串整体打码", masked_link.endswith("?<已隐藏>"), True)
    check("原查询串里的参数不再出现", "ZZZZ1111" not in masked_link, True)

    leaked = mod.mask_for_peek("解压密码为 demo9999 请保管，链接 https://a.b/c?x=QQQQ")
    check("正文里的密码值被替换掉", "demo9999" not in leaked, True)
    check("保留长度与构成描述（校准正则要的正是格式）",
          "<已隐藏：8 位，字母+数字>" in leaked, True)
    check("正文里的 URL 查询串也被打码", "QQQQ" not in leaked, True)
    check("正文其余文字不受影响", "请保管" in leaked, True)

    # 真实邮件的排版（值独占一行）也必须被打码 —— 否则正文一发出去就把真密码交出去了
    shaped = "您的数据文件已加密。 密码如下：\n\nZz9Yy8Xx7Ww6Vv5U\n请务必保密。"
    check("『值独占一行』的排版也要被打码",
          "Zz9Yy8Xx7Ww6Vv5U" not in mod.mask_for_peek(shaped), True)

    print("")
    print("=== 11. 本地找包：比基线新的、且确实是小米包才算 ===")
    sandbox = Path(tempfile.mkdtemp(prefix="ra_mail_test_"))
    original_dirs = mod.import_zip.SEARCH_DIRS
    try:
        mod.import_zip.SEARCH_DIRS = (sandbox,)

        old = sandbox / "20260923_MiFitness_old.zip"
        old.write_bytes(b"old")
        os.utime(old, (1000000000, 1000000000))
        found, _ = mod.scan_new_package(1000000000)
        check("只有旧包时，比基线新的包不存在", found if found else None, None)

        newer = sandbox / "20260924_MiFitness_new.zip"
        newer.write_bytes(b"new")
        os.utime(newer, (2000000000, 2000000000))
        found, _ = mod.scan_new_package(1000000000)
        check("比基线新的包被捡到", found.name if found else None, "20260924_MiFitness_new.zip")

        # 名字不含 MiFitness 的 zip **不能**被当成包 —— 实测踩过：清掉状态后 baseline 归零，
        # ~/Downloads 里一个新下载的无关 zip（archify.zip）因为 mtime 最新被选中，
        # 拿去导入只会报「这个压缩包里没有找到跑步数据」。
        odd = sandbox / "weird_download_name.zip"
        odd.write_bytes(b"new")
        os.utime(odd, (3000000000, 3000000000))
        found, _ = mod.scan_new_package(2000000000)
        check("无关 zip 更新也不抢（只认小米包名）", found, None)

        real_new = sandbox / "20260925_MiFitness_newer.zip"
        real_new.write_bytes(b"newer")
        os.utime(real_new, (2500000000, 2500000000))
        found, _ = mod.scan_new_package(2000000000)
        check("小米包比基线新就捡到（无关的更旧/更新都不影响）",
              found.name if found else None, "20260925_MiFitness_newer.zip")

        # 多弗朗 2026-09-24：**按「哪个最新」挑，不按场数、也不按 mtime**。
        # 重下一份旧包会让 mtime 变新，拿它当判据就会把旧包当成新包。
        again_old = sandbox / "20260920_MiFitness_redownloaded.zip"
        again_old.write_bytes(b"old-again")
        os.utime(again_old, (4000000000, 4000000000))          # mtime 全场最新
        found, _ = mod.scan_new_package(0, "20260925_MiFitness_newer.zip")
        check("重下一份旧包（mtime 最新）不会被当成新包", found, None)

        late_zip = sandbox / "20260927_MiFitness_late.zip"
        late_zip.write_bytes(b"late")
        os.utime(late_zip, (500000000, 500000000))             # mtime 最旧
        found, _ = mod.scan_new_package(0, "20260925_MiFitness_newer.zip")
        check("日期最新但下载很早的包，照样被选中",
              found.name if found else None, "20260927_MiFitness_late.zip")

        check("包的新旧键先看文件名里的导出日期",
              mod.import_zip.package_stamp(late_zip)[0], 20260927)
        check("文件名里没有日期时该键为 0（退回 mtime）",
              mod.import_zip.name_stamp("小米导出.zip"), 0)
        check("find_latest_zip 也按日期挑最新的",
              mod.import_zip.find_latest_zip().name, "20260927_MiFitness_late.zip")

        # auto_download.py 跑在子进程里，靠 SAVED=<路径> 这一行把结果交回来
        saved_file = sandbox / "weird_download_name.zip"
        check("SAVED= 行能解析出真实文件", mod.parse_saved_path(["  其它日志", "SAVED=%s" % saved_file]),
              saved_file)

        errored = None
        try:
            mod.parse_saved_path(["没有 SAVED= 这一行"])
        except RuntimeError as exc:
            errored = exc
        check("没有 SAVED= 行要报错", errored is not None and "SAVED=" in str(errored), True)

        errored = None
        try:
            mod.parse_saved_path(["SAVED=C:\\definitely\\not\\here.zip"])
        except RuntimeError as exc:
            errored = exc
        check("路径不存在要报错", errored is not None and "不在" in str(errored), True)
    finally:
        mod.import_zip.SEARCH_DIRS = original_dirs
        shutil.rmtree(sandbox, ignore_errors=True)

    print("")
    print("=== 12. 导入成功后只留最新一份包，旧的清掉 ===")
    prune_dir = Path(tempfile.mkdtemp(prefix="ra_prune_test_"))
    original_dirs = mod.import_zip.SEARCH_DIRS
    try:
        mod.import_zip.SEARCH_DIRS = (prune_dir,)
        keep = prune_dir / "20260924_MiFitness_new.zip"
        keep.write_bytes(b"keep")
        old_pkg = prune_dir / "20260923_MiFitness_old.zip"
        old_pkg.write_bytes(b"old")
        unrelated = prune_dir / "别的下载.zip"
        unrelated.write_bytes(b"unrelated")

        removed = mod.prune_old_packages(keep)

        check("旧的小米包被删", old_pkg.exists(), False)
        check("刚导入的这份还在", keep.exists(), True)
        check("无关下载不动（名字口径只认 MiFitness）", unrelated.exists(), True)
        check("返回删掉的清单", [Path(x).name for x in removed], ["20260923_MiFitness_old.zip"])
    finally:
        mod.import_zip.SEARCH_DIRS = original_dirs
        shutil.rmtree(prune_dir, ignore_errors=True)

    print("")
    print("=== 13. 清理：只列不删 / 只删小米包 / 不碰看板数据 ===")
    clean_dir = Path(tempfile.mkdtemp(prefix="ra_clean_test_"))
    try:
        pkg = clean_dir / "20260923_MiFitness_c3_data_copy.zip"
        pkg.write_bytes(b"pkg")
        unrelated = clean_dir / "别的下载.zip"
        unrelated.write_bytes(b"unrelated")
        state_file = clean_dir / "mail_state.json"
        state_file.write_text('{"handled_uids": ["26", "27"], "last_success": "x"}',
                              encoding="utf-8")

        packages, state = clean.collect(search_dirs=(clean_dir,), state_path=state_file)
        check("预演只列出小米包（名字口径）",
              [p.name for p in packages], ["20260923_MiFitness_c3_data_copy.zip"])
        check("预演认出状态文件", state, state_file)
        check("预演阶段什么都不删",
              (pkg.exists(), unrelated.exists(), state_file.exists()), (True, True, True))
        check("状态文件有可读摘要", "2 封" in clean.describe_state(state_file), True)

        removed, failed = clean.apply(packages, state)
        check("真删：包没了", pkg.exists(), False)
        check("真删：状态文件没了", state_file.exists(), False)
        check("真删：无关下载还在", unrelated.exists(), True)
        check("真删：返回清单 2 项", len(removed), 2)
        check("没有删不掉的", failed, [])

        empty, no_state = clean.collect(search_dirs=(clean_dir,), state_path=state_file)
        check("清空后再预演：什么都没有", (empty, no_state), ([], None))
    finally:
        shutil.rmtree(clean_dir, ignore_errors=True)

    print("")
    print("=" * 46)
    if failures:
        print("FAILED %d 项：%s" % (len(failures), failures))
        return 1
    print("ALL_PASS  邮件解析、判定与取信策略全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
