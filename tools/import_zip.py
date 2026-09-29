# -*- coding: utf-8 -*-
"""一键导入小米隐私中心导出包 → data.js

设计要点
1. 压缩包带密码（传统 ZipCrypto，Python 标准库 zipfile 直接支持，无需第三方库）。
2. **只读白名单内的两个成员**，全程流式解密，不落盘：
     * hlth_center_sport_record.csv   每场跑步的汇总字段
     * hlth_center_fitness_data.csv   逐分钟心率 / 步数（未压缩 199 MB，必须流式）
   其余 32 个成员（user_member_profile / user_device_setting / user_fitness_data_records
   / MiFitness_data_copy_guide.pdf 等）**一次都不会被打开**，因此不会产生任何副本。
3. 幂等：同一份包跑一次和跑十次结果一致。
4. **新旧只认「哪个更新」，不认场数多少**（2026-09-24 多弗朗要求）：本地有多个包时按
   文件名里的导出日期挑最新的那个；写入前也只比「最新一场的日期」——比看板旧才拒绝
   （防重下一份旧包把看板倒推回去）。场数只用来打一行提醒，不再当写不写的依据：
   场数少不代表包旧（导出侧的坏时间戳会被年份规则滤掉，实测出现过）。

用法
    python tools/import_zip.py                    # 自动找最新的导出包
    python tools/import_zip.py <zip 路径>          # 指定包（也支持把包拖到 .cmd 上）
    python tools/import_zip.py --list              # 只列出将要读取的成员，不需要密码
    python tools/import_zip.py --password xxx      # 临时提供密码
    python tools/import_zip.py --no-prompt         # 无人值守：缺密码就直接报错，绝不挂住

返回码
    0 成功｜2 找不到包｜3 没密码｜4 密码错｜5 AES 包｜6 包比看板旧（拒绝写入）｜7 包里没有跑步数据成员

密码来源（按顺序）
    --password  >  环境变量 MIFITNESS_ZIP_PASSWORD  >  交互输入（需要 TTY）

    **刻意不落盘、不读密码文件**：小米的导出密码会变，存下来只会变成"上次的密码"。
    正常路径下没人手输：tools/fetch_from_mail.py 从邮件里取到密码后，经环境变量传给
    子进程，用完即弃。导入窗口（tools/import_gui.py）里**没有**密码框 —— 2026-09-24
    起只走邮箱这一条路，手动导入只剩命令行这一种方式。
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime
import getpass
import io
import json
import os
from pathlib import Path
import re
import sys
import zipfile

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OUT = PROJECT_DIR / "data.js"
PREV_OUT = PROJECT_DIR / "data.prev.js"

# 搜索导出包的位置（按顺序；一个都找不到就报错，并告诉他怎么手动指定）。
# inbox / latest 收在项目内（2026-09-23 起）：项目打包时自动带走，不再依赖 D:\RunningData。
# D:\网页下载 是浏览器实际的下载落点（2026-09-24 实测确认：那里的包一直没被找到，
# 因为老配置只认它下面的「新建文件夹」，而那个子目录并不存在）。
SEARCH_DIRS = (
    PROJECT_DIR / "inbox",
    PROJECT_DIR / "latest",
)
# 只找项目内的目录（2026-09-24 收敛，多弗朗要求）。
# 以前还包含 D:\网页下载 与系统下载目录，代价是：清掉状态后 baseline 归零，
# 那里任何一个刚下载的 zip 都会被当成「最新的导出包」（实测 archify.zip 就被选中过）。
# 自动下载的落点本来就是本项目 inbox，没必要再去外面翻。
# 手动导入不受影响：命令行传的是显式路径（python tools/import_zip.py <zip>），不走这套搜索。

# ==== 小米导出包的名字口径（唯一权威）====
# 清理旧包、清理下载产物都按这一条判断；**绝不按「旧的 zip」这种宽口径删** ——
# `D:\网页下载` 是他的下载目录，里面还有别的文件。
PACKAGE_NAME_RE = re.compile(r"MiFitness.*\.zip$", re.I)

# ==== 白名单：只允许读这两个成员，其余一律不打开 ====
SPORT_SUFFIX = "hlth_center_sport_record.csv"
FITNESS_SUFFIX = "hlth_center_fitness_data.csv"
READ_WHITELIST = (SPORT_SUFFIX, FITNESS_SUFFIX)

# 明确「绝对不会碰」的敏感成员前缀，仅用于向用户展示，不参与逻辑
SENSITIVE_HINTS = (
    "user_member_profile",
    "user_fitness_profile",
    "user_device_setting",
    "user_fitness_data_records",
    "user_pk_",
    "tws_user_privacy_history",
    "user_health_plan_records",
    "data_copy_guide",
)

TZ = datetime.timezone(datetime.timedelta(hours=8))
log: list[str] = []


def say(msg: str) -> None:
    log.append(msg)
    print(msg)


def local(ts: int) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(int(ts), TZ)


def num(value, default=0):
    return value if isinstance(value, (int, float)) else default


# ------------------------------------------------------------------ 找包 / 开包
# 小米的导出包以**导出日期**开头：20260923_2848269717_MiFitness_c3_data_copy.zip
# 这个日期是「内容的新旧」；mtime 只是「什么时候下到本地」。
STAMP_RE = re.compile(r"^(\d{8})")


def name_stamp(name: str) -> int:
    """文件名里的导出日期（YYYYMMDD）；没有就是 0。"""
    match = STAMP_RE.match(name or "")
    return int(match.group(1)) if match else 0


def package_stamp(path: Path) -> tuple[int, float]:
    """包的「新旧」排序键：先看文件名里的导出日期，再兜底看 mtime。

    为什么不能只看 mtime：重新下一份**旧**包会让 mtime 变新，于是被当成最新
    （实测过：改了搜索目录后，一个刚下载的无关 zip 就被选中过）。日期是内容自带的，
    不受下载时间影响。
    """
    return (name_stamp(path.name), path.stat().st_mtime)


def find_latest_zip() -> Path | None:
    """搜索目录里**内容最新**的导出包 —— 不是「最近下载的那个」。"""
    candidates = []
    for folder in SEARCH_DIRS:
        if not folder.is_dir():
            continue
        for item in folder.glob("*.zip"):
            # 口径与以前保持一致（MiFitness / MiWear 都算），这次只改「挑哪个」不改「挑不挑」。
            if "MiFitness" not in item.name and "MiWear" not in item.name:
                continue
            try:
                package_stamp(item)
            except OSError:
                continue
            candidates.append(item)
    if not candidates:
        return None
    return max(candidates, key=package_stamp)


def match_members(names: list[str]) -> dict[str, str]:
    """按后缀在白名单里挑成员；返回 {内部键: 压缩包内完整名}。"""
    picked: dict[str, str] = {}
    for name in names:
        base = name.rsplit("/", 1)[-1]
        for suffix in READ_WHITELIST:
            if base.endswith(suffix):
                picked[suffix] = name
    return picked


def resolve_password(args) -> str | None:
    """密码只认「本次调用」给的：命令行参数或环境变量。

    刻意**不读密码文件** —— 小米的导出密码会变，存下来只会变成"上次的密码"，
    让人对着一个过期值反复失败。窗口会每次都问一遍。
    """
    if args.password:
        return args.password
    env = os.environ.get("MIFITNESS_ZIP_PASSWORD")
    if env:
        return env
    # 注意：Windows 上 getpass 会绕过 stdin 重定向直接读控制台，被模块/计划任务调用时
    # 会永久挂住。所以自动化入口必须显式传 --no-prompt。
    if getattr(args, "no_prompt", False):
        return None
    if not sys.stdin or not sys.stdin.isatty():
        return None
    try:
        return getpass.getpass("小米导出包密码（输入时不显示）: ") or None
    except (EOFError, KeyboardInterrupt):
        return None


# ------------------------------------------------------------------ 两个成员的解析
def read_sport_records(stream) -> list[dict]:
    """sport_record.csv：每场一行，Key=outdoor_running，Value 是 JSON。"""
    raw = []
    wrapper = io.TextIOWrapper(stream, encoding="utf-8-sig", errors="replace", newline="")
    for row in csv.DictReader(wrapper):
        if row.get("Key") != "outdoor_running":
            continue
        try:
            value = json.loads(row["Value"])
        except Exception:
            continue
        if not value.get("distance"):
            continue
        start = int(value.get("start_time") or row.get("Time") or 0)
        end = int(value.get("end_time") or 0)
        if end <= start:
            end = start + int(value.get("duration") or 0)
        raw.append({"start": start, "end": end, "v": value})
    raw.sort(key=lambda r: r["start"])
    return raw


def scan_minute_series(stream, cut: int):
    """fitness_data.csv：长表，Key ∈ {steps, heart_rate}，流式读，不落盘。"""
    hr_pts: list[tuple[int, object]] = []
    step_pts: list[tuple[int, int, float]] = []
    wrapper = io.TextIOWrapper(stream, encoding="utf-8-sig", errors="replace", newline="")
    reader = csv.reader(wrapper)
    next(reader, None)
    for row in reader:
        if len(row) < 5:
            continue
        key = row[2]
        if key != "steps" and key != "heart_rate":
            continue
        try:
            t = int(row[3])
        except Exception:
            continue
        if t < cut:
            continue
        try:
            if key == "heart_rate":
                hr_pts.append((t, json.loads(row[4]).get("bpm")))
            else:
                d = json.loads(row[4])
                step_pts.append((t, d.get("steps") or 0, d.get("distance") or 0))
        except Exception:
            pass
    hr_pts.sort()
    step_pts.sort()
    return hr_pts, step_pts


def window(points, a: int, b: int):
    lo, hi = 0, len(points)
    while lo < hi:
        mid = (lo + hi) // 2
        if points[mid][0] < a:
            lo = mid + 1
        else:
            hi = mid
    out = []
    i = lo
    while i < len(points) and points[i][0] <= b:
        out.append(points[i])
        i += 1
    return out


# ------------------------------------------------------------------ 组装
def _dedupe_minute(points, keep):
    """同一分钟写了两条记录时只留覆盖最完整的那条。

    口径唯一在 metrics.minute_window_points（那里写了为什么必须去重）。这里延迟
    导入并降级：指标模块出问题不该连带整个导入失败，退化成原样返回即可 ——
    metrics 层计算时还会再兜一次，看板结论不受影响。
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import metrics as _metrics
        return _metrics.minute_window_points(points, keep)
    except Exception:                              # pragma: no cover
        return points


