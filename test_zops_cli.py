#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_zops_cli.py —— zops_cli.py 自包含测试: loopback 假 hub 上验证一条命令闭环。

不碰真设备、不开串口、不起真 hub: 用标准库 http.server 在 127.0.0.1 随机端口
起一个假 hub(线程里跑), 模拟 /api/status、/api/tabs、/api/tabs/<i>/send、
/api/tabs/<i>/receive(实现 wait_ms 长轮询)、/api/tabs/<i>/info、/api/rigs、
/api/rigs/<名>/on; 发送后延迟一小段时间才"吐出"应答, 用来验证 CLI 的等待逻辑。

覆盖:
    T1 等到特征 -> 退出码 0, matched/output/tab/reason/capture_consistent 字段正确;
    T1b 人读模式(不带 --json)退出码 0 且输出含命中行;
    T2 超时 -> 退出码 1, reason=timeout;
    T3 板名不存在 -> 退出码 2, stderr 列出可用板名;
    T4 传端口名(ttyUSB0/COM3)被拒 -> 退出码 2, reason=port_name_rejected;
    T5 网关不通 -> 退出码 2, reason=hub_unreachable;
    T6 默认跳过命令回显行再匹配; --count-echo 关闭跳过;
    T7 boards --json 字段(title/hint/name_source/tab/rig/flash_state) + 无 tab 的
       rig 也列出 + ZOPS_HUB 环境变量生效;
    T8 rig on 放行(退出码 0); off/coldboot/enter-loader/catch-uboot/bind 一律拒绝
       (退出码 2, reason=power_action_forbidden)。

风格参考 test_agent_http.py(check()/fails 计数, 末行打总结)。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(HERE, "zops_cli.py")

real = sys.stdout
fails = []


def check(name, cond, detail=""):
    if cond:
        real.write("  ok  %s\n" % name)
    else:
        real.write("FAIL  %s  %s\n" % (name, detail))
        fails.append(name)


# ---------------------------------------------------------------- 假 hub

class ApiErr(Exception):
    """假 hub 的业务错误(与真 hub 的 JSON 错误包络同构)。"""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class FakeBoard:
    """仿真一块板的串口账本: send 即回显命令本身, delay 秒后才吐出应答。"""

    # 命令 -> 应答; 不在表里的命令永不回应(供超时用例)
    RESPONSES = {"echo UNIQUE-7f3a": "UNIQUE-7f3a\r\n"}

    def __init__(self, title, name_source, hint, port, rig=None, delay=0.4):
        self.title = title
        self.name_source = name_source
        self.hint = hint
        self.port = port
        self.rig = rig
        self.delay = delay
        self.lock = threading.Lock()
        self.buf = ""

    def send(self, text):
        with self.lock:
            self.buf += "\r\n" + text + "\r\n"     # 回显与真串口一致
        answer = self.RESPONSES.get(text.strip())
        if answer is not None:
            threading.Timer(self.delay, self._append, args=(answer,)).start()

    def _append(self, chunk):
        with self.lock:
            self.buf += chunk

    def snapshot(self):
        with self.lock:
            return len(self.buf), self.buf


