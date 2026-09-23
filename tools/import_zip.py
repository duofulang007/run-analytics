# -*- coding: utf-8 -*-
"""一键导入小米隐私中心导出包 → data.js

设计要点
1. 压缩包带密码（传统 ZipCrypto，Python 标准库 zipfile 直接支持，无需第三方库）。
2. **只读白名单内的两个成员**，全程流式解密，不落盘：
     * hlth_center_sport_record.csv   每场跑步的汇总字段
     * hlth_center_fitness_data.csv   逐分钟心率 / 步数（未压缩 199 MB，必须流式）
   其余 32 个成员（user_member_profile / user_device_setting / user_fitness_data_records
   / MiFitness_data_copy_guide.pdf 等）**一次都不会被打开**，因此不会产生任何副本。
3. 幂等：同一份包跑一次和跑十次结果一致；场次数变少时拒绝写入（防「导出漏了几天」把历史吞掉）。

用法
    python tools/import_zip.py                    # 自动找最新的导出包
    python tools/import_zip.py <zip 路径>          # 指定包（也支持把包拖到 .cmd 上）
    python tools/import_zip.py --list              # 只列出将要读取的成员，不需要密码
    python tools/import_zip.py --password xxx      # 临时提供密码
    python tools/import_zip.py --no-prompt         # 无人值守：缺密码就直接报错，绝不挂住

返回码
    0 成功｜2 找不到包｜3 没密码｜4 密码错｜5 AES 包｜6 场次数倒退（拒绝写入）｜7 包里没有跑步数据成员

密码来源（按顺序）
    --password  >  环境变量 MIFITNESS_ZIP_PASSWORD  >  交互输入（需要 TTY）

    **刻意不落盘、不读密码文件**：小米的导出密码会变，存下来只会变成"上次的密码"。
    正常用法是在导入窗口（tools/import_gui.py）里每次现填。
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
import sys
import zipfile

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OUT = PROJECT_DIR / "data.js"
PREV_OUT = PROJECT_DIR / "data.prev.js"

# 搜索导出包的位置（按顺序，找不到就报错并提示拖拽）。
# inbox / latest 收在项目内（2026-09-23 起）：项目打包时自动带走，不再依赖 D:\RunningData。
SEARCH_DIRS = (
    PROJECT_DIR / "inbox",
    PROJECT_DIR / "latest",
    Path(r"D:\网页下载\新建文件夹"),
    Path.home() / "Downloads",
)

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
def find_latest_zip() -> Path | None:
    best: Path | None = None
    best_mtime = -1.0
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
            "paceSeries": pace_series,
            "cadSeries": cad_series,
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


def previous_run_count() -> int:
    if not DEFAULT_OUT.is_file():
        return 0
    try:
        text = DEFAULT_OUT.read_text(encoding="utf-8")
        payload = json.loads(text.split("=", 1)[1].rstrip().rstrip(";"))
        return len(payload.get("runs") or [])
    except Exception:
        return 0


# ------------------------------------------------------------------ 主流程
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="小米隐私中心导出包 → runner-lab/data.js")
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
        say("找不到导出包。请把 zip 拖到本脚本（或 import-running-data.cmd）上，")
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

        say("")
        say("压缩包内共 %d 个成员，本次只会解密并读取 %d 个：" % (len(names), len(picked)))
        for suffix, name in picked.items():
            info = zf.getinfo(name)
            say("  [读取] %-52s %9.1f KB（未压缩）" % (name[-52:], info.file_size / 1024.0))
        say("  其余 %d 个成员一个都不会被打开，其中包括：" % (len(names) - len(picked)))
        shown = 0
        for n in sensitive:
            if shown >= 6:
                say("         …（共 %d 个敏感成员，全部跳过）" % len(sensitive))
                break
            say("  [跳过] %s" % n.rsplit("/", 1)[-1])
            shown += 1

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
            say("没有拿到密码。两种给法任选其一：")
            say("  1) 在导入窗口里输入（推荐，每次现填，不留痕）")
            say("  2) python tools/import_zip.py --password 你的密码，或设环境变量 MIFITNESS_ZIP_PASSWORD")
            return 3

        # 先用最小成员验密码，避免拿错密码去解 199 MB 的文件
        probe_name = picked.get(SPORT_SUFFIX) or next(iter(picked.values()))
        try:
            with zf.open(probe_name, pwd=password.encode("utf-8")) as probe:
                probe.read(16)
        except RuntimeError as exc:
            say("")
            say("密码不对（%s）。请在导入窗口里重新输入。" % exc)
            return 4
        except NotImplementedError:
            say("")
            say("这个包用的是 AES 加密，标准库不支持。装一下 pyzipper 再重试：")
            say("  backend\\.venv\\Scripts\\python.exe -m pip install pyzipper")
            return 5

        say("")
        say("密码校验通过，开始流式解析（不落盘）…")
        with zf.open(probe_name, pwd=password.encode("utf-8")) as stream:
            raw = read_sport_records(stream)
        say("  sport_record : %d 场户外跑步" % len(raw))

        year_counts = collections.Counter(local(r["start"]).year for r in raw)
        say("  年份分布     : %s" % dict(sorted(year_counts.items())))

        raw = [r for r in raw if local(r["start"]).year >= 2025]
        cut = int(datetime.datetime(2025, 1, 1, tzinfo=TZ).timestamp())
        hr_pts, step_pts = [], []
        if FITNESS_SUFFIX in picked:
            with zf.open(picked[FITNESS_SUFFIX], pwd=password.encode("utf-8")) as stream:
                hr_pts, step_pts = scan_minute_series(stream, cut)
        say("  逐分钟序列   : 心率点 %d，步数点 %d" % (len(hr_pts), len(step_pts)))

        runs = build_runs(raw, hr_pts, step_pts)
        months = build_months(runs)

    old_count = previous_run_count()
    if old_count and len(runs) < old_count and not args.force:
        say("")
        say("拒绝写入：这次只解析出 %d 场，上一版有 %d 场。" % (len(runs), old_count))
        say("导出可能漏了历史（换机 / 同步延迟）。确认没问题就加 --force 再来一次。")
        return 6

    payload = {
        "generated": local(int(datetime.datetime.now(TZ).timestamp())).strftime("%Y-%m-%d %H:%M"),
        "scope": "2026 年 · 仅户外跑步",
        "runs": runs,
        "months": months,
    }

    out_path = Path(args.out)
    if out_path.is_file():
        PREV_OUT.write_text(out_path.read_text(encoding="utf-8"), encoding="utf-8")
    with out_path.open("w", encoding="utf-8") as handle:
        handle.write("window.RUNNER_DATA = ")
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write(";\n")

    say("")
    say("完成：%s（%.1f KB）" % (out_path, out_path.stat().st_size / 1024.0))
    say("  场次 %d（上一版 %d，%+d）  月份 %d 个"
        % (len(runs), old_count, len(runs) - old_count, len(months)))
    if PREV_OUT.is_file():
        say("  上一版已备份到 %s" % PREV_OUT.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
