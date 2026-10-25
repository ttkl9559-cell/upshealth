#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
upshealth · fnOS UPS 电池健康与电能质量监控
--------------------------------------------------
纯读取 NUT，不启动驱动、不碰 USB、不修改任何系统文件。
Python 3 标准库单文件实现，无第三方依赖。

设计约束（见规划书）：
  1. UPS 名不硬编码，每次 upsc -l 动态发现
  2. 变量缺失是常态，0 与「读不到」严格区分
  3. 采样不得快于驱动 pollinterval
  4. 时间戳强制单调
  5. NUT 不可达时明确降级，绝不用旧缓存冒充
"""

import os
import sys
import io
import re
import json
import time
import gzip
import shutil
import sqlite3
import hashlib
import tarfile
import tempfile
import platform
import threading
import subprocess
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

APP_NAME = "upshealth"
APP_CN = "UPS 健康"
FALLBACK_VERSION = "1.0.1"

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.environ.get("TRIM_APPDEST") or os.path.dirname(HERE)
DATA_DIR = os.environ.get("TRIM_PKGVAR") or os.path.join(APP_DIR, "data")
PORT = int(os.environ.get("TRIM_SERVICE_PORT") or 8778)
WWW_DIR = os.path.join(HERE, "www")
DB_PATH = os.path.join(DATA_DIR, "upshealth.db")
CFG_PATH = os.path.join(DATA_DIR, "config.json")
LOG_PATH = os.path.join(DATA_DIR, "upshealth.log")

GITHUB_REPO = "ttkl9559-cell/upshealth"

# ======================================================================
# 变量元数据：62 项完整中文映射
# 分组顺序即前端展示顺序；kind: num 数值 / str 文本 / flag 开关 / status 状态
# ======================================================================
VAR_META = {
    # ---- 电池 9 ----
    "battery.charge":            ("电池", "电量", "%", "num"),
    "battery.runtime":           ("电池", "剩余续航", "s", "num"),
    "battery.voltage":           ("电池", "电池电压", "V", "num"),
    "battery.voltage.nominal":   ("电池", "标称电压", "V", "num"),
    "battery.temperature":       ("电池", "电池温度", "℃", "num"),
    "battery.capacity":          ("电池", "设计容量系数", "%", "num"),
    "battery.charge.low":        ("电池", "低电量阈值", "%", "num"),
    "battery.charge.warning":    ("电池", "电量警告阈值", "%", "num"),
    "battery.type":              ("电池", "电池类型", "", "str"),
    # ---- 输入 11 ----
    "input.voltage":             ("输入", "输入电压", "V", "num"),
    "input.voltage.nominal":     ("输入", "标称输入电压", "V", "num"),
    "input.frequency":           ("输入", "输入频率", "Hz", "num"),
    "input.source":              ("输入", "供电来源", "", "str"),
    "input.sensitivity":         ("输入", "灵敏度", "", "str"),
    "input.transfer.high":       ("输入", "高压切换点", "V", "num"),
    "input.transfer.high.max":   ("输入", "高压切换点上限", "V", "num"),
    "input.transfer.high.min":   ("输入", "高压切换点下限", "V", "num"),
    "input.transfer.low":        ("输入", "低压切换点", "V", "num"),
    "input.transfer.low.max":    ("输入", "低压切换点上限", "V", "num"),
    "input.transfer.low.min":    ("输入", "低压切换点下限", "V", "num"),
    # ---- 输出 5 ----
    "output.voltage":            ("输出", "输出电压", "V", "num"),
    "output.voltage.nominal":    ("输出", "标称输出电压", "V", "num"),
    "output.current":            ("输出", "输出电流", "A", "num"),
    "output.frequency":          ("输出", "输出频率", "Hz", "num"),
    "output.frequency.nominal":  ("输出", "标称输出频率", "Hz", "num"),
    # ---- 负载 7 ----
    "ups.status":                ("状态", "运行状态", "", "status"),
    "ups.load":                  ("负载", "负载率", "%", "num"),
    "ups.realpower":             ("负载", "实际功率", "W", "num"),
    "ups.power.nominal":         ("负载", "额定容量", "VA", "num"),
    "ups.beeper.status":         ("状态", "蜂鸣器", "", "flag"),
    "ups.start.auto":            ("状态", "来电自启", "", "flag"),
    "ups.type":                  ("状态", "UPS 类型", "", "str"),
    # ---- 定时器 3 ----
    "ups.delay.start":           ("定时器", "启动延时", "s", "num"),
    "ups.timer.shutdown":        ("定时器", "关机倒计时", "s", "num"),
    "ups.timer.reboot":          ("定时器", "重启倒计时", "s", "num"),
    "ups.timer.start":           ("定时器", "启动倒计时", "s", "num"),
    # ---- 设备 9 ----
    "device.mfr":                ("设备", "制造商", "", "str"),
    "device.model":              ("设备", "型号", "", "str"),
    "device.serial":             ("设备", "序列号", "", "str"),
    "device.type":               ("设备", "设备类型", "", "str"),
    "ups.mfr":                   ("设备", "UPS 制造商", "", "str"),
    "ups.model":                 ("设备", "UPS 型号", "", "str"),
    "ups.serial":                ("设备", "UPS 序列号", "", "str"),
    "ups.vendorid":              ("设备", "USB 厂商 ID", "", "str"),
    "ups.productid":             ("设备", "USB 产品 ID", "", "str"),
    # ---- 驱动 15 ----
    "driver.name":               ("驱动", "驱动名称", "", "str"),
    "driver.version":            ("驱动", "NUT 版本", "", "str"),
    "driver.version.data":       ("驱动", "子驱动版本", "", "str"),
    "driver.version.internal":   ("驱动", "驱动内部版本", "", "str"),
    "driver.version.usb":        ("驱动", "libusb 版本", "", "str"),
    "driver.parameter.port":     ("驱动", "端口", "", "str"),
    "driver.parameter.bus":      ("驱动", "USB 总线", "", "str"),
    "driver.parameter.product":  ("驱动", "产品名", "", "str"),
    "driver.parameter.vendor":   ("驱动", "厂商名", "", "str"),
    "driver.parameter.vendorid": ("驱动", "驱动厂商 ID", "", "str"),
    "driver.parameter.productid":("驱动", "驱动产品 ID", "", "str"),
    "driver.parameter.serial":   ("驱动", "驱动序列号", "", "str"),
    "driver.parameter.pollinterval": ("驱动", "采样间隔", "s", "num"),
    "driver.parameter.pollfreq": ("驱动", "内部轮询频率", "s", "num"),
    "driver.parameter.synchronous": ("驱动", "同步模式", "", "str"),
    # ---- 台达私有 2 ----
    "x.additional.lowbatt":      ("厂商扩展", "固件低电量保护点", "%", "num"),
    "x.additional.devicetype":   ("厂商扩展", "连接方式", "", "str"),
}
VAR_GROUP_ORDER = ["电池", "输入", "输出", "负载", "状态", "定时器", "设备", "驱动", "厂商扩展"]

# 容易误读的变量加说明（前端作为小字提示），避免把 battery.capacity=100 当成「电池健康 100%」
VAR_NOTE = {
    "battery.capacity": "NUT 规范里的「设计容量百分比」，不是实际容量、也不是健康度。真正容量见「电池」页的实测容量。",
    "battery.runtime": "UPS 按当前负载与电量自行估算。电量计失准时不可靠——实测过电量上涨而续航反而下降。",
    "battery.charge.low": "NUT 的低电量报警线，只用于触发关机策略，本身不会切断输出。",
    "input.transfer.high": "输入电压越过该值会切换到电池供电。",
    "input.transfer.low": "输入电压低于该值会切换到电池供电。",
    "x.additional.lowbatt": "台达固件内部真正执行切断输出的保护点，比 NUT 的 charge.low 更靠后。",
    "ups.status": "OL 在线 / OB 电池 / LB 低电量 / RB 需换电池 / CHRG 充电中 / DISCHRG 放电中 / FSD 强制关机。",
    "ups.beeper.status": "enabled 蜂鸣器会响，disabled 为静音。",
    "ups.timer.shutdown": "-1 表示当前没有待执行的关机倒计时。",
    "ups.realpower": "实际有功功率（W），与 ups.load（视在功率百分比）口径不同。",
    "ups.power.nominal": "额定视在功率（VA），不是瓦数。",
}

# 入库的 12 个时序指标 → samples 宽表列名
SERIES = {
    "in_v":  "input.voltage",
    "in_hz": "input.frequency",
    "out_v": "output.voltage",
    "out_a": "output.current",
    "out_hz":"output.frequency",
    "load":  "ups.load",
    "rp":    "ups.realpower",
    "chg":   "battery.charge",
    "rt":    "battery.runtime",
    "bat_v": "battery.voltage",
    "temp":  "battery.temperature",
}
SERIES_COLS = ["in_v", "in_hz", "out_v", "out_a", "out_hz",
               "load", "rp", "chg", "rt", "bat_v", "temp"]

# ups.status 位定义
STATUS_BITS = [
    ("OL",      "在线",      1 << 0),
    ("OB",      "电池供电",  1 << 1),
    ("LB",      "低电量",    1 << 2),
    ("RB",      "需换电池",  1 << 3),
    ("CHRG",    "充电中",    1 << 4),
    ("DISCHRG", "放电中",    1 << 5),
    ("ALARM",   "告警",      1 << 6),
    ("BYPASS",  "旁路",      1 << 7),
    ("CAL",     "校准中",    1 << 8),
    ("OFF",     "已关闭",    1 << 9),
    ("OVER",    "超载",      1 << 10),
    ("TRIM",    "降压",      1 << 11),
    ("BOOST",   "升压",      1 << 12),
    ("FSD",     "强制关机",  1 << 13),
]
BIT_OL, BIT_OB, BIT_LB, BIT_RB, BIT_CHRG, BIT_DISCHRG, BIT_ALARM, BIT_FSD = (
    1, 2, 4, 8, 16, 32, 64, 8192)

RES_LABEL = {0: "10 秒", 1: "1 分钟", 2: "10 分钟"}


# ======================================================================
# 日志
# ======================================================================
_log_lock = threading.Lock()


def log(msg, level="info"):
    line = "%s [%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), level, msg)
    with _log_lock:
        try:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except Exception:
            pass
        try:
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 10 * 1024 * 1024:
                for i in (2, 1):
                    src = LOG_PATH + (".%d" % i if i > 1 else "")
                    dst = LOG_PATH + ".%d" % (i + 1)
                    if os.path.exists(src):
                        try:
                            os.replace(src, dst)
                        except Exception:
                            pass
                os.replace(LOG_PATH, LOG_PATH + ".1")
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


# ======================================================================
# 配置
# ======================================================================
DEFAULT_CFG = {
    "poll_interval": 5,          # 秒，最小 3（驱动 pollinterval=2）
    "ups_name": "",              # 空 = 自动发现
    "auto_check_update": True,
    "update_mirror": "",         # 例如 https://ghfast.top/  加速 GitHub
    "battery_ah": 9.0,           # 电池标称容量 Ah（用于容量测算）
    "battery_dod": 0.85,         # 允许放电深度
    "retain_raw_h": 24,
    "retain_1m_d": 30,
    "retain_10m_d": 1095,
    "range_default": "1h",
}


def load_cfg():
    cfg = dict(DEFAULT_CFG)
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            for k in DEFAULT_CFG:
                if k in saved:
                    cfg[k] = saved[k]
    except Exception:
        pass
    return cfg


def save_cfg(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CFG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CFG_PATH)


CFG = load_cfg()


# ======================================================================
# 版本
# ======================================================================
def read_version():
    """优先读数据目录的 version.txt（内置更新后写入），否则读 manifest。"""
    vf = os.path.join(DATA_DIR, "version.txt")
    try:
        with open(vf, "r", encoding="utf-8") as f:
            v = f.read().strip()
            if v:
                return v
    except Exception:
        pass
    for p in (os.path.join(APP_DIR, "manifest"), os.path.join(HERE, "manifest")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("version"):
                        v = line.split("=", 1)[1].strip()
                        if v:
                            return v
        except Exception:
            continue
    return FALLBACK_VERSION


VERSION = read_version()


# ======================================================================
# NUT 读取
# ======================================================================
def _which(name):
    for p in ("/usr/bin/" + name, "/bin/" + name, "/usr/sbin/" + name,
              "/sbin/" + name, "/usr/local/bin/" + name):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return shutil.which(name)


UPSC = _which("upsc")


def _run(args, timeout=6):
    """返回 (rc, stdout, stderr)"""
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout,
                           env=dict(os.environ, LC_ALL="C"))
        return (p.returncode,
                p.stdout.decode("utf-8", "replace"),
                p.stderr.decode("utf-8", "replace"))
    except FileNotFoundError:
        return (-2, "", "command not found: %s" % args[0])
    except subprocess.TimeoutExpired:
        return (-3, "", "timeout")
    except Exception as e:
        return (-1, "", str(e))


class NutError(Exception):
    def __init__(self, code, msg, hint=""):
        Exception.__init__(self, msg)
        self.code = code
        self.message = msg
        self.hint = hint


def list_ups():
    """动态发现 UPS 实例名，绝不硬编码。"""
    if not UPSC:
        raise NutError("NUT_MISSING", "未找到 upsc 命令",
                       "系统未安装 NUT，或不在预期路径。飞牛可在 设置 → UPS 电源管理 中启用后重试。")
    rc, out, err = _run([UPSC, "-l"], timeout=8)
    if rc != 0:
        low = (err + out).lower()
        if "connection refused" in low or "cannot connect" in low or "failed to connect" in low:
            raise NutError("NUT_UNREACHABLE", "连不上 NUT 服务（127.0.0.1:3493）",
                           "nut-server 可能未运行。若已卸载 UPS 电源管理应用，NUT 会随之停止。")
        raise NutError("NUT_ERROR", "upsc -l 执行失败：%s" % (err.strip() or out.strip())[:200])
    names = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        # 形如 127.0.0.1:1428391 或 1428391
        names.append(line.rsplit(":", 1)[-1] if ":" in line else line)
    if not names:
        raise NutError("NO_DEVICE", "NUT 在运行但没有发现任何 UPS",
                       "UPS 可能未接 USB 线，或驱动未启动。")
    return names


def pick_ups():
    """按配置优先，否则用第一个。"""
    names = list_ups()
    want = (CFG.get("ups_name") or "").strip()
    if want and want in names:
        return want, names
    return names[0], names


def read_vars(name):
    """读全量变量。返回 dict。失败抛 NutError。"""
    rc, out, err = _run([UPSC, name], timeout=8)
    if rc != 0:
        low = (err + out).lower()
        if "connection refused" in low:
            raise NutError("NUT_UNREACHABLE", "连不上 NUT 服务")
        if "data stale" in low or "driver not connected" in low:
            raise NutError("DEVICE_STALE", "驱动与 UPS 通信中断（data stale）",
                           "USB 线可能松动，或驱动进程已退出。")
        if "unknown ups" in low:
            raise NutError("NO_DEVICE", "UPS 实例 %s 不存在" % name)
        raise NutError("DEVICE_NO_RESPONSE", "读取 UPS 失败：%s" % (err.strip() or out.strip())[:200],
                       "设备存在但无响应，检查 USB 连接。")
    data = {}
    for line in out.splitlines():
        line = line.rstrip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip()
        if not k or " " in k:
            continue
        data[k] = v.strip()
    if not data:
        raise NutError("DEVICE_NO_RESPONSE", "UPS 返回空数据")
    return data


def fnum(v):
    """严格数值转换：读不到返回 None，不返回 0。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    m = re.match(r"^[-+]?\d+(?:\.\d+)?$", s)
    if not m:
        return None
    try:
        return float(s)
    except Exception:
        return None


