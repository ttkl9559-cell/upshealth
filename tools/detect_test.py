"""
事件检测单元测试：用伪造的 upsc 读数驱动 Collector，走遍所有状态分支。

动机：形如 `BIT_OVER` 未定义这类错误，编译期查不出来，只有 UPS 状态
**恰好发生跳变**、进入对应分支时才会炸 —— 靠人肉点页面很难覆盖。
这个测试把每一个分支都强制走一遍。
"""
import importlib.util
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "app", "server.py")

TMP = tempfile.mkdtemp(prefix="upsdetect-")
os.environ["TRIM_PKGVAR"] = os.path.join(TMP, "data")
os.environ["TRIM_APPDEST"] = os.path.join(TMP, "app")
os.makedirs(os.environ["TRIM_PKGVAR"], exist_ok=True)
os.makedirs(os.environ["TRIM_APPDEST"], exist_ok=True)


def load_server():
    spec = importlib.util.spec_from_file_location("ups_server", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


S = load_server()


def base_raw(status):
    """按 VAR_META 造一份完整的伪读数，status 单独指定。"""
    raw = {}
    for key, (_g, _label, unit, kind) in S.VAR_META.items():
        if key == "ups.status":
            raw[key] = status
        elif kind == "num":
            raw[key] = "230.0"
        elif kind == "flag":
            raw[key] = "enabled"
        else:
            raw[key] = "test"
    raw.update({
        "battery.charge": "88.0",
        "battery.runtime": "600.0",
        "battery.voltage": "27.1",
        "battery.temperature": "27.9",
        "battery.capacity": "100.0",
        "ups.load": "25.0",
        "ups.realpower": "250.0",
        "input.voltage": "235.0",
        "input.frequency": "49.9",
        "output.voltage": "230.0",
        "output.current": "1.2",
        "output.frequency": "49.9",
        "input.source": "normal",
        "battery.type": "PbAc",
        "device.mfr": "DELTA",
        "device.model": "UPS102R2RT2B035",
        "device.serial": "TEST0001",
        "driver.name": "usbhid-ups",
        "driver.version": "2.8.0",
        "ups.beeper.status": "enabled",
        "ups.type": "online",
        "ups.mfr": "DELTA",
        "ups.model": "UPS102R2RT2B035",
        "ups.serial": "TEST0001",
    })
    return raw


CUR = {"status": "OL CHRG"}
S.read_vars = lambda name: base_raw(CUR["status"])
S.pick_ups = lambda: ("testups", ["testups"])


def feed(status):
    CUR["status"] = status
    S.COLLECTOR.tick()


def event_types():
    # Store.events 返回 (id, ts, ups, type, from_st, to_st, chg, rt, in_v, out_v, load, temp, note)
    return [r[3] for r in S.STORE.events(limit=200)]


def main():
    S.COLLECTOR.first_ever = True

    seq = [
        ("OL CHRG", None),           # 首次 -> INIT
        ("OL CHRG", None),           # 状态未变 -> 无事件
        ("OL", "CHRG 消失"),         # 状态变化，走完整分支（BIT_OVER 就在这条路上）
        ("OB DISCHRG", "POWER_LOSS"),
        ("OB LB DISCHRG", "LOW_BATTERY"),
        ("OL CHRG", "POWER_RESTORE"),
        ("OL OVER", "OVERLOAD"),
        ("OL RB", "REPLACE_BATTERY"),
        ("OL ALARM", "ALARM"),
        ("OL FSD", "FORCED_SHUTDOWN"),
    ]
    raised = []
    for status, expect in seq:
        try:
            feed(status)
            got = event_types()
            tail = got[-1] if got else "-"
            print("  %-16s -> 最新事件 %-18s %s" % (status, tail, "期望 " + expect if expect else ""))
        except Exception as e:
            raised.append((status, e))
            print("  %-16s -> EXCEPTION %s: %s" % (status, type(e).__name__, e))

    types = set(event_types())
    wanted = {"INIT", "POWER_LOSS", "LOW_BATTERY", "POWER_RESTORE",
              "OVERLOAD", "REPLACE_BATTERY", "ALARM", "FORCED_SHUTDOWN"}
    missing = wanted - types
    print("\n事件类型：", sorted(types))
    if missing:
        print("MISSING:", sorted(missing))
    if raised:
        for st, e in raised:
            print("RAISED on %s: %r" % (st, e))
    ok = not raised and not missing
    print("RESULT:", "PASS" if ok else "FAIL")
    shutil.rmtree(TMP, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