class FakeHub:
    """两块板 + 两个 rig(rig-B 无在线 tab, 验证 boards 合成清单)。"""

    def __init__(self):
        self.boards = {
            0: FakeBoard(title="3588 A", name_source="rig绑定",
                         hint="host:tq3588 soc:3588",
                         port="/dev/serial/by-path/1.4.2-port0", rig="rig-A"),
            1: FakeBoard(title="t536-console", name_source="boards.json",
                         hint="host:t536-bld dtb:t536.dtb",
                         port="/dev/serial/by-path/1.4.4-port0", rig=None,
                         delay=0.3),
        }
        self.rigs = [
            {"name": "rig-A", "power": {"method": "h503", "channel": 1},
             "controller": {"ok": True, "detail": "口在"},
             "target": {"name": "3588 A", "ok": True,
                        "console": "/dev/serial/by-path/1.4.2-port0"},
             "flash": {"state": "ready", "serial": "k34f0001",
                       "download_chain": "1.4.3", "board": "3588 A"}},
            {"name": "rig-B", "power": {"method": "h503", "channel": 2},
             "controller": {"ok": True, "detail": "口在"},
             "target": {"name": "3588 B", "ok": False, "console": None},
             "flash": {"state": "offline", "serial": "k34f0002",
                       "download_chain": None, "board": "3588 B"}},
        ]

    def tab_entry(self, i):
        board = self.boards[i]
        n, _ = board.snapshot()
        entry = {"index": i, "title": board.title, "port": board.port,
                 "open": True, "baud": 1500000, "encoding": "utf-8",
                 "rx_count": n, "tx_count": 0, "pending_bytes": 0,
                 "agent_port": 8321 + i, "name_source": board.name_source,
                 "hint": board.hint, "board_auto": None}
        if board.rig:
            entry["rig"] = board.rig
            entry["rig_name"] = board.title
            entry["rig_console"] = board.port
        return entry


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeZopsHub/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, _fmt, *_args):
        pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        try:
            data = self._route(method)
            self._send(200, {"ok": True, "data": data})
        except ApiErr as e:
            self._send(e.status, {"ok": False, "error": e.message})
        except Exception as e:   # 假 hub 任何意外也回 JSON 包络, 不挂起客户端
            self._send(500, {"ok": False, "error": "假 hub 内部错误: %s" % e})

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiErr(400, "请求体必须是 UTF-8 JSON")

    def _route(self, method):
        parts = urlsplit(self.path)
        seg = [s for s in parts.path.split("/") if s]
        query = parse_qs(parts.query)
        hub = self.server.hub
        if len(seg) < 2 or seg[0] != "api":
            raise ApiErr(404, "未知路径")
        if seg[1] == "status" and len(seg) == 2:
            return {"name": "zops", "version": "fake",
                    "tab_count": len(hub.boards)}
        if seg[1] == "tabs" and len(seg) == 2:
            return {"tabs": [hub.tab_entry(i) for i in sorted(hub.boards)]}
        if seg[1] == "tabs" and len(seg) == 4:
            idx = int(seg[2])
            if idx not in hub.boards:
                raise ApiErr(404, "Tab %s 不存在" % idx)
            board = hub.boards[idx]
            if method == "POST" and seg[3] == "send":
                body = self._read_body()
                text = body.get("text")
                if not isinstance(text, str) or not text:
                    raise ApiErr(400, "text 必须是非空字符串")
                board.send(text)
                return {"index": idx, "sent": True,
                        "bytes": len(text.encode("utf-8")) + 1}
            if method == "GET" and seg[3] == "info":
                n, _ = board.snapshot()
                return {"capture_bytes": n, "capture_consistent": True,
                        "open": True, "port": board.port,
                        "encoding": "utf-8", "rx_total": n}
            if method == "GET" and seg[3] == "receive":
                return self._receive(board, query)
            raise ApiErr(404, "未知操作")
        if seg[1] == "rigs" and len(seg) == 2:
            return {"rigs": hub.rigs}
        if seg[1] == "rigs" and len(seg) == 4 and method == "POST":
            name, action = seg[2], seg[3]
            if name not in {r["name"] for r in hub.rigs}:
                raise ApiErr(404, "无组合 %r" % name)
            if action != "on":
                raise ApiErr(400, "fake hub 只放行 on")
            return {"started": True, "rig": name, "action": "on"}
        raise ApiErr(404, "未知路径")

    def _receive(self, board, query):
        """带 offset 的长轮询: 无新数据时等到 wait_ms 上限, 数据一到立即返回。"""
        offset = int(query.get("offset", ["0"])[0] or 0)
        max_bytes = int(query.get("max_bytes", ["65536"])[0] or 65536)
        wait_ms = int(query.get("wait_ms", ["0"])[0] or 0)
        deadline = time.monotonic() + min(wait_ms, 5000) / 1000.0
        while time.monotonic() < deadline:
            n, _ = board.snapshot()
            if n > offset:
                break
            time.sleep(0.02)
        n, text = board.snapshot()
        chunk = text[offset:offset + max_bytes]
        return {"offset": offset, "next_offset": offset + len(chunk),
                "bytes": len(chunk), "skipped": 0, "text": chunk, "hex": "",
                "decode_errors": 0, "encoding": "utf-8", "cursor": offset,
                "rx_total": n, "tx_total": 0, "capture_bytes": n}

    def _send(self, status, payload):
        blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)


# ---------------------------------------------------------------- 搭台