def decode_status(s):
    mask = 0
    words = (s or "").split()
    for code, _cn, bit in STATUS_BITS:
        if code in words:
            mask |= bit
    return mask


def status_labels(mask):
    return [cn for code, cn, bit in STATUS_BITS if mask & bit]


def status_codes(mask):
    return [code for code, cn, bit in STATUS_BITS if mask & bit]


# ======================================================================
# 存储
# ======================================================================
class Store(object):
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.local = threading.local()
        self._init()

    def conn(self):
        c = getattr(self.local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA temp_store=MEMORY")
            self.local.c = c
        return c

    def _init(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with self.lock:
            c = self.conn()
            c.executescript("""
CREATE TABLE IF NOT EXISTS samples(
  ts INTEGER NOT NULL,
  res INTEGER NOT NULL,
  in_v REAL, in_hz REAL, out_v REAL, out_a REAL, out_hz REAL,
  load REAL, rp REAL, chg REAL, rt REAL, bat_v REAL, temp REAL,
  st INTEGER DEFAULT 0,
  n INTEGER DEFAULT 1,
  in_v_min REAL, in_v_max REAL,
  PRIMARY KEY(ts, res)
);
CREATE INDEX IF NOT EXISTS ix_samples_res_ts ON samples(res, ts);

CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  ups TEXT,
  type TEXT NOT NULL,
  from_st TEXT,
  to_st TEXT,
  chg REAL, rt REAL,
  in_v REAL, out_v REAL, load REAL, temp REAL,
  note TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ix_events_type ON events(type, ts);

CREATE TABLE IF NOT EXISTS cycles(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  start_ts INTEGER, end_ts INTEGER,
  start_chg REAL, end_chg REAL,
  min_rt REAL, avg_power REAL,
  wh REAL, kind TEXT DEFAULT 'unknown',
  note TEXT
);

CREATE TABLE IF NOT EXISTS baselines(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER, ah REAL, capacity_pct REAL, note TEXT
);
""")
            c.commit()

    # ---------- 写入 ----------
    def write_sample(self, ts, res, row, st, n=1, lo=None, hi=None):
        with self.lock:
            c = self.conn()
            c.execute(
                "INSERT OR REPLACE INTO samples(ts,res,in_v,in_hz,out_v,out_a,out_hz,"
                "load,rp,chg,rt,bat_v,temp,st,n,in_v_min,in_v_max) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, res, row.get("in_v"), row.get("in_hz"), row.get("out_v"),
                 row.get("out_a"), row.get("out_hz"), row.get("load"),
                 row.get("rp"), row.get("chg"), row.get("rt"), row.get("bat_v"),
                 row.get("temp"), st, n, lo, hi))
            c.commit()

    def add_event(self, ev):
        with self.lock:
            c = self.conn()
            c.execute(
                "INSERT INTO events(ts,ups,type,from_st,to_st,chg,rt,in_v,out_v,load,temp,note) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (ev.get("ts"), ev.get("ups"), ev.get("type"), ev.get("from_st"),
                 ev.get("to_st"), ev.get("chg"), ev.get("rt"), ev.get("in_v"),
                 ev.get("out_v"), ev.get("load"), ev.get("temp"), ev.get("note")))
            c.commit()

    def add_cycle(self, cyc):
        with self.lock:
            c = self.conn()
            c.execute(
                "INSERT INTO cycles(start_ts,end_ts,start_chg,end_chg,min_rt,avg_power,wh,kind,note) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (cyc.get("start_ts"), cyc.get("end_ts"), cyc.get("start_chg"),
                 cyc.get("end_chg"), cyc.get("min_rt"), cyc.get("avg_power"),
                 cyc.get("wh"), cyc.get("kind", "unknown"), cyc.get("note")))
            c.commit()

    # ---------- 查询 ----------
    def last_sample(self):
        c = self.conn()
        r = c.execute("SELECT ts,st FROM samples ORDER BY ts DESC LIMIT 1").fetchone()
        return (r[0], r[1]) if r else (None, None)

    def last_event_ts(self):
        c = self.conn()
        r = c.execute("SELECT MAX(ts) FROM events").fetchone()
        return r[0] if r else None

    def history(self, since, until=None):
        """
        按时间跨度选最合适的粒度：
          ≤2h  → raw(10s)，细节最全（raw 只留 24h，2h 内必定完整）
          ≤30d → 1m，1m 保留 30 天，24h 图不会缺前段
          >30d → 10m
        若目标层无数据则逐级回退到更粗的层。
        """
        until = until or int(time.time())
        span = until - since
        if since <= 0:
            order = [2, 1]
        elif span <= 2 * 3600:
            order = [0, 1, 2]
        elif span <= 30 * 86400:
            order = [1, 2]
        else:
            order = [2]
        c = self.conn()
        for res in order:
            rows = c.execute(
                "SELECT ts,in_v,in_hz,out_v,out_a,out_hz,load,rp,chg,rt,bat_v,temp,st,"
                "in_v_min,in_v_max FROM samples WHERE res=? AND ts>=? AND ts<=? ORDER BY ts",
                (res, since, until)).fetchall()
            if rows:
                return rows, res
        return [], order[0]

    def events(self, limit=200, etype=None, since=None):
        c = self.conn()
        q = "SELECT id,ts,ups,type,from_st,to_st,chg,rt,in_v,out_v,load,temp,note FROM events"
        where, args = [], []
        if etype:
            where.append("type=?")
            args.append(etype)
        if since:
            where.append("ts>=?")
            args.append(since)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(int(limit))
        return c.execute(q, args).fetchall()

    def cycles(self, limit=50):
        c = self.conn()
        return c.execute(
            "SELECT id,start_ts,end_ts,start_chg,end_chg,min_rt,avg_power,wh,kind,note "
            "FROM cycles ORDER BY start_ts DESC LIMIT ?", (int(limit),)).fetchall()

    def event_counts(self):
        c = self.conn()
        return dict(c.execute("SELECT type,COUNT(*) FROM events GROUP BY type").fetchall())

    def db_size(self):
        try:
            t = os.path.getsize(self.path)
            for suf in ("-wal", "-shm"):
                if os.path.exists(self.path + suf):
                    t += os.path.getsize(self.path + suf)
            return t
        except Exception:
            return 0

    def counts(self):
        """行数统计较重，60 秒缓存，避免每次轮询都扫表。"""
        cache = getattr(self, "_cnt_cache", None)
        now = time.time()
        if cache and now - cache[0] < 60:
            return cache[1]
        c = self.conn()
        out = {}
        for r in (0, 1, 2):
            out["res%d" % r] = c.execute(
                "SELECT COUNT(*) FROM samples WHERE res=?", (r,)).fetchone()[0]
        out["events"] = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        out["cycles"] = c.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]
        self._cnt_cache = (now, out)
        return out

    # ---------- 维护 ----------
    def _coalesce(self, res_from, res_to, bucket, cutoff):
        """把 res_from 层按 bucket 秒聚合成 res_to 层，并删除已聚合的旧数据。"""
        c = self.conn()
        rows = c.execute(
            "SELECT ts,in_v,in_hz,out_v,out_a,out_hz,load,rp,chg,rt,bat_v,temp,st "
            "FROM samples WHERE res=? AND ts<? ORDER BY ts", (res_from, cutoff)).fetchall()
        if not rows:
            return 0
        buckets = {}
        for r in rows:
            b = (r[0] // bucket) * bucket
            d = buckets.get(b)
            if d is None:
                d = buckets[b] = {"n": 0, "st": 0, "sum": [0.0] * 11,
                                  "cnt": [0] * 11, "lo": None, "hi": None}
            d["n"] += 1
            d["st"] |= (r[12] or 0)
            for i in range(11):
                v = r[i + 1]
                if v is not None:
                    d["sum"][i] += v
                    d["cnt"][i] += 1
            iv = r[1]
            if iv is not None:
                d["lo"] = iv if d["lo"] is None else min(d["lo"], iv)
                d["hi"] = iv if d["hi"] is None else max(d["hi"], iv)
        # 单事务批量写入，避免逐行 commit（8640 行逐行提交会卡死磁盘）
        payload = []
        for b in sorted(buckets):
            d = buckets[b]
            vals = []
            for i in range(11):
                vals.append(round(d["sum"][i] / d["cnt"][i], 3) if d["cnt"][i] else None)
            payload.append((b, res_to) + tuple(vals) + (d["st"], d["n"], d["lo"], d["hi"]))
        with self.lock:
            c.executemany(
                "INSERT OR REPLACE INTO samples(ts,res,in_v,in_hz,out_v,out_a,out_hz,"
                "load,rp,chg,rt,bat_v,temp,st,n,in_v_min,in_v_max) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", payload)
            c.execute("DELETE FROM samples WHERE res=? AND ts<?", (res_from, cutoff))
            c.commit()
        return len(payload)

    def downsample(self, raw_h=24, m1_d=30, m10_d=1095):
        """滚动聚合：raw → 1m → 10m，然后删除过期数据。每小时跑一次。"""
        now = int(time.time())
        n1 = self._coalesce(0, 1, 60, now - raw_h * 3600)
        if n1:
            log("降采样 raw→1m：%d 个分钟桶" % n1)
        n2 = self._coalesce(1, 2, 600, now - m1_d * 86400)
        if n2:
            log("降采样 1m→10m：%d 个十分钟桶" % n2)
        with self.lock:
            c = self.conn()
            c.execute("DELETE FROM samples WHERE res=2 AND ts<?", (now - m10_d * 86400,))
            c.commit()

    def vacuum(self):
        with self.lock:
            c = self.conn()
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            c.execute("VACUUM")
            c.commit()
        log("VACUUM 完成")


STORE = Store(DB_PATH)


# ======================================================================
# 健康评估
# ======================================================================
def assess(live, raw):
    """电池健康：能量守恒反推容量。返回 dict。"""
    chg = live.get("chg")
    rt = live.get("rt")
    rp = live.get("rp")
    vn = fnum(raw.get("battery.voltage.nominal"))
    ah = float(CFG.get("battery_ah") or 9.0)
    dod = float(CFG.get("battery_dod") or 0.85)

    out = {
        "score": None, "grade": "unknown", "grade_cn": "数据不足",
        "capacity_pct": None, "nominal_wh": None, "usable_wh": None,
        "full_runtime_s": None, "basis": None, "notes": [],
    }
    if not vn or vn <= 0 or ah <= 0:
        out["notes"].append("缺少标称电压或电池容量配置，无法测算")
        return out
    nominal_wh = vn * ah
    out["nominal_wh"] = round(nominal_wh, 1)

    if not (chg and chg > 5 and rt and rt > 0 and rp and rp > 0):
        out["notes"].append("需电量 >5% 且有负载时才能测算")
        return out

    full_rt = rt / (chg / 100.0)
    usable = rp * full_rt / 3600.0
    cap = usable / (nominal_wh * dod) * 100.0

    out["full_runtime_s"] = int(full_rt)
    out["usable_wh"] = round(usable, 1)
    out["capacity_pct"] = round(cap, 1)
    out["basis"] = ("按当前 %0.0f%% 电量下续航 %d 秒、负载 %0.0f W 反推满电可放出 %0.1f Wh，"
                    "对比标称 %0.1f Wh × 放电深度 %0.0f%%"
                    % (chg, rt, rp, usable, nominal_wh, dod * 100))

    # 健康分：容量为主，辅以 RB 告警与温度
    score = max(0.0, min(100.0, cap))
    if live.get("st", 0) & BIT_RB:
        score -= 15
        out["notes"].append("UPS 当前报告需更换电池（RB）")
    temp = live.get("temp")
    if temp is not None and temp > 30:
        score -= min(10.0, (temp - 30) * 2)
        out["notes"].append("电池温度 %.1f℃ 偏高，每高 10℃ 寿命约减半" % temp)
    out["score"] = int(round(max(0.0, min(100.0, score))))

    s = out["score"]
    if s >= 80:
        out["grade"], out["grade_cn"] = "good", "良好"
    elif s >= 60:
        out["grade"], out["grade_cn"] = "fair", "关注"
    elif s >= 40:
        out["grade"], out["grade_cn"] = "poor", "建议更换"
    else:
        out["grade"], out["grade_cn"] = "critical", "尽快更换"
    return out


# ======================================================================
# 采集器
# ======================================================================
class Collector(object):
    def __init__(self, store):
        self.store = store
        self.snap = None            # 最近一次快照（内存，HTTP 只读这个）
        self.err = None             # 最近一次错误
        self.last_ts = 0
        self.mono_ts = 0
        self.last_mask = None
        self.last_status_str = ""
        self.ups_name = None
        self.ups_list = []
        self.names_seen = []
        self.ob_start = None        # 放电周期起点
        self.ob_min_rt = None
        self.ob_powers = []
        self.ob_start_chg = None
        self.tick_count = 0
        self.started = int(time.time())
        self.downsample_due = time.time() + 600
        self.boot_marked = False
        self.first_ever = False
        self.tick_lock = threading.Lock()

    # ---------- 主循环 ----------
    def run(self):
        # 启动时检查观测盲区
        try:
            self._mark_boot()
        except Exception as e:
            log("启动标记失败: %s" % e, "warn")
        while True:
            t0 = time.time()
            try:
                self.tick()
            except NutError as e:
                self._set_error(e.code, e.message, e.hint)
            except Exception as e:
                self._set_error("INTERNAL", "采集异常：%s" % e)
                log("采集异常: %s" % e, "error")
            # 降采样
            if time.time() >= self.downsample_due:
                self.downsample_due = time.time() + 3600
                try:
                    self.store.downsample(CFG["retain_raw_h"], CFG["retain_1m_d"],
                                          CFG["retain_10m_d"])
                except Exception as e:
                    log("降采样失败: %s" % e, "error")
            interval = max(3, int(CFG.get("poll_interval") or 5))
            time.sleep(max(0.5, interval - (time.time() - t0)))

    def _set_error(self, code, msg, hint=""):
        self.err = {"code": code, "message": msg, "hint": hint,
                    "ts": int(time.time())}
        # 保持旧快照的元信息，但标记过期 —— 前端必须能看出「这不是当前值」
        if self.snap:
            self.snap["stale"] = True
            self.snap["err"] = self.err
        log("[%s] %s" % (code, msg), "warn")

    def _mark_boot(self):
        last_ts, _st = self.store.last_sample()
        now = int(time.time())
        # 是否首次安装（此前一条事件都没有）
        self.first_ever = self.store.last_event_ts() is None
        if last_ts and now - last_ts > 180:
            gap = now - last_ts
            self.store.add_event({
                "ts": last_ts, "ups": "", "type": "BLIND_SPOT",
                "from_st": "", "to_st": "",
                "note": "观测中断 %d 分 %d 秒（关机 / 断电 / 服务停止期间，UPS 行为不可知）"
                        % (gap // 60, gap % 60),
            })
            log("发现观测盲区 %d 秒，已记录" % gap)
        self.store.add_event({
            "ts": now, "ups": "", "type": "SESSION_START", "note": "服务启动 v%s" % VERSION,
        })
        self.boot_marked = True

    # ---------- 单次采集 ----------
    def tick(self):
        """带重入保护：手动触发与定时采集撞车时，后来的直接跳过。"""
        if not self.tick_lock.acquire(blocking=False):
            return
        try:
            self._tick()
        finally:
            self.tick_lock.release()

    def _tick(self):
        name, names = pick_ups()
        self.ups_list = names
        if names != self.names_seen:
            if self.names_seen:
                self.store.add_event({
                    "ts": int(time.time()), "ups": name, "type": "DEVICE_CHANGED",
                    "note": "UPS 实例列表变化：%s → %s" % (self.names_seen, names),
                })
            self.names_seen = list(names)
        self.ups_name = name

        raw = read_vars(name)
        self.tick_count += 1

        # 变量集变化检测（驱动升级后可能多出/少掉字段）
        cur_keys = set(raw.keys())
        if getattr(self, "_prev_keys", None) is None:
            self._prev_keys = cur_keys
        elif cur_keys != self._prev_keys:
            added = sorted(cur_keys - self._prev_keys)
            removed = sorted(self._prev_keys - cur_keys)
            self.store.add_event({
                "ts": int(time.time()), "ups": name, "type": "VARS_CHANGED",
                "note": "变量集变化 新增 %d / 移除 %d：%s%s"
                        % (len(added), len(removed),
                           ("+" + ",".join(added[:8]) if added else ""),
                           (" -" + ",".join(removed[:8]) if removed else "")),
            })
            self._prev_keys = cur_keys

        snap = self._build(name, raw)
        self.snap = snap
        self.err = None

        # 时间戳单调修正
        ts = snap["ts"]
        if ts <= self.mono_ts:
            ts = self.mono_ts + 1
            snap["ts"] = ts
            log("时间戳回跳，已修正为 %d" % ts, "warn")
        self.mono_ts = ts
        self.last_ts = ts

        # 入库
        row = {c: snap["live"].get(c) for c in SERIES_COLS}
        self.store.write_sample(ts, 0, row, snap["live"]["st"], 1,
                                snap["live"].get("in_v"), snap["live"].get("in_v"))

        # 事件
        self._detect(snap, raw)

    def _build(self, name, raw):
        live = {}
        live["status"] = raw.get("ups.status", "")
        live["st"] = decode_status(live["status"])
        live["labels"] = status_labels(live["st"])
        live["codes"] = status_codes(live["st"])
        live["in_v"] = fnum(raw.get("input.voltage"))
        live["in_hz"] = fnum(raw.get("input.frequency"))
        live["out_v"] = fnum(raw.get("output.voltage"))
        live["out_a"] = fnum(raw.get("output.current"))
        live["out_hz"] = fnum(raw.get("output.frequency"))
        live["load"] = fnum(raw.get("ups.load"))
        live["rp"] = fnum(raw.get("ups.realpower"))
        live["chg"] = fnum(raw.get("battery.charge"))
        live["rt"] = fnum(raw.get("battery.runtime"))
        live["bat_v"] = fnum(raw.get("battery.voltage"))
        live["temp"] = fnum(raw.get("battery.temperature"))
        live["nominal_va"] = fnum(raw.get("ups.power.nominal"))
        live["source"] = raw.get("input.source") or ""

        device = {
            "name": name,
            "mfr": raw.get("device.mfr") or raw.get("ups.mfr") or "",
            "model": raw.get("device.model") or raw.get("ups.model") or "",
            "serial": raw.get("device.serial") or raw.get("ups.serial") or "",
            "type": raw.get("ups.type") or "",
            "driver": raw.get("driver.name") or "",
            "driver_data": raw.get("driver.version.data") or "",
            "nut": raw.get("driver.version") or "",
            "vid": raw.get("ups.vendorid") or "",
            "pid": raw.get("ups.productid") or "",
            "lowbatt_fw": fnum(raw.get("x.additional.lowbatt")),
            "lowbatt_nut": fnum(raw.get("battery.charge.low")),
        }
        # 变量完整度
        total = len(VAR_META)
        present = sum(1 for k in VAR_META if k in raw)
        extra = sorted(k for k in raw if k not in VAR_META)

        return {
            "ok": True,
            "ts": int(time.time()),
            "stale": False,
            "err": None,
            "device": device,
            "live": live,
            "health": assess(live, raw),
            "raw": raw,
            "cov": {"present": present, "total": total, "extra": extra},
            "meta": {
                "ver": VERSION,
                "poll": CFG.get("poll_interval"),
                "uptime": int(time.time()) - self.started,
                "ticks": self.tick_count,
                "ups_list": self.ups_list,
                "db": self.store.db_size(),
            },
        }

    # ---------- 事件检测 ----------
    def _detect(self, snap, raw):
        live = snap["live"]
        mask = live["st"]
        prev = self.last_mask
        ts = snap["ts"]
        common = {
            "ts": ts, "ups": snap["device"]["name"],
            "from_st": self.last_status_str, "to_st": live["status"],
            "chg": live["chg"], "rt": live["rt"], "in_v": live["in_v"],
            "out_v": live["out_v"], "load": live["load"], "temp": live["temp"],
        }

        if prev is None:
            # 只在首次安装时记 INIT；重启有 SESSION_START 标记起点，不再重复
            if getattr(self, "first_ever", False):
                self.store.add_event(dict(common, type="INIT",
                                          note="开始观测，当前状态 %s" % (live["status"] or "未知")))
                self.first_ever = False
        elif mask != prev:
            # 语义事件优先
            if (prev & BIT_OB) == 0 and (mask & BIT_OB):
                self.store.add_event(dict(common, type="POWER_LOSS",
                                          note="市电中断，切换电池供电"))
                self.ob_start = ts
                self.ob_min_rt = live["rt"]
                self.ob_start_chg = live["chg"]
                self.ob_powers = []
            elif (prev & BIT_OB) and (mask & BIT_OB) == 0:
                self.store.add_event(dict(common, type="POWER_RESTORE",
                                          note="市电恢复"))
                self._close_cycle(snap, restored=True)
            if (prev & BIT_LB) == 0 and (mask & BIT_LB):
                self.store.add_event(dict(common, type="LOW_BATTERY",
                                          note="进入低电量状态"))
            if (prev & BIT_RB) == 0 and (mask & BIT_RB):
                self.store.add_event(dict(common, type="REPLACE_BATTERY",
                                          note="UPS 报告需要更换电池"))
            if (prev & BIT_ALARM) == 0 and (mask & BIT_ALARM):
                self.store.add_event(dict(common, type="ALARM", note="UPS 告警"))
            if (prev & BIT_FSD) == 0 and (mask & BIT_FSD):
                self.store.add_event(dict(common, type="FORCED_SHUTDOWN",
                                          note="UPS 触发强制关机"))
            if (prev & BIT_OVER) == 0 and (mask & BIT_OVER):
                self.store.add_event(dict(common, type="OVERLOAD", note="输出超载"))

        # 放电中：跟踪最低续航与平均功率
        if mask & BIT_OB:
            if self.ob_start is None:
                self.ob_start = ts
                self.ob_start_chg = live["chg"]
                self.ob_min_rt = live["rt"]
                self.ob_powers = []
            if live["rt"] is not None:
                self.ob_min_rt = live["rt"] if self.ob_min_rt is None else min(self.ob_min_rt, live["rt"])
            if live["rp"]:
                self.ob_powers.append(live["rp"])

        # 电量异常跳变
        if (prev is not None and live["chg"] is not None
                and self._last_chg is not None):
            d = abs(live["chg"] - self._last_chg)
            if d >= 15:
                self.store.add_event(dict(common, type="CHARGE_JUMP",
                                          note="电量单周期跳变 %.0f%% → %.0f%%（电量计可能失准）"
                                               % (self._last_chg, live["chg"])))
        if live["chg"] is not None:
            self._last_chg = live["chg"]

        self.last_mask = mask
        self.last_status_str = live["status"]

    _last_chg = None

    def _close_cycle(self, snap, restored):
        if self.ob_start is None:
            return
        live = snap["live"]
        avg_p = (sum(self.ob_powers) / len(self.ob_powers)) if self.ob_powers else None
        dur = snap["ts"] - self.ob_start
        wh = (avg_p * dur / 3600.0) if avg_p else None
        # 判定类型：时间短且未出现 LB → 人为测试；否则记断电
        kind = "outage"
        if dur < 300 and not (snap["live"]["st"] & BIT_LB):
            kind = "test"
        self.store.add_cycle({
            "start_ts": self.ob_start, "end_ts": snap["ts"],
            "start_chg": self.ob_start_chg, "end_chg": live["chg"],
            "min_rt": self.ob_min_rt, "avg_power": avg_p,
            "wh": round(wh, 1) if wh else None, "kind": kind,
            "note": "持续 %d 分 %d 秒" % (dur // 60, dur % 60),
        })
        log("放电周期结束：%s，%d 秒，%s Wh" % (kind, dur, wh))
        self.ob_start = None
        self.ob_min_rt = None
        self.ob_start_chg = None
        self.ob_powers = []


COLLECTOR = Collector(STORE)


# ======================================================================
# 内置更新
# ======================================================================
def _gh_api(path):
    base = "https://api.github.com"
    mirror = (CFG.get("update_mirror") or "").strip()
    url = base + path
    if mirror:
        url = mirror.rstrip("/") + "/" + url
    req = Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "upshealth/" + VERSION,
    })
    with urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def ver_tuple(v):
    v = (v or "").strip().lstrip("vV")
    parts = re.split(r"[.\-+]", v)
    out = []
    for p in parts:
        if p.isdigit():
            out.append(int(p))
        else:
            m = re.match(r"^(\d+)", p)
            out.append(int(m.group(1)) if m else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:4])


