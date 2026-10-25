#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""power_stats() 回归测试：造假数据验证统计口径。

重点验证两个容易搞错的地方：
  1. OB 期间 input.voltage=0 不能算进「电压过低」
  2. 人为测试（test）不能算进「停电次数」
"""
import os
import sys
import tempfile
import time

os.environ["TRIM_PKGVAR"] = tempfile.mkdtemp(prefix="upshealth_pt_")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import server  # noqa: E402

S = server.STORE
now = int(time.time())
fails = []


def add_sample(ts, in_v, in_hz, st, res=0):
    """res=0 是 10 秒档（只留最近 24 小时），res=1 是 1 分钟档（更早的历史）。"""
    S.write_sample(ts, res, {"in_v": in_v, "in_hz": in_hz, "out_v": 230.0,
                           "load": 25.0, "rp": 232.0, "chg": 100.0,
                           "rt": 1279.0, "bat_v": 27.1, "temp": 27.9}, st)


# 100 个正常点：235V / 49.9Hz，OL（放远一点，避免与后面几组时间戳重叠）
for i in range(100):
    add_sample(now - 12000 + i * 30, 235.0, 49.9, server.BIT_OL)
# 5 个低压点：190V（越下限 198）
for i in range(5):
    add_sample(now - 2000 + i * 30, 190.0, 49.8, server.BIT_OL)
# 3 个高压点：250V（越上限 242）
for i in range(3):
    add_sample(now - 1800 + i * 30, 250.0, 50.0, server.BIT_OL)
# 20 个 OB 期间的点：in_v=0（绝不能被当成电压过低！）
for i in range(20):
    add_sample(now - 1500 + i * 30, 0.0, 0.0, server.BIT_OB)

# 20 天前的历史数据：必须走 1 分钟档，且不能被漏掉
for i in range(10):
    add_sample(now - 20 * 86400 + i * 60, 231.0, 50.0, server.BIT_OL, res=1)

# 两次真断电 + 一次人为测试
S.add_cycle({"start_ts": now - 7200, "end_ts": now - 6600, "start_chg": 100,
             "end_chg": 96, "min_rt": 900, "avg_power": 232, "wh": 38.7,
             "kind": "outage", "note": "持续 10 分 0 秒"})
S.add_cycle({"start_ts": now - 900, "end_ts": now - 840, "start_chg": 100,
             "end_chg": 99, "min_rt": 1200, "avg_power": 230, "wh": 3.8,
             "kind": "outage", "note": "持续 1 分 0 秒"})
S.add_cycle({"start_ts": now - 400, "end_ts": now - 340, "start_chg": 100,
             "end_chg": 100, "min_rt": 1100, "avg_power": 228, "wh": 3.8,
             "kind": "test", "note": "持续 1 分 0 秒"})

r = S.power_stats(30)
q = r["quality"]
print("=" * 60)
print("samples      :", q["samples"])
print("v_min/max/avg:", q["v_min"], q["v_max"], q["v_avg"])
print("under/over %% :", q["under_pct"], q["over_pct"])
print("hz           :", q["hz_min"], q["hz_max"], q["hz_avg"])
print("hist         :", q["hist"])
print("outages today:", r["outages"]["today"])
print("tests        :", r["outages"]["tests"])
print("recent       :", len(r["recent_outages"]))
print("=" * 60)

if q["samples"] != 118:
    fails.append("样本数应为 118（100+5+3 + 10 条历史，OB 的 20 个必须排除），得到 %d" % q["samples"])
if q["under_pct"] <= 0:
    fails.append("190V 应被计入欠压")
if abs(q["under_pct"] - round(5 * 100.0 / 118, 2)) > 0.01:
    fails.append("欠压占比算错：%s" % q["under_pct"])
if abs(q["over_pct"] - round(3 * 100.0 / 118, 2)) > 0.01:
    fails.append("过压占比算错：%s" % q["over_pct"])
if q["v_min"] != 190.0:
    fails.append("v_min 应为 190（不能是 OB 期间的 0），得到 %s" % q["v_min"])
if len(r["recent_outages"]) != 2:
    fails.append("recent_outages 只应含 2 次真断电，得到 %d" % len(r["recent_outages"]))
if r["outages"]["today"]["count"] + r["outages"]["window"]["count"] < 2:
    fails.append("断电次数统计漏了")
if r["outages"]["tests"]["count"] != 1:
    fails.append("人为测试统计错：%s" % r["outages"]["tests"])

if fails:
    print("FAIL x%d" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
