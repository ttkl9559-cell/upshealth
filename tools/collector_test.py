#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集器行为回归：失联事件只记一次、恢复补一条、切换瞬间的无效帧被剔除。

这些分支平时看不见，真出问题时才知道有没有写对，所以用单元测试钉住。
"""
import os
import sys
import tempfile
import time

os.environ["TRIM_PKGVAR"] = tempfile.mkdtemp(prefix="upshealth_col_")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import server  # noqa: E402

fails = []


def ev_types():
    return [r[0] for r in server.STORE.conn().execute(
        "SELECT type FROM events ORDER BY id").fetchall()]


# ---------------- 1. 失联：连续失败只记一条 ----------------
col = server.Collector(server.STORE)
col.ups_name = "1428391"
col.snap = {"live": {"status": "OL"}}

for _ in range(5):
    col._set_error("NUT_DOWN", "upsd 无响应")

types = ev_types()
n_lost = types.count("DEVICE_LOST")
print("失联事件条数:", n_lost, " 失败计数:", col.dev_down_ticks)
if n_lost != 1:
    fails.append("连续 5 次失败应只记 1 条 DEVICE_LOST，实际 %d 条" % n_lost)
if col.dev_down_ticks != 4:
    fails.append("失败计数应为 4（首次不计），实际 %d" % col.dev_down_ticks)
if col.dev_down_since is None:
    fails.append("失联状态未置位")

# ---------------- 2. 恢复：补一条 DEVICE_BACK，带时长 ----------------
col.dev_down_since = int(time.time()) - 125      # 伪造 2 分 5 秒前失联
fake = {
    "ts": int(time.time()),
    "device": {"name": "1428391"},
    "live": {"status": "OL", "st": server.BIT_OL, "chg": 100.0, "rt": 1143.0,
             "in_v": 235.0, "out_v": 230.0, "load": 28.0, "temp": 27.9},
}
col._mark_device_back(fake)
types = ev_types()
n_back = types.count("DEVICE_BACK")
print("恢复事件条数:", n_back)
if n_back != 1:
    fails.append("恢复应记 1 条 DEVICE_BACK，实际 %d 条" % n_back)
if col.dev_down_since is not None:
    fails.append("恢复后失联状态未清除")

row = server.STORE.conn().execute(
    "SELECT note FROM events WHERE type='DEVICE_BACK'").fetchone()
print("恢复备注:", row[0] if row else None)
if not row or "2 分 5 秒" not in row[0]:
    fails.append("DEVICE_BACK 备注里的失联时长不对：%s" % (row[0] if row else "无"))

# ---------------- 3. 再次恢复不应重复写 ----------------
col._mark_device_back(fake)
if ev_types().count("DEVICE_BACK") != 1:
    fails.append("无失联状态时不应再写 DEVICE_BACK")

# ---------------- 4. 切换瞬间的无效帧 ----------------
RAW_NORMAL = {
    "ups.status": "OL", "input.voltage": "235.0", "input.frequency": "49.9",
    "output.voltage": "230.0", "output.current": "1.1", "output.frequency": "49.9",
    "ups.load": "28.0", "ups.realpower": "249.0", "battery.charge": "100",
    "battery.runtime": "1143", "battery.voltage": "27.2",
    "battery.temperature": "27.9", "ups.power.nominal": "1000",
    "input.source": "normal", "battery.voltage.nominal": "24.0",
}
ok_snap = col._build("1428391", RAW_NORMAL)
print("有效帧 valid:", ok_snap["live"]["valid"], " rp:", ok_snap["live"]["rp"])
if not ok_snap["live"]["valid"]:
    fails.append("正常帧被判成无效")
if ok_snap["live"]["rp"] != 249.0:
    fails.append("正常帧的实功率被清掉了")

RAW_GLITCH = dict(RAW_NORMAL)
RAW_GLITCH.update({
    "output.voltage": "0.0", "input.voltage": "0.0", "ups.load": "0.0",
    "ups.realpower": "0.0", "output.current": "0.0", "ups.status": "OB",
})
bad_snap = col._build("1428391", RAW_GLITCH)
live = bad_snap["live"]
print("切换帧 valid:", live["valid"], " rp:", live["rp"], " load:", live["load"])
if live["valid"]:
    fails.append("切换瞬间的 0 值帧没被识别出来")
if live["rp"] is not None or live["load"] is not None or live["out_v"] is not None:
    fails.append("无效帧的负载/电压字段没被清空，会污染曲线")
if live["chg"] != 100.0:
    fails.append("无效帧不应丢掉电量这种仍然可信的字段")

# ---------------- 5. 断电兜底功率 ----------------
col.ob_start = int(time.time()) - 600
col.ob_start_chg = 100.0
col.ob_powers = []
col.ob_min_rt = 900.0
col.last_good_rp = 249.0
col._close_cycle(fake, restored=True)
cyc = server.STORE.conn().execute(
    "SELECT avg_power, wh FROM cycles ORDER BY id DESC LIMIT 1").fetchone()
print("兜底后的周期功率:", cyc)
if not cyc or not cyc[0]:
    fails.append("放电周期无功率样本时没有用断电前的值兜底")
elif abs(cyc[1] - 249.0 * 600 / 3600.0) > 1.0:
    fails.append("兜底算出的能耗不对：%s" % (cyc[1],))

print("\n" + "=" * 56)
if fails:
    print("FAIL x%d" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