def check_update(force=False):
    try:
        rel = _gh_api("/repos/%s/releases/latest" % GITHUB_REPO)
    except HTTPError as e:
        if e.code == 404:
            return {"ok": False, "error": "仓库或 Release 不存在（%s）" % GITHUB_REPO}
        return {"ok": False, "error": "GitHub API HTTP %s" % e.code}
    except URLError as e:
        return {"ok": False, "error": "无法访问 GitHub：%s" % getattr(e, "reason", e),
                "hint": "可在设置里填更新镜像前缀（如 https://ghfast.top/）后重试"}
    except Exception as e:
        return {"ok": False, "error": "检查更新失败：%s" % e}

    tag = rel.get("tag_name") or ""
    latest = tag.lstrip("vV")
    assets = []
    for a in rel.get("assets") or []:
        assets.append({
            "name": a.get("name"), "size": a.get("size"),
            "url": a.get("browser_download_url"),
            "sha256": (a.get("digest") or "").replace("sha256:", "") or None,
        })
    # 选当前架构的包
    mach = (platform.machine() or "").lower()
    arch = "arm" if mach in ("aarch64", "arm64", "armv8l", "armv7l") else "x86"
    fpk = None
    for a in assets:
        n = (a["name"] or "").lower()
        if n.endswith(".fpk") and arch in n:
            fpk = a
            break
    if not fpk:
        for a in assets:
            if (a["name"] or "").lower().endswith(".fpk"):
                fpk = a
                break
    # sha256 附件必须与选中的 fpk 严格同名（多架构包里不能串）
    sha = None
    if fpk and fpk.get("name"):
        want = (fpk["name"] + ".sha256").lower()
        for a in assets:
            if (a["name"] or "").lower() == want:
                sha = a
                break
    if not sha:
        for a in assets:
            if (a["name"] or "").lower().endswith(".sha256"):
                sha = a
                break

    newer = ver_tuple(latest) > ver_tuple(VERSION)
    return {
        "ok": True,
        "current": VERSION,
        "latest": latest,
        "tag": tag,
        "newer": newer,
        "published": rel.get("published_at"),
        "notes": (rel.get("body") or "")[:6000],
        "page": rel.get("html_url"),
        "arch": arch,
        "fpk": fpk,
        "sha_asset": sha,
        "assets": assets,
    }


