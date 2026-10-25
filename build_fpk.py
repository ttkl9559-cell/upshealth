#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
upshealth fpk 构建脚本（纯 Python，Windows / macOS / Linux 均可运行）

用法:
    python build_fpk.py            # 构建当前平台对应的包（默认 x86）
    python build_fpk.py --arm      # 构建 arm64 包
    python build_fpk.py --both     # 两个都构建
    python build_fpk.py --out DIR  # 指定输出目录

产物: dist/upshealth_<version>_<arch>.fpk
"""
import os
import sys
import gzip
import struct
import hashlib
import zlib
import io
import re
import tarfile

ROOT = os.path.dirname(os.path.abspath(__file__))
APPNAME = "upshealth"
DISPLAY_NAME = "UPS 健康"
PORT = 8778
REPO = "https://github.com/ttkl9559-cell/upshealth"


# ----------------------------------------------------------------------
# PNG 图标（纯标准库生成，无需 Pillow）
# ----------------------------------------------------------------------
def png_chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def make_icon(size):
    """深色圆角底 + 电池轮廓 + 闪电，与 fnmonitor 的扁平风格一致。"""
    W = H = size
    rows = []
    rad = size * 0.22
    bg = (20, 20, 20)
    fg = (242, 242, 240)

    cx, cy = size / 2.0, size / 2.0
    # 电池主体
    bw, bh = size * 0.50, size * 0.30
    bx0, by0 = cx - bw / 2 - size * 0.03, cy - bh / 2
    bx1, by1 = bx0 + bw, by0 + bh
    stroke = max(1.5, size * 0.045)
    # 正极凸起
    nub_x0, nub_x1 = bx1, bx1 + size * 0.055
    nub_y0, nub_y1 = cy - bh * 0.20, cy + bh * 0.20

    # 闪电折线（归一化坐标）
    bolt = [(0.50, 0.18), (0.40, 0.52), (0.50, 0.52), (0.44, 0.82),
            (0.63, 0.44), (0.52, 0.44), (0.60, 0.18)]

    def in_round_rect(x, y, x0, y0, x1, y1, r):
        if x < x0 or x > x1 or y < y0 or y > y1:
            return False
        nx = min(max(x, x0 + r), x1 - r)
        ny = min(max(y, y0 + r), y1 - r)
        return (x - nx) ** 2 + (y - ny) ** 2 <= r * r

    def dist_to_poly(px, py, pts):
        best = 1e9
        for i in range(len(pts) - 1):
            ax, ay = pts[i]
            bx, by = pts[i + 1]
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy
            t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
            qx, qy = ax + t * dx, ay + t * dy
            d = ((px - qx) ** 2 + (py - qy) ** 2) ** 0.5
            if d < best:
                best = d
        return best

    def point_in_poly(px, py, pts):
        inside = False
        n = len(pts)
        for i in range(n):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % n]
            if (y1 > py) != (y2 > py):
                xin = (x2 - x1) * (py - y1) / (y2 - y1 + 1e-12) + x1
                if px < xin:
                    inside = not inside
        return inside

    bolt_px = [(p[0] * size, p[1] * size) for p in bolt]
    body_r = size * 0.055

    for y in range(H):
        row = bytearray([0])
        for x in range(W):
            # 外圆角
            if not in_round_rect(x, y, 0, 0, W - 1, H - 1, rad):
                row += bytes((0, 0, 0, 0))
                continue
            px = bg
            if size >= 48:
                # 电池外壳描边
                outer = in_round_rect(x, y, bx0, by0, bx1, by1, body_r)
                inner = in_round_rect(x, y, bx0 + stroke, by0 + stroke,
                                      bx1 - stroke, by1 - stroke, max(1, body_r - stroke * 0.6))
                if outer and not inner:
                    px = fg
                # 正极凸起
                elif (nub_x0 <= x <= nub_x1 and nub_y0 <= y <= nub_y1):
                    px = fg
                # 闪电
                elif point_in_poly(x, y, bolt_px):
                    px = fg
            elif size >= 32:
                # 小尺寸简化：只画电池框 + 实心闪电
                if in_round_rect(x, y, bx0, by0, bx1, by1, body_r) and not \
                        in_round_rect(x, y, bx0 + stroke * 1.4, by0 + stroke * 1.4,
                                      bx1 - stroke * 1.4, by1 - stroke * 1.4, max(1, body_r - stroke)):
                    px = fg
                elif point_in_poly(x, y, bolt_px):
                    px = fg
            row += bytes((px[0], px[1], px[2], 255))
        rows.append(bytes(row))

    ihdr = struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", ihdr)
            + png_chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + png_chunk(b"IEND", b""))


# ----------------------------------------------------------------------
# tar / gzip 工具（固定 uid/mtime，保证可重现构建）
# ----------------------------------------------------------------------
def gz(data):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as g:
        g.write(data)
    return buf.getvalue()


UID, GID, UNAME = 1001, 1001, "runner"


def tar_add(tf, name, data=None, mode=0o644, isdir=False):
    ti = tarfile.TarInfo(name)
    ti.uid = UID
    ti.gid = GID
    ti.uname = UNAME
    ti.gname = UNAME
    ti.mtime = 0
    if isdir:
        ti.type = tarfile.DIRTYPE
        ti.mode = 0o755
        ti.size = 0
        tf.addfile(ti)
    else:
        ti.mode = mode
        ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))


# ----------------------------------------------------------------------
# 版本
# ----------------------------------------------------------------------
def read_version():
    p = os.path.join(ROOT, "app", "server.py")
    with open(p, "r", encoding="utf-8") as f:
        src = f.read()
    m = re.search(r'FALLBACK_VERSION\s*=\s*"([^"]+)"', src)
    if not m:
        raise SystemExit("无法从 app/server.py 读取 FALLBACK_VERSION")
    return m.group(1)


# ----------------------------------------------------------------------
# 模板
# ----------------------------------------------------------------------
MANIFEST = """appname         = {appname}
version         = {version}
display_name    = {display_name}
desc            = 飞牛 UPS 监控与电池健康：62 项 NUT 变量全量展示、电池实测容量与衰减评估、市电中断与切换事件记录、输入电压趋势、内置在线更新。纯读取，不改动系统。
arch            = {arch}
source          = thirdparty
desktop_uidir   = ui
desktop_applaunchname = {appname}.Application
maintainer      = ttkl9559-cell
maintainer_url  = {repo}
distributor     = ttkl9559-cell
distributor_url = {repo}
service_port    = {port}
ctl_stop        = true
platform        = 
install_type    = root
checksum        = {md5}
"""

UI_CONFIG = """{
  ".url": {
    "%(app)s.Application": {
      "title": "%(title)s",
      "desc": "UPS 监控与电池健康",
      "icon": "images/icon_{0}.png",
      "type": "url",
      "port": "%(port)s",
      "protocol": "http",
      "url": "/",
      "allUsers": true
    }
  }
}
"""

PRIVILEGE = """{
    "defaults": {
        "run-as": "root"
    }
}
"""

RESOURCE = """{
    "data-share":
    {
        "shares": [
            {
                "name": "%(app)s",
                "permission":
                {
                    "rw": ["%(app)s"]
                }
            }
        ]
    }
}
"""

CMD_MAIN = r"""#!/bin/bash
set -u
APP_NAME="${TRIM_APPNAME:-%(app)s}"
APP_DIR="${TRIM_APPDEST:-/var/apps/${APP_NAME}}"
DATA_DIR="${TRIM_PKGVAR:-/vol1/@appdata/${APP_NAME}}"
PORT="${TRIM_SERVICE_PORT:-%(port)d}"
LOG="${DATA_DIR}/upshealth.log"
PID_FILE="${DATA_DIR}/upshealth.pid"