def build_runs(raw: list[dict], hr_pts, step_pts) -> list[dict]:
    runs = []
    for item in raw:
        v = item["v"]
        a, b = item["start"], item["end"]
        dur = int(num(v.get("duration"), b - a))
        dist = num(v.get("distance"))
        pace = (dur / (dist / 1000.0)) if dist else 0

        hr_series = []
        for t, bpm in window(hr_pts, a, b):
            if isinstance(bpm, (int, float)) and bpm:
                hr_series.append([round((t - a) / 60.0, 2), int(bpm)])

        pace_series = []
        cad_series = []
        for t, steps, d in window(step_pts, a, b):
            minute = round((t - a) / 60.0, 2)
            if d and d > 10:
                pace_series.append([minute, round(60000.0 / d, 1)])
            if steps:
                cad_series.append([minute, int(steps)])

        runs.append({
            "id": a,
            "year": local(a).year,
            "date": local(a).strftime("%Y-%m-%d"),
            "time": local(a).strftime("%H:%M"),
            "month": local(a).strftime("%Y-%m"),
            "distance": round(dist / 1000.0, 3),
            "duration": dur,
            "pace": round(pace, 1),
            "steps": int(num(v.get("steps"))),
            "calories": int(num(v.get("calories"))),
            "cadence": int(num(v.get("avg_cadence"))),
            "cadenceMax": int(num(v.get("max_cadence"))),
            "stride": round(num(v.get("avg_stride")) / 100.0, 3),
            "hr": int(num(v.get("avg_hrm"))),
            "hrMax": int(num(v.get("max_hrm"))),
            "hrMin": int(num(v.get("min_hrm"))),
            "td": num(v.get("avg_touchdown_duration")),
            "tdMin": num(v.get("min_touchdown_duration")),
            "tdMax": num(v.get("max_contact_time") or v.get("max_touchdown_duration")),
            "va": num(v.get("avg_vertical_amplitude")),
            "vaMin": num(v.get("min_vertical_amplitude")),
            "vaMax": num(v.get("max_vertical_amplitude")),
            "vsr": num(v.get("avg_vertical_stride_ratio")),
            "vsrMin": num(v.get("min_vertical_stride_ratio")),
            "vsrMax": num(v.get("max_vertical_stride_ratio")),
            "vo2": num(v.get("vo2_max")),
            "effect": num(v.get("train_effect")),
            "load": num(v.get("train_load")),
            "recover": num(v.get("recover_time")),
            "zones": {
                "warm": int(num(v.get("hrm_warm_up_duration"))),
                "fat": int(num(v.get("hrm_fat_burning_duration"))),
                "aer": int(num(v.get("hrm_aerobic_duration"))),
                "ana": int(num(v.get("hrm_anaerobic_duration"))),
                "ext": int(num(v.get("hrm_extreme_duration"))),
            },
            "hrSeries": hr_series,
            "paceSeries": _dedupe_minute(pace_series, "min"),
            "cadSeries": _dedupe_minute(cad_series, "max"),
        })
    return runs


