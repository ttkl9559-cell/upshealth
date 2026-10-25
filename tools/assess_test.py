#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""assess() 回归测试：各工况下的健康判定必须说得通。

关键约束（这是重写 assess 的原因）：
  换过电池、表现正常的这套配置，绝不能因为高倍率损耗被判成「建议更换」。
"""
import os
import sys
import tempfile

os.environ["TRIM_PKGVAR"] = tempfile.mkdtemp(prefix="upshealth_test_")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import server  # noqa: E402

BIT_OL = server.BIT_OL
BIT_RB = server.BIT_RB

RAW = {
    "battery.voltage.nominal": "24.0",
    "battery.capacity": "100",
    "battery.charge.low": "20",
}

fails = []


def mk(chg, rt, rp, st=BIT_OL, temp=27.9):
    return {"chg": chg, "rt": rt, "rp": rp, "st": st, "temp": temp,
            "load": rp / 1000.0 * 100, "bat_v": 27.1}


def check(name, live, want_verdict=None, want_ratio=None, want_score_min=None,
          want_score_max=None):
    h = server.assess(live, RAW)
    print("\n-- %s" % name)
    print("   verdict=%s (%s)  score=%s  ratio=%s  C=%.2f  expect=%s~%s s"
          % (h["verdict"], h["verdict_cn"], h["score"], h["ratio"],
             h["c_rate"] or 0,
             (h["expect_runtime_s"] or [0, 0])[0], (h["expect_runtime_s"] or [0, 0])[1]))
    if h.get("basis"):
        print("   basis: %s" % h["basis"])
    for n in h["notes"]:
        print("   note: %s" % n)
    if want_verdict and h["verdict"] != want_verdict:
        fails.append("%s: verdict %s != %s" % (name, h["verdict"], want_verdict))
    if want_ratio is not None:
        if h["ratio"] is None or abs(h["ratio"] - want_ratio) > 0.06:
            fails.append("%s: ratio %s 偏离期望 %.2f" % (name, h["ratio"], want_ratio))
    if want_score_min is not None and (h["score"] or 0) < want_score_min:
        fails.append("%s: score %s < %s" % (name, h["score"], want_score_min))
    if want_score_max is not None and (h["score"] or 100) > want_score_max:
        fails.append("%s: score %s > %s" % (name, h["score"], want_score_max))
    return h


print("=" * 66)
print("assess() 回归测试")
print("=" * 66)

# 1. 现场真实读数（2026-10-25 15:36，254W 工况下 UPS 报 1143s）
check("真实工况 254W / 1143s", mk(100, 1143, 254),
      want_verdict="ok", want_ratio=0.90, want_score_min=85)

# 2. 真实工况（恢复后 232W / 1279s）
check("真实工况 232W / 1279s", mk(100, 1279, 232),
      want_verdict="ok", want_score_min=85)

# 3. 轻载：倍率低，同样电池应该撑更久，且仍判正常
check("轻载 100W / 3700s", mk(100, 3700, 100), want_verdict="ok")

# 4. 电量只有 60%：按比例折算后仍是正常
check("电量 60% / 767s @232W", mk(60, 767, 232), want_verdict="ok")

# 5. 明显衰减：同样 232W 只撑 400 秒（约 6.7 分钟）
check("严重衰减 232W / 400s", mk(100, 400, 232),
      want_verdict="suspect", want_score_max=55)

# 6. 中等衰减：500 秒
check("中等衰减 232W / 620s", mk(100, 620, 232), want_score_max=80)

# 7. RB 位点亮：固件明确报需更换，评分必须扣
h_rb = check("RB 点亮 232W / 800s", mk(100, 800, 232, st=BIT_OL | BIT_RB))
h_no = server.assess(mk(100, 800, 232), RAW)
if h_rb["score"] >= h_no["score"]:
    fails.append("RB 位未生效：%s >= %s" % (h_rb["score"], h_no["score"]))

# 8. 无负载：不能瞎判
h0 = check("无负载", mk(100, 1143, 0), want_verdict="unknown")
if h0["score"] is not None:
    fails.append("无负载时不应该给分数，得到 %s" % h0["score"])

# 9. 极端倍率：负载 900W
check("极端倍率 900W / 200s", mk(100, 200, 900), want_verdict="unknown")

# 10. 从未深放时，必须明确提示「续航是估算值」
h = server.assess(mk(100, 1143, 254), RAW)
if h["calibrated"] is not False:
    fails.append("新装无放电记录，calibrated 应为 False，得到 %s" % h["calibrated"])
if not any("深度放电" in n for n in h["notes"]):
    fails.append("未做深放时缺少「续航是按设计表估算」的提示")

print("\n" + "=" * 66)
if fails:
    print("FAIL x%d" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