find_server() {
    if [ -f "${APP_DIR}/server/server.py" ]; then echo "${APP_DIR}/server"; return 0; fi
    if [ -f "${APP_DIR}/target/server/server.py" ]; then echo "${APP_DIR}/target/server"; return 0; fi
    return 1
}

is_up() {
    curl -sf "http://127.0.0.1:${PORT}/api/ping" >/dev/null 2>&1
}

start() {
    mkdir -p "${DATA_DIR}"
    if [ -f "${PID_FILE}" ] && kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
        echo "already running (pid $(cat "${PID_FILE}"))"; return 0
    fi
    SRC=$(find_server) || { echo "ERROR: server/server.py not found under ${APP_DIR}"; return 1; }
    if command -v ss >/dev/null 2>&1 && ss -tln 2>/dev/null | grep -q ":${PORT} "; then
        echo "ERROR: port ${PORT} already in use"; return 1
    fi
    cd "${SRC}" || return 1
    # 用绝对路径启动：这样进程命令行含完整路径，stop 时的 pkill 兜底才能命中
    nohup /usr/bin/python3 -u "${SRC}/server.py" >> "${LOG}" 2>&1 < /dev/null &
    PID=$!
    echo "${PID}" > "${PID_FILE}"
    for i in $(seq 1 20); do
        if is_up; then echo "started (pid ${PID})"; return 0; fi
        sleep 1
    done
    echo "WARN: started but /api/ping not ready yet (pid ${PID})"
    return 0
}

