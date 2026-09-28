# -*- coding: utf-8 -*-
"""
从小米隐私中心导出包中提取跑步数据，产出 data.js。

数据源：
  hlth_center_sport_record.csv    每次运动 1 行，Value 为 JSON，含跑姿三项等 62 字段
  hlth_center_fitness_data.csv    长表，Key=steps 给出逐分钟步数/距离/卡路里；Key=heart_rate 给出逐分钟心率
"""
import csv
import json
import os
import sys
import datetime
import collections

# 本机备用脚本（早期版本）：需要手动解压导出包、改 SRC/PREFIX 才能跑。
# 正常导入请用 import_zip.py（直接读加密 zip）。这里的路径是本机专属值，不具通用性。
SRC = r"D:\网页下载\新建文件夹"
PREFIX = "20260922_2848269717_MiFitness_"
# 相对本脚本定位：tools/ 的上一级就是项目根，搬到哪里都能用
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data.js")
LOG = os.path.join(os.environ.get("TEMP", os.path.expanduser("~/AppData/Local/Temp")), "runner_extract.log")

log = []
TZ = datetime.timezone(datetime.timedelta(hours=8))


def local(ts):
    return datetime.datetime.fromtimestamp(int(ts), TZ)


# ---------------------------------------------------------------- 1. 运动记录
sport_path = os.path.join(SRC, PREFIX + "hlth_center_sport_record.csv")
raw = []
with open(sport_path, encoding="utf-8-sig", errors="replace", newline="") as f:
    for row in csv.DictReader(f):
        if row.get("Key") != "outdoor_running":
            continue
        try:
            v = json.loads(row["Value"])
        except Exception:
            continue
        if not v.get("distance"):
            continue
        st = int(v.get("start_time") or row.get("Time") or 0)
        et = int(v.get("end_time") or 0)
        if et <= st:
            et = st + int(v.get("duration") or 0)
        raw.append({"start": st, "end": et, "v": v})

raw.sort(key=lambda r: r["start"])
log.append("outdoor_running 有效记录 = %d" % len(raw))
log.append("全部年份分布 = %s" % dict(sorted(collections.Counter(
    local(r["start"]).year for r in raw).items())))

# 2026 为主，2025 作同比基准
runsAll = [r for r in raw if local(r["start"]).year >= 2025]
log.append("2025 起记录 = %d" % len(runsAll))
log.append("  2025 = %d, 2026 = %d" % (
    sum(1 for r in runsAll if local(r["start"]).year == 2025),
    sum(1 for r in runsAll if local(r["start"]).year == 2026)))

# ------------------------------------------------- 2. 逐分钟序列
fit_path = os.path.join(SRC, PREFIX + "hlth_center_fitness_data.csv")
cut = int(datetime.datetime(2025, 1, 1, tzinfo=TZ).timestamp())

hr_pts = []
st_pts = []
with open(fit_path, encoding="utf-8-sig", errors="replace", newline="") as f:
    r = csv.reader(f)
    next(r)
    for row in r:
        if len(row) < 5:
            continue
        k = row[2]
        if k != "steps" and k != "heart_rate":
            continue
        try:
            t = int(row[3])
        except Exception:
            continue
        if t < cut:
            continue
        if k == "heart_rate":
            try:
                hr_pts.append((t, json.loads(row[4]).get("bpm")))
            except Exception:
                pass
        else:
            try:
                d = json.loads(row[4])
                st_pts.append((t, d.get("steps") or 0, d.get("distance") or 0))
            except Exception:
                pass

hr_pts.sort()
st_pts.sort()
log.append("2026 起 心率点 = %d, 步数点 = %d" % (len(hr_pts), len(st_pts)))


def window(points, a, b):
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


# ---------------------------------------------------------------- 3. 组装跑步
def _dedupe_minute(points, keep):
    """同一分钟两条记录时只留最完整的一条。

    口径唯一在 metrics.minute_window_points。本脚本是早期备用入口（正常导入走
    import_zip.py），这里跟主链路用同一份口径，免得两条路产出不一样的 data.js。
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import metrics as _metrics
        return _metrics.minute_window_points(points, keep)
    except Exception:
        return points


num = lambda v, d=0: (v if isinstance(v, (int, float)) else d)

runs = []
for r in runsAll:
    v = r["v"]
    a, b = r["start"], r["end"]
    dur = int(num(v.get("duration"), b - a))
    dist = num(v.get("distance"))
    pace = (dur / (dist / 1000.0)) if dist else 0

    hser = window(hr_pts, a, b)
    sser = window(st_pts, a, b)

    hr_series = []
    for t, bpm in hser:
        if isinstance(bpm, (int, float)) and bpm:
            hr_series.append([round((t - a) / 60.0, 2), int(bpm)])

    pace_series = []
    cad_series = []
    for t, s, d in sser:
        m = round((t - a) / 60.0, 2)
        if d and d > 10:
            pace_series.append([m, round(dur * 0 + 60000.0 / d, 1)])
        if s:
            cad_series.append([m, int(s)])

    zones = {
        "warm": int(num(v.get("hrm_warm_up_duration"))),
        "fat": int(num(v.get("hrm_fat_burning_duration"))),
        "aer": int(num(v.get("hrm_aerobic_duration"))),
        "ana": int(num(v.get("hrm_anaerobic_duration"))),
        "ext": int(num(v.get("hrm_extreme_duration"))),
    }

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
        "zones": zones,
        "hrSeries": hr_series,
        "paceSeries": _dedupe_minute(pace_series, "min"),
        "cadSeries": _dedupe_minute(cad_series, "max"),
    })

log.append("产出跑步条数 = %d" % len(runs))
log.append("有逐分钟心率的场次 = %d" % sum(1 for x in runs if len(x["hrSeries"]) > 5))
log.append("有逐分钟配速的场次 = %d" % sum(1 for x in runs if len(x["paceSeries"]) > 5))
log.append("有逐分钟步频的场次 = %d" % sum(1 for x in runs if len(x["cadSeries"]) > 5))

months = collections.OrderedDict()
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
log.append("覆盖月份 = %s" % list(months.keys()))

payload = {
    "generated": local(int(datetime.datetime.now(TZ).timestamp())).strftime("%Y-%m-%d %H:%M"),
    "scope": "2026 年 · 仅户外跑步",
    "runs": runs,
    "months": list(months.values()),
}

with open(OUT, "w", encoding="utf-8") as f:
    f.write("window.RUNNER_DATA = ")
    json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    f.write(";\n")

log.append("输出 %s  大小 %.1f KB" % (OUT, os.path.getsize(OUT) / 1024.0))

# 抽样核对
for x in runs[-2:]:
    log.append("样例 %s  %s  %.2fkm  %ds  配速%.0fs/km  步频%d  步幅%.2fm  心率%d  触地%s  振幅%s  步幅比%s  序列(h/p/c)=%d/%d/%d"
               % (x["date"], x["time"], x["distance"], x["duration"], x["pace"], x["cadence"],
                  x["stride"], x["hr"], x["td"], x["va"], x["vsr"],
                  len(x["hrSeries"]), len(x["paceSeries"]), len(x["cadSeries"])))

with open(LOG, "w", encoding="utf-8") as f:
    f.write("\n".join(log))
print("done")
