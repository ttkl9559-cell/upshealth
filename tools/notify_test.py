#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通知推送端到端自检。

不联外网：本地起一个假 SMTP 收信端、一个假 Webhook 收报端，
把 6 种通道全部打一遍，校验报文格式、去抖、静音、密钥脱敏。

    python tools/notify_test.py

CI 里也会跑这一项。任何一条不符合预期都会 FAIL 退出。
"""
import importlib.util
import json
import os
import re
import shutil
import socketserver
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="upsnt-")
SMTP_PORT = 2525
HOOK_PORT = 2526

# ----------------------------------------------------------------------
# 假 SMTP 收信端
# ----------------------------------------------------------------------
MAILS = []


class SmtpSink(socketserver.StreamRequestHandler):
    def handle(self):
        w = self.wfile
        w.write(b"220 fake-smtp ready\r\n")

        while True:
            line = self.rfile.readline()
            if not line:
                return
            cmd = line.decode("utf-8", "replace").strip()
            up = cmd.upper()
            if up.startswith(("EHLO", "HELO")):
                w.write(b"250-fake\r\n250-8BITMIME\r\n250 OK\r\n")
            elif up.startswith("MAIL FROM"):
                self.mail_from = cmd
                w.write(b"250 OK\r\n")
            elif up.startswith("RCPT TO"):
                self.rcpt = getattr(self, "rcpt", []) + [cmd]
                w.write(b"250 OK\r\n")
            elif up == "DATA":
                w.write(b"354 end with .\r\n")
                buf = []
                while True:
                    ln = self.rfile.readline()
                    if ln in (b".\r\n", b".\n", b""):
                        break
                    buf.append(ln)
                MAILS.append({
                    "from": getattr(self, "mail_from", ""),
                    "rcpt": getattr(self, "rcpt", []),
                    "data": b"".join(buf).decode("utf-8", "replace"),
                })
                self.rcpt = []
                w.write(b"250 OK queued\r\n")
            elif up == "QUIT":
                w.write(b"221 bye\r\n")
                return
            elif up.startswith("RSET"):
                w.write(b"250 OK\r\n")
            elif up.startswith("NOOP"):
                w.write(b"250 OK\r\n")
            else:
                w.write(b"250 OK\r\n")


# ----------------------------------------------------------------------
# 假 HTTP 收报端
# ----------------------------------------------------------------------
HOOKS = []


class HookSink(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace")
        ctype = self.headers.get("Content-Type") or ""
        rec = {"path": self.path, "ctype": ctype, "raw": raw}
        if "json" in ctype:
            try:
                rec["json"] = json.loads(raw)
            except Exception:
                rec["json"] = None
        else:
            rec["form"] = {k: v[0] for k, v in parse_qs(raw).items()}
        HOOKS.append(rec)
        body = b'{"errcode":0,"errmsg":"ok","code":0,"data":{"pushid":"1"}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def start_servers():
    smtp = socketserver.ThreadingTCPServer(("127.0.0.1", SMTP_PORT), SmtpSink)
    smtp.daemon_threads = True
    threading.Thread(target=smtp.serve_forever, daemon=True).start()

    httpd = ThreadingHTTPServer(("127.0.0.1", HOOK_PORT), HookSink)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return smtp, httpd


# ----------------------------------------------------------------------
def load_server():
    os.environ["TRIM_PKGVAR"] = TMP
    os.environ["TRIM_APPDEST"] = ROOT
    spec = importlib.util.spec_from_file_location(
        "ups_srv", os.path.join(ROOT, "app", "server.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("  ✓ %s" % name)
    else:
        print("  ✗ %s  %s" % (name, detail))
        FAILS.append(name)


def main():
    print("临时数据目录：%s" % TMP)
    start_servers()
    S = load_server()
    time.sleep(0.3)

    hook = "http://127.0.0.1:%d" % HOOK_PORT

    # ---------------- 1. 报文渲染 ----------------
    print("\n[1] 报文渲染")
    ev = {"ts": int(time.time()), "ups": "dev1", "type": "POWER_LOSS",
          "from_st": "OL CHRG", "to_st": "OB DISCHRG",
          "chg": 100, "rt": 1170, "in_v": 0, "out_v": 230, "load": 27, "temp": 27.9,
          "note": "市电中断，切换电池供电"}
    dev = {"mfr": "DELTA", "model": "UPS102R2RT2B035", "serial": "AAX18C00777WC"}
    title, body, level = S.build_message(ev, dev)
    check("标题含严重标记与型号", "【严重】" in title and "UPS102R2RT2B035" in title, title)
    check("级别为 crit", level == "crit", level)
    check("正文含断电说明", "市电中断，切换电池供电" in body)
    check("续航格式化", "19 分 30 秒" in body, body)
    check("序列号进入正文", "AAX18C00777WC" in body)

    # ---------------- 2. 六种通道实测 ----------------
    print("\n[2] 通道发信")
    S.CFG["notify_channels"] = [
        {"id": "m1", "type": "smtp", "name": "邮件", "enabled": True,
         "config": {"host": "127.0.0.1", "port": SMTP_PORT, "tls": "plain",
                    "user": "", "pass": "", "from": "ups@test.local",
                    "to": "a@test.local,b@test.local"}},
        {"id": "w1", "type": "wecom", "name": "企微", "enabled": True,
         "config": {"url": hook + "/cgi-bin/webhook/send?key=SECRETKEY"}},
        {"id": "d1", "type": "dingtalk", "name": "钉钉", "enabled": True,
         "config": {"url": hook + "/robot/send?access_token=TOKEN123"}},
        {"id": "b1", "type": "bark", "name": "Bark", "enabled": True,
         "config": {"key": "BARKKEY", "url": hook}},
        {"id": "s1", "type": "serverchan", "name": "Server酱", "enabled": True,
         "config": {"key": "SCTKEY", "url": hook + "/SCTKEY.send"}},
        {"id": "h1", "type": "webhook", "name": "通用", "enabled": True,
         "config": {"url": hook + "/generic?token=ABCDEF"}},
    ]
    res = S.NOTIFIER.dispatch(title, body, "POWER_LOSS", level)
    time.sleep(0.6)
    by_ch = {r["channel"]: r for r in res}
    check("6 个通道全部返回成功",
          len(res) == 6 and all(r["ok"] for r in res),
          [r for r in res if not r["ok"]])

    # SMTP 校验
    check("SMTP 收到 1 封", len(MAILS) == 1, len(MAILS))
    if MAILS:
        m = MAILS[0]
        check("SMTP 两个收件人", len(m["rcpt"]) == 2, m["rcpt"])
        check("SMTP 主题含中文且已编码",
              "=?utf-8?" in m["data"].lower() or "Subject:" in m["data"], m["data"][:120])
        check("SMTP 正文为 UTF-8 base64",
              "Content-Transfer-Encoding: base64" in m["data"]
              or "charset=\"utf-8\"" in m["data"].lower())

    # 企微 / 钉钉 报文
    wecom = [h for h in HOOKS if h["path"].startswith("/cgi-bin/webhook")]
    ding = [h for h in HOOKS if h["path"].startswith("/robot/send")]
    check("企微 POST 到正确路径", len(wecom) == 1, len(wecom))
    if wecom:
        j = wecom[0].get("json") or {}
        check("企微分组为 text", j.get("msgtype") == "text", j.get("msgtype"))
        check("企微内容含标题", "市电中断" in (j.get("text") or {}).get("content", ""))
    check("钉钉收到 1 条", len(ding) == 1, len(ding))
    if ding:
        j = ding[0].get("json") or {}
        check("钉钉 msgtype=text", j.get("msgtype") == "text")

    bark = [h for h in HOOKS if h["path"] == "/push"]
    check("Bark 走 /push", len(bark) == 1, [h["path"] for h in HOOKS])
    if bark:
        j = bark[0].get("json") or {}
        check("Bark 带 device_key", j.get("device_key") == "BARKKEY", j.get("device_key"))
        check("Bark 严重事件用 timeSensitive", j.get("level") == "timeSensitive", j.get("level"))

    sc = [h for h in HOOKS if h["path"].endswith(".send")]
    check("Server酱 表单提交", len(sc) == 1 and "desp" in (sc[0].get("form") or {}),
          (sc[0].get("form") if sc else None))

    gen = [h for h in HOOKS if h["path"].startswith("/generic")]
    check("通用 webhook 收到 JSON", len(gen) == 1, len(gen))
    if gen:
        j = gen[0].get("json") or {}
        check("通用 webhook 字段齐全",
              all(k in j for k in ("title", "body", "event", "level", "ts")), list(j))
        check("通用 webhook event 正确", j.get("event") == "POWER_LOSS", j.get("event"))

    # ---------------- 3. 发送记录 ----------------
    print("\n[3] 发送记录落库")
    logs = S.STORE.notify_logs(20)
    check("记录已写入", len(logs) >= 6, len(logs))
    check("记录含通道名", any(x["channel"] == "邮件" for x in logs))

    # ---------------- 4. 去抖 ----------------
    print("\n[4] 去抖与限流")
    S.CFG["notify_channels"] = [c for c in S.CFG["notify_channels"] if c["id"] == "h1"]
    S.CFG["notify_events"] = ["POWER_LOSS"]
    S.CFG["notify_enabled"] = True
    S.CFG["notify_debounce_s"] = 60
    S.CFG["notify_max_per_min"] = 8
    S.NOTIFIER._last_ts.clear()
    S.NOTIFIER._window.clear()
    before = len(HOOKS)
    for _ in range(3):
        S.NOTIFIER.on_event(ev)
    time.sleep(0.8)
    check("3 次同类事件只发 1 次", len(HOOKS) - before == 1, len(HOOKS) - before)

    # ---------------- 5. 静音时段 ----------------
    print("\n[5] 静音时段")
    now = time.localtime()
    S.CFG["notify_quiet"] = "%02d:%02d-%02d:%02d" % (
        now.tm_hour, now.tm_min, (now.tm_hour + 2) % 24, now.tm_min)
    S.NOTIFIER._last_ts.clear()
    before = len(HOOKS)
    warn_ev = dict(ev, type="CHARGE_JUMP")
    S.CFG["notify_events"] = ["CHARGE_JUMP", "POWER_LOSS"]
    S.NOTIFIER._handle(warn_ev)
    check("静音时段压制警告级事件", len(HOOKS) == before, len(HOOKS) - before)
    S.NOTIFIER._handle(dict(ev, type="POWER_LOSS"))
    check("静音时段不压制严重事件", len(HOOKS) - before == 1, len(HOOKS) - before)
    S.CFG["notify_quiet"] = ""

    # ---------------- 6. 密钥脱敏 ----------------
    print("\n[6] 密钥脱敏")
    masked = S.mask_channel({"type": "smtp", "name": "邮件",
                             "config": {"host": "h", "user": "u", "pass": "REALPASS",
                                        "to": "a@b"}})
    check("SMTP 密码被打码", masked["config"]["pass"] == "***", masked["config"]["pass"])
    mu = S.mask_url(hook + "/cgi-bin/webhook/send?key=SECRETKEY")
    check("URL query 里的 key 被打码", "SECRETKEY" not in mu, mu)
    mu2 = S.mask_url("https://sctapi.ftqq.com/SCTKEY123.send")
    check("Server酱 SendKey 被打码", "SCTKEY123" not in mu2, mu2)
    mu3 = S.mask_url("https://api.day.app/BARKKEY/push")
    check("Bark key 被打码", "BARKKEY" not in mu3, mu3)
    merged = S.merge_secrets({"pass": "***"}, {"pass": "REALPASS"})
    check("*** 能还原成真实密码", merged["pass"] == "REALPASS", merged["pass"])
    merged2 = S.merge_secrets({"pass": "NEWPASS"}, {"pass": "REALPASS"})
    check("填了新密码则用新的", merged2["pass"] == "NEWPASS", merged2["pass"])

    # ---------------- 7. 失败要报错，不能静默 ----------------
    print("\n[7] 失败路径")
    ok, err = S.NOTIFIER.send_one(
        {"type": "wecom", "name": "坏地址", "config": {"url": ""}}, "t", "b")
    check("空 Webhook 地址被判失败", (not ok) and "Webhook" in (err or ""), err)
    ok2, err2 = S.NOTIFIER.send_one(
        {"type": "smtp", "name": "x", "config": {"host": "", "to": ""}}, "t", "b")
    check("空 SMTP 配置被判失败", not ok2, err2)
    ok3, err3 = S.NOTIFIER.send_one(
        {"type": "wecom", "name": "x",
         "config": {"url": "http://127.0.0.1:1/none"}}, "t", "b")
    check("连不上时返回错误串", (not ok3) and err3, err3)

    print("\n" + "=" * 46)
    if FAILS:
        print("RESULT: FAIL  失败 %d 项：%s" % (len(FAILS), "、".join(FAILS)))
        return 1
    print("RESULT: PASS  全部通过")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