stop() {
    if [ -f "${PID_FILE}" ]; then
        P=$(cat "${PID_FILE}")
        kill "${P}" 2>/dev/null || true
        for i in $(seq 1 10); do
            kill -0 "${P}" 2>/dev/null || break
            sleep 0.4
        done
        kill -9 "${P}" 2>/dev/null || true
        rm -f "${PID_FILE}"
    fi
    # 兜底：按本应用 server.py 的绝对路径精确匹配，绝不按 app.py / server.py 泛杀
    pkill -9 -f "${APP_DIR}/server/server.py" 2>/dev/null || true
    pkill -9 -f "${APP_DIR}/target/server/server.py" 2>/dev/null || true
    echo "stopped"
}

status() {
    if [ -f "${PID_FILE}" ] && kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
        echo "running (pid $(cat "${PID_FILE}"))"; return 0
    fi
    echo "stopped"; return 1
}

case "${1:-start}" in
    start) start ;;
    stop) stop ;;
    status) status ;;
    restart) stop; sleep 1; start ;;
    *) echo "Usage: $0 {start|stop|status|restart}"; exit 1 ;;
esac
"""

INSTALL_CALLBACK = r"""#!/bin/bash
set -u
DATA_DIR="${TRIM_PKGVAR:-/vol1/@appdata/${TRIM_APPNAME:-%(app)s}}"
mkdir -p "${DATA_DIR}" 2>/dev/null
exit 0
"""

UNINSTALL_CALLBACK = r"""#!/bin/bash
set -u
DATA_DIR="${TRIM_PKGVAR:-/vol1/@appdata/${TRIM_APPNAME:-%(app)s}}"
if [ -f "${DATA_DIR}/upshealth.pid" ]; then
    kill "$(cat "${DATA_DIR}/upshealth.pid")" 2>/dev/null || true
