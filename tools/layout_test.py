"""
本地布局验证：模拟 fpk 安装后的真实目录结构
  <root>/server/server.py + <root>/www/index.html
起服务 -> 打接口 -> 杀掉，全程一个进程内完成。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request as u

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SRC = ROOT
LAY = os.path.join(os.environ.get("TEMP", "/tmp"), "laytest")
PORT = 8799


def build_layout():
    if os.path.isdir(LAY):
        shutil.rmtree(LAY)
    os.makedirs(os.path.join(LAY, "server"))
    os.makedirs(os.path.join(LAY, "www"))
    os.makedirs(os.path.join(LAY, "data"))
    shutil.copy2(os.path.join(SRC, "app", "server.py"), os.path.join(LAY, "server", "server.py"))
    shutil.copy2(os.path.join(SRC, "app", "www", "index.html"), os.path.join(LAY, "www", "index.html"))
    return LAY


def wait_port(port, timeout=25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = socket.socket()
        s.settimeout(1)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            time.sleep(0.3)
        finally:
            s.close()
    return False


def main():
    lay = build_layout()
    env = dict(os.environ)
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        env.pop(k, None)
    env["TRIM_PKGVAR"] = os.path.join(lay, "data")
    env["TRIM_SERVICE_PORT"] = str(PORT)
    env["TRIM_APPDEST"] = lay

    log = open(os.path.join(lay, "run.log"), "wb")
    p = subprocess.Popen([sys.executable, "-u", os.path.join(lay, "server", "server.py")],
                         env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        if not wait_port(PORT):
            print("FAIL: port never opened")
            print(open(os.path.join(lay, "run.log"), encoding="utf-8", errors="replace").read()[-2000:])
            return 1
        op = u.build_opener(u.ProxyHandler({}))
        fails = 0

        r = op.open("http://127.0.0.1:%d/" % PORT, timeout=10)
        body = r.read()
        ok = r.status == 200 and b"<!DOCTYPE html>" in body[:200]
        print("%-22s %s  http=%s bytes=%d" % ("GET /", "OK" if ok else "FAIL", r.status, len(body)))
        fails += 0 if ok else 1

        for path in ("/api/ping", "/api/meta"):
            r = op.open("http://127.0.0.1:%d%s" % (PORT, path), timeout=10)
            d = json.loads(r.read().decode("utf-8"))
            if path == "/api/ping":
                ok = d.get("ok") is True
                print("%-22s %s  %s" % (path, "OK" if ok else "FAIL", d))
            else:
                wd = d.get("www_dir") or ""
                ok = os.path.normcase(os.path.normpath(wd)) == os.path.normcase(os.path.normpath(os.path.join(lay, "www")))
                print("%-22s %s  www_dir=%s" % (path, "OK" if ok else "FAIL", wd))
                print("%-22s     data_dir=%s" % ("", d.get("data_dir")))
            fails += 0 if ok else 1

        # 更新包解包路径（不改真库，只校验 apply_update 的备份目标解析）
        r = op.open("http://127.0.0.1:%d/api/update/check" % PORT, timeout=30)
        d = json.loads(r.read().decode("utf-8"))
        print("%-22s %s  ok=%s latest=%s newer=%s" % ("/api/update/check", "OK" if d.get("ok") else "WARN",
                                                     d.get("ok"), d.get("latest"), d.get("newer")))
        if d.get("ok"):
            print("%-22s     fpk=%s" % ("", (d.get("fpk") or {}).get("name")))
            print("%-22s     sha_asset=%s" % ("", (d.get("sha_asset") or {}).get("name")))

        print("RESULT:", "PASS" if fails == 0 else "FAIL(%d)" % fails)
        return 0 if fails == 0 else 1
    finally:
        p.terminate()
        try:
            p.wait(timeout=8)
        except Exception:
            p.kill()
        log.close()


if __name__ == "__main__":
    sys.exit(main())