def _download(url, dest, mirror=""):
    if mirror:
        url2 = mirror.rstrip("/") + "/" + url
        try:
            _download_raw(url2, dest)
            return url2
        except Exception as e:
            log("镜像下载失败，回退直连：%s" % e, "warn")
    _download_raw(url, dest)
    return url


def _download_raw(url, dest):
    req = Request(url, headers={"User-Agent": "upshealth/" + VERSION})
    tmp = dest + ".part"
    with urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    os.replace(tmp, dest)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def apply_update(url, expect_sha=None):
    """
    下载 fpk → 校验 → 解出 server/ui → 语法检查 → 备份 → 覆盖 → execv 重启自己。
    全程不碰系统文件，只改本应用目录。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    tmpfpk = os.path.join(DATA_DIR, "update.fpk")
    url = _download(url, tmpfpk, CFG.get("update_mirror") or "")

    if expect_sha:
        got = sha256_file(tmpfpk)
        if got.lower() != expect_sha.lower():
            return {"ok": False, "error": "SHA256 校验失败", "expect": expect_sha, "got": got}

    # 解 fpk → app.tgz → server/ + ui/
    stage = tempfile.mkdtemp(prefix="upsupd-", dir=DATA_DIR)
    try:
        with tarfile.open(tmpfpk, "r:gz") as tf:
            member = None
            for m in tf.getmembers():
                if m.name.endswith("app.tgz"):
                    member = m
                    break
            if not member:
                return {"ok": False, "error": "更新包格式异常：未找到 app.tgz"}
            app_tgz = os.path.join(stage, "app.tgz")
            with open(app_tgz, "wb") as f:
                f.write(tf.extractfile(member).read())
        with tarfile.open(app_tgz, "r:gz") as tf:
            for m in tf.getmembers():
                name = m.name.lstrip("./")
                if not (name.startswith("server/") or name.startswith("www/")
                        or name.startswith("ui/")):
                    continue
                tf.extract(m, stage)
    except Exception as e:
        return {"ok": False, "error": "解包失败：%s" % e}

    new_server = os.path.join(stage, "server", "server.py")
    if not os.path.isfile(new_server):
        alt = os.path.join(stage, "server", "app.py")
        new_server = alt if os.path.isfile(alt) else None
    if not new_server:
        return {"ok": False, "error": "更新包里没有服务端程序"}

    # 语法检查：新代码编译不过就拒绝更新（避免更新后起不来）
    try:
        src = open(new_server, "r", encoding="utf-8").read()
        compile(src, "server.py", "exec")
    except SyntaxError as e:
        return {"ok": False, "error": "新版本代码语法错误，已拒绝更新：%s" % e}

    # 备份
    backup = os.path.join(DATA_DIR, "backup")
    os.makedirs(backup, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for rel in ("server/server.py", "www/index.html"):
        src_p = os.path.join(HERE, rel)
        if os.path.isfile(src_p):
            dst = os.path.join(backup, ("%s.%s" % (rel.replace("/", "_"), stamp)))
            try:
                shutil.copy2(src_p, dst)
            except Exception:
                pass

    # 覆盖
    written = []
    try:
        shutil.copy2(new_server, os.path.join(HERE, "server.py"))
        written.append("server.py")
        new_www = os.path.join(stage, "www", "index.html")
        if os.path.isfile(new_www):
            dst_www = os.path.join(WWW_DIR, "index.html")
            shutil.copy2(new_www, dst_www)
            written.append("index.html")
    except Exception as e:
        return {"ok": False, "error": "写入失败：%s" % e}

    # 提取新版本号
    new_ver = None
    m = re.search(r'FALLBACK_VERSION\s*=\s*"([^"]+)"', src)
    if m:
        new_ver = m.group(1)
    if new_ver:
        try:
            with open(os.path.join(DATA_DIR, "version.txt"), "w", encoding="utf-8") as f:
                f.write(new_ver)
        except Exception:
            pass
        # 同步 app 目录 manifest（让应用中心也显示新版本）
        try:
            mp = os.path.join(APP_DIR, "manifest")
            if os.path.isfile(mp):
                lines = open(mp, "r", encoding="utf-8").read().splitlines()
                out = []
                for ln in lines:
                    if re.match(r"^\s*version\s*=", ln):
                        out.append("version         = %s" % new_ver)
                    else:
                        out.append(ln)
                with open(mp, "w", encoding="utf-8") as f:
                    f.write("\n".join(out) + "\n")
        except Exception:
            pass

    shutil.rmtree(stage, ignore_errors=True)
    try:
        os.remove(tmpfpk)
    except Exception:
        pass

    STORE.add_event({"ts": int(time.time()), "type": "UPDATE",
                     "note": "内置更新 %s → %s，重启服务" % (VERSION, new_ver or "?")})
    log("更新完成 %s → %s，准备重启" % (VERSION, new_ver), "info")

    # 延迟重启自己：先把响应发出去
    def _restart():
        time.sleep(1.2)
        try:
            os.execv(sys.executable, [sys.executable, os.path.join(HERE, "server.py")])
        except Exception as e:
            log("execv 重启失败，进程退出等待守护拉起：%s" % e, "error")
            os._exit(1)
    threading.Thread(target=_restart, daemon=True).start()

    return {"ok": True, "written": written, "from": VERSION, "to": new_ver,
            "restart": True}


# ======================================================================
# HTTP
# ======================================================================
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "upshealth/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # 静默，避免轮询刷日志

    # ---------- 工具 ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, obj)

    def _err(self, code, msg, **kw):
        d = {"ok": False, "error": msg}
        d.update(kw)
        self._json(d, code)

    def _qs(self):
        u = urlparse(self.path)
        return u.path, parse_qs(u.query)

    def _csrf_ok(self):
        """双闸：JSON Content-Type + 自定义头。浏览器跨站带不了自定义头会先触发预检。"""
        ct = (self.headers.get("Content-Type") or "").lower()
        if "application/json" not in ct:
            return False, "Content-Type 必须是 application/json"
        if self.headers.get("X-UPS-Request") != "1":
            return False, "缺少 X-UPS-Request 头"
        return True, ""

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        if n <= 0 or n > 1 << 20:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8", "replace") or "{}")
        except Exception:
            return {}

    # ---------- GET ----------
    def do_GET(self):
        path, qs = self._qs()
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path == "/api/ping":
                return self._json({"ok": True, "ver": VERSION})
            if path == "/api/status":
                return self._api_status()
            if path == "/api/vars":
                return self._api_vars()
            if path == "/api/events":
                return self._api_events(qs)
            if path == "/api/history":
                return self._api_history(qs)
            if path == "/api/cycles":
                return self._api_cycles(qs)
            if path == "/api/meta":
                return self._api_meta()
            if path == "/api/settings":
                return self._json({"ok": True, "config": CFG})
            if path == "/api/update/check":
                return self._json(dict(check_update(), version=VERSION))
            if path == "/api/export.csv":
                return self._api_export_csv(qs)
            # 先按真实静态文件解析（图标 / 诊断页等），找不到才回落到 SPA 首页
            rel = path.lstrip("/")
            if rel and ".." not in rel and os.path.isfile(os.path.join(WWW_DIR, rel)):
                return self._static(rel)
            return self._static("index.html")
        except Exception as e:
            log("GET %s 异常: %s" % (path, e), "error")
            return self._err(500, "服务器内部错误：%s" % e)

    # ---------- POST ----------
    def do_POST(self):
        path, qs = self._qs()
        ok, why = self._csrf_ok()
        if not ok:
            return self._err(403, "请求被拒绝：%s" % why)
        try:
            if path == "/api/settings":
                return self._api_settings_save()
            if path == "/api/update/apply":
                return self._api_update_apply()
            if path == "/api/collect-now":
                threading.Thread(target=lambda: _safe_tick(), daemon=True).start()
                return self._json({"ok": True})
            if path == "/api/db/vacuum":
                threading.Thread(target=STORE.vacuum, daemon=True).start()
                return self._json({"ok": True, "note": "已开始整理，稍后查看大小"})
            return self._err(404, "接口不存在")
        except Exception as e:
            log("POST %s 异常: %s" % (path, e), "error")
            return self._err(500, "服务器内部错误：%s" % e)

    # ---------- 具体实现 ----------
    def _static(self, rel):
        safe = os.path.normpath(rel).replace("\\", "/").lstrip("/")
        if safe.startswith("..") or ":" in safe:
            return self._err(403, "非法路径")
        p = os.path.join(WWW_DIR, safe)
        if not os.path.isfile(p):
            return self._err(404, "文件不存在: " + safe)
        ext = os.path.splitext(p)[1].lower()
        ctype = STATIC_TYPES.get(ext) or mimetypes.guess_type(p)[0] or "application/octet-stream"
        with open(p, "rb") as f:
            data = f.read()
        self._send(200, data, ctype)

    def _api_status(self):
        snap = COLLECTOR.snap
        if snap is None:
            e = COLLECTOR.err or {"code": "WARMUP", "message": "正在采集第一份数据",
                                  "hint": "首次采集通常在 5 秒内完成"}
            return self._json({
                "ok": False, "warmup": True, "err": e, "meta": {"ver": VERSION},
                "device": {}, "live": {}, "health": {}, "raw": {},
                "cov": {"present": 0, "total": len(VAR_META), "extra": []},
            })
        out = dict(snap)
        out["meta"] = dict(snap.get("meta") or {})
        out["meta"]["db"] = STORE.db_size()
        out["meta"]["uptime"] = int(time.time()) - COLLECTOR.started
        out["meta"]["counts"] = STORE.counts()
        return self._json(out)

    def _api_vars(self):
        rows = []
        by_group = {}
        snap = COLLECTOR.snap or {}
        raw = snap.get("raw") or {}
        for key, (grp, cn, unit, kind) in VAR_META.items():
            item = {
                "key": key, "group": grp, "cn": cn, "unit": unit, "kind": kind,
                "value": raw.get(key),
                "present": key in raw,
                "note": VAR_NOTE.get(key, ""),
            }
            if kind == "num":
                item["num"] = fnum(raw.get(key))
            by_group.setdefault(grp, []).append(item)
        for grp in VAR_GROUP_ORDER:
            if grp in by_group:
                rows.append({"group": grp, "items": by_group[grp]})
        # 未收录的变量（不同机型可能多出字段）
        extra = []
        for k in sorted(raw):
            if k not in VAR_META:
                extra.append({"key": k, "value": raw.get(k)})
        if extra:
            rows.append({"group": "未收录", "items": [
                {"key": e["key"], "group": "未收录", "cn": e["key"], "unit": "",
                 "kind": "str", "value": e["value"], "present": True} for e in extra]})
        dev = (COLLECTOR.snap or {}).get("device") or {}
        low_fw = dev.get("lowbatt_fw")
        low_nut = dev.get("lowbatt_nut")
        gap = None
        if low_fw is not None and low_nut is not None:
            gap = round(low_nut - low_fw, 1)
        return self._json({
            "ok": True, "groups": rows,
            "total": len(VAR_META),
            "present": sum(1 for k in VAR_META if k in raw),
            "extra_count": len(extra),
            "lowbatt": {"firmware": low_fw, "nut": low_nut, "gap": gap},
        })

    def _api_events(self, qs):
        try:
            limit = min(2000, max(1, int((qs.get("limit") or ["200"])[0])))
        except Exception:
            limit = 200
        etype = (qs.get("type") or [None])[0] or None
        rows = STORE.events(limit, etype)
        cols = ["id", "ts", "ups", "type", "from_st", "to_st", "chg", "rt",
                "in_v", "out_v", "load", "temp", "note"]
        evs = [dict(zip(cols, r)) for r in rows]
        labels = {
            "POWER_LOSS": "市电中断", "POWER_RESTORE": "市电恢复",
            "LOW_BATTERY": "低电量", "REPLACE_BATTERY": "需更换电池",
            "ALARM": "告警", "FORCED_SHUTDOWN": "强制关机", "OVERLOAD": "超载",
            "INIT": "开始观测", "SESSION_START": "服务启动",
            "BLIND_SPOT": "观测盲区", "DEVICE_CHANGED": "设备变化",
            "VARS_CHANGED": "变量集变化", "CHARGE_JUMP": "电量跳变",
            "UPDATE": "软件更新",
        }
        for e in evs:
            e["label"] = labels.get(e["type"], e["type"])
        return self._json({"ok": True, "events": evs, "counts": STORE.event_counts(),
                           "labels": labels})

    def _api_history(self, qs):
        rng = (qs.get("range") or ["1h"])[0]
        spans = {"10m": 600, "30m": 1800, "1h": 3600, "6h": 21600, "24h": 86400,
                 "7d": 604800, "30d": 2592000, "90d": 7776000, "all": 10 ** 9}
        span = spans.get(rng, 3600)
        now = int(time.time())
        since = 0 if rng == "all" else now - span
        rows, res = STORE.history(since, now)
        keys = ["ts"] + SERIES_COLS + ["st", "in_v_min", "in_v_max"]
        data = [dict(zip(keys, r)) for r in rows]
        return self._json({"ok": True, "range": rng, "res": res,
                           "res_label": RES_LABEL.get(res, ""), "points": data})

    def _api_cycles(self, qs):
        try:
            limit = min(500, max(1, int((qs.get("limit") or ["50"])[0])))
        except Exception:
            limit = 50
        rows = STORE.cycles(limit)
        cols = ["id", "start_ts", "end_ts", "start_chg", "end_chg", "min_rt",
                "avg_power", "wh", "kind", "note"]
        return self._json({"ok": True, "cycles": [dict(zip(cols, r)) for r in rows]})

    def _api_meta(self):
        dev = (COLLECTOR.snap or {}).get("device") or {}
        return self._json({
            "ok": True, "version": VERSION, "app": APP_NAME, "app_cn": APP_CN,
            "repo": GITHUB_REPO, "port": PORT,
            "data_dir": DATA_DIR, "www_dir": WWW_DIR,
            "python": sys.version.split()[0],
            "upsc": UPSC, "db": STORE.db_size(), "counts": STORE.counts(),
            "uptime": int(time.time()) - COLLECTOR.started,
            "ticks": COLLECTOR.tick_count,
            "err": COLLECTOR.err, "device": dev,
            "vars_meta": len(VAR_META),
        })

    def _api_export_csv(self, qs):
        rng = (qs.get("range") or ["24h"])[0]
        spans = {"1h": 3600, "24h": 86400, "7d": 604800, "30d": 2592000, "all": 10 ** 9}
        now = int(time.time())
        since = 0 if rng == "all" else now - spans.get(rng, 86400)
        rows, res = STORE.history(since, now)
        buf = io.StringIO()
        head = ["时间"] + [SERIES.get(c, c) for c in SERIES_COLS] + ["状态位"]
        buf.write(",".join(head) + "\n")
        for r in rows:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r[0]))
            vals = [ts] + [("" if v is None else v) for v in r[1:12]] + [str(r[12] or 0)]
            buf.write(",".join(str(v) for v in vals) + "\n")
        data = buf.getvalue().encode("utf-8-sig")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition",
                         'attachment; filename="upshealth-%s.csv"' % rng)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _api_settings_save(self):
        body = self._body()
        allowed = {
            "poll_interval": int, "ups_name": str, "auto_check_update": bool,
            "update_mirror": str, "battery_ah": float, "battery_dod": float,
            "retain_raw_h": int, "retain_1m_d": int, "retain_10m_d": int,
            "range_default": str,
        }
        changed = {}
        for k, typ in allowed.items():
            if k not in body:
                continue
            v = body[k]
            try:
                if typ is bool:
                    v = bool(v)
                elif typ is int:
                    v = int(v)
                elif typ is float:
                    v = float(v)
                else:
                    v = str(v)
            except Exception:
                return self._err(400, "参数 %s 类型错误" % k)
            if k == "poll_interval":
                v = max(3, min(300, v))
            if k == "battery_ah":
                v = max(0.5, min(500.0, v))
            if k == "battery_dod":
                v = max(0.3, min(1.0, v))
            CFG[k] = v
            changed[k] = v
        save_cfg(CFG)
        # 配置变化后重新评估健康分
        if COLLECTOR.snap and ("battery_ah" in changed or "battery_dod" in changed):
            try:
                COLLECTOR.snap["health"] = assess(
                    COLLECTOR.snap["live"], COLLECTOR.snap.get("raw") or {})
            except Exception:
                pass
        return self._json({"ok": True, "config": CFG})

    def _api_update_apply(self):
        body = self._body()
        info = check_update()
        if not info.get("ok"):
            return self._json(info, 400)
        if not info.get("newer") and not body.get("force"):
            return self._json({"ok": False, "error": "已是最新版本 %s" % VERSION}, 400)
        fpk = info.get("fpk")
        if not fpk:
            return self._err(400, "该 Release 未提供 .fpk 安装包")
        sha = body.get("sha256") or fpk.get("sha256")
        if not sha and info.get("sha_asset"):
            try:
                with urlopen(Request(info["sha_asset"]["url"],
                                     headers={"User-Agent": "upshealth/" + VERSION}),
                             timeout=20) as r:
                    txt = r.read().decode("utf-8", "replace")
                m = re.search(r"\b([0-9a-fA-F]{64})\b", txt)
                if m:
                    sha = m.group(1)
            except Exception as e:
                log("取 sha256 失败：%s" % e, "warn")
        res = apply_update(fpk["url"], sha)
        return self._json(res, 200 if res.get("ok") else 400)


def _safe_tick():
    try:
        COLLECTOR.tick()
    except NutError as e:
        COLLECTOR._set_error(e.code, e.message, e.hint)
    except Exception as e:
        COLLECTOR._set_error("INTERNAL", str(e))


# ======================================================================
# 启动
# ======================================================================
class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(WWW_DIR, exist_ok=True)
    log("=" * 60)
    log("%s v%s 启动，端口 %d" % (APP_CN, VERSION, PORT))
    log("APP_DIR=%s DATA_DIR=%s" % (APP_DIR, DATA_DIR))
    log("upsc=%s" % UPSC)

    t = threading.Thread(target=COLLECTOR.run, daemon=True)
    t.start()
    # 首次采集同步等 8 秒，让打开页面就有数据
    for _ in range(16):
        if COLLECTOR.snap is not None:
            break
        time.sleep(0.5)

    try:
        httpd = Server(("0.0.0.0", PORT), Handler)
    except OSError as e:
        log("端口 %d 绑定失败：%s" % (PORT, e), "error")
        sys.exit(1)
    log("HTTP 就绪 http://0.0.0.0:%d" % PORT)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
