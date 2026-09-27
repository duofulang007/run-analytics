"""tools/metrics.py —— 指标计算模块（口径只此一份）

所有前端要显示的统计量都在这里算，结果写进 data.js 的 metrics 段。
前端只负责渲染，不再自己算指标 —— 避免同一屏出现两套打架的口径。

用法：
    python tools/metrics.py            # 读 data.js，算完写回 metrics 段
    python tools/metrics.py --check    # 只算不写，打印摘要
    python tools/metrics.py --selftest # 离线自测，不读 data.js

设计约束（每一条都是踩过的坑）：
  * ACWR / 单调性 必须把**休息日按 0 计入**。只算有训练的日子会让单调性虚高
    好几倍（实测 1.36 会被算成 9.81，结论从"正常"翻成"高风险"）。
  * 有氧底座 = 热身 + 燃脂 + 有氧 三者。只算热身+燃脂会把占大头的有氧跑
    误判成高强度，结论完全反掉。
  * 解耦用前后半程 EF 的比值。逐分钟序列不足 12 点的不算。
  * 临界速度用标准二参数模型 speed = CS + D'/T，对 1/T 做线性回归，截距才是 CS。
  * 所有分组比较每组至少 3 场才出结论，否则只给原始均值。
  * 缺失就是缺失，不做插值、不用代理量冒充（例如不能拿 hrMin 当静息心率）。
"""

import argparse
import collections
import json
import math
import re
import statistics as st
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_JS = ROOT / "data.js"

# 统计窗口；浏览器只读取已计算结果。
WINDOW_DAYS = 28          # 默认窗口
HERO_WEEKS = 12           # 主指标曲线长度
GAIT_RECENT = 10          # 跑姿「最近表现」取样场次
EQUIV_HR = 155            # 等效配速的锚定心率
EQUIV_TOL = 8             # 锚定心率的容差
STABLE_CV = 0.12          # 判定「稳态跑」的配速变异上限
MIN_GROUP = 3             # 分组比较的最小样本量