server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
server.hub = FakeHub()
server.daemon_threads = True
threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1},
                 daemon=True).start()
HUB_URL = "http://127.0.0.1:%d" % server.server_address[1]


def get_free_port():
    """领一个空闲端口再关掉: 连它必被拒, 供网关不通用例。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def run_cli(*argv, env_extra=None, timeout=60):
    """子进程跑一次 CLI; 显式 pin 假 hub, 防止误连本机可能在跑的真网关。"""
    env = dict(os.environ)
    env.pop("ZOPS_HUB", None)
    env.pop("ZOPS_TOKEN", None)
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, CLI] + list(argv),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, env=env, cwd=HERE)


def out_json(proc):
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return {}


# ---------------------------------------------------------------- T1 等到特征
real.write("== T1/T1b 发命令等特征 ==\n")
proc = run_cli("run", "--board", "3588 A", "--send", "echo UNIQUE-7f3a",
               "--expect", "UNIQUE-7f3a", "--timeout", "8",
               "--hub", HUB_URL, "--json")
j = out_json(proc)
check("T1 等到特征退出码 0", proc.returncode == 0,
      "rc=%s stdout=%r stderr=%r" % (proc.returncode, proc.stdout[:200], proc.stderr[:200]))
check("T1 ok/matched 正确", j.get("ok") is True and j.get("matched") == "UNIQUE-7f3a",
      json.dumps(j, ensure_ascii=False)[:300])
check("T1 默认跳过回显行", j.get("matched") != "echo UNIQUE-7f3a")
check("T1 output 含回显与应答",
      "echo UNIQUE-7f3a" in (j.get("output") or "") and "UNIQUE-7f3a" in (j.get("output") or ""))
check("T1 字段齐备", j.get("board") == "3588 A" and j.get("tab") == 0
      and j.get("reason") is None and j.get("capture_consistent") is True
      and isinstance(j.get("elapsed_ms"), int) and j["elapsed_ms"] >= 300,
      json.dumps(j, ensure_ascii=False)[:300])

proc = run_cli("run", "--board", "3588 A", "--send", "echo UNIQUE-7f3a",
               "--expect", "UNIQUE", "--timeout", "8", "--hub", HUB_URL)
check("T1b 人读模式退出码 0 且含命中行",
      proc.returncode == 0 and "UNIQUE-7f3a" in proc.stdout,
      "rc=%s out=%r" % (proc.returncode, proc.stdout[:200]))

# ---------------------------------------------------------------- T2 超时
real.write("== T2 超时 ==\n")
proc = run_cli("run", "--board", "3588 A", "--send", "never-answers-1f",
               "--expect", "XNOPE", "--timeout", "1.5", "--hub", HUB_URL, "--json")
j = out_json(proc)
check("T2 超时退出码 1", proc.returncode == 1, "rc=%s" % proc.returncode)
check("T2 reason=timeout 且 matched=null",
      j.get("ok") is False and j.get("reason") == "timeout" and j.get("matched") is None,
      json.dumps(j, ensure_ascii=False)[:300])

# ---------------------------------------------------------------- T3 板名不存在
real.write("== T3 板名不存在 ==\n")
proc = run_cli("run", "--board", "ghost-board", "--send", "x",
               "--timeout", "2", "--hub", HUB_URL, "--json")
j = out_json(proc)
check("T3 板名不存在退出码 2", proc.returncode == 2 and j.get("reason") == "no_such_board",
      "rc=%s %s" % (proc.returncode, proc.stdout[:200]))
check("T3 stderr 列出可用板名",
      "3588 A" in proc.stderr and "t536-console" in proc.stderr,
      proc.stderr[:300])

# ---------------------------------------------------------------- T4 端口名被拒
real.write("== T4 端口名被拒 ==\n")
for port_name in ("ttyUSB0", "COM3", "/dev/ttyACM0"):
    proc = run_cli("run", "--board", port_name, "--send", "x",
                   "--timeout", "2", "--hub", HUB_URL, "--json")
    j = out_json(proc)
    check("T4 %s 被拒(退出码 2)" % port_name,
          proc.returncode == 2 and j.get("reason") == "port_name_rejected",
          "rc=%s %s" % (proc.returncode, proc.stdout[:200]))
check("T4 拒绝时也列出可用板名", "3588 A" in proc.stderr, proc.stderr[:300])
check("T4 提示里的命令带绝对路径(换目录照抄也能跑)",
      ('"%s" boards' % os.path.abspath(CLI)) in proc.stderr, proc.stderr[-300:])

# ---------------------------------------------------------------- T5 网关不通
real.write("== T5 网关不通 ==\n")
proc = run_cli("run", "--board", "3588 A", "--send", "x", "--timeout", "2",
               "--hub", "http://127.0.0.1:%d" % get_free_port(), "--json")
j = out_json(proc)
check("T5 网关不通退出码 2 且 reason=hub_unreachable",
      proc.returncode == 2 and j.get("reason") == "hub_unreachable",
      "rc=%s %s" % (proc.returncode, proc.stdout[:200]))

# ---------------------------------------------------------------- T6 回显开关
real.write("== T6 --count-echo ==\n")
proc = run_cli("run", "--board", "3588 A", "--send", "echo UNIQUE-7f3a",
               "--expect", "UNIQUE-7f3a", "--timeout", "8", "--count-echo",
               "--hub", HUB_URL, "--json")
j = out_json(proc)
check("T6 --count-echo 不跳回显, 命中回显行",
      proc.returncode == 0 and j.get("matched") == "echo UNIQUE-7f3a",
      "rc=%s %s" % (proc.returncode, proc.stdout[:300]))

# ---------------------------------------------------------------- T7 boards
real.write("== T7 boards --json ==\n")
proc = run_cli("boards", "--hub", HUB_URL, "--json")
j = out_json(proc)
check("T7 boards ok", proc.returncode == 0 and j.get("ok") is True,
      "rc=%s %s" % (proc.returncode, proc.stdout[:200]))
by_title = {b.get("title"): b for b in j.get("boards") or []}
b_a = by_title.get("3588 A")
check("T7 tab0 字段(title/hint/来源/rig/flash)",
      b_a is not None and b_a.get("tab") == 0 and b_a.get("rig") == "rig-A"
      and b_a.get("flash_state") == "ready" and b_a.get("name_source") == "rig绑定"
      and b_a.get("hint") and b_a.get("port"),
      json.dumps(b_a, ensure_ascii=False)[:300] if b_a else "缺 3588 A")
check("T7 板名来源 boards.json",
      by_title.get("t536-console", {}).get("name_source") == "boards.json")
b_b = by_title.get("3588 B")
check("T7 无 tab 的 rig 也列出(tab=null, flash offline)",
      b_b is not None and b_b.get("tab") is None and b_b.get("flash_state") == "offline"
      and b_b.get("rig") == "rig-B",
      json.dumps(b_b, ensure_ascii=False)[:300] if b_b else "缺 3588 B")

proc = run_cli("boards", "--json", env_extra={"ZOPS_HUB": HUB_URL}, timeout=30)
check("T7b ZOPS_HUB 环境变量生效",
      proc.returncode == 0 and out_json(proc).get("ok") is True,
      "rc=%s %r" % (proc.returncode, proc.stdout[:120]))

# ---------------------------------------------------------------- T8 rig
real.write("== T8 rig on/off ==\n")
proc = run_cli("rig", "rig-A", "on", "--hub", HUB_URL, "--json")
j = out_json(proc)
check("T8 rig on 放行(退出码 0)", proc.returncode == 0 and j.get("ok") is True,
      "rc=%s %s" % (proc.returncode, proc.stdout[:200]))
for action in ("off", "coldboot", "enter-loader", "catch-uboot", "bind"):
    proc = run_cli("rig", "rig-A", action, "--hub", HUB_URL, "--json")
    j = out_json(proc)
    check("T8 rig %s 被拒(退出码 2)" % action,
          proc.returncode == 2 and j.get("reason") == "power_action_forbidden",
          "rc=%s %s" % (proc.returncode, proc.stdout[:200]))
proc = run_cli("rig", "rig-A", "off", "--hub", HUB_URL)
check("T8 拒绝文案说明真实断电须人来做",
      "真实断电" in (proc.stdout + proc.stderr),
      (proc.stdout + proc.stderr)[:300])

# ---------------------------------------------------------------- 收尾
server.shutdown()
server.server_close()
real.write("\nZOPS CLI TEST %s  (%d 项失败)\n" % ("OK" if not fails else "FAILED", len(fails)))
sys.exit(0 if not fails else 1)