def build_months(runs: list[dict]) -> list[dict]:
    months: collections.OrderedDict = collections.OrderedDict()
    for x in runs:
        m = months.setdefault(x["month"], {"month": x["month"], "count": 0, "distance": 0.0,
                                           "duration": 0, "calories": 0, "load": 0})
        m["count"] += 1
        m["distance"] += x["distance"]
        m["duration"] += x["duration"]
        m["calories"] += x["calories"]
        m["load"] += x["load"]
    for m in months.values():
        m["distance"] = round(m["distance"], 1)
        m["pace"] = round(m["duration"] / m["distance"], 1) if m["distance"] else 0
    return list(months.values())


def previous_scope() -> tuple[int, str]:
    """现有 data.js 的（场次数，最新一场的日期）。

    要的是「最新到哪一天」—— 判断新包该不该写进看板，比的就是这一个值。
    """
    if not DEFAULT_OUT.is_file():
        return 0, ""
    try:
        text = DEFAULT_OUT.read_text(encoding="utf-8")
        payload = json.loads(text.split("=", 1)[1].rstrip().rstrip(";"))
        runs = payload.get("runs") or []
        return len(runs), max((r.get("date") or "" for r in runs), default="")
    except Exception:
        return 0, ""


# ------------------------------------------------------------------ 主流程
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="小米隐私中心导出包 → run-analytics/data.js")
    parser.add_argument("zip", nargs="?", help="导出包路径；省略则自动搜索最新的一个")
    parser.add_argument("--password", help="压缩包密码（一次性；默认读环境变量 MIFITNESS_ZIP_PASSWORD）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出 data.js 路径")
    parser.add_argument("--list", action="store_true", help="只列出将读取的成员，不解析（不需要密码）")
    parser.add_argument("--no-prompt", action="store_true",
                        help="禁止交互输入密码；模块 / 计划任务等无人值守调用必须带这个参数")
    parser.add_argument("--force", action="store_true", help="场次数变少时仍然写入")
    args = parser.parse_args(argv)

    zip_path = Path(args.zip) if args.zip else find_latest_zip()
    if zip_path is None or not zip_path.is_file():
        say("找不到导出包。把包路径直接传进来（python tools/import_zip.py <zip>），")
        say("或者把包放进以下任一目录后重跑：")
        for folder in SEARCH_DIRS:
            say("  " + str(folder))
        return 2

    say("导出包  : %s" % zip_path)
    say("大小    : %.1f MB" % (zip_path.stat().st_size / 1024 / 1024))

    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        names = [i.filename for i in infos]
        picked = match_members(names)
        sensitive = [n for n in names
                     if any(h in n for h in SENSITIVE_HINTS)
                     and n.rsplit("/", 1)[-1] not in picked.values()]

        # 隐私交代压成 3 行：以前把 6 个「跳过」的敏感成员名逐个列出来，每次导入都是
        # 同样那 11 行，纯噪声（实测占导入日志 17 行里的 11 行）。只读的那两个要留全名，
        # 那是「只读白名单」这个承诺的证据；不读的给个总数就够。
        say("")
        say("包内 %d 个成员，只解密读取这 %d 个（其余 %d 个一律不打开，含 %d 个敏感成员）："
            % (len(names), len(picked), len(names) - len(picked), len(sensitive)))
        for suffix, name in picked.items():
            info = zf.getinfo(name)
            # 只报白名单后缀，不报完整成员名 —— 完整名前面那截（uid + 日期 + MiFitness_）
            # 每个成员都一样，截断显示还会切出半截 "…_2848…"，反而看不懂。
            say("  [读取] %-42s %9.1f KB 未压缩" % (suffix, info.file_size / 1024.0))

        if not picked:
            # 丢错了包（别的导出、下载没完成、或者不是小米的包）时给一句人话，
            # 不要甩 StopIteration 的 traceback —— 用户看到的是「工具坏了」而不是「包不对」。
            say("")
            say("这个压缩包里没有找到跑步数据（需要 %s）。" % "、".join(READ_WHITELIST))
            say("请确认丢进来的是小米运动健康的导出包，并且下载完整。")
            return 7

        if args.list:
            say("")
            say("--list 模式：未读取任何内容、未解密、未写入。")
            return 0

        password = resolve_password(args)
        if not password:
            say("")
            say("没有拿到密码。给法任选其一：")
            say("  1) python tools/import_zip.py <包路径> --password 你的密码（命令行手动导入）")
            say("  2) 设环境变量 MIFITNESS_ZIP_PASSWORD 再跑")
            say("  3) 正常路径不用管这一步：双击「从邮箱导入.cmd」，密码由邮件里取。")
            return 3

        # 先用最小成员验密码，避免拿错密码去解 199 MB 的文件
        probe_name = picked.get(SPORT_SUFFIX) or next(iter(picked.values()))
        try:
            with zf.open(probe_name, pwd=password.encode("utf-8")) as probe:
                probe.read(16)
        except RuntimeError as exc:
            say("")
            say("密码不对（%s）。" % exc)
            say("命令行手动导入的话重新跑一次、换一个密码；走邮箱的话直接重跑"
                "「从邮箱导入.cmd」，它会自动拿最新一封邮件里的密码。")
            return 4
        except NotImplementedError:
            say("")
            say("这个包用的是 AES 加密，标准库不支持。装一下 pyzipper 再重试：")
            say("  .venv\\Scripts\\python.exe -m pip install pyzipper")
            return 5

        say("")
        say("密码校验通过，开始解析（不落盘）…")
        with zf.open(probe_name, pwd=password.encode("utf-8")) as stream:
            raw = read_sport_records(stream)
        # 场次和年份挤一行：年份分布只在「场数比上一版少」时有用（定位导出侧的坏时间戳），
        # 单独占一行不值当。
        year_counts = collections.Counter(local(r["start"]).year for r in raw)
        say("  场次 : %d 场户外跑步（年份 %s）"
            % (len(raw), " / ".join("%d:%d" % kv for kv in sorted(year_counts.items()))))

        total_raw = len(raw)
        raw = [r for r in raw if local(r["start"]).year >= 2025]
        dropped_out_of_range = total_raw - len(raw)
        cut = int(datetime.datetime(2025, 1, 1, tzinfo=TZ).timestamp())
        hr_pts, step_pts = [], []
        if FITNESS_SUFFIX in picked:
            with zf.open(picked[FITNESS_SUFFIX], pwd=password.encode("utf-8")) as stream:
                hr_pts, step_pts = scan_minute_series(stream, cut)
        # 这一行要在读 195 MB 之后才出现（约 5 秒），是等待期间唯一的心跳 —— 别和上一行合并
        say("  逐分钟 : 心率 %d 点，步数 %d 点" % (len(hr_pts), len(step_pts)))

        runs = build_runs(raw, hr_pts, step_pts)
        months = build_months(runs)

    # 写不写，只看「哪份更新」，不看场数多少（多弗朗 2026-09-24 的要求）。
    old_count, old_latest = previous_scope()
    new_latest = max((r["date"] for r in runs), default="")
    if old_latest and new_latest and new_latest < old_latest and not args.force:
        say("")
        say("拒绝写入：这份包里最新的一场是 %s，看板里已经是 %s —— 这是一份更旧的导出包。"
            % (new_latest, old_latest))
        say("（判据是「最新的一场」，不是场数：重下一份旧包不该把看板倒推回去。）")
        say("确实要用它覆盖就加 --force。")
        return 6
    if old_count and len(runs) < old_count:
        say("")
        say("提醒：这次 %d 场，上一版 %d 场（少 %d 场）。但最新一场是 %s，比看板的 %s 新，所以照写。"
            % (len(runs), old_count, old_count - len(runs), new_latest, old_latest or "—"))
        # 少数记录的时间戳在导出侧就是坏的（实测出现过：一场 2026 年的跑步变成 2000 年），
        # 被「只看 2025 年及以后」滤掉 —— 这种情况不是丢了历史。
        if dropped_out_of_range:
            say("  少的那部分里有 %d 场时间戳落在 2025 年之前（导出侧异常），被年份规则滤掉了，"
                "不是历史丢了。" % dropped_out_of_range)
        else:
            say("  如果不是你自己删过记录，把这份日志发我看看。")

    payload = {
        "generated": local(int(datetime.datetime.now(TZ).timestamp())).strftime("%Y-%m-%d %H:%M"),
        "scope": "2026 年 · 仅户外跑步",
        "runs": runs,
        "months": months,
    }

    # metrics 段必须在这里一起算：前端不再自己算指标（口径只此一份）。
    # 漏了这一步，导入完的 data.js 会没有 metrics，页面只剩"先跑 metrics.py"的提示。
    # 算指标失败不应该让整个导入失败 —— 数据本身已经写进去了，降级为警告即可。
    say("")
    try:
        # 显式把 tools/ 加进 sys.path：以 `python tools/import_zip.py` 直接跑时
        # sys.path[0] 已经是它，但以 `python -m tools.import_zip` 跑时不是（踩过）。
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import metrics as _metrics
        payload["metrics"] = _metrics.build(runs)
        say("已算好指标与 %d 条发现" % len(payload["metrics"]["findings"]))
    except Exception as exc:                       # pragma: no cover
        say("警告：指标计算失败（%s），看板会提示先跑 python tools/metrics.py" % exc)

    out_path = Path(args.out)
    if out_path.is_file():
        PREV_OUT.write_text(out_path.read_text(encoding="utf-8"), encoding="utf-8")
    with out_path.open("w", encoding="utf-8") as handle:
        handle.write("window.RUNNER_DATA = ")
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write(";\n")

    say("")
    say("完成：%s（%.1f KB）｜场次 %d（上一版 %d，%+d）｜月份 %d 个"
        % (out_path, out_path.stat().st_size / 1024.0,
           len(runs), old_count, len(runs) - old_count, len(months)))
    if PREV_OUT.is_file():
        say("  上一版已备份到 %s" % PREV_OUT.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