# ---------------------------------------------------------------- 基础工具
def percentile(vals, p):
    """线性插值分位。空序列返回 None（不返回 0，0 会被误当成真实值）。"""
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    k = (len(vals) - 1) * p
    lo, hi = int(math.floor(k)), min(int(math.ceil(k)), len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def band(vals):
    """参考带 = P25 / P50 / P75。用他自己的历史分位，不用跑圈通行口径。"""
    vals = [v for v in vals if v is not None]
    if len(vals) < 5:
        return None
    return {"p25": round(percentile(vals, .25), 1),
            "p50": round(percentile(vals, .5), 1),
            "p75": round(percentile(vals, .75), 1),
            "n": len(vals)}


def mean(vals):
    vals = [v for v in vals if v is not None]
    return round(st.mean(vals), 1) if vals else None


def cv(vals):
    """变异系数。空或均值为 0 时返回 1（视为不稳定）。"""
    vals = [v for v in vals if v]
    if len(vals) < 3 or not st.mean(vals):
        return 1.0
    return st.pstdev(vals) / st.mean(vals)


def dist_band(d):
    if d is None:
        return None
    return "<4" if d < 4 else "4-6" if d < 6 else "6-8" if d < 8 else "8-12" if d < 12 else "12+"


def intensity_band(r):
    """强度档：低强度 = 热身+燃脂+有氧 占 85% 以上。"""
    z = r.get("zones") or {}
    tot = sum(z.values() or [0])
    if not tot:
        return None
    return "低强度为主" if (z.get("warm", 0) + z.get("fat", 0) + z.get("aer", 0)) / tot >= .85 \
        else "含阈上"


# ---------------------------------------------------------------- 负荷模型
def day_loads(runs):
    """{日期: 当日总负荷}。休息日不出现（取值时按 0 处理）。"""
    out = collections.defaultdict(float)
    for r in runs:
        out[r["date"]] += r.get("load") or 0
    return out


def daily_series(by_day, first, last):
    """把稀疏的日负荷铺成连续日序列（缺的填 0），休息日必须计入。"""
    d, out = first, []
    while d <= last:
        out.append((d.isoformat(), by_day.get(d.isoformat(), 0.0)))
        d += timedelta(days=1)
    return out


def load_model(by_day, first, last):
    """42/7-day load EWMAs; their difference is not a measured physiological state."""
    ctl = atl = 0.0
    series = []
    for d, load in daily_series(by_day, first, last):
        ctl += (load - ctl) / 42.0
        atl += (load - atl) / 7.0
        series.append([d, round(ctl, 1), round(atl, 1), round(ctl - atl, 1)])
    return series


def tsb_zone(tsb):
    if tsb is None:
        return None
    if tsb < -30:
        return "偏重"
    if tsb < -10:
        return "生产性疲劳"
    if tsb < 5:
        return "平稳"
    if tsb < 25:
        return "新鲜"
    return "偏休息"


def acwr(by_day, end, acute=7, chronic=28):
    """急慢性负荷比。两端都含休息日（按 0 计入），否则比值会失真。"""
    a = sum(by_day.get((end - timedelta(days=i)).isoformat(), 0) for i in range(acute)) / acute
    c = sum(by_day.get((end - timedelta(days=i)).isoformat(), 0) for i in range(chronic)) / chronic
    if not c:
        return None, a, c
    return a / c, a, c


def acwr_series(by_day, end, weeks=12):
    out = []
    for w in range(weeks - 1, -1, -1):
        d = end - timedelta(days=w * 7)
        r, a, c = acwr(by_day, d)
        if r is not None:
            out.append([d.isoformat(), round(r, 2), round(a, 1), round(c, 1)])
    return out


def monotony_strain(by_day, week_end):
    """Foster 原口径：7 天窗口，休息日计 0。单调性>2 视为偏高。"""
    vals = [by_day.get((week_end - timedelta(days=i)).isoformat(), 0.0) for i in range(7)]
    m = st.mean(vals)
    sd = st.pstdev(vals)
    if not sd:
        return None
    mono = m / sd
    return {"mono": round(mono, 2), "strain": round(m * mono), "days": sum(1 for v in vals if v > 0)}


def monotony_series(by_day, end, weeks=12):
    out = []
    for w in range(weeks - 1, -1, -1):
        d = end - timedelta(days=w * 7)
        r = monotony_strain(by_day, d)
        if r:
            out.append([d.isoformat()] + [r["mono"], r["strain"], r["days"]])
    return out


# ---------------------------------------------------------------- 能力指标
def efficiency_factor(r):
    """EF = 速度(m/min) / 心率。只对稳态跑有意义，所以调用方要先过 cv() 筛。"""
    if not r.get("pace") or not r.get("hr"):
        return None
    return round((1000.0 / r["pace"]) * 60 / r["hr"], 3)


def decoupling(r):
    """有氧解耦 Pa:HR = 后半程 EF / 前半程 EF - 1。负值 = 后半程心率漂高。"""
    ps = [(m, p) for m, p in (r.get("paceSeries") or []) if p]
    hs = [(m, h) for m, h in (r.get("hrSeries") or []) if h]
    if len(ps) < 12 or len(hs) < 12:
        return None
    mid = ps[-1][0] / 2.0
    p1 = [p for m, p in ps if m <= mid]
    p2 = [p for m, p in ps if m > mid]
    h1 = [h for m, h in hs if m <= mid]
    h2 = [h for m, h in hs if m > mid]
    if not (p1 and p2 and h1 and h2):
        return None
    ef1 = (1000.0 / st.mean(p1)) * 60 / st.mean(h1)
    ef2 = (1000.0 / st.mean(p2)) * 60 / st.mean(h2)
    if not ef1:
        return None
    return round((ef2 / ef1 - 1) * 100, 1)


def run_segment_analysis(run):
    """按本场总时长三等分，汇总逐分钟配速、心率、步频的中位数。"""
    series = {
        "pace": run.get("paceSeries") or [],
        "hr": run.get("hrSeries") or [],
        "cadence": run.get("cadSeries") or [],
    }
    duration = (run.get("duration") or 0) / 60.0
    if duration <= 0:
        minutes = []
        for points in series.values():
            for point in points:
                try:
                    minutes.append(float(point[0]))
                except (TypeError, ValueError, IndexError):
                    continue
        duration = max(minutes) if minutes else 0
    cuts = [0.0, duration / 3, duration * 2 / 3, duration]
    labels = (("front", "前程"), ("middle", "中程"), ("back", "后程"))
    segments = []
    for index, (key, label) in enumerate(labels):
        start, end = cuts[index], cuts[index + 1]
        values = {}
        for metric, points in series.items():
            picked = []
            for point in points:
                try:
                    minute, value = float(point[0]), float(point[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if not (math.isfinite(minute) and math.isfinite(value) and value > 0):
                    continue
                in_segment = start <= minute < end or (index == 2 and minute == end)
                if in_segment:
                    picked.append(value)
            values[metric] = {
                "value": round(percentile(picked, .5), 1) if len(picked) >= 3 else None,
                "n": len(picked),
            }
        segments.append({
            "key": key,
            "label": label,
            "minutes": [round(start, 1), round(end, 1)],
            **values,
        })

    front, back = segments[0], segments[-1]
    vs_front = {
        metric: round(back[metric]["value"] - front[metric]["value"], 1)
        if front[metric]["value"] is not None and back[metric]["value"] is not None else None
        for metric in series
    }
    thresholds = {"pace": 5, "hr": 3, "cadence": 3}
    units = {"pace": "秒/km", "hr": "bpm", "cadence": "步/分"}
    directions = {
        "pace": ("慢 ", "快 "),
        "hr": ("高 ", "低 "),
        "cadence": ("多 ", "少 "),
    }
    vs_front_text = {}
    for metric, value in vs_front.items():
        if value is None:
            vs_front_text[metric] = "样本不足"
        elif abs(value) < thresholds[metric]:
            vs_front_text[metric] = "接近"
        else:
            direction = directions[metric][0] if value > 0 else directions[metric][1]
            vs_front_text[metric] = direction + str(round(abs(value))) + " " + units[metric]
    pace_delta, hr_delta = vs_front["pace"], vs_front["hr"]
    if pace_delta is None or hr_delta is None:
        guidance = "分钟样本不足，暂不判断前后程变化。"
    elif pace_delta > 5 and hr_delta > 3:
        guidance = "后程慢了 %d 秒/公里，心率高了 %d bpm；下次前段再稳一点，给后程留余量。" % (
            round(pace_delta), round(hr_delta))
    elif pace_delta > 5:
        guidance = "后程慢了 %d 秒/公里，心率没有明显升高；下次先保持舒服节奏，再观察后程。" % round(pace_delta)
    elif pace_delta < -5 and abs(hr_delta) <= 3:
        guidance = "后程快了 %d 秒/公里，心率变化不大；这次节奏控制较稳。" % round(abs(pace_delta))
    elif pace_delta < -5 and hr_delta < -3:
        guidance = "后程快了 %d 秒/公里，心率低了 %d bpm；这次后程较从容。" % (
            round(abs(pace_delta)), round(abs(hr_delta)))
    elif pace_delta < -5:
        guidance = "后程快了 %d 秒/公里，心率也更高；下次前段可更从容，给后程留余量。" % round(abs(pace_delta))
    elif hr_delta > 3:
        guidance = "后程配速接近前程，心率高了 %d bpm；下次继续留意中后程体感。" % round(hr_delta)
    elif hr_delta < -3:
        guidance = "后程配速接近前程，心率低了 %d bpm；这次后程状态较从容。" % round(abs(hr_delta))
    else:
        guidance = "后程配速与前程接近，心率变化也小；下次可以沿用这次的节奏。"
    return {
        "basis": "按本场时长三等分",
        "durationMinutes": round(duration, 1),
        "minPoints": 3,
        "segments": segments,
        "vsFront": vs_front,
        "vsFrontText": vs_front_text,
        "guidance": guidance,
    }


def equiv_pace(runs, target=EQUIV_HR, tol=EQUIV_TOL):
    """等效配速：同心率下的配速。这是唯一控制住强度变量后还能比较的能力量。"""
    return [(r["date"], r["pace"]) for r in runs
            if r.get("pace") and r.get("hr") and abs(r["hr"] - target) <= tol]


def equiv_pace_weekly(runs, end, weeks=HERO_WEEKS):
    pts = equiv_pace(runs)
    out = []
    for w in range(weeks - 1, -1, -1):
        a = (end - timedelta(days=w * 7 + 6)).isoformat()
        b = (end - timedelta(days=w * 7)).isoformat()
        v = [p for d, p in pts if a <= d <= b]
        out.append([b, round(st.mean(v), 1) if v else None, len(v)])
    return out


def critical_speed(runs):
    """二参数模型 distance = CS*T + D'  =>  speed = CS + D'/T，对 1/T 回归取截距。

    ⚠️ 这个模型要求各时长的**全力**成绩。他 95 场里绝大多数是轻松跑，
    直接拿全部点回归会被那一坨集中点带偏，算出比中位配速还慢的 CS（实测踩过：
    全量回归得 8.45 km/h，而他最快一场是 5'26"）。所以先按时长分档、
    每档只取最快的一场，逼近「速度-时长曲线」的上包络。
    样本不足或结果不落在观测范围内时返回 None —— 宁可不出这个指标，也不出假数。
    """
    pts = [(r["duration"] / 60.0, r["distance"] / (r["duration"] / 3600.0))
           for r in runs if r.get("duration", 0) >= 300 and r.get("distance")]
    if len(pts) < 12:
        return None

    # 按时长分档（对数分档，短时长密一些），每档取最快一场
    edges = [5, 8, 12, 18, 25, 35, 50, 70, 100, 1000]
    best = {}
    for t, s in pts:
        for i in range(len(edges) - 1):
            if edges[i] <= t < edges[i + 1]:
                if i not in best or s > best[i][1]:
                    best[i] = (t, s)
                break
    picked = sorted(best.values())
    if len(picked) < 5:
        return None

    xs = [1.0 / t for t, _ in picked]
    ys = [s for _, s in picked]
    n = len(xs)
    mx, my = st.mean(xs), st.mean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if not den:
        return None
    b = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / den
    a = my - b * mx

    speeds = [s for _, s in pts]
    # 合理性守卫：CS 必须落在「最快一场」与「中位速度」之间，否则不可信
    if not (percentile(speeds, .5) * 0.95 <= a <= max(speeds) * 1.02):
        return None
    if b <= 0:
        return None
    return {"speed": round(a, 2), "pace": int(3600 / a), "dPrime": round(b, 3),
            "n": n, "buckets": len(picked)}


def aerobic_base_share(r):
    z = r.get("zones") or {}
    tot = sum(z.values() or [0])
    if not tot:
        return None
    return round((z.get("warm", 0) + z.get("fat", 0) + z.get("aer", 0)) / tot * 100, 1)


# ---------------------------------------------------------------- 分组
def group_runs(runs):
    """距离档 × 强度档。每组至少 3 场才标记为可出结论。"""
    g = collections.OrderedDict()
    for r in runs:
        db, ib = dist_band(r.get("distance")), intensity_band(r)
        if not db or not ib:
            continue
        k = db + " · " + ib
        g.setdefault(k, []).append(r)
    out = []
    for k in sorted(g):
        v = g[k]
        out.append({
            "key": k,
            "n": len(v),
            "ok": len(v) >= MIN_GROUP,
            "distance": mean([x["distance"] for x in v]),
            "pace": mean([x["pace"] for x in v]),
            "hr": mean([x.get("hr") for x in v]),
            "cadence": mean([x.get("cadence") for x in v]),
            "td": mean([x.get("td") for x in v]),
        })
    return out


# ---------------------------------------------------------------- findings
def _finding(fid, kicker, title, conclusion, chart, tone, score):
    return {"id": fid, "kicker": kicker, "title": title, "conclusion": conclusion,
            "chart": chart, "tone": tone, "score": round(score, 1)}


def build_findings(ctx):
    """规则层。每条规则独立读全量数据，产出一个「发现」。
    显著性 = 新鲜度 × 偏离度 × 样本量 —— 三者缺一都会把噪声顶到首屏。"""
    f = []
    runs = ctx["runs"]
    end = ctx["end"]
    today = ctx["today"]

    # ① 数据新鲜度：过期时它必须压过所有分析
    days_old = (today - end).days
    if days_old > 3:
        f.append(_finding(
            "freshness", "数据", "数据已 %d 天没更新" % days_old,
            "下面所有结论都基于 %s 之前的数据，先刷新再判断" % end.isoformat(),
            {"type": "bullet", "value": days_old, "max": 14, "target": 3,
             "label": "距上次导入", "unit": "天", "tone": "warn"},
            "warn", 100 + days_old))

    # ② 跑姿变化：以最近表现为准，这是最容易和「全量历史」结论相反的一条。
    #    四项合并成一张哑铃图 —— 它们量纲不同，但哑铃图每行各自归一化，可以并排。
    recent = runs[-GAIT_RECENT:]
    gait_rows, moved = [], []
    for key, label, worse, unit in (("cadence", "步频", "low", "spm"),
                                    ("td", "触地时间", "high", "ms"),
                                    ("va", "垂直振幅", "high", "cm"),
                                    ("vsr", "垂直步幅比", "high", "%")):
        b = ctx["bands"].get(key)
        if not b:
            continue
        cur = mean([r.get(key) for r in recent])
        if cur is None:
            continue
        iqr = (b["p75"] - b["p25"]) or 1
        z = (cur - b["p50"]) / iqr * (1 if worse == "high" else -1)
        gait_rows.append({"label": label, "from": b["p50"], "to": cur,
                          "band": [b["p25"], b["p75"]],
                          "good": "down" if worse == "high" else "up"})
        if abs(z) >= 0.5:
            moved.append((label, cur, b["p50"], z, unit))
    if gait_rows:
        moved.sort(key=lambda x: -abs(x[3]))
        if moved:
            n_up = sum(1 for m in moved if m[3] < 0)
            n_bad = len(moved) - n_up
            head = "%d 项明显改善" % n_up if n_up and not n_bad else \
                   "%d 项明显偏离" % n_bad if n_bad and not n_up else \
                   "%d 项改善、%d 项偏离" % (n_up, n_bad)
            detail = "；".join(
                "%s %.1f（历史中位 %.1f）" % (m[0], m[1], m[2]) for m in moved[:3])
            f.append(_finding(
                "gait", "跑姿",
                "跑姿近 %d 场：%s" % (GAIT_RECENT, head),
                detail + "。参考带用的是你自己的 P25–P75，不是跑圈通行口径",
                {"type": "dumbbell", "rows": gait_rows, "unit": ""},
                "good" if n_up > n_bad else "warn",
                52 + sum(min(abs(m[3]), 6) for m in moved) * 4))

    # ③ 等效配速：唯一控制住强度变量的能力量
    h = ctx["hero"]
    if h and h["n"] >= 8:
        d = h["delta"]
        if d is None:
            verdict = "样本不足，不做对比"
        elif d < -1:
            verdict = "比更早的 %d 场快 %.1f 秒" % (h["deltaN"], abs(d))
        elif d > 1:
            verdict = "比更早的 %d 场慢 %.1f 秒" % (h["deltaN"], abs(d))
        else:
            verdict = "与更早的 %d 场基本持平" % h["deltaN"]
        f.append(_finding(
            "equiv", "能力", "等效配速 %s" % fmt_pace(h["value"]),
            verdict + "（同心率 %d bpm，已控制强度变量）" % EQUIV_HR,
            {"type": "line", "series": [[s[0], s[1]] for s in h["series"] if s[1]],
             "band": [h["p25"], h["p75"]], "invert": True, "unit": "s"},
            "neutral", 40 + min(abs(d or 0), 30) * 1.6))

    # ④ 有氧底座 + 解耦：训练结构对不对
    ab = ctx["aerobic"]
    if ab["baseRecent"] is not None and ab["baseMedian"] is not None:
        gap = ab["baseRecent"] - ab["baseMedian"]
        f.append(_finding(
            "aerobicBase", "结构", "有氧底座占比 %.0f%%" % ab["baseRecent"],
            "全量中位 %.0f%%，近 28 天%s" % (ab["baseMedian"],
                                        "明显抬升" if gap > 10 else "基本持平"),
            {"type": "bullet", "value": ab["baseRecent"], "max": 100, "target": 70,
             "label": "近 28 天低强度占比", "unit": "%",
             "tone": "good" if ab["baseRecent"] >= 70 else "warn"},
            "good" if gap > 10 else "neutral", 45 + min(abs(gap), 40)))
    if ab["n"] >= 10:
        f.append(_finding(
            "decoupling", "有氧", "有氧解耦中位 %+.1f%%" % ab["decouplingMedian"],
            "%d 场里只有 %.0f%% 达标（|解耦| < 5%%）" % (ab["n"], ab["passRate"] * 100),
            {"type": "histogram", "hist": ab["hist"], "mark": 0,
             "judge": [-5, 5], "unit": "%"},
            "good" if ab["passRate"] >= .6 else "warn", 50 + (1 - ab["passRate"]) * 30))

    # ⑤ 负荷安全
    a = ctx["acwr"]
    if a["value"]:
        safe = .8 <= a["value"] <= 1.3
        f.append(_finding(
            "acwr", "负荷", "急慢性负荷比 %.2f" % a["value"],
            "安全带 0.8–1.3，当前%s" % ("在带内" if safe else "已出带"),
            {"type": "line", "series": [[s[0], s[1]] for s in a["series"]],
             "safeBand": [.8, 1.3], "unit": ""},
            "good" if safe else "warn", 42 if safe else 78))

    # ⑥ 跑量环比：涨太快要单独报警，主动减量不报警
    if ctx["volume28"] and ctx["volumePrev"]:
        delta = ctx["volume28"] / ctx["volumePrev"] - 1
        if delta > .10:
            f.append(_finding(
                "volume", "负荷", "28 天跑量 %+.0f%%" % (delta * 100),
                "%.1f km → %.1f km，涨得偏快" % (ctx["volumePrev"], ctx["volume28"]),
                {"type": "bullet", "value": round(ctx["volume28"], 1),
                 "max": max(ctx["volume28"], ctx["volumePrev"]) * 1.4,
                 "target": ctx["volumePrev"] * 1.1,
                 "label": "近 28 天跑量", "unit": "km", "tone": "warn"},
                "warn", 40 + min(delta, 1.5) * 40))

    # ⑦ 最近一场复盘
    last = runs[-1] if runs else None
    if last:
        d = ctx["derived"].get(last["id"], {})
        dec = d.get("decoup")
        f.append(_finding(
            "lastRun", "单场", "最近一场 · %s" % last["date"][5:],
            "%.1f km · %s · 心率 %.0f%s" % (
                last["distance"], fmt_pace(last["pace"]), last["hr"] or 0,
                ("，解耦 %+.1f%%" % dec) if dec is not None else ""),
            {"type": "stackedBar", "zones": last["zones"], "unit": "s"},
            "neutral", 38 + (14 - min(days_old, 14)) * 2))

    # ⑧ 时段对比：同距离档下才可比，这是本项目特有的分组维度
    if ctx["todCompare"]:
        f.append(_finding(
            "timeOfDay", "时段", ctx["todCompare"]["title"],
            ctx["todCompare"]["conclusion"],
            {"type": "smallMultiples", "panels": ctx["todCompare"]["panels"]},
            "neutral", 34))

    # ⑨ 训练日历（26 周分布）
    f.append(_finding(
        "calendar", "节奏", "训练日历 · 近 26 周",
        "共 %d 场，%.0f km" % (len(runs), sum(r["distance"] for r in runs)),
        {"type": "calendar", "days": ctx["calendar"], "unit": "km"},
        "neutral", 30))

    f.sort(key=lambda x: -x["score"])
    return f


def fmt_pace(s):
    if s is None:
        return "—"
    s = int(round(s))
    return "%d'%02d\"" % (s // 60, s % 60)


def s1(v):
    """一位小数的字符串；None → —。板式里的数字一律先转成字符串，
    免得前端再决定「几位小数」——展示口径也只留一处。"""
    return "—" if v is None else "%.1f" % v


def s2(v):
    return "—" if v is None else "%.2f" % v


def week_key(r):
    """ISO 周的周一。周是有数据密度最高的粒度（28 周 / 84 周），所以它是默认视图。"""
    d = date.fromisoformat(r["date"])
    return (d - timedelta(days=d.weekday())).isoformat()


def bucketize(runs, keyfn):
    """按任意时间粒度分桶。

    只输出聚合值，**不输出场次清单** —— 溯源要用的场次由前端按日期区间
    从 runs 里现筛，避免同一份数据在 data.js 里存两遍。
    返回 [key, 里程, 场次, 配速, 心率, 负荷]，样本不足 MIN_GROUP 的照样输出，
    由前端负责灰化并标 n（用户明确要求：显示但灰化，而不是隐藏或降级）。
    """
    g = collections.OrderedDict()
    for r in sorted(runs, key=lambda x: x["date"]):
        g.setdefault(keyfn(r), []).append(r)
    out = []
    for k in sorted(g):
        v = g[k]
        dist = sum(x["distance"] for x in v)
        dur = sum(x["duration"] for x in v)
        hrs = [x.get("hr") for x in v if x.get("hr")]
        out.append([
            k, round(dist, 1), len(v),
            int(dur / dist) if dist else None,
            round(st.mean(hrs)) if hrs else None,
            round(sum(x.get("load") or 0 for x in v), 1),
        ])
    return out


# ---------------------------------------------------------------- 日视图
DAY_WINDOW = 14          # 「最近 14 天」—— 多弗朗要的「十几天的内容，精确到日」
DOW_NAMES = "一二三四五六日"


def _day_zone_share(rs):
    """当天的心率区间占比。返回 (阈上占比%, 有氧底座占比%, total)。"""
    z = {"warm": 0.0, "fat": 0.0, "aer": 0.0, "ana": 0.0, "ext": 0.0}
    for r in rs:
        rz = r.get("zones") or {}
        for k in z:
            z[k] += rz.get(k) or 0
    total = sum(z.values())
    if not total:
        return 0.0, 0.0, 0.0
    # 有氧底座 = 热身 + 燃脂 + 有氧（三者都在无氧阈以下）。只算前两项会把
    # 占大头的有氧跑误判成高强度 —— 这条踩过，见 build_findings 里的同款注释。
    return (z["ana"] + z["ext"]) / total * 100, (z["warm"] + z["fat"] + z["aer"]) / total * 100, total


def _day_rx(tsb, n, hard_pct):
    """「这一天之后，下次该怎么跑」—— 一句话动作，不是一句描述。

    判据全部复用本项目已有的量（TSB 分区 + 强度结构），**不发明新指标**。
    chips 只给区间，不给假精确的数值。
    """
    if n == 0:
        return {"tone": "rest", "head": "休息日：不用补跑",
                "why": "当天没有训练。少跑一天不会掉体能，连着把缺的补回来才是风险。",
                "chips": []}
    if tsb is None:
        return {"tone": "info", "head": "按感觉跑", "why": "负荷数据不足，不做判断。", "chips": []}
    if tsb < -30:
        return {"tone": "warn", "head": "下次降一档：跑短、跑慢",
                "why": "这天结束时 TSB %.1f，已进 −30 警戒区 —— 疲劳明显高于体能，加量只会抬伤病概率。" % tsb,
                "chips": ["距离减半", "心率 <145"]}
    if tsb < -10:
        chips = ["维持跑量", "不加距离"]
        why = "TSB %.1f 落在生产性疲劳区 —— 这正是练出体能的位置，维持就好。" % tsb
        if hard_pct >= 30:
            why += "但这天阈上占了 %.0f%%，有氧差一点。" % hard_pct
            chips = ["补一次纯轻松跑", "心率 <145"]
        return {"tone": "info", "head": "下次按计划跑，别加量", "why": why, "chips": chips}
    return {"tone": "good", "head": "下次可以上强度",
            "why": "TSB %.1f，疲劳已经退掉 —— 质量课放在这种状态下才跑得出来。" % tsb,
            "chips": ["可跑节奏跑", "先热身 1–2 km"]}


def day_view(runs, by_day, lm, end, window=DAY_WINDOW):
    """最近 N 天的日视图 ——「精确到日」的那一层。

    为什么预计算而不是前端现拼：
      · 每天的「下次处方」是**判断**不是渲染，判断必须口径唯一
      · 每天的 CTL/ATL/TSB 直接从负荷模型的日序列里取，**不另算一遍**，
        否则同一根线会在页面上出现两个版本
      · 休息日按 0 负荷计入（与 ACWR / 单调性同一条规矩），所以窗口内
        没跑步的日子也要占一格 —— 那正是「节奏」本身

    返回 {"window", "days": [...], "summary": {...}}。前端只负责画。
    """
    index = {row[0]: row for row in lm}           # date -> [date, ctl, atl, tsb]
    per_day = collections.defaultdict(list)
    for r in runs:
        per_day[r["date"]].append(r)

    days = []
    for i in range(window - 1, -1, -1):
        d = (end - timedelta(days=i)).isoformat()
        rs = per_day.get(d, [])
        dist = sum(x["distance"] for x in rs)
        dur = sum(x["duration"] for x in rs)
        hrs = [x.get("hr") for x in rs if x.get("hr")]
        row = index.get(d)
        ctl, atl, tsb = (row[1], row[2], row[3]) if row else (None, None, None)
        hard, base, _tot = _day_zone_share(rs)
        days.append({
            "date": d,
            "dow": DOW_NAMES[date.fromisoformat(d).weekday()],
            "km": round(dist, 2),
            "n": len(rs),
            "pace": int(round(dur / dist)) if dist else None,
            "hr": round(st.mean(hrs)) if hrs else None,
            "load": round(sum(x.get("load") or 0 for x in rs), 1),
            "ctl": ctl, "atl": atl, "tsb": tsb,
            "zone": tsb_zone(tsb),
            "hardPct": round(hard, 1),
            "basePct": round(base, 1),
            "rx": _day_rx(tsb, len(rs), hard),
        })

    km = sum(x["km"] for x in days)
    nrn = sum(x["n"] for x in days)
    trained = sum(1 for x in days if x["n"])
    t0, t1 = days[0]["tsb"], days[-1]["tsb"]
    z0, z1 = days[0]["zone"], days[-1]["zone"]
    last = days[-1]

    # 一句话结论：先说这十几天干了什么，再说接下来该怎么跑（指导性）。
    if trained == 0:
        head = "最近 %d 天没有训练记录" % window
        why = "先在小米 App 里申请一次导出，再看这几天该怎么安排。"
    else:
        head = "最近 %d 天跑了 %d 天 / %s km" % (window, trained, round(km, 1))
        if t0 is None or t1 is None or z0 == z1:
            why = "状态一直在「%s」区间。" % (z1 or "—")
        else:
            why = "状态从 %s 走到 %s（TSB %s → %s）。" % (
                z0, z1, ("%+.1f" % t0) if t0 is not None else "—",
                ("%+.1f" % t1) if t1 is not None else "—")
        why += "下次：%s" % last["rx"]["head"]

    return {
        "window": window,
        "days": days,
        "summary": {
            "window": window,
            "km": round(km, 1),
            "runs": nrn,
            "trainedDays": trained,
            "restDays": window - trained,
            "avgPerWeek": round(km / window * 7, 1),
            "tsbFrom": t0, "tsbTo": t1,
            "zoneFrom": z0, "zoneTo": z1,
            "tsbSeries": [[x["date"], x["tsb"]] for x in days],
            "head": head, "why": why,
        },
    }


# ---------------------------------------------------------------- 跑步复盘
def _period_stats(rs):
    zones = {k: sum((r.get("zones") or {}).get(k, 0) for r in rs)
             for k in ("warm", "fat", "aer", "ana", "ext")}
    zone_total = sum(zones.values())
    distance = sum(r.get("distance") or 0 for r in rs)
    duration = sum(r.get("duration") or 0 for r in rs)
    return {
        "n": len(rs), "km": round(distance, 1),
        "avgKm": round(distance / len(rs), 2) if rs else None,
        "longestKm": round(max((r.get("distance") or 0 for r in rs)), 2) if rs else None,
        "pace": int(round(duration / distance)) if distance else None,
        "hr": round(mean([r.get("hr") for r in rs])) if rs else None,
        "cadence": round(mean([r.get("cadence") for r in rs])) if rs else None,
        "td": round(mean([r.get("td") for r in rs])) if rs else None,
        "zones": zones,
        "aerobicPct": round((zones["warm"] + zones["fat"] + zones["aer"]) / zone_total * 100)
                      if zone_total else None,
        "highPct": round((zones["ana"] + zones["ext"]) / zone_total * 100)
                   if zone_total else None,
        "ids": [str(r["id"]) for r in rs],
    }


def _matched_pace_delta(current, baseline):
    """只在距离档与强度档都相同、且两侧各至少 3 场时比较配速。"""
    def grouped(rs):
        out = collections.defaultdict(list)
        for r in rs:
            db, ib = dist_band(r.get("distance")), intensity_band(r)
            if db and ib and r.get("pace"):
                out[(db, ib)].append(r["pace"])
        return out

    a, b = grouped(current), grouped(baseline)
    pairs = [(a[k], b[k]) for k in a.keys() & b.keys() if len(a[k]) >= MIN_GROUP and len(b[k]) >= MIN_GROUP]
    if not pairs:
        return {"ok": False, "n": 0, "seconds": None}
    weights = [min(len(x), len(y)) for x, y in pairs]
    delta = sum((st.mean(x) - st.mean(y)) * w for (x, y), w in zip(pairs, weights)) / sum(weights)
    return {"ok": True, "n": sum(weights), "seconds": round(delta)}


def _easy_share(rs):
    """心率区间分钟数中的低强度占比；没有区间数据时保持缺失。"""
    easy = total = 0
    for r in rs:
        z = r.get("zones") or {}
        easy += sum(z.get(k, 0) for k in ("warm", "fat", "aer"))
        total += sum(z.values())
    return round(easy / total * 100) if total else None


def build_insight_snapshot(runs, end):
    """首屏最新一场 + 相邻 28 天组合比较。只输出有证据支撑的方向。"""
    last = runs[-1]
    recent_start = (end - timedelta(days=27)).isoformat()
    previous_start = (end - timedelta(days=55)).isoformat()
    previous_end = (end - timedelta(days=28)).isoformat()
    current = [r for r in runs if recent_start <= r["date"] <= end.isoformat()]
    previous = [r for r in runs if previous_start <= r["date"] <= previous_end]

    def period(rs):
        distances = [r.get("distance") for r in rs if r.get("distance")]
        return {"n": len(rs), "km": round(sum(distances), 1),
                "avgKm": round(st.mean(distances), 2) if distances else None,
                "longestKm": round(max(distances), 2) if distances else None,
                "easyPct": _easy_share(rs),
                "ids": [str(r["id"]) for r in rs]}

    cur, prev = period(current), period(previous)
    enough = cur["n"] >= MIN_GROUP and prev["n"] >= MIN_GROUP
    deltas = {
        "km": round(cur["km"] - prev["km"], 1) if enough else None,
        "sessions": cur["n"] - prev["n"] if enough else None,
        "avgKm": round(cur["avgKm"] - prev["avgKm"], 2) if enough and cur["avgKm"] and prev["avgKm"] else None,
        "longestKm": round(cur["longestKm"] - prev["longestKm"], 2) if enough and cur["longestKm"] and prev["longestKm"] else None,
        "easyPp": cur["easyPct"] - prev["easyPct"] if enough and cur["easyPct"] is not None and prev["easyPct"] is not None else None,
    }

    # 周期配速必须在同一距离档、同一强度档分别有至少三场。
    paired = []
    for db in ("<4", "4-6", "6-8", "8-12", "12+"):
        for ib in ("低强度为主", "含阈上"):
            a = [r for r in current if dist_band(r.get("distance")) == db and intensity_band(r) == ib and r.get("pace") and r.get("hr")]
            b = [r for r in previous if dist_band(r.get("distance")) == db and intensity_band(r) == ib and r.get("pace") and r.get("hr")]
            if len(a) >= MIN_GROUP and len(b) >= MIN_GROUP:
                pace_delta = round(st.mean(r["pace"] for r in a) - st.mean(r["pace"] for r in b))
                hr_delta = round(st.mean(r["hr"] for r in a) - st.mean(r["hr"] for r in b))
                dist_delta = round(st.mean(r["distance"] for r in a) - st.mean(r["distance"] for r in b), 1)
                a_paces, b_paces = [r["pace"] for r in a], [r["pace"] for r in b]
                noise = max(5, round(((percentile(a_paces, .75) - percentile(a_paces, .25))
                                       + (percentile(b_paces, .75) - percentile(b_paces, .25))) / 2))
                signal = "mixed"
                if abs(dist_delta) <= .3 and hr_delta >= -2 and pace_delta > noise:
                    signal = "slower"
                elif abs(dist_delta) <= .3 and hr_delta <= 2 and pace_delta < -noise:
                    signal = "faster"
                paired.append({"band": db, "intensity": ib, "currentN": len(a), "previousN": len(b),
                               "paceSeconds": pace_delta, "hrBpm": hr_delta,
                               "distanceKm": dist_delta, "paceNoiseSeconds": noise,
                               "signal": signal,
                               "currentIds": [str(r["id"]) for r in a],
                               "previousIds": [str(r["id"]) for r in b]})
    paired.sort(key=lambda x: (-min(x["currentN"], x["previousN"]), x["band"]))

    # 最新一场：除了项目既有的距离档和强度档，再把距离限在 ±20%、心率限在 ±5 bpm。
    # 这是“类似跑”，不足三场就只显示事实，不下进退结论。
    cut56 = (end - timedelta(days=56)).isoformat()
    comparable = [r for r in runs[:-1] if last.get("distance") and last.get("pace") and last.get("hr")
                  and intensity_band(last) is not None
                  and r["date"] >= cut56 and r.get("pace") and r.get("hr")
                  and dist_band(r.get("distance")) == dist_band(last.get("distance"))
                  and intensity_band(r) == intensity_band(last)
                  and abs(r["distance"] - last["distance"]) <= last["distance"] * .2
                  and abs(r["hr"] - last["hr"]) <= 5]
    same = {"n": len(comparable), "paceMedian": None, "paceSeconds": None,
            "hrMedian": None, "hrBpm": None, "highPctMedian": None, "highPp": None,
            "ids": [str(r["id"]) for r in comparable]}
    if len(comparable) >= MIN_GROUP:
        pace_base = percentile([r["pace"] for r in comparable], .5)
        hr_base = percentile([r["hr"] for r in comparable], .5)
        same.update({"paceMedian": round(pace_base), "paceSeconds": round(last["pace"] - pace_base),
                     "hrMedian": round(hr_base), "hrBpm": round(last["hr"] - hr_base)})
        shares = [100 - _easy_share([r]) for r in comparable if _easy_share([r]) is not None]
        last_share = _easy_share([last])
        if len(shares) >= MIN_GROUP and last_share is not None:
            same["highPctMedian"] = round(percentile(shares, .5))
            same["highPp"] = 100 - last_share - same["highPctMedian"]

    last_easy = _easy_share([last])
    return {"last": {"id": str(last["id"]), "date": last["date"], "time": last.get("time"),
                     "distance": round(last["distance"], 2), "duration": last.get("duration"),
                     "pace": last.get("pace"), "hr": last.get("hr"),
                     "highPct": 100 - last_easy if last_easy is not None else None,
                     "comparable": same},
            "recent28": cur, "previous28": prev, "delta28": deltas,
            "paired": paired, "periodEnough": enough}


def timeline_series(runs, gran):
    """Weekly rows for the review, including empty weeks and a fair partial-week baseline."""
    if gran != "week":
        raise ValueError("Only weekly review is supported")
    def week_start(value):
        return value - timedelta(days=value.weekday())

    by = collections.defaultdict(list)
    for run in runs:
        by[week_start(date.fromisoformat(run["date"]))].append(run)
    first = week_start(date.fromisoformat(runs[0]["date"]))
    as_of = date.fromisoformat(runs[-1]["date"])
    last = week_start(as_of)
    rows = []
    key = first
    while key <= last:
        full_end = key + timedelta(days=6)
        through = min(full_end, as_of)
        row_runs = [run for run in by.get(key, []) if date.fromisoformat(run["date"]) <= through]
        rows.append({"key": key.isoformat(), "label": key.strftime("%m/%d"),
                     "from": key.isoformat(), "to": full_end.isoformat(),
                     "through": through.isoformat(), "partial": through < full_end,
                     **_period_stats(row_runs)})
        key += timedelta(days=7)

    for index, row in enumerate(rows):
        current_runs = by.get(date.fromisoformat(row["key"]), [])
        previous = rows[index - 1] if index else None
        previous_runs = by.get(date.fromisoformat(previous["key"]), []) if previous else []
        if previous and row["partial"]:
            elapsed = (date.fromisoformat(row["through"]) - date.fromisoformat(row["from"])).days + 1
            cutoff = date.fromisoformat(previous["from"]) + timedelta(days=elapsed - 1)
            previous_runs = [run for run in previous_runs if date.fromisoformat(run["date"]) <= cutoff]
        base = _period_stats(previous_runs) if previous else None
        enough = bool(base and row["n"] >= MIN_GROUP and base["n"] >= MIN_GROUP)
        row["compare"] = {"previous": {
            "n": base["n"] if base else 0,
            "through": (date.fromisoformat(previous["from"]) + timedelta(
                days=(date.fromisoformat(row["through"]) - date.fromisoformat(row["from"])).days)).isoformat()
                if previous and row["partial"] else previous["through"] if previous else None,
            "kmDelta": round(row["km"] - base["km"], 1) if enough else None,
            "pace": _matched_pace_delta(current_runs, previous_runs) if previous else
                    {"ok": False, "n": 0, "seconds": None},
            "enough": enough,
        }}
    return rows


def build_board(ctx):
    """Current review pages, with a load proxy and per-run performance evidence."""
    runs, end = ctx["runs"], ctx["end"]
    return {
        "timeline": {"week": {"rows": timeline_series(runs, "week")}},
        "insights": build_insight_snapshot(runs, end),
        "fitness": build_fitness_view(runs, ctx["loadSeries"]),
    }


def _similar_runs(target, candidates):
    """Keep effort, distance and available intensity close before comparing pace."""
    if not target.get("distance") or not target.get("pace") or not target.get("hr"):
        return []
    intensity = intensity_band(target)
    if intensity is None:
        return []
    return [run for run in candidates
            if run.get("distance") and run.get("pace") and run.get("hr")
            and dist_band(run["distance"]) == dist_band(target["distance"])
            and intensity_band(run) == intensity
            and abs(run["distance"] - target["distance"]) <= target["distance"] * .2
            and abs(run["hr"] - target["hr"]) <= 5]


def build_fitness_view(runs, load_series):
    """42/7-day load estimates and comparable-run observations, not a fitness diagnosis."""
    if not runs or not load_series:
        return {"series": [], "runs": []}
    index = {row[0]: row for row in load_series}
    end = date.fromisoformat(runs[-1]["date"])
    start = (end - timedelta(days=119)).isoformat()
    series = [row[:3] for row in load_series if row[0] >= start]
    entries = []
    previous_date = None
    for i, run in enumerate(runs):
        run_date = date.fromisoformat(run["date"])
        lookback = (run_date - timedelta(days=56)).isoformat()
        prior = [r for r in runs[:i] if lookback <= r["date"] < run["date"]]
        comparable = _similar_runs(run, prior)
        paces = [r["pace"] for r in comparable]
        baseline = percentile(paces, .5) if len(paces) >= MIN_GROUP else None
        noise = max(8, round(percentile(paces, .75) - percentile(paces, .25))) if baseline is not None else None
        delta = round(run["pace"] - baseline) if baseline is not None else None
        rest_days = max(0, (run_date - previous_date).days - 1) if previous_date else None
        signal = "insufficient"
        if delta is not None:
            signal = "improved" if delta < -noise else "declined" if delta > noise else "similar"
        entries.append({"id": str(run["id"]), "date": run["date"],
                        "distance": round(run["distance"], 2), "pace": run.get("pace"),
                        "hr": run.get("hr"), "load": run.get("load"),
                        "base": index[run["date"]][1], "recent": index[run["date"]][2],
                        "restDays": rest_days, "signal": signal, "deltaSeconds": delta,
                        "noiseSeconds": noise, "baselinePace": round(baseline) if baseline is not None else None,
                        "comparableIds": [str(r["id"]) for r in comparable]})
        previous_date = run_date
    current = series[-1][1]
    before_date = (end - timedelta(days=28)).isoformat()
    before = index.get(before_date)
    # A 28-day difference is meaningful only with load samples across both periods.
    recent_n = sum(1 for run in runs if run["date"] >= (end - timedelta(days=27)).isoformat())
    previous_n = sum(1 for run in runs if (end - timedelta(days=55)).isoformat() <= run["date"] < (end - timedelta(days=27)).isoformat())
    change = round(current - before[1], 1) if before and recent_n >= MIN_GROUP and previous_n >= MIN_GROUP else None
    return {"series": series, "runs": entries, "asOf": end.isoformat(),
            "base": current, "recent": series[-1][2], "change28": change,
            "recentN": recent_n, "previousN": previous_n}


# ---------------------------------------------------------------- 汇总
def build(runs, today=None):
    runs = sorted(runs, key=lambda r: r["date"])
    if not runs:
        return None
    today = today or date.today()
    end = date.fromisoformat(runs[-1]["date"])
    first = date.fromisoformat(runs[0]["date"])
    by_day = day_loads(runs)

    # 逐场派生量
    derived = {}
    run_segments = {}
    for r in runs:
        ps = [p for _, p in (r.get("paceSeries") or []) if p]
        derived[r["id"]] = {
            "ef": efficiency_factor(r) if cv(ps) < STABLE_CV else None,
            "cv": round(cv(ps), 3),
            "decoup": decoupling(r),
            "base": aerobic_base_share(r),
        }
        run_segments[str(r["id"])] = run_segment_analysis(r)

    # 参考带（全部用他自己的历史分位）
    bands = {k: band([r.get(k) for r in runs])
             for k in ("cadence", "td", "va", "vsr", "stride", "pace", "hr")}
    bands["ef"] = band([v["ef"] for v in derived.values()])
    bands["decoup"] = band([v["decoup"] for v in derived.values()])

    # 主指标：等效配速
    eq = equiv_pace(runs)
    eqs = equiv_pace_weekly(runs, end)
    eq_vals = [p for _, p in eq]
    cutoff = (end - timedelta(days=HERO_WEEKS * 7)).isoformat()
    recent_eq = [p for d, p in eq if d >= cutoff]
    older_eq = [p for d, p in eq if d < cutoff]
    hero = {
        "key": "equivPace",
        "value": mean(recent_eq),
        "n": len(recent_eq),
        "p25": percentile(recent_eq, .25),
        "p75": percentile(recent_eq, .75),
        "delta": (mean(recent_eq) - mean(older_eq)) if (recent_eq and older_eq) else None,
        "deltaN": len(older_eq),
        "series": eqs,
        "allP50": percentile(eq_vals, .5),
        "hr": EQUIV_HR,
        "weeks": HERO_WEEKS,
    }

    # 负荷模型
    lm = load_model(by_day, first, end)
    cur = lm[-1]
    status = {"ctl": cur[1], "atl": cur[2], "tsb": cur[3], "zone": tsb_zone(cur[3]),
              "recover": runs[-1].get("recover"), "series": lm[-HERO_WEEKS * 7:]}

    # 负荷安全
    r, a, c = acwr(by_day, end)
    acwr_d = {"value": round(r, 2) if r else None, "acute": round(a, 1), "chronic": round(c, 1),
              "series": acwr_series(by_day, end)}

    # 有氧
    dec_vals = [v["decoup"] for v in derived.values() if v["decoup"] is not None]
    bins = [-30, -20, -15, -10, -5, 0, 5, 10, 15, 30]
    hist = [[bins[i], bins[i + 1], 0] for i in range(len(bins) - 1)]
    for v in dec_vals:
        for i in range(len(bins) - 1):
            if bins[i] <= v < bins[i + 1]:
                hist[i][2] += 1
                break
    base_all = [v["base"] for v in derived.values() if v["base"] is not None]
    cut28 = (end - timedelta(days=WINDOW_DAYS)).isoformat()
    base_recent = [v["base"] for k, v in derived.items()
                   if v["base"] is not None
                   and next((x["date"] for x in runs if x["id"] == k), "") >= cut28]
    aerobic = {
        "decouplingMedian": percentile(dec_vals, .5),
        "passRate": round(sum(1 for v in dec_vals if abs(v) < 5) / len(dec_vals), 3) if dec_vals else 0,
        "n": len(dec_vals),
        "hist": hist,
        "baseMedian": percentile(base_all, .5),
        "baseRecent": percentile(base_recent, .5),
        "baseHist": _hist10(base_all),
    }

    # 小卡
    recent28 = [x for x in runs if x["date"] >= cut28]
    prev28 = [x for x in runs
              if (end - timedelta(days=WINDOW_DAYS * 2)).isoformat() <= x["date"] < cut28]
    vol28 = sum(x["distance"] for x in recent28)
    vol_prev = sum(x["distance"] for x in prev28)
    cs = critical_speed(runs)
    ef_all = [v["ef"] for v in derived.values() if v["ef"] is not None]
    gait_recent = {k: mean([x.get(k) for x in runs[-GAIT_RECENT:]])
                   for k in ("cadence", "td", "va", "vsr")}
    small = [
        {"key": "volume", "label": "近 28 天跑量", "value": round(vol28, 1), "unit": "km",
         "note": "环比 %+.0f%%" % ((vol28 / vol_prev - 1) * 100) if vol_prev else "—",
         "tone": "warn" if vol_prev and vol28 / vol_prev - 1 > .10 else "neutral"},
        {"key": "cs", "label": "临界速度", "value": cs["speed"] if cs else None, "unit": "km/h",
         "note": fmt_pace(cs["pace"]) + " / km" if cs else "样本不足", "tone": "neutral"},
        {"key": "ef", "label": "效率因子 EF", "value": percentile(ef_all, .5), "unit": "",
         "note": "稳态 %d 场" % len(ef_all), "tone": "neutral"},
        {"key": "cadence", "label": "步频", "value": gait_recent["cadence"], "unit": "spm",
         "note": "近 %d 场 · 历史中位 %.0f" % (GAIT_RECENT, bands["cadence"]["p50"])
                 if bands["cadence"] else "—",
         "tone": "good" if bands["cadence"] and gait_recent["cadence"] > bands["cadence"]["p50"] else "neutral"},
        {"key": "va", "label": "垂直振幅", "value": gait_recent["va"], "unit": "cm",
         "note": "近 %d 场 · 历史中位 %.1f" % (GAIT_RECENT, bands["va"]["p50"])
                 if bands["va"] else "—",
         "tone": "good" if bands["va"] and gait_recent["va"] < bands["va"]["p50"] else "neutral"},
        {"key": "fresh", "label": "数据新鲜度", "value": (today - end).days, "unit": "天前",
         "note": "最后一场 " + end.isoformat()[5:],
         "tone": "warn" if (today - end).days > 3 else "good"},
    ]

    # 时段对比（同距离档内才比）
    tod_buckets = collections.defaultdict(list)
    for r in runs:
        h = int(r["time"][:2])
        t = "下午" if 14 <= h < 18 else "傍晚" if 18 <= h < 22 else "其他"
        tod_buckets[(dist_band(r["distance"]), t)].append(r)
    panels = []
    for db in ("<4", "4-6", "6-8", "8-12", "12+"):
        pm = tod_buckets.get((db, "下午"), [])
        em = tod_buckets.get((db, "傍晚"), [])
        if len(pm) >= MIN_GROUP and len(em) >= MIN_GROUP:
            panels.append({"label": db + " km", "a": mean([x["pace"] for x in pm]),
                           "b": mean([x["pace"] for x in em]),
                           "na": len(pm), "nb": len(em)})
    tod = None
    if panels:
        gap = mean([p["a"] - p["b"] for p in panels])
        tod = {
            "title": "傍晚比下午快 %.0f s/km" % gap,
            "conclusion": "同距离档下比较；傍晚心率更高，说明是跑得更用力而不是状态更好",
            "panels": panels,
        }

    # 日历（近 26 周）
    km_day = collections.defaultdict(float)
    for r in runs:
        km_day[r["date"]] += r["distance"]
    cal = []
    for i in range(26 * 7 - 1, -1, -1):
        d = (end - timedelta(days=i)).isoformat()
        cal.append([d, round(km_day.get(d, 0), 2)])

    ctx = {
        "runs": runs, "end": end, "today": today, "bands": bands, "hero": hero,
        "acwr": acwr_d, "aerobic": aerobic, "derived": derived, "status": status,
        "volume28": vol28, "volumePrev": vol_prev, "todCompare": tod, "calendar": cal,
        "loadSeries": lm,
    }
    findings = build_findings(ctx)
    # 复盘页面只需要首页摘要与逐周汇总。
    board = build_board(ctx)

    return {
        "computed": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "lastRun": end.isoformat(),
        "today": today.isoformat(),
        "daysOld": (today - end).days,
        "hero": hero,
        "status": status,
        "acwr": acwr_d,
        "aerobic": aerobic,
        "cs": cs,
        "bands": bands,
        "small": small,
        "gait": {k: {"now": gait_recent[k], "base": bands[k]["p50"] if bands[k] else None}
                 for k in ("cadence", "td", "va", "vsr")},
        "runSegments": run_segments,
        "groups": group_runs(runs),
        "tod": tod,
        "calendar": cal,
        "buckets": {
            "week": bucketize(runs, week_key),
            "month": bucketize(runs, lambda r: r["date"][:7]),
            "year": bucketize(runs, lambda r: r["date"][:4]),
            "day": bucketize(runs, lambda r: r["date"]),
        },
        # 「精确到日」的那一层：最近 14 天每天一条（含休息日）+ 下次处方
        "dayview": day_view(runs, by_day, lm, end),
        # 跑后首页与周复盘所需数据。
        "board": board,
        "findings": findings,
    }


def _hist10(vals):
    h = [0] * 10
    for v in vals:
        if v is not None:
            h[min(9, int(v / 10))] += 1
    return [[i * 10, i * 10 + 10, n] for i, n in enumerate(h)]


# ---------------------------------------------------------------- IO
def load(path=DATA_JS):
    text = path.read_text(encoding="utf-8")
    obj = json.loads(text[text.index("{"): text.rindex("}") + 1])
    return text[:text.index("{")], obj, text[text.rindex("}") + 1:]


def save(prefix, obj, suffix, path=DATA_JS):
    path.write_text(prefix + json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + suffix,
                    encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description="重算 data.js 的 metrics 段")
    ap.add_argument("--check", action="store_true", help="只算不写")
    ap.add_argument("--selftest", action="store_true", help="离线自测")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    prefix, obj, suffix = load()
    runs = obj["runs"]
    m = build(runs)
    if m is None:
        print("没有数据", file=sys.stderr)
        return 1
    print("场次 %d   主指标 等效配速 %s（n=%d，%s）"
          % (len(runs), fmt_pace(m["hero"]["value"]), m["hero"]["n"],
             ("%+.1fs" % m["hero"]["delta"]) if m["hero"]["delta"] is not None else "无对比"))
    print("状态 TSB %.1f（%s）· ACWR %.2f · 解耦中位 %+.1f%% · CS %.2f km/h"
          % (m["status"]["tsb"], m["status"]["zone"], m["acwr"]["value"] or 0,
             m["aerobic"]["decouplingMedian"] or 0, m["cs"]["speed"] if m["cs"] else 0))
    print("findings %d 条：" % len(m["findings"]))
    for f in m["findings"]:
        print("   %5.1f  [%s] %s — %s" % (f["score"], f["kicker"], f["title"], f["conclusion"]))
    if args.check:
        print("\n（--check：没有写回）")
        return 0
    obj["metrics"] = m
    save(prefix, obj, suffix)
    print("\n已写回 data.js")
    return 0


# ---------------------------------------------------------------- 自测
def selftest():
    """离线自测：不读 data.js，用构造数据验证每个口径。"""
    errs = []

    def ck(name, got, want, tol=1e-6):
        ok = abs(got - want) <= tol if isinstance(want, (int, float)) else got == want
        if not ok:
            errs.append("%s: 得到 %r，期望 %r" % (name, got, want))

    ck("percentile 中位", percentile([1, 2, 3, 4, 5], .5), 3, 1e-9)
    ck("percentile 空", percentile([], .5), None)
    ck("band 样本不足", band([1, 2, 3]), None)

    # ACWR 必须把休息日算进去：慢性窗口是 28 天，测试数据也要铺满 28 天
    end = date(2026, 9, 24)
    flat = {(end - timedelta(days=i)).isoformat(): 60.0 for i in range(28)}
    r, a, c = acwr(flat, end)
    ck("ACWR 稳定负荷应为 1.0", round(r, 3), 1.0, 1e-3)

    # 后 7 天翻倍 -> 急性 120 / 慢性 75 = 1.6
    spike = dict(flat)
    for i in range(7):
        spike[(end - timedelta(days=i)).isoformat()] = 120.0
    r2, a2, c2 = acwr(spike, end)
    ck("ACWR 末周翻倍应约 1.6", round(r2, 2), 1.6, .02)

    # 隔天跑：慢性窗口里一半是 0，比值必须跟着降下来（不是 1.0）
    alt = {(end - timedelta(days=i)).isoformat(): 60.0 for i in range(0, 28, 2)}
    r3, _, _ = acwr(alt, end)
    if r3 is None or not (1.0 < r3 < 1.3):
        errs.append("ACWR 隔天跑应略高于 1（实得 %r）—— 说明休息日没被算进去" % r3)

    # 单调性：全勤时标准差为 0 -> None；隔天跑应有确定值
    ck("单调性 全勤返回 None", monotony_strain(flat, end), None)
    ms = monotony_strain(alt, end)
    if ms is None or ms["mono"] <= 0:
        errs.append("单调性 隔天跑应有正值")

    # 解耦：后半程心率漂高 -> 负值
    run = {"paceSeries": [[i, 400] for i in range(20)],
           "hrSeries": [[i, 150 + (10 if i >= 10 else 0)] for i in range(20)]}
    ck("解耦 后半心率+10 应约 -6.7%", decoupling(run), round((150 / 160 - 1) * 100, 1), .2)
    ck("解耦 序列不足返回 None", decoupling({"paceSeries": [[0, 400]], "hrSeries": [[0, 150]]}), None)

    # 前、中、后程按总时长三等分；三段均应显示分钟配速、心率和步频中位数。
    segment_run = {
        "duration": 30 * 60,
        "paceSeries": [[i, 420 if i < 10 else 410 if i < 20 else 430] for i in range(30)],
        "hrSeries": [[i, 140 if i < 10 else 150 if i < 20 else 160] for i in range(30)],
        "cadSeries": [[i, 170 if i < 10 else 172 if i < 20 else 168] for i in range(30)],
    }
    split = run_segment_analysis(segment_run)
    ck("分段分析有前中后三段", len(split["segments"]), 3)
    ck("分段分析配速前段", split["segments"][0]["pace"]["value"], 420)
    ck("分段分析配速中段", split["segments"][1]["pace"]["value"], 410)
    ck("分段分析配速后段", split["segments"][2]["pace"]["value"], 430)
    ck("分段分析后段心率变化", split["vsFront"]["hr"], 20)
    ck("分段分析后段步频变化", split["vsFront"]["cadence"], -2)
    ck("分段配速差值用语", split["vsFrontText"]["pace"], "慢 10 秒/km")
    ck("分段心率差值用语", split["vsFrontText"]["hr"], "高 20 bpm")
    if "后程慢了 10 秒/公里" not in split["guidance"] or "心率高了 20 bpm" not in split["guidance"]:
        errs.append("分段综合提示没有同时解释配速与心率变化")
    easy_finish = run_segment_analysis({
        "duration": 30 * 60,
        "paceSeries": [[i, 420 if i < 10 else 410 if i < 20 else 400] for i in range(30)],
        "hrSeries": [[i, 150 if i < 10 else 145 if i < 20 else 140] for i in range(30)],
        "cadSeries": [[i, 170] for i in range(30)],
    })
    if "后程较从容" not in easy_finish["guidance"]:
        errs.append("后程提速且心率下降时，综合提示方向不正确")
    sparse = run_segment_analysis({"duration": 60, "paceSeries": [], "hrSeries": [], "cadSeries": []})
    ck("分钟数据缺失时不伪造分段值", sparse["segments"][0]["hr"]["value"], None)

    # 有氧底座：热身+燃脂+有氧 才算低强度
    ck("有氧底座 含 aer", aerobic_base_share(
        {"zones": {"warm": 10, "fat": 20, "aer": 30, "ana": 30, "ext": 10}}), 60.0, .01)

    # 距离档边界
    ck("距离档 3.9", dist_band(3.9), "<4")
    ck("距离档 4.0", dist_band(4.0), "4-6")
    ck("距离档 12.0", dist_band(12.0), "12+")

    # 逐场判断要有三场先前可比跑；休息后的改善也只是表现线索。
    fitness_paces = [400, 402, 398, 380, 430]
    fitness_runs = [{"id": i + 1, "date": "2026-09-%02d" % (i * 2 + 1),
                     "distance": 5, "pace": value, "hr": 150, "load": 50,
                     "zones": {"aer": 100}}
                    for i, value in enumerate(fitness_paces)]
    fitness_load = load_model(day_loads(fitness_runs), date(2026, 9, 1), date(2026, 9, 9))
    fv = build_fitness_view(fitness_runs, fitness_load)
    ck("首场没有可比基线", fv["runs"][0]["signal"], "insufficient")
    ck("三场后识别提升", fv["runs"][3]["signal"], "improved")
    ck("休息日可追溯", fv["runs"][3]["restDays"], 1)
    ck("随后识别回落", fv["runs"][4]["signal"], "declined")
    if not fitness_load[1][2] < fitness_load[0][2]:
        errs.append("休息日应降低近期负荷估算")

    # 临界速度：构造严格符合 speed = CS + D'/T 的数据。
    # 至少要铺满 12 个点、且跨 5 个以上时长档，否则函数会（正确地）拒绝出值。
    cs_true, dp_true = 11.0, 0.5      # km/h, km
    fake = []
    for t_min in (6, 8, 10, 13, 16, 20, 25, 32, 40, 52, 65, 80, 100, 120):
        sp = cs_true + dp_true / t_min
        fake.append({"duration": t_min * 60, "distance": sp * (t_min / 60.0)})
    cs = critical_speed(fake)
    if cs is None:
        errs.append("临界速度 拟合失败")
    else:
        ck("临界速度 CS", round(cs["speed"], 2), cs_true, 0.05)

    # 全是轻松跑（速度与时长的关系被打乱）时应拒绝出值，而不是硬拟合一个假数
    flat_runs = [{"duration": 30 * 60, "distance": 4.5} for _ in range(14)]
    if critical_speed(flat_runs) is not None:
        errs.append("临界速度 对无区分度的数据应返回 None")

    # 分组门槛
    g = group_runs([{"distance": 5, "zones": {"aer": 100}, "pace": 400, "hr": 150}] * 3)
    if not g or not g[0]["ok"]:
        errs.append("分组 3 场应标记为可出结论")
    g2 = group_runs([{"distance": 5, "zones": {"aer": 100}, "pace": 400, "hr": 150}] * 2)
    if not g2 or g2[0]["ok"]:
        errs.append("分组 2 场不该标记为可出结论")

    # 日视图：休息日必须占格 + 「下次处方」必须跟着 TSB 走
    d0 = date(2026, 3, 1)
    by_day_f = {d0.isoformat(): 100.0, (d0 + timedelta(days=2)).isoformat(): 120.0}
    fake_runs = [
        {"date": d0.isoformat(), "distance": 6.0, "duration": 2400, "hr": 150,
         "load": 100.0, "zones": {"aer": 900, "ana": 100}},
        {"date": (d0 + timedelta(days=2)).isoformat(), "distance": 8.0, "duration": 3200,
         "hr": 152, "load": 120.0, "zones": {"aer": 1000, "ana": 400}},
    ]
    lm_f = load_model(by_day_f, d0, d0 + timedelta(days=2))
    dv = day_view(fake_runs, by_day_f, lm_f, d0 + timedelta(days=2), window=4)
    if len(dv["days"]) != 4:
        errs.append("日视图 窗口 4 天应给 4 条")
    if not any(x["n"] == 0 for x in dv["days"]):
        errs.append("日视图 休息日也被算成训练日了（休息日必须占一格）")
    if dv["days"][-1]["date"] != (d0 + timedelta(days=2)).isoformat():
        errs.append("日视图 最后一格不是结束日，窗口会整体错位")
    if not all(isinstance(x["rx"].get("head"), str) and x["rx"]["head"] for x in dv["days"]):
        errs.append("日视图 每天都要有「下次处方」的动作")
    if dv["summary"]["runs"] != 2:
        errs.append("日视图 summary.runs 应为 2")
    # 强度结构：窗口头部会往前带几天休息日，所以取**第一个有跑步的那天**来验。
    # 那天的 zones = {aer: 900, ana: 100} → 阈上 = 100/1000 = 10.0%
    run_days = [x for x in dv["days"] if x["n"]]
    if len(run_days) != 2:
        errs.append("日视图 应恰好有 2 个训练日")
    elif abs(run_days[0]["hardPct"] - 10.0) > 0.2:
        errs.append("日视图 hardPct 口径不对：%s" % run_days[0]["hardPct"])

    # 最新一场不足三场可比时不报进退。
    sample = [
        {"id": i, "date": (end - timedelta(days=10-i)).isoformat(),
         "distance": 8.0, "pace": 410+i, "hr": 150,
         "zones": {"warm": 0, "fat": 0, "aer": 900, "ana": 100, "ext": 0}}
        for i in range(3)
    ]
    latest = {"id": 99, "date": end.isoformat(), "time": "18:00", "distance": 8.2,
              "duration": 3400, "pace": 410, "hr": 151,
              "zones": {"warm": 0, "fat": 0, "aer": 850, "ana": 150, "ext": 0}}
    thin_review = build_insight_snapshot(sample[:2] + [latest], end)
    ck("最新一场 两场可比不下结论", thin_review["last"]["comparable"]["paceSeconds"], None)
    full_review = build_insight_snapshot(sample + [latest], end)
    ck("最新一场 三场可比才出配速差", full_review["last"]["comparable"]["paceSeconds"], -1)
    ck("最新一场 对照场次可追溯", full_review["last"]["comparable"]["ids"], ["0", "1", "2"])
    ck("近28天 场次可追溯", full_review["recent28"]["ids"], ["0", "1", "2", "99"])
    # 未结束的周按相同已过去天数对比，不拿 6 天和 7 天直接比较。
    period_runs = []
    week_start = date(2026, 9, 7)
    for i in range(7):
        d = week_start + timedelta(days=i)
        period_runs.append({"id": "p" + str(i), "date": d.isoformat(),
                            "distance": 10.0, "duration": 4000, "pace": 400, "hr": 150,
                            "cadence": 175, "td": 270,
                            "zones": {"aer": 600, "ana": 400}})
    for i in range(6):
        d = week_start + timedelta(days=7 + i)
        period_runs.append({"id": "c" + str(i), "date": d.isoformat(),
                            "distance": 9.0, "duration": 3600, "pace": 400, "hr": 150,
                            "cadence": 175, "td": 270,
                            "zones": {"aer": 600, "ana": 400}})
    current_week = timeline_series(period_runs, "week")[-1]
    ck("周期 未结束的一周标记进行中", current_week["partial"], True)
    ck("周期 进行中截止日期", current_week["through"], "2026-09-19")
    ck("周期 上期按相同6天截取", current_week["compare"]["previous"]["through"], "2026-09-12")
    ck("周期 同天数对比跑量差", current_week["compare"]["previous"]["kmDelta"], -6.0)
    ck("周期 同天数对比样本数", current_week["compare"]["previous"]["n"], 6)

    if errs:
        print("自测失败 %d 项：" % len(errs))
        for e in errs:
            print("  - " + e)
        return 1
    print("自测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