fi
rm -f "${DATA_DIR}/upshealth.pid" 2>/dev/null
# 数据目录（数据库 / 日志 / 配置 / 备份）由飞牛在卸载确认后清理，这里不删
exit 0
"""


# ----------------------------------------------------------------------
# 构建
# ----------------------------------------------------------------------
def build(arch, out_dir):
    version = read_version()
    app_dir = os.path.join(ROOT, "app")
    server_py = os.path.join(app_dir, "server.py")
    index_html = os.path.join(app_dir, "www", "index.html")
    for p in (server_py, index_html):
        if not os.path.isfile(p):
            raise SystemExit("缺少源文件: %s" % p)

    with open(server_py, "rb") as f:
        server_bytes = f.read()
    with open(index_html, "rb") as f:
        index_bytes = f.read()

    # 语法自检：包里的服务端必须能编译
    compile(server_bytes.decode("utf-8"), "server.py", "exec")

    icons = {s: make_icon(s) for s in (64, 128, 256)}
    ui_conf = UI_CONFIG % {"app": APPNAME, "title": DISPLAY_NAME, "port": PORT}

    # ---- 内层 app.tgz ----
    app_tar = io.BytesIO()
    with tarfile.open(fileobj=app_tar, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        tar_add(tf, "server", isdir=True)
        tar_add(tf, "server/server.py", server_bytes)
        tar_add(tf, "www", isdir=True)
        tar_add(tf, "www/index.html", index_bytes)
        tar_add(tf, "ui", isdir=True)
        tar_add(tf, "ui/config", ui_conf.encode("utf-8"))
        tar_add(tf, "ui/images", isdir=True)
        for s in (64, 128, 256):
            tar_add(tf, "ui/images/icon_%d.png" % s, icons[s])
    app_tgz = gz(app_tar.getvalue())
    md5 = hashlib.md5(app_tgz).hexdigest()

    manifest = MANIFEST.format(
        appname=APPNAME, version=version, display_name=DISPLAY_NAME,
        arch=("arm64" if arch == "arm" else "x86_64"),
        repo=REPO, port=PORT, md5=md5,
    )

    # ---- 外层 fpk ----
    fpk_tar = io.BytesIO()
    subst = {"app": APPNAME, "port": PORT}
    with tarfile.open(fileobj=fpk_tar, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        tar_add(tf, "app.tgz", app_tgz)
        tar_add(tf, "cmd", isdir=True)
        tar_add(tf, "cmd/main", (CMD_MAIN % subst).encode("utf-8"), 0o755)
        tar_add(tf, "cmd/install_callback", (INSTALL_CALLBACK % subst).encode("utf-8"), 0o755)
        tar_add(tf, "cmd/install_init", b"#!/bin/bash\nexit 0\n", 0o755)
        tar_add(tf, "cmd/uninstall_callback", (UNINSTALL_CALLBACK % subst).encode("utf-8"), 0o755)
        tar_add(tf, "cmd/uninstall_init", b"#!/bin/bash\nexit 0\n", 0o755)
        tar_add(tf, "cmd/upgrade_callback", b"#!/bin/bash\nexit 0\n", 0o755)
        tar_add(tf, "cmd/upgrade_init", b"#!/bin/bash\nexit 0\n", 0o755)
        tar_add(tf, "config", isdir=True)
        tar_add(tf, "config/privilege", PRIVILEGE.encode("utf-8"))
        tar_add(tf, "config/resource", (RESOURCE % subst).encode("utf-8"))
        tar_add(tf, "ICON.PNG", icons[128])
        tar_add(tf, "ICON_256.PNG", icons[256])
        tar_add(tf, "manifest", manifest.encode("utf-8"))
        tar_add(tf, "ui", isdir=True)
        tar_add(tf, "ui/config", ui_conf.encode("utf-8"))
        tar_add(tf, "ui/images", isdir=True)
        for s in (64, 128, 256):
            tar_add(tf, "ui/images/%d.png" % s, icons[s])

    fpk = gz(fpk_tar.getvalue())
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "%s_%s_%s.fpk" % (APPNAME, version, arch))
    with open(out, "wb") as f:
        f.write(fpk)
    # 附带 sha256，供内置更新校验
    with open(out + ".sha256", "w", encoding="utf-8") as f:
        f.write("%s  %s\n" % (hashlib.sha256(fpk).hexdigest(),
                              os.path.basename(out)))
    return out, version, len(fpk), md5


def main():
    args = sys.argv[1:]
    arches = ["x86"]
    if "--arm" in args:
        arches = ["arm"]
    if "--both" in args:
        arches = ["x86", "arm"]
    out_dir = os.path.join(ROOT, "dist")
    if "--out" in args:
        out_dir = args[args.index("--out") + 1]

    for a in arches:
        out, ver, size, md5 = build(a, out_dir)
        print("built  %-46s %7.1f KB  v%s  app.tgz md5=%s"
              % (os.path.basename(out), size / 1024.0, ver, md5))
    print("output dir: %s" % out_dir)


if __name__ == "__main__":
    main()
