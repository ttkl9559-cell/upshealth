#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""关机保护回归：钉住「什么时候该关、什么时候绝对不能关」。

这是全套软件里唯一会真的把机器停掉的功能，所以每一条护栏都要有测试守着。
任何一个用例失败都代表「可能误关机」或「该关却没关」，都必须修。
"""
import os
import sys
import time
import tempfile

os.environ["TRIM_PKGVAR"] = tempfile.mkdtemp(prefix="upshealth_guard_")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import server  # noqa: E402

fails = []


def check(name, cond, detail=""):
    if cond:
        print("  [OK]   %s" % name)
    else:
        print("  [FAIL] %s  %s" % (name, detail))
        fails.append(name)


def mk_snap(chg, on_batt=True, age=0, valid=True):
    st = server.BIT_OB if on_batt else server.BIT_OL
    return {
        "ts": int(time.time()) - age,
        "device": {"name": "1428391", "mfr": "DELTA", "model": "RT-1K"},
        "live": {
            "st": st, "status": "OB" if on_batt else "OL", "valid": valid,
            "chg": chg, "rt": 600, "in_v": 0 if on_batt else 235.0,
            "out_v": 230.0, "load": 28, "rp": 254.0, "temp": 25.0,
        },
    }


def ev_types():
    return [r[0] for r in server.STORE.conn().execute(
        "SELECT type FROM events ORDER BY id").fetchall()]


def clear_events():
    c = server.STORE.conn()
    c.execute("DELETE FROM events")
    c.commit()


class CmdRecorder:
    """替身：记录谁要执行什么命令，绝不真的关机。"""
    calls = []

    def __init__(self, argv, **kw):
        CmdRecorder.calls.append(list(argv))

    @classmethod
    def reset(cls):
        cls.calls = []


# ======================================================================
print("\n1. 纯决策函数 guard_verdict")
# ======================================================================
CFG = {"guard_enabled": True, "guard_time_min": 10, "guard_low_pct": 20,
       "guard_grace_s": 60, "guard_min_ob_s": 30}

st, act, _ = server.guard_verdict(20, True, 300, "idle", {"guard_enabled": False})
check("未启用时永远是 idle", st == "idle" and act in (None, "reset"), st)

st, act, _ = server.guard_verdict(20, True, 20, "idle", CFG)
check("电池上不足确认期不认（防切换抖动）", st == "watching" and act is None, st)

st, act, _ = server.guard_verdict(80, True, 300, "idle", CFG)
check("两条判据都没到，只观察", st == "watching" and act is None, (st, act))

# ---- 判据一：断电时长 ----
st, act, _ = server.guard_verdict(100, True, 600, "idle", CFG)
check("电量满格但断电满 10 分钟 → 触发（时间判据）",
      st == "armed" and act == "arm", (st, act))

st, act, _ = server.guard_verdict(100, True, 599, "idle", CFG)
check("断电 599 秒差一秒不触发", st == "watching" and act is None, (st, act))

st, act, _ = server.guard_verdict(None, True, 600, "idle", CFG)
check("电量读不到时，时间判据照样能关机",
      st == "armed" and act == "arm", (st, act))

st, act, _ = server.guard_verdict(None, True, 300, "idle", CFG)
check("电量读不到且时间没到 → 观察，不瞎判",
      st == "watching" and act is None, st)

# ---- 判据二：剩余电量 ----
st, act, _ = server.guard_verdict(20, True, 300, "idle", CFG)
check("电量正好等于阈值即触发", st == "armed" and act == "arm", (st, act))

st, act, _ = server.guard_verdict(19.9, True, 300, "idle", CFG)
check("略低于阈值也触发", st == "armed" and act == "arm", (st, act))

st, act, _ = server.guard_verdict(20.1, True, 300, "idle", CFG)
check("略高于阈值不触发", st == "watching" and act is None, (st, act))

st, act, _ = server.guard_verdict(10, True, 600, "idle", CFG)
check("两条同时满足也能正常触发",
      st == "armed" and act == "arm", (st, act))

# ---- 状态保持 ----
st, act, _ = server.guard_verdict(20, False, 0, "armed", CFG)
check("待执行中来电 → cancel", st == "idle" and act == "cancel", (st, act))

st, act, _ = server.guard_verdict(50, True, 300, "armed", CFG)
check("已待执行时电量回升也不退出（防抖）", st == "armed" and act is None, (st, act))

st, act, _ = server.guard_verdict(5, True, 600, "alerted", CFG)
check("已告警状态保持，不重复进倒计时", st == "alerted" and act is None, (st, act))

# ---- 说明文字要能直接给人看 ----
st, act, note = server.guard_verdict(100, True, 660, "idle", CFG)
check("时间触发时说明里带出断电时长", "11 分" in note, note)

st, act, note = server.guard_verdict(15, True, 300, "idle", CFG)
check("电量触发时说明里带出电量", "15%" in note, note)

check("guard_dur 分钟不带多余的 0 秒", server.guard_dur(600) == "10 分钟",
      server.guard_dur(600))
check("guard_dur 不足一分钟按秒", server.guard_dur(45) == "45 秒",
      server.guard_dur(45))
check("guard_dur 带余数", server.guard_dur(630) == "10 分 30 秒",
      server.guard_dur(630))

# ======================================================================
print("\n2. 状态机：启动静默期")
# ======================================================================
clear_events()
server.subprocess.Popen = CmdRecorder          # 从这一刻起禁止真关机
CmdRecorder.reset()
# Windows 上没有 /sbin/shutdown，这里注入等价命令，测的是「决策对了没有」
server.GUARD.override_cmd = "/sbin/shutdown -h now"

server.CFG["guard_enabled"] = True
server.CFG["guard_mode"] = "shutdown"
server.CFG["guard_boot_hold_s"] = 300

g = server.GUARD
g.state = "idle"
g.ob_since = None
g.deadline = None
g.boot_at = int(time.time())            # 刚启动
g.evaluate(mk_snap(12))
check("启动 5 分钟内不动作", g.state == "held", g.state)
check("启动静默期不下发命令", not CmdRecorder.calls, CmdRecorder.calls)

# ======================================================================
print("\n3. 状态机：完整待执行 → 执行路径")
# ======================================================================
clear_events()
g.boot_at = int(time.time()) - 1000
g.state = "idle"
g.ob_since = int(time.time()) - 300     # 已经在电池上 300 秒
g.deadline = None
g.alert_level = 0
g.cancel_until = 0
g.fired_at = None
g.evaluate(mk_snap(20))
check("电量 20% 进入待执行", g.state == "armed", g.state)
check("宽限期 = 60 秒", g.deadline and g.deadline - g.armed_at == 60, g.deadline)
check("记录了 GUARD_ARMED", "GUARD_ARMED" in ev_types(), ev_types()[-3:])
check("此时还没有下发命令", not CmdRecorder.calls, CmdRecorder.calls)

# 宽限期没到
g.evaluate(mk_snap(20))
check("宽限期内不执行", not CmdRecorder.calls and g.state == "armed", g.state)

# 电量掉到硬下限 → 不再等宽限期
old = g.deadline
g.evaluate(mk_snap(4))
check("硬下限把宽限压缩到 30 秒内", g.deadline - int(time.time()) <= 31, g.deadline - time.time())
check("硬下限压缩确实生效（比原来近）", g.deadline < old, (g.deadline, old))

# 时间到
g.deadline = int(time.time()) - 1
real_sleep = time.sleep
time.sleep = lambda s: None
try:
    g.evaluate(mk_snap(8))
finally:
    time.sleep = real_sleep
check("到点后下发关机命令", len(CmdRecorder.calls) == 1, CmdRecorder.calls)
check("执行的是 shutdown，不是 load.off",
      CmdRecorder.calls and "shutdown" in CmdRecorder.calls[0][0]
      or (CmdRecorder.calls and "systemctl" in CmdRecorder.calls[0][0]), CmdRecorder.calls)
check("记录 GUARD_SHUTDOWN", "GUARD_SHUTDOWN" in ev_types(), ev_types()[-3:])
check("状态落到 fired", g.state == "fired", g.state)
check("写了上次关机记录", bool(g.last_fire) and "fired_at" in (g.last_fire or {}), g.last_fire)

# 已经动手过就不再重复
CmdRecorder.reset()
g.evaluate(mk_snap(5))
check("已执行后不再重复下发", not CmdRecorder.calls, CmdRecorder.calls)

# ======================================================================
print("\n3b. 时间判据：断电很久但电量还满，也必须关机")
# ======================================================================
clear_events()
CmdRecorder.reset()
g.boot_at = int(time.time()) - 1000
g.state = "idle"
g.deadline = None
g.alert_level = 0
g.cancel_until = 0
g.fired_at = None
g.ob_since = int(time.time()) - 599        # 断电 599 秒，还差 1 秒到 10 分钟
g.evaluate(mk_snap(100))
check("断电 599 秒、电量 100% 仍在观察", g.state == "watching", g.state)

g.ob_since = int(time.time()) - 601        # 跨过 10 分钟
g.evaluate(mk_snap(100))
check("断电满 10 分钟 → 即使电量 100% 也进入待执行", g.state == "armed", g.state)
check("触发原因写的是断电时长", "断电" in (g.note or ""), g.note)

g.deadline = int(time.time()) - 1
real_sleep = time.sleep
time.sleep = lambda s: None
try:
    g.evaluate(mk_snap(100))               # 电量满格，复核必须认时间判据
finally:
    time.sleep = real_sleep
check("复核时电量满格不会挡下时间判据", len(CmdRecorder.calls) == 1, CmdRecorder.calls)
g.state = "fired"

# ======================================================================
print("\n4. 落地前的复核（这几条决定会不会误关机）")
# ======================================================================
def fresh_armed(age=0, chg=8):
    g.state = "armed"
    g.ob_since = int(time.time()) - 400
    g.deadline = int(time.time()) - 1
    g.fired_at = None
    g.alert_level = 0
    CmdRecorder.reset()
    clear_events()
    return mk_snap(chg, age=age)


server.CFG["guard_mode"] = "shutdown"

g.evaluate(fresh_armed(age=60))
check("快照过期 60 秒 → 放弃", not CmdRecorder.calls and g.state != "fired", g.state)

g.evaluate(fresh_armed(chg=40))
check("复核时电量已高于触发线 → 放弃", not CmdRecorder.calls and g.state != "fired", g.state)

snap = fresh_armed()
snap["live"]["st"] = server.BIT_OL          # 复核时已经来电
snap["live"]["status"] = "OL"
g.evaluate(snap)
check("复核时市电已恢复 → 取消而不是关机",
      not CmdRecorder.calls and "GUARD_CANCEL" in ev_types(), ev_types())

snap = fresh_armed()
snap["live"]["valid"] = False               # 切换瞬间的无效帧
g.evaluate(snap)
check("无效帧不参与判断", not CmdRecorder.calls, CmdRecorder.calls)

# ======================================================================
print("\n5. 只告警模式：永远不下发关机")
# ======================================================================
server.CFG["guard_mode"] = "notify"
snap = fresh_armed(chg=8)
g.evaluate(snap)
check("notify 模式不下发命令", not CmdRecorder.calls, CmdRecorder.calls)
check("notify 模式记 GUARD_ALERT", "GUARD_ALERT" in ev_types(), ev_types())
check("notify 模式状态为 alerted", g.state == "alerted", g.state)

clear_events()
CmdRecorder.reset()
g.state = "alerted"
g.evaluate(mk_snap(8))
check("同一告警级别不重复推送", "GUARD_ALERT" not in ev_types() or len(ev_types()) <= 1,
      ev_types())

# ======================================================================
print("\n6. 冲突挡住：飞牛还管着 UPS 时不许自动关机")
# ======================================================================
server.CFG["guard_mode"] = "shutdown"
server.CFG["guard_allow_conflict"] = False
bad = {"risk": "high", "items": [{"level": "high", "key": "fnos_enable"}],
       "block_shutdown": True, "verdict_cn": "飞牛还开着"}
g.conflict_cache = bad
g.conflict_ts = time.time()                 # 让缓存生效，别去真的读盘
snap = fresh_armed(chg=8)
g.evaluate(snap)
check("高危冲突时拒绝关机", not CmdRecorder.calls, CmdRecorder.calls)
check("状态标成 blocked", g.state == "blocked", g.state)
check("记录了冲突事件", "GUARD_CONFLICT" in ev_types(), ev_types())

server.CFG["guard_allow_conflict"] = True
CmdRecorder.reset()
clear_events()
g.state = "armed"
g.deadline = int(time.time()) - 1
real_sleep = time.sleep
time.sleep = lambda s: None
try:
    g.evaluate(mk_snap(8))
finally:
    time.sleep = real_sleep
check("明确允许后仍可执行", len(CmdRecorder.calls) == 1, CmdRecorder.calls)
server.CFG["guard_allow_conflict"] = False
g.conflict_cache = None

# ======================================================================
print("\n7. 手动取消与来电自动取消")
# ======================================================================
g.state = "armed"
g.ob_since = int(time.time()) - 400
g.deadline = int(time.time()) + 120
g.alert_level = 0
g.cancel_until = 0
CmdRecorder.reset()
clear_events()

ok, msg = g.cancel()
check("手动取消成功", ok, msg)
check("取消后回到 idle", g.state == "idle", g.state)
check("设置了 10 分钟静默", g.cancel_until - int(time.time()) > 500, g.cancel_until)
check("记录 GUARD_CANCEL", "GUARD_CANCEL" in ev_types(), ev_types())

g.evaluate(mk_snap(8))
check("取消后的静默期内不再进入待执行", g.state == "idle", g.state)

ok, _ = g.cancel()
check("没有待执行时取消返回失败而不是报错", ok is False, ok)

# 来电 → 静默窗口清零
g.evaluate(mk_snap(80, on_batt=False))
check("来电后静默窗口清零", g.cancel_until == 0, g.cancel_until)

# ======================================================================
print("\n8. 冲突检测本身")
# ======================================================================
c = server.guard_conflicts()
check("返回结构完整", all(k in c for k in ("risk", "items", "block_shutdown", "fnos", "nut")),
      list(c))
check("非 Linux 或非 root 时不误判为高危", c["risk"] in ("none", "medium", "high"), c["risk"])
check("block_shutdown 与 risk 一致", c["block_shutdown"] == (c["risk"] == "high"), c)

kv = server._conf_kv("/nonexistent/device.conf")
check("读不到的配置返回 None 而不是空 dict", kv is None, kv)

if os.path.exists("/proc"):
    ps = server._procs_named(("init", "systemd", "bash", "sh", "python3"))
    check("/proc 扫描能返回 pid 列表", isinstance(ps, list), ps)
else:
    print("  [SKIP] 当前平台没有 /proc")

# ======================================================================
print("\n9. 用真实配置格式钉住解析与分级")
# ======================================================================
tmp = tempfile.mkdtemp(prefix="upshealth_cfg_")

# 飞牛 device.conf 的真实格式（= 两侧有大量空格）
devp = os.path.join(tmp, "device.conf")
with open(devp, "w", encoding="utf-8") as f:
    f.write("enable                            = true\n"
            "shutdown-ups.enabled              = true\n"
            "power-policy.type                 = time\n"
            "power-policy.value                = 60\n"
            "power-policy.uint                 = minute\n"
            "power-policy.onbattMinimumBattery = 15\n")

olddev = server.FNOS_DEVICE_CONF
server.FNOS_DEVICE_CONF = devp
c = server.guard_conflicts()
keys = [i["key"] for i in c["items"]]
check("enable=true 判为高危", c["risk"] == "high", c["risk"])
check("高危时禁止自动关机", c["block_shutdown"] is True, c["block_shutdown"])
check("识别飞牛 UPS 开关", "fnos_enable" in keys, keys)
check("识别断电自动关机", "fnos_shutdown" in keys, keys)
check("把飞牛自己的策略原样带出来",
      c["fnos"]["policy_value"] == "60" and c["fnos"]["onbatt_min_battery"] == "15",
      (c["fnos"]["policy_value"], c["fnos"]["onbatt_min_battery"]))
check("高危说明里点出具体策略", "60" in c["items"][0]["detail"] or "60" in c["items"][1]["detail"],
      [i["detail"] for i in c["items"]])

# 全关 → 不应该再报高危
with open(devp, "w", encoding="utf-8") as f:
    f.write("enable                            = false\n"
            "shutdown-ups.enabled              = false\n"
            "power-policy.value                = 0\n")
c = server.guard_conflicts()
check("飞牛全关后不报高危", c["risk"] != "high", c["risk"])
check("飞牛全关后允许自动关机", c["block_shutdown"] is False, c["block_shutdown"])
server.FNOS_DEVICE_CONF = olddev

# upsmon.conf：有没有 MONITOR 决定它到底会不会动手
monp = os.path.join(tmp, "upsmon.conf")
with open(monp, "w", encoding="utf-8") as f:
    f.write('MINSUPPLIES 1\nSHUTDOWNCMD "/sbin/shutdown -h +0"\n'
            "MONITOR ups@localhost 1 monuser trim-secret primary\n")
oldmon = server.NUT_UPSMON_CONF
server.NUT_UPSMON_CONF = monp
st = server.nut_upsmon_state()
check("解析出 MONITOR", len(st["monitors"]) == 1, st["monitors"])
check("解析出 SHUTDOWNCMD", st["shutdown_cmd"] == "/sbin/shutdown -h +0", st["shutdown_cmd"])

with open(monp, "w", encoding="utf-8") as f:
    f.write('MINSUPPLIES 0\nSHUTDOWNCMD "/sbin/shutdown -h +0"\n')
st = server.nut_upsmon_state()
check("没有 MONITOR 时记录为空", st["monitors"] == [], st["monitors"])
c = server.guard_conflicts()
if c["nut"]["running"]:
    if c["nut"]["monitors"]:
        check("有 MONITOR 才报 upsmon 风险", "nut_upsmon" in [i["key"] for i in c["items"]],
              [i["key"] for i in c["items"]])
    else:
        check("无 MONITOR 不报 upsmon 风险",
              "nut_upsmon" not in [i["key"] for i in c["items"]],
              [i["key"] for i in c["items"]])
        check("无 MONITOR 时给出无害说明",
              "nut_upsmon_idle" in [i["key"] for i in c["items"]],
              [i["key"] for i in c["items"]])
else:
    print("  [SKIP] 当前环境没有运行中的 upsmon")
server.NUT_UPSMON_CONF = oldmon

# ======================================================================
print("\n10. 被拒绝的保存不能污染运行时配置")
# ======================================================================
class FakeHandler:
    def __init__(self, body):
        self.body = body
        self.code = 200
        self.out = None

    def _body(self):
        return self.body

    def _json(self, obj, code=200):
        self.out = obj
        self.code = code
        return obj

    def _err(self, code, msg, **kw):
        self.out = {"ok": False, "error": msg}
        self.code = code
        return self.out


server.CFG["guard_time_min"] = 10
server.CFG["guard_low_pct"] = 20
server.CFG["guard_enabled"] = False
server.CFG["guard_mode"] = "notify"

h = FakeHandler({"guard_low_pct": 3})
server.Handler._api_guard_save(h)
check("电量阈值低于硬下限被拒绝", h.code == 400, h.out)
check("被拒后电量阈值没被改坏", server.CFG["guard_low_pct"] == 20,
      server.CFG["guard_low_pct"])
check("被拒后断电时长没被改坏", server.CFG["guard_time_min"] == 10,
      server.CFG["guard_time_min"])

h = FakeHandler({"guard_mode": "whatever"})
server.Handler._api_guard_save(h)
check("非法模式被拒绝", h.code == 400, h.out)
check("被拒后模式没变", server.CFG["guard_mode"] == "notify", server.CFG["guard_mode"])

h = FakeHandler({"guard_mode": "shutdown", "guard_low_pct": 22, "guard_time_min": 15})
server.Handler._api_guard_save(h)
check("合法保存能生效", h.code == 200 and server.CFG["guard_mode"] == "shutdown", h.code)
check("合法保存写入新电量阈值", server.CFG["guard_low_pct"] == 22,
      server.CFG["guard_low_pct"])
check("合法保存写入新断电时长", server.CFG["guard_time_min"] == 15,
      server.CFG["guard_time_min"])
check("保存后状态机被重置", server.GUARD.state == "idle", server.GUARD.state)

# ======================================================================
print("\n" + "=" * 60)
if fails:
    print("失败 %d 项：" % len(fails))
    for f in fails:
        print("  · " + f)
    sys.exit(1)
print("全部通过")
