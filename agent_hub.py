#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent_hub.py —— ZOps 网关: serial_debug 多实例聚合(多板子 × 一个 AI 入口)。

背景: serial_debug.py 一个进程只管一个串口, agent_http.py 的 API 面固定单 Tab;
要同时看多块板子, 只能一个实例配一个 MCP 入口, 板子多了入口爆炸。本网关把
127.0.0.1 上所有 serial_debug 实例自动发现并聚合成 QSerial 风格的多 Tab API,
mcp_bridge.py 不用改, ZCode 只配一个 MCP 入口就能看所有板子:

    ZCode --MCP--> mcp_bridge.py --port 8300 --> agent_hub.py(本文件)
        |-- 8321 serial_debug(ttyUSB0)   人看终端1
        |-- 8322 serial_debug(ttyUSB1)   人看终端2
        `-- ... 扫描区间内所有实例

设计约定:
    * Tab 序号 = 扫描到的实例按 agent 端口升序排列的位置(每次请求实时解析,
      实例退出后序号会前移, AI 用前先 list_tabs 即可, 每项带 agent_port 可定位);
    * 响应包络与 agent_http.py 完全一致({"ok":..,"data"/"error"}), 桥零适配;
    * 网关无状态不缓存: 每次 list/status 现场并行探测, 实例上下线立即反映;
    * 不代开进程: POST /api/tabs 回 409 并附启动命令提示(实例必须活在终端/
      tmux 里, 人要看日志、要能手工键入, headless 没有意义);
    * X-Agent-Token 透传: 实例若设了令牌, 调用方带对应令牌经网关原样转发。

用法:
    python3 agent_hub.py                       # 监听 8300, 扫描 8321..8400
    python3 agent_hub.py --listen 8301 --scan-min 8400 --scan-max 8500
    python3 agent_hub.py --token <令牌>        # 网关自身要求令牌

常驻运行(nohup, 日志进 logs/):
    nohup python3 agent_hub.py >> logs/hub.log 2>&1 &
"""

import argparse
import http.client
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HUB_VERSION = "3.11"
MAX_BODY_BYTES = 1024 * 1024          # 与 agent_http.py 同限, 转发体不做放大
PROBE_WORKERS = 16                    # 扫描区间并行探测线程数
INSTANCE_NAME = "serial_debug"        # /api/status 名字非此者不收编(避免误收别的 HTTP 服务)
HINT_SCAN_BYTES = 64 * 1024           # 被动取证每次回看账本尾部的字节数
HINT_TIMEOUT = 5.0                    # 取证请求超时秒数
AUDIT_PREVIEW = 200                   # 审计里 receive 内容预览的最大字符数
SEND_PREVIEW = 600                    # AI 发送内容预览上限(长组合命令要看得全)
AUDIT_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "logs", "hub_audit.jsonl")
# 板名映射文件按"上报的串口路径(或其 basename)"匹配, 值形如 {"name": "TQ3588", "note": "..."}
BOARDS_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "boards.json")
# 被动取证签名(只读账本, 绝不发字节): 每类取出现最多的一条, 拼成 hint 给 AI 对照
_HINT_PATTERNS = (
    ("host", re.compile(r"(?:root|admin)@([A-Za-z0-9][A-Za-z0-9_.-]{2,30})")),
    ("soc", re.compile(r"(?:Rockchip[ _]?RK|r[Kk])(35\d{2})")),
    ("dtb", re.compile(r"\b([A-Za-z0-9_-]{3,40}\.dtb)\b")),
    ("uboot", re.compile(r"(U-Boot 20\d\d[\w.+-]{2,20})")),
)
# 探测/转发要捕获的网络异常: URLError(含 HTTPError 超类)/OSError(含 socket 超时)/
# HTTPException(端口上蹲着非 HTTP 服务时 BadStatusLine 等, 它不是 OSError 子类)
_NET_ERRORS = (urllib.error.URLError, OSError, http.client.HTTPException)


class AgentApiError(Exception):
    """携带 HTTP 状态码的业务错误, 由协议层转为 JSON 错误响应(与 agent_http.py 同构)。"""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class Instance:
    """一个已被发现的 serial_debug 实例(= 聚合后的一个 Tab)。"""

    __slots__ = ("agent_port", "status")

    def __init__(self, agent_port, status):
        self.agent_port = agent_port
        self.status = status            # 其 /api/status 的 data 字段

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.agent_port}"

    def fetch_tabs(self, timeout):
        """取该实例的 Tab 表(单实例固定 1 个 Tab 0), 失败抛 URLError/OSError。"""
        return _request_json("GET", self.base_url + "/api/tabs", timeout=timeout)

    def forward(self, method, path_and_query, body, token, timeout):
        """把 /api/tabs/0/<action> 请求原样转发给实例, 返回 (状态码, 响应体字节)。"""
        request = urllib.request.Request(self.base_url + path_and_query,
                                         data=body, method=method)
        if body is not None:
            request.add_header("Content-Type", "application/json; charset=utf-8")
        if token:
            request.add_header("X-Agent-Token", token)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:   # 实例的业务错误(409/400..)原样透传
            return error.code, error.read()


class Hub:
    """发现与聚合逻辑; 协议路由在 _Handler, 状态只在本类。"""

    def __init__(self, host, listen_port, scan_min, scan_max,
                 probe_timeout=1.0, forward_timeout=30.0, token="", boards_path=None,
                 audit_path=AUDIT_DEFAULT, ignore_ports=()):
        if scan_min <= listen_port <= scan_max:
            raise SystemExit(f"[ERROR] 监听端口 {listen_port} 落在扫描区间 "
                             f"[{scan_min},{scan_max}] 内, 会自己发现自己, 请换端口")
        self.host = host
        self.listen_port = listen_port
        self.scan_min = scan_min
        self.scan_max = scan_max
        self.probe_timeout = probe_timeout
        self.forward_timeout = forward_timeout
        self.token = token or ""
        self.boards_path = boards_path
        self.ignore_ports = frozenset(ignore_ports)   # 兼容门/转发器端口, 收编会造重复Tab
        self.audit_path = audit_path
        if audit_path:
            os.makedirs(os.path.dirname(audit_path), exist_ok=True)
        self._disc_cache = None           # (monotonic, 实例列表) 短 TTL 缓存
        self._rigctl = None               # 延迟 import 的 rigctl 模块
        self._rig_out = {}                # 组合名 -> 最近一次网页动作输出
        self._rig_lock = threading.Lock() # 电源动作串行化
        self._usb_cache = {}              # 组合名 -> (monotonic, usb模式) 10s 缓存
        self._h503_cache = {}             # 组合名 -> (monotonic, proto, msg) 30s 缓存
        self._boards_cache = (None, {})   # (mtime, 映射表), mtime 变了才重读(免重启)
        self._rigs_cache = (None, {})     # (mtime, rigs.json), bind 回填后免重启即生效
        self._hint_cache = {}             # agent_port -> (capture_bytes, hint)
        self._pool = ThreadPoolExecutor(PROBE_WORKERS)
        self._log_lock = threading.Lock()
        self._audit_lock = threading.Lock()
        self._last_seen = ()            # 上次打印过的实例端口集合, 变化时记一行

    # ------------------------------------------------------------ 发现

    DISC_TTL = 1.5                    # 发现结果缓存秒数: 每请求全量扫 80 口是浏览器
                                      # 终端卡顿首因; 1.5s 内的实例增减延后一拍反映
    def discover(self):
        """并行探测扫描区间, 返回按端口升序的实例列表(带短 TTL 缓存)。"""
        now = time.monotonic()
        if self._disc_cache and now - self._disc_cache[0] < self.DISC_TTL:
            return self._disc_cache[1]
        ports = [p for p in range(self.scan_min, self.scan_max + 1)
                 if p not in self.ignore_ports]
        found = [inst for inst in self._pool.map(self._probe, ports) if inst]
        self._disc_cache = (now, found)
        self._log_change(found)
        return found

    def disc_invalidate(self):
        """转发失败时清缓存(实例刚退出), 下次请求强制重扫。"""
        self._disc_cache = None

    def _probe(self, port):
        try:
            payload = _request_json("GET", f"http://127.0.0.1:{port}/api/status",
                                    timeout=self.probe_timeout)
        except (*_NET_ERRORS, ValueError):
            return None
        data = payload.get("data") or {}
        # 认 serial_debug 的 status 面; 名字不符的 HTTP 服务(别的工具)不收编
        if payload.get("ok") and data.get("name") == INSTANCE_NAME:
            return Instance(port, data)
        return None

    def instance_at(self, index):
        """按聚合 Tab 序号取实例; 越界报 404(提示先 list_tabs 对齐序号)。"""
        found = self.discover()
        if not 0 <= index < len(found):
            hint = ",".join(f"{i.agent_port}({i.status.get('port')})" for i in found) or "无实例在线"
            raise AgentApiError(404, f"Tab {index} 不存在(当前在线: {hint}); "
                                     f"实例增减后序号会变, 请先 GET /api/tabs 对齐")
        return found[index]

    def _log_change(self, found):
        """实例集合变化时向 stdout 记一行(nohup 重定向即为网关日志)。"""
        now = tuple(i.agent_port for i in found)
        with self._log_lock:
            if now != self._last_seen:
                self._last_seen = now
                detail = ", ".join(f"{i.agent_port}->{i.status.get('port')}" for i in found)
                print(f"[hub] 实例集合更新: [{detail or '空'}]", flush=True)

    # ------------------------------------------------------------ 聚合 API

    def status(self):
        found = self.discover()
        return {"name": "zops", "version": HUB_VERSION,
                "agent_for": f"{INSTANCE_NAME} 1.1+agent",
                "listen_port": self.listen_port,
                "scan_range": [self.scan_min, self.scan_max],
                "tab_count": len(found),
                "open": any(i.status.get("open") for i in found),
                "instances": [{"agent_port": i.agent_port, **{
                    k: i.status.get(k) for k in ("port", "open", "tab_count")}}
                    for i in found]}

    def list_tabs(self):
        """并行取各实例 Tab 表, 重排 index 为聚合序号并附 agent_port/板名/取证 hint。"""
        found = self.discover()
        entries = []
        for index, inst in enumerate(found):
            try:
                payload = inst.fetch_tabs(self.probe_timeout)
            except (*_NET_ERRORS, ValueError) as error:
                raise AgentApiError(502, f"实例 {inst.agent_port} 取 Tab 失败: {error}")
            if not payload.get("ok"):
                raise AgentApiError(502, f"实例 {inst.agent_port} 返回错误: "
                                         f"{payload.get('error', '未知')}")
            tabs = payload.get("data", {}).get("tabs") or []
            if not tabs:
                raise AgentApiError(502, f"实例 {inst.agent_port} 未返回 Tab")
            entry = dict(tabs[0])
            entry["index"] = index            # 单实例 Tab 0 -> 聚合序号
            entry["agent_port"] = inst.agent_port
            # ② 板名: boards.json 人工映射优先(权威, 可带备注)
            board = self._board_of(entry.get("port"))
            entry["board"] = board.get("name") if board else None
            if board and board.get("note"):
                entry["board_note"] = board["note"]
            # ③ rig 槽位: rigctl bind 回填的 console 就是「这块板是 A 还是 B」的事实源
            #    (boards.json 是物理口快照, 挪线没重登记时它认不出来; rig 绑定能)
            rig = self._rig_of(entry.get("port"))
            if rig:
                entry["rig"] = rig["rig"]          # 如 rig-A
                entry["rig_name"] = rig["name"]    # 如 3588 A
                entry["rig_console"] = rig["console"]
            # ④ 被动取证: 只读账本尾部提取签名(零字节发送)
            entry["hint"], parts = self._hint_of(inst)
            entry["board_auto"] = self._auto_name(parts)   # 未登记板自动派生板名
            entries.append(entry)
        self._finalize_titles(entries)
        return {"tabs": entries}

    def _finalize_titles(self, entries):
        """定标题: 人工名 > rig 绑定名 > 自动名; 同名撞车一律加物理插口后缀。

        boards.json 是「物理插口→板名」的快照, 挪线后对不上; rigs.json 的
        target.console 是 `rigctl bind` 实测回填的槽位身份, 所以它排第二位——
        绑定过的板子即使没重登记 boards.json 也能显示成「3588 A/B」。

        两块同型号板(如两台 TQ3588)会派生出同名, 撞车时全部加 @插口 短后缀,
        保证 Tab 标题两两不同——AI 认板子的最后防线。
        """
        names = [e.get("board") or e.get("rig_name") or e.get("board_auto")
                 for e in entries]
        duplicate = {n for n in names if n and names.count(n) > 1}
        for entry, name in zip(entries, names):
            if not name:
                continue
            entry["title"] = name if name not in duplicate else f"{name}@{self._jack_of(entry.get('port'))}"
            if entry.get("board"):
                entry["name_source"] = "boards.json"
            elif entry.get("rig_name"):
                entry["name_source"] = "rig绑定"
            else:
                entry["name_source"] = "hint自动"

    @staticmethod
    def _jack_of(port):
        """物理插口短名: by-path 提 hub 链路段(如 1.4.4), 其余取 basename(如 ttyUSB3)。"""
        m = re.search(r"usb-([0-9][\d.:]*)", port or "")
        if m:
            segs = m.group(1).split(":")
            return segs[1] if len(segs) > 1 else segs[0]
        return os.path.basename(port or "") or "?"

    # ------------------------------------------------------------ 板名映射 / 被动取证

    def _boards(self):
        """读 boards.json; mtime 没变用缓存, 改完文件即生效不用重启网关。"""
        if not self.boards_path or not os.path.isfile(self.boards_path):
            return {}
        mtime = os.stat(self.boards_path).st_mtime
        if self._boards_cache[0] != mtime:
            try:
                with open(self.boards_path, encoding="utf-8") as f:
                    mapping = json.load(f)
            except (OSError, ValueError) as e:
                print(f"[hub] boards.json 读取失败(沿用旧表): {e}", flush=True)
                mapping = self._boards_cache[1]
            self._boards_cache = (mtime, mapping if isinstance(mapping, dict) else {})
        return self._boards_cache[1]

    def _board_of(self, port):
        """按上报串口路径全串/basename 两级匹配板名条目(by-path 与 ttyUSBx 写法都认)。"""
        if not port:
            return None
        mapping = self._boards()
        entry = mapping.get(port) or mapping.get(os.path.basename(port))
        return entry if isinstance(entry, dict) else None

    def _rigs(self):
        """读 rigs.json; mtime 没变用缓存, rigctl bind 改完文件即生效不用重启。"""
        path = self._rigs_path()
        if not os.path.isfile(path):
            return {}
        mtime = os.stat(path).st_mtime
        if self._rigs_cache[0] != mtime:
            try:
                with open(path, encoding="utf-8") as f:
                    rigs = json.load(f)
            except (OSError, ValueError) as e:
                print(f"[hub] rigs.json 读取失败(沿用旧表): {e}", flush=True)
                rigs = self._rigs_cache[1]
            self._rigs_cache = (mtime, rigs if isinstance(rigs, dict) else {})
        return self._rigs_cache[1]

    def _rig_of(self, port):
        """串口路径 → 登记它的受控槽位 {"rig","name","console"}; 认不出返回 None。

        只认 target.console(受控板控制台), 不认 controller(治具控制台不是板子)。
        匹配复用 rigctl.port_matches: 链路段优先, by-path 键与 ttyUSBn 实例都认。
        """
        if not port:
            return None
        try:
            R = self._rig_mod()          # 复用 rigctl 的归一化规则, 不另造一套
            rigs = self._rigs()
        except Exception:
            return None
        for rig_name, rig in rigs.items():
            if not isinstance(rig, dict):
                continue
            target = rig.get("target") or {}
            console = target.get("console")
            if not console:
                continue
            try:
                if R.port_matches(port, console):
                    return {"rig": rig_name,
                            "name": target.get("name") or rig_name,
                            "console": console}
            except Exception:
                continue
        return None

    def _hint_of(self, inst):
        """被动取证: 回看实例账本尾部提取 host/SoC/dtb/U-Boot 签名。

        绝不发字节、不动实例读游标(offset 绝对读); 账本字数没涨就沿用上次结论,
        任何失败都返回 (None, {}) 不阻塞 Tab 列表——取证是兜底不是依赖。
        返回 (hint 串, {签名类型: 值}) 两份, 后者供自动命名用。
        """
        try:
            payload = _request_json("GET", inst.base_url + "/api/info",
                                    timeout=self.probe_timeout)
            cap = (payload.get("data") or {}).get("capture_bytes") or 0
        except (*_NET_ERRORS, ValueError):
            return None, {}
        cached = self._hint_cache.get(inst.agent_port)
        if cached and cached[0] == cap:
            return cached[1], cached[2]
        if cap <= 0:
            self._hint_cache[inst.agent_port] = (cap, None, {})
            return None, {}
        offset = max(0, cap - HINT_SCAN_BYTES)
        try:
            payload = _request_json(
                "GET",
                f"{inst.base_url}/api/tabs/0/receive?offset={offset}&max_bytes={HINT_SCAN_BYTES}",
                timeout=HINT_TIMEOUT)
            text = (payload.get("data") or {}).get("text") or ""
        except (*_NET_ERRORS, ValueError):
            return None, {}
        parts = {}
        for key, pattern in _HINT_PATTERNS:
            hits = pattern.findall(text)
            if hits:
                parts[key] = Counter(hits).most_common(1)[0][0]
        shown = {k: ("RK" + v if k == "soc" else v) for k, v in parts.items()}
        hint = " ".join(f"{k}:{v}" for k, v in shown.items()) or None
        self._hint_cache[inst.agent_port] = (cap, hint, parts)
        return hint, parts

    @staticmethod
    def _auto_name(parts):
        """从取证签名派生板名: 主机名 > SoC > dtb 名(去扩展名); 静默板返回 None 不猜。"""
        if parts.get("host"):
            return parts["host"]
        if parts.get("soc"):
            return "RK" + parts["soc"]
        if parts.get("dtb", "").endswith(".dtb"):
            return parts["dtb"][:-4]
        return None

    def proxy(self, index, action, method, query, body, token, src="agent"):
        """/api/tabs/{i}/{action} -> 实例 /api/tabs/0/{action}, 字节级透传。

        返回 (状态码, 响应体字节): 实例回的已是完整 {"ok":..} 包络(含业务错误),
        原样上送, 网关不重打包——大块 receive 不多吃一遍 JSON 序列化。
        """
        if action not in ("open", "close", "send", "receive", "info"):
            raise AgentApiError(404, f"未知操作: {action}")
        inst = self.instance_at(index)
        # 实例侧 info 挂根路径(/api/info), 其余动作带 tab 段
        path = "/api/info" if action == "info" else f"/api/tabs/0/{action}"
        if query:
            path += "?" + query
        try:
            status, payload = inst.forward(method, path, body, token, self.forward_timeout)
        except _NET_ERRORS as error:
            self.disc_invalidate()
            raise AgentApiError(502, f"实例 {inst.agent_port} 无响应(可能刚退出): {error}")
        if not payload:   # 异常对端回空体, 补一个合法错误包络
            payload = json.dumps({"ok": False, "error": f"实例 {inst.agent_port} 返回空响应"},
                                 ensure_ascii=False).encode("utf-8")
        self._audit(inst, action, body, payload, status, src)
        return status, payload

    def _rig_mod(self):
        if self._rigctl is None:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import rigctl as _R
            self._rigctl = _R
        return self._rigctl

    def _rigs_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "rigs.json")

    def _detect_safe(self, R, cmd_port, key):
        """H503 探测包一层 3 秒超时(串口口僵死时 open 可能阻塞, 不能拖死状态条)。"""
        cached = self._h503_cache.get(key)
        if cached and time.monotonic() - cached[0] < 30:
            return cached[1], cached[2]
        result = {}

        def run():
            result["r"] = R.h503_detect(cmd_port)

        th = threading.Thread(target=run, daemon=True)
        th.start()
        th.join(3.0)
        if th.is_alive():
            r = (None, "探测超时(命令口僵死/被占)")
        else:
            r = result.get("r") or (None, "无应答")
        self._h503_cache[key] = (time.monotonic(), r[0], r[1])
        return r

    def _tab_by_console(self, R, key):
        if not key:
            return None
        try:
            tabs = self.list_tabs()["tabs"]
        except AgentApiError:
            return None
        for tab in tabs:
            try:
                if R.port_matches(tab.get("port"), key):
                    return tab
            except Exception:
                continue
        return None

    def rigs_status(self):
        """组合(rig)状态聚合: 控制器/受控/USB 三项, 供网页状态条; 全程只读。"""
        R = self._rig_mod()
        rigs = R.load_rigs(self._rigs_path())
        tnow = time.monotonic()
        out = []
        for name, rig in rigs.items():
            ctrl = rig.get("controller") or {}
            tgt = rig.get("target") or {}
            p = ctrl.get("power") or {}
            entry = {"name": name, "power": {"method": p.get("method"),
                                             "channel": p.get("channel")}}
            if str(p.get("method", "")).startswith("h503"):
                port_ok = os.path.exists(ctrl.get("cmd_port") or "")
                if not port_ok:
                    entry["controller"] = {"ok": False, "detail": "命令口不存在(板没插/掉线)"}
                else:
                    proto, msg = self._detect_safe(R, ctrl.get("cmd_port"), name)
                    entry["controller"] = {
                        "ok": True,   # 口在即算在; 协议探测只是加分项(电源动作时还会再探)
                        "detail": f"{proto or '口在·协议探测被占'}({msg})"}
            else:
                tab = self._tab_by_console(R, ctrl.get("console"))
                entry["controller"] = {
                    "ok": bool(tab), "detail": f"实例{tab['agent_port']} @{tab['baud']}" if tab else "无实例",
                    "tab": tab["agent_port"] if tab else None}
            thit = self._tab_by_console(R, tgt.get("console"))
            if not thit:
                # 登记口失效(线被挪): 给出明确指引, 而不是让下游会话去借别的 tab
                entry["target_hint"] = (f"登记控制台口 {tgt.get('console')} 无实例——"
                    f"跑 `python3 rigctl.py {name} relocate` 自动重定位(会重启该板)")
            cached = self._usb_cache.get(name)
            if cached and tnow - cached[0] < 10:
                mode = cached[1]
            else:
                try:
                    mode = R.usb_mode_for(rig)
                except Exception:
                    mode = None
                self._usb_cache[name] = (tnow, mode)
            entry["target"] = {"name": tgt.get("name"), "ok": bool(thit),
                               "detail": (f"实例{thit['agent_port']}" if thit else
                                          f"无实例(登记 @{tgt.get('console')})" if tgt.get("console")
                                          else "无实例(未登记控制台)"),
                               "console": tgt.get("console"),
                               "tab": thit["agent_port"] if thit else None,
                               "usb": mode, "enter_loader_cmd": tgt.get("enter_loader_cmd")}
            entry["verified"] = rig.get("verified") or {}
            entry["last"] = self._rig_out.get(name)
            out.append(entry)
        return out

    def rigs_action(self, name, action, src="agent"):
        """网页电源动作: 后台线程执行 rigctl 动作, 输出存 _rig_out 供状态条回显。"""
        R = self._rig_mod()
        rigs = R.load_rigs(self._rigs_path())
        if name not in rigs:
            raise AgentApiError(404, f"无组合 {name!r}(现有: {', '.join(rigs)})")
        if action not in ("on", "off", "coldboot", "enter-loader", "catch-uboot"):
            raise AgentApiError(400, f"未知动作 {action!r}")

        def _audit_rig(tag, detail=""):
            """电源动作落审计账: 谁在何时对哪个组合按了什么、结果如何。

            此前电源动作不进审计(只记串口读写), 出过'用户说按了冷启动但板子
            没反应'却无从查证是否到达网关的盲区(2026-11-17 实测教训)。"""
            if not self.audit_path:
                return
            try:
                import json as _j
                rec = {"t": time.strftime("%H:%M:%S"), "board": f"rig:{name}",
                       "dev": "power", "action": action, "status": 0,
                       "src": src, "tag": tag}
                if detail:
                    rec["detail"] = detail[:200]
                with self._audit_lock:
                    with open(self.audit_path, "a", encoding="utf-8") as f:
                        f.write(_j.dumps(rec, ensure_ascii=False) + "\n")
            except Exception as e:
                print(f"[hub] 电源动作审计失败: {e}", flush=True)   # 不再静默吞

        _audit_rig("accepted")

        def worker():
            # 子进程执行 rigctl: 彻底隔离输出——redirect_stdout 是进程全局,
            # 会把别的线程(如 hub 自己的实例集合日志)的 print 一起抓进来(已实测踩到)。
            # -u: 子进程 stdout 走管道时默认块缓冲, 不加它"实时进度"会变一次性输出
            cmd = [sys.executable, "-u",
                   os.path.join(os.path.dirname(os.path.abspath(__file__)), "rigctl.py"),
                   name, action]
            lines = []
            # 执行前先确认 rigctl.py 能编译: 曾经编辑到一半时按钮被点, 子进程直接
            # SyntaxError 崩掉, 用户只看到"按钮没反应"。给个明确提示。
            rigctl_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rigctl.py")
            try:
                import py_compile as _pc
                _pc.compile(rigctl_py, doraise=True, cfile="/tmp/_zops_rigctl.pyc")
            except Exception as e:
                self._rig_out[name] = (time.strftime("%H:%M:%S ")
                                       + f"[{action}] ✘ rigctl.py 当前无法编译({e}); "
                                         f"先跑 python3 check_code.py 修复")
                return
            try:
                with self._rig_lock:
                    # 逐行读子进程输出并实时更新状态条: 冷启动有 25s 放电静默期,
                    # 攒到最后才更新会让人以为"只关了没开"(实测反馈)。
                    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, text=True, bufsize=1)
                    for line in proc.stdout or ():
                        line = line.rstrip("\n")
                        if not line:
                            continue
                        lines.append(line)
                        self._rig_out[name] = (time.strftime("%H:%M:%S ")
                                               + f"[{action}] " + " ⟶ ".join(lines[-3:]))
                    proc.wait(timeout=180)
                    if proc.returncode not in (0, None):
                        lines.append(f"[退出码 {proc.returncode}]")
            except subprocess.TimeoutExpired:
                proc.kill()
                lines.append(f"[超时] {action} 超过 180s")
            except Exception as e:
                lines.append(f"[异常] {e}")
            tail = " ⟶ ".join(lines[-3:]) if lines else "(无输出)"
            self._rig_out[name] = time.strftime("%H:%M:%S ") + f"[{action}] " + tail
            _audit_rig("done", tail)

        threading.Thread(target=worker, daemon=True).start()
        return {"started": True, "rig": name, "action": action}

    def audit_tail(self, n=30):
        """取审计账最后 n 条(供浏览器 AI 活动条); 任何失败返回空表不阻塞页面。"""
        try:
            with open(self.audit_path, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return []
        out = []
        for line in lines[-max(1, int(n)):]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def _audit(self, inst, action, body, payload, status, src="agent"):
        """AI 串口活动审计: 每次经网关的 send/receive/open/close 落一行 JSONL。

        这是"人围观 AI"的强制卡口——只要 AI 走 hub(唯一推荐路径), 它读到的串口
        内容就必然先经过这里; 人用 hub_audit_view.py 实时镜像。审计失败绝不
        影响转发(吞异常), 审计缺一条好过断一条链路。
        """
        if not self.audit_path:
            return
        try:
            dev = inst.status.get("port") or "?"
            rec = {"t": time.strftime("%H:%M:%S"),
                   "board": (self._board_of(dev) or {}).get("name") or os.path.basename(dev),
                   "dev": self._jack_of(dev), "action": action, "status": status,
                   "src": src}
            if action == "send" and body:
                req = json.loads(body)
                rec["out"] = (req.get("text") or req.get("hex") or "")[:SEND_PREVIEW]
            elif action == "receive" and payload:
                resp = (json.loads(payload) or {}).get("data") or {}
                if not resp.get("bytes"):
                    return               # 空读(长轮询无数据)不写审计, 免得每400ms刷一条
                text = resp.get("text") or ""
                rec["offset"] = resp.get("offset")
                rec["bytes"] = resp.get("bytes")
                rec["preview"] = text[-AUDIT_PREVIEW:]   # 尾部才是最新应答
            line = json.dumps(rec, ensure_ascii=False)
            with self._audit_lock:
                try:
                    if os.path.getsize(self.audit_path) > 8 * 1024 * 1024:
                        os.replace(self.audit_path, self.audit_path + ".1")  # 轮转保一份
                except OSError:
                    pass
                with open(self.audit_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:
            pass

    def refuse_create(self):
        raise AgentApiError(
            409, "hub 只聚合已启动的 serial_debug 实例, 不代开进程; 新板子请运行: "
                 "python3 serial_debug.py -p /dev/serial/by-path/<物理口路径> "
                 "(ttyUSBx 会因重插变号, by-path 稳定) -b <波特率> --agent-http "
                 f"<{self.scan_min}..{self.scan_max} 的空闲端口>, "
                 "在终端/tmux 里启动(人要看日志), 出现后自动进 Tab 列表; "
                 f"板名登记: {self.boards_path or 'boards.json'}")


def _request_json(method, url, body=None, timeout=5.0):
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Accept", "application/json")
    if body is not None:
        request.add_header("Content-Type", "application/json; charset=utf-8")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class _Handler(BaseHTTPRequestHandler):
    """网关协议层: 令牌校验、路由分发、JSON 编解码(面与 agent_http.py 保持一致)。"""

    server_version = f"SerialAgentHub/{HUB_VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, _fmt, *_args):   # 常驻网关, 静默逐请求日志(集合变化由 hub 记)
        pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        hub = self.server.hub
        try:
            if hub.token and self.headers.get("X-Agent-Token") != hub.token:
                raise AgentApiError(401, "访问令牌缺失或不匹配")
            parts = urlsplit(self.path)
            if method == "GET" and parts.path in ("/", "/ui", "/index.html"):
                self._send_bytes(200, UI_HTML.encode("utf-8"),
                                 ctype="text/html; charset=utf-8")
                return
            if method == "GET" and parts.path.startswith("/static/"):
                name = parts.path[len("/static/"):]
                if name not in ("xterm.js", "xterm.css", "fit.js"):
                    raise AgentApiError(404, "未知静态文件")
                fp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "webui", name)
                ctype = "text/css" if name.endswith(".css") else "application/javascript"
                with open(fp, "rb") as f:      # 静态库版本固定, 给长缓存省流量
                    self._send_bytes(200, f.read(), ctype=f"{ctype}; charset=utf-8",
                                     cache="public, max-age=604800")
                return
            seg = [s for s in parts.path.split("/") if s]
            status, raw, payload = self._route(hub, method, seg, parts.query)
            if raw:   # 实例回的完整包络, 字节级透传
                self._send_bytes(status, payload)
            else:
                self._send_json(status, {"ok": True, "data": payload})
        except AgentApiError as e:
            self._send_json(e.status, {"ok": False, "error": e.message})
        except Exception as e:   # 网关自身意外也回 JSON, 不给客户端挂起
            self._send_json(500, {"ok": False, "error": f"网关内部错误: {e}"})

    def _route(self, hub, method, seg, query):
        """返回 (状态码, 是否原始包络, 数据对象或字节)。"""
        if len(seg) < 2 or seg[0] != "api":
            raise AgentApiError(404, "未知路径")
        if seg[1] == "status" and len(seg) == 2:
            self._need(method, "GET")
            return 200, False, hub.status()
        if seg[1] == "ports" and len(seg) == 2:
            self._need(method, "GET")
            return 200, False, {"ports": _list_serial_ports()}
        if seg[1] == "info" and len(seg) == 2:
            self._need(method, "GET")
            return 200, False, hub.status()      # 网关无自有账本, info 即聚合状态
        if seg[1] == "tabs":
            return self._route_tabs(hub, method, seg, query)
        if seg[1] == "rigs" and len(seg) == 2:
            self._need(method, "GET")
            return 200, False, {"rigs": hub.rigs_status()}
        if seg[1] == "rigs" and len(seg) == 4:
            self._need(method, "POST")
            return 200, False, hub.rigs_action(
                seg[2], seg[3], src=self.headers.get("X-Hub-Src", "agent"))
        if seg[1] == "audit" and len(seg) == 2:
            self._need(method, "GET")
            n = int(parse_qs(query).get("n", ["30"])[0])   # query 是原始串(proxy 要原样转发)
            return 200, False, {"records": hub.audit_tail(max(1, min(n, 1000)))}
        raise AgentApiError(404, "未知路径")

    def _route_tabs(self, hub, method, seg, query):
        if len(seg) == 2:
            if method == "GET":
                return 200, False, hub.list_tabs()
            if method == "POST":
                self._read_body()
                hub.refuse_create()      # 必抛 409(附新板子上线方法)
            raise AgentApiError(405, "仅支持 GET/POST")
        if len(seg) != 4 or not seg[2].isdigit():
            raise AgentApiError(404, "路径应为 /api/tabs/{index}/{action}")
        body = self._read_body() if method == "POST" else None
        token = self.headers.get("X-Agent-Token", "")
        src = self.headers.get("X-Hub-Src", "agent")
        status, payload = hub.proxy(int(seg[2]), seg[3], method, query, body, token, src)
        return status, True, payload

    @staticmethod
    def _need(method, want):
        if method != want:
            raise AgentApiError(405, f"仅支持 {want}")

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            raise AgentApiError(400, f"请求体应为 0..{MAX_BODY_BYTES} 字节")
        return self.rfile.read(length) if length else b""

    def _send_json(self, status, payload):
        self._send_bytes(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _send_bytes(self, status, data, ctype="application/json; charset=utf-8",
                    cache="no-store"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache)   # 页面/接口 no-store, 静态库长缓存
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# 浏览器控制台: 按板切换 + 实时串口流 + 网页直接发命令 + AI 活动条。
# 增量拉流用绝对 offset 读(不消费实例 cursor, 不干扰 AI 的 tail 读); 零外部依赖。
UI_HTML = r"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>串口终端 · serial_hub</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/xterm.css">
<style>
 html,body{height:100%;margin:0;overflow:hidden}
 body{font:13px Consolas,monospace;background:#0b0e12;color:#cdd6e0;
   display:flex;flex-direction:column}
 #bar{display:flex;gap:6px;padding:7px 10px;background:#151a20;flex-wrap:wrap;align-items:center}
 .tab{border:1px solid #3b4757;background:#1d242c;color:#cdd6e0;border-radius:6px;
   padding:4px 12px;cursor:pointer;font:13px Consolas,monospace}
 .tab.on{background:#2f6fed;border-color:#2f6fed;color:#fff}
 #st{margin-left:auto;color:#7f8c9b;font-size:12px;max-width:42%;text-align:right}
 #term{flex:1;min-height:0;padding:4px 8px;overflow:hidden}
 #rigs{display:flex;flex-direction:column;gap:3px;padding:5px 10px;
   background:#12161c;border-bottom:1px solid #232b34;font-size:12px;
   flex-shrink:0;cursor:default}
 #rigs.folded .rig{display:none}
 #rigs.folded{padding:2px 10px}
 #rigs.folded::after{content:"rig 状态条已收起(双击展开)";color:#4a5568;font-size:11px}
 .rig{display:flex;gap:8px;align-items:center;white-space:nowrap;
   overflow-x:auto;scrollbar-width:thin;scrollbar-color:#3b4757 transparent}
 .rig::-webkit-scrollbar{height:4px}
 .rig::-webkit-scrollbar-thumb{background:#3b4757;border-radius:2px}
 .rig .chip,.rig .rbtn,.rig b{flex-shrink:0}
 .chip{border-radius:5px;padding:1px 7px;background:#1d242c;border:1px solid #3b4757}
 .chip.ok{color:#7ee787;border-color:#2ea043}
 .chip.bad{color:#ff7b72;border-color:#c93c37}
 .chip.warn{color:#d29922}
 .chip.jump{cursor:pointer}
 .rbtn{background:#232b34;border:1px solid #3b4757;color:#cdd6e0;border-radius:5px;
   padding:1px 9px;cursor:pointer;font:12px Consolas,monospace}
 .rbtn:hover{background:#2f6fed;color:#fff}
 .last{color:#7f8c9b;font-size:11px;overflow:hidden;text-overflow:ellipsis}
 #ai{position:fixed;right:10px;bottom:10px;width:430px;max-width:45vw;max-height:170px;
   overflow:auto;background:rgba(21,26,32,.94);border:1px solid #3b4757;border-radius:8px;
   padding:6px 10px;font:11px/1.6 Consolas,monospace;color:#8fd18f;white-space:pre-wrap;
   pointer-events:none;z-index:5}
</style></head><body>
<div id="bar"><span style="color:#7f8c9b;font-size:12px">板子:</span>
 <span id="tabs" style="display:contents"></span><span id="st"></span>
 <button class="rbtn" onclick="openPasteBox()" title="打开粘贴框(保底入口)">📋粘贴</button>
 <span style="color:#4a5568;font-size:11px;margin-left:8px">hub v3.4</span></div>
<div id="rigs">rig 状态条加载中…</div>
<div id="term"></div>
<textarea id="pbox" placeholder="在这里按 Ctrl+V 粘贴, 然后点下方发送(多行按安全粘贴规则发给当前板)"
 style="display:none;position:fixed;z-index:10;left:50%;top:30%;transform:translate(-50%,-50%);
 width:min(560px,80vw);height:200px;background:#151a20;color:#e6edf3;border:1px solid #2f6fed;
 border-radius:8px;padding:12px;font:13px Consolas,monospace"></textarea>
<div id="pbtns" style="display:none;position:fixed;z-index:10;left:50%;top:calc(30% + 120px);
 transform:translateX(-50%)">
 <button class="rbtn" style="background:#2f6fed;color:#fff;border:0;padding:8px 22px"
  onclick="sendPasteBox()">发送到板子</button>
 <button class="rbtn" style="margin-left:10px;padding:8px 16px" onclick="closePasteBox()">取消</button>
</div>
<div id="lock" style="display:none;position:fixed;left:50%;transform:translateX(-50%);
 bottom:14px;background:#2d2a1e;border:1px solid #d29922;color:#d8b466;border-radius:7px;
 padding:5px 14px;font:12px Consolas,monospace;cursor:pointer;z-index:9"></div>
<div id="ai">AI 活动条</div>
<script src="/static/xterm.js"></script>
<script src="/static/fit.js"></script>
<script>
const $=id=>document.getElementById(id);
const term=new Terminal({cursorBlink:true,fontFamily:'Consolas,"Courier New",monospace',
 fontSize:14,scrollback:5000,
 theme:{background:'#0b0e12',foreground:'#cdd6e0',cursor:'#5fd75f',
        selectionBackground:'#2f6fed55'}});
const fit=new FitAddon.FitAddon();term.loadAddon(fit);term.open($('term'));fit.fit();
addEventListener('resize',()=>fit.fit());
let tabs=[],active=null,raw=false,line='',hist=[],hi=0;const state={};
async function jget(u){const r=await fetch(u,{signal:AbortSignal.timeout(8000)});
 if(!r.ok)throw new Error('HTTP '+r.status);
 const j=await r.json();if(!j.ok)throw new Error(j.error||'错误');return j.data}
async function jpost(u,b,src){const r=await fetch(u,{method:'POST',
 signal:AbortSignal.timeout(8000),
 headers:{'Content-Type':'application/json','X-Hub-Src':src||'web'},
 body:JSON.stringify(b)});const j=await r.json();
 if(!j.ok)throw new Error(j.error||'错误');return j.data}
function note(s){$('st').textContent=s}
async function pollTabs(){try{const d=await jget('/api/tabs');tabs=d.tabs;renderTabs();
 if(active===null&&tabs.length)sel(tabs[0].index);
 note(tabs.length?('在线 '+tabs.length+' 板'):'无实例在线')}catch(e){note('hub 不可达: '+e.message)}}
let _tabsSig='';
/* 签名必须含 active: 否则 sel() 只改 active 不重建 DOM, 选中标签永远不高亮
   (2026-11-21 实测: 点 tab 后按 Enter 有反应, 但蓝色高亮停在上一个) */
function renderTabs(){const sig=JSON.stringify([active,tabs.map(t=>[t.index,t.title,t.open])]);
 if(sig===_tabsSig)return;_tabsSig=sig;
 const bar=$('tabs');bar.textContent='';
 for(const t of tabs){const b=document.createElement('button');
  b.className='tab'+(t.index===active?' on':'');
  b.textContent=(t.open?'● ':'○ ')+t.title;
  b.title=[t.title,'来源: '+(t.name_source||'未知'),
           t.rig?('槽位: '+t.rig_name+' ('+t.rig+')'):'',
           t.port||'',(t.hint||'').slice(0,300)].filter(Boolean).join('\n');
  b.onclick=()=>sel(t.index);bar.appendChild(b)}}
async function sel(i){active=i;const s=state[i]||(state[i]={off:null,buf:'',raw:false,hist:[],hi:0});
 raw=s.raw;line='';renderTabs();
 term.reset();
 if(s.buf)term.write(s.buf);
 try{const info=await jget(`/api/tabs/${i}/info`);s.raw=!!info.raw_send}catch(e){s.raw=false}
 raw=s.raw;note((s.raw?'键击直通':'行模式(旧实例)')+' · '+note2());
 if(s.off===null){try{const d=await jget(`/api/tabs/${i}/receive?tail=1&max_bytes=4000`);
   if(d.text){s.buf+=d.text;term.write(d.text)}s.off=d.next_offset}
  catch(e){term.write('\r\n\x1b[31m(读取失败: '+e.message+')\x1b[0m')}}
 term.focus()}
function note2(){const t=tabs.find(x=>x.index===active);
 return t?(t.title+(t.open?'':' (串口未开)')):''}
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
let errStreak=0;
let follow=true;          // 跟随滚动: 选中文字/上翻时暂停, 防洪水把选区冲走
function emit(tab,txt){  // 唯一写屏入口: 冻结时只积压, 恢复时补写(账本照收不丢)
 const s=state[tab]; if(!s)return;
 if(follow&&active===tab){term.write(txt);setFollowChip()}
 else{s.pending=(s.pending||'')+txt;
  if(s.pending.length>600000)s.pending=s.pending.slice(-400000);
  setFollowChip()}}
function resumeFollow(){ if(follow)return; follow=true;
 for(const k in state){const s=state[k];
  if(s&&s.pending){let t=s.pending;s.pending='';
   if(t.length>250000){term.write('\x1b[90m⟦…冻结期积压已截断, 完整内容在磁盘账本⟧\x1b[0m\r\n');t=t.slice(-250000)}
   if(k==active)term.write(t)}}
 term.scrollToBottom();setFollowChip();term.focus()}
let _chipSig='';
function setFollowChip(){               // 每串数据都会调——签名不变时零 DOM 操作(防渲染抖动)
 const n=(state[active]&&state[active].pending)?state[active].pending.length:0;
 const sig=follow+'|'+(follow?0:n);
 if(sig===_chipSig)return;_chipSig=sig;
 $('lock').style.display=follow?'none':'block';
 if(!follow)$('lock').textContent='⏸ 已暂停跟随(选择中'+(n?',积压'+n+'B':'')+') — Esc/点此恢复'}
async function streamLoop(){while(true){
 if(active===null||document.hidden){await sleep(300);continue}
 const tab=active,s=state[tab];          // 固定本轮所属 tab, 防切页后写错画面
 if(!s){await sleep(200);continue}
 const show=()=>{if(active===tab)return true;return false};
 if(s.off===null){   // 初次/失配重同步: 实例重启会清账本, 旧 offset 必失配
  try{const d=await jget(`/api/tabs/${tab}/receive?tail=1&max_bytes=4000`);
   if(d.bytes>0){const mark='\r\n\x1b[90m⟦串口通道已重连, 已同步到最新⟧\x1b[0m\r\n';
    s.buf+=mark.replace(/\x1b\[[0-9;?]*[A-Za-z]/g,'')+d.text;
    if(show())emit(tab,mark+d.text);
    if(s.buf.length>200000)s.buf=s.buf.slice(-150000)}
   s.off=d.next_offset;s.err=0}
  catch(e){await sleep(800)}
  continue}
 const t0=Date.now();
 try{const d=await jget(`/api/tabs/${tab}/receive?offset=${s.off}&max_bytes=16384&wait_ms=400`);
  if(d.bytes>0){s.buf+=d.text;
   if(/\x1b\[\?2004h/.test(d.text))s.brl=true;      // 板端 shell 开了 bracketed-paste
   if(/\x1b\[\?2004l/.test(d.text))s.brl=false;
   if(s.buf.length>200000)s.buf=s.buf.slice(-150000);
   if(show())emit(tab,d.text)}      // 只画当前页; 别的 tab 数据留在 buf, 切回时重绘
  s.off=d.next_offset;s.err=0;
  if(Date.now()-t0<100&&d.bytes===0)await sleep(150)   // 旧实例不认 wait_ms, 防空转
 }catch(e){
  s.err=(s.err||0)+1;                 // 按 tab 计数(原全局变量会跨 tab 累积)
  if(s.err>=3||/超出|已落盘/.test(e.message||'')){s.off=null;s.err=0}
  await sleep(500)}}}
let chain=Promise.resolve();      // 键击串行化保顺序
let pend="",ptab=null,ptimer=null; // 25ms 微批量: 相邻键击合并成一个 POST, 省一半以上往返
function rawSend(data){if(ptab===null||ptab===active){pend+=data}else{flushPend();pend=data}
 ptab=active;if(ptimer)return;ptimer=setTimeout(flushPend,25)}
function flushPend(){ptimer=null;const chunk=pend,tab=ptab;pend="";ptab=null;
 if(!chunk||tab===null)return;chain=chain.then(()=>
  jpost(`/api/tabs/${tab}/send`,{text:chunk,nl:false}))
 .catch(e=>note('发送失败: '+e.message))}
function lineKey(d){const s=state[active];
 for(let i=0;i<d.length;i++){const ch=d[i];
  if(ch==='\x1b'){   // ESC 序列整段吞掉(旧行模式不支持透传), 方向键映射本页历史
   const m=/^\x1b(O\w|\[[0-9;]*[A-Za-z])/.exec(d.slice(i));
   if(m){const f=m[0].slice(-1);
    if(f==='A'&&s.hi>0){s.hi--;line=s.hist[s.hi]||'';term.write('\r\x1b[K'+line)}
    else if(f==='B'){s.hi=Math.min(s.hi+1,s.hist.length);line=s.hist[s.hi]||'';
      term.write('\r\x1b[K'+line)}
    i+=m[0].length-1}
   continue}
  if(ch==='\r'){const v=line;line='';term.write('\r\n');
   if(v){s.hist.push(v);s.hi=s.hist.length;
    jpost(`/api/tabs/${active}/send`,{text:v})
     .catch(e=>note('发送失败: '+e.message))}}
  else if(ch==='\x7f'){if(line){line=line.slice(0,-1);term.write('\b \b')}}
  else if(ch==='\x03'){line='';term.write('^C\r\n')}
  else if(ch>=' '||ch==='\t'){line+=ch;term.write(ch)}}}
// ---- 复制/粘贴四件套 (http 局域网无 navigator.clipboard, 用 execCommand 兜底) ----
let lastPaste='';                                   // 右键重发用(非安全上下文读不了剪贴板)
function copyText(text){
 if(navigator.clipboard&&window.isSecureContext){
  navigator.clipboard.writeText(text).then(()=>note('已复制 '+text.length+' 字'),()=>note('复制失败'));
  return}
 const ta=document.createElement('textarea');ta.value=text;
 ta.style.cssText='position:fixed;opacity:0;top:0;left:0';
 document.body.appendChild(ta);ta.focus();ta.select();
 try{document.execCommand('copy');note('已复制 '+text.length+' 字')}
 catch(e){note('复制失败: 浏览器拦截, 请右键手动复制')}
 ta.remove();term.focus()}
async function waitShellQuiet(tab,timeout=9000){
 // 等板端 shell 提示符回来再发下一行——上一条命令的回显/ioctl 还在刷时灌下一行,
 // 行会互相穿插断裂(实测: for 循环被撕成 done4:/sh:done4)。超时则照发(带警告)。
 const s=state[tab];const t0=Date.now();
 while(Date.now()-t0<timeout){
  await sleep(200);
  const tail=((s&&s.buf)||'').slice(-300).replace(/\x1b\[[0-9;?]*[A-Za-z]/g,'');
  if(/(^|[\r\n])[^\r\n]{0,80}[#$>]\s*$/.test(tail))return true}
 return false}

async function smartPaste(text){
 if(active===null||!text)return;
 lastPaste=text;
 const s=state[active];
 text=text.replace(/\r\n/g,'\n').replace(/\r/g,'\n');
 let lines=text.split('\n');                       // 必须 let: 语义合并段会整体重赋值
                                                   // (const 时 lines=merged 抛 TypeError 且被
                                                   //  async 静默吞掉, 表现为"粘贴无任何反应")
 if(lines.length===1){                              // 单行: 原样进板端行编辑, 不自动执行
  if(raw){
   // 长行分片: 整块灌 1.5M 时板端 busybox 回显跟不上会丢字符/劈字(实测首行丢 f、中文断裂)
   for(let j=0;j<text.length;j+=48){rawSend(text.slice(j,j+48));await sleep(12)}
   note('已粘贴单行(未执行, 回车才生效)')}
  else{for(const ch of text)lineKey(ch)}
  return}
 // 多行: 板端支持 bracketed-paste(bash 提示符带 2004h)则整块包裹, 不然逐行限速
 if(raw&&s&&s.brl){
  if(text.length>4096&&!confirm('粘贴 '+text.length+' 字到板子, 确认?'))return;
  rawSend('\x1b[2004~'+text+'\x1b[2014~');
  note('已粘贴 '+lines.length+' 行(bracketed, 整块进板端, 回车才执行)');return}
 if(lines.length>6||text.length>2048){
  if(!confirm('将逐行发送 '+lines.length+' 行到板子(每行都会执行), 确认?'))return}
 // 多行文本先做**语义合并**: 把 for/if/while 与其 do/done、续行反斜杠等粘回单行,
 // 否则逐行发给非续行模式的 shell(如 busybox ash)会把 for...do 单独执行报语法错
 // (实测: 'for..;do' 单独一行 → syntax error near unexpected token)
 let merged=[],buf='';
 for(const raw2 of lines){
  const t=raw2.trim();
  if(buf){buf+=( /[;&&|{(\[]\s*$/.test(buf)?' ':'; ')+raw2.trim();   // 上一段已带 ; & | ( 等分隔符则空格, 否则补 '; '
   if(/\b(done|fi|esac)\s*$/.test(buf)){merged.push(buf);buf=''}
   continue}
  if(/\b(do|then|else)\s*$/.test(t)||/&&\s*$/.test(t)||/\\\s*$/.test(t)||/[({]\s*$/.test(t)){
   buf=t;continue}
  merged.push(t)}
 if(buf)merged.push(buf);
 text=merged.join('\n');lines=merged;
 note('合并为 '+merged.length+' 条语义语句, 逐条执行(每条等提示符)…');
 for(let i=0;i<lines.length;i++){
  const ln=lines[i];
  if(raw){
   for(let j=0;j<ln.length;j+=48){rawSend(ln.slice(j,j+48));await sleep(12)}
   rawSend('\r');
   const ok=await waitShellQuiet(active,15000);   // 复合语句(如 for 循环带 sleep)耗时长, 等待放宽
   if(!ok)note('⚠ 第'+(i+1)+'条 15s 未见到提示符, 继续下一条')}
  else{lineKey('\r')}                              // 行模式旧实例: 只喂回车示意
 }
 note('粘贴完成('+merged.length+' 条语句)')}
term.attachCustomKeyEventHandler(ev=>{
 if(ev.type!=='keydown')return true;
 const sel=()=>term.getSelection();
 if(ev.key==='c'&&ev.ctrlKey&&!ev.shiftKey){        // Ctrl+C: 有选区=复制, 无=中断板子
  if(sel()){copyText(sel());ev.preventDefault();return false}
  return true}
 if(ev.key==='C'&&ev.ctrlKey&&ev.shiftKey){         // Ctrl+Shift+C: 无条件复制
  if(sel()){copyText(sel())}ev.preventDefault();return false}
 if(ev.key==='Escape'){                            // Esc: 丢选区+恢复跟随
  term.clearSelection();resumeFollow();return true}
 return true});
term.element.addEventListener('mousedown',()=>{follow=false});   // 开始选择即冻结
term.element.addEventListener('mouseup',()=>{setTimeout(()=>{
 if(!term.getSelection()&&follow===false&&!selectHold)resumeFollow()},60)});
let selectHold=false;
term.onSelectionChange(()=>{selectHold=!!term.getSelection();setFollowChip()});
async function directPaste(){                     // 安全上下文(https/localhost)才能读剪贴板
 if(navigator.clipboard&&window.isSecureContext){
  try{const t=await navigator.clipboard.readText();
   if(t){smartPaste(t);return}}catch(e){}}
 openPasteBox()}                                 // http 环境读不了 → 弹框保底
term.element.addEventListener('contextmenu',ev=>{    // 右键: 有选区=复制, 无=直接粘贴
 ev.preventDefault();
 const t=term.getSelection();
 if(t){copyText(t);term.clearSelection();return}
 directPaste()});
term.element.addEventListener('auxclick',ev=>{       // 中键: 直接粘贴(https 下生效)
 if(ev.button===1){ev.preventDefault();directPaste()}});
// 粘贴拦截必须在 document 捕获阶段: xterm 的 handlePasteEvent 会对 paste 事件
// stopPropagation(), 挂在 window/bubble 层的监听永远收不到(v3.3 因此整体失效,
// 2026-11-19 实测教训)。捕获先到 → preventDefault+stopPropagation 后 xterm 的
// 原始直通路径不再执行, smartPaste 全权处理。
document.addEventListener('paste',ev=>{
 if(ev.target!==term.textarea)return;   // 粘贴框等原生粘贴放行
 const t=ev.clipboardData&&ev.clipboardData.getData('text/plain');
 if(!t)return;
 ev.preventDefault();ev.stopPropagation();
 smartPaste(t)},true);
term.onData(d=>{if(active===null)return;
 if(raw){rawSend(d)}else{lineKey(d)}});
const TL=[   // AI 命令人话翻译规则(首条命中即用, $1..$9=正则分组); 想加词在这补一行
 [/reboot|shutdown|halt|poweroff/i,"重启/关机 ⚠"],
 [/rm\s+(-[a-zA-Z]+\s+)*-r/i,"递归删除 ⚠"],
 [/dd\s+if=/i,"裸设备读写 ⚠"],
 [/upgrade_tool|rkdeveloptool|fastboot|flashcp/i,"烧录/刷机 ⚠"],
 [/mkfs/i,"格式化 ⚠"],
 [/for\s+g\s+in[\s\S]*gpio/,"批量写继电器GPIO(控受控板电源)"],
 [/gpio(196|197|388|389)/,"继电器GPIO$1(RIG-A控电)"],
 [/relay_test\.sh\s+(on|off)/,"继电器全$1(控受控板电源)"],
 [/\/sys\/class\/gpio/,"操作GPIO"],
 [/thermal_zone\d+\/temp/,"读SoC温度"],
 [/dmesg/i,"看内核日志"],
 [/journalctl/,"看系统日志"],
 [/systemctl\s+restart\s+(\S+)/,"重启服务 $1"],
 [/systemctl\s+(stop|start|enable|disable)\s+(\S+)/,"$1 服务 $2"],
 [/systemctl\s+status\s+(\S+)/,"看服务 $1 状态"],
 [/(insmod|modprobe)\s+(\S+)/,"加载模块 $2"],
 [/rmmod\s+(\S+)/,"卸载模块 $1"],
 [/lsusb/,"枚举USB设备"],
 [/\badb\b/,"adb调试"],
 [/apt-get|apt\s+install|pip3?\s+install/,"装软件包"],
 [/devmem/,"直读写物理寄存器"],
 [/i2c(det|get|set)/,"I2C $1"],
 [/tail\s+-f/,"跟踪日志"],
 [/cat\s+([^\s|;]+)/,"读 $1"],
 [/echo\s+[^>]*>\s*([^\s]+)/,"写 $1"],
 [/ls\b/,"列目录"],
 [/\bps\b|\btop\b|\bfree\b/,"看进程/内存"],
 [/\bdf\b|\bdu\b/,"看磁盘"],
 [/ping/,"网络连通测试"],
 [/ip\s+addr|ifconfig/,"看网卡"],
 [/ethtool/,"看网口"],
 [/sync/,"刷盘"],
];
const TL_ALL=[   // 整条命令优先规则(拆段前全文匹配, 复合命令的语义靠它不碎)
 [/for\s+\w+\s+in[\s\S]*\/sys\/class\/gpio[\s\S]*\/value/,"批量写继电器GPIO(控受控板电源)"],
];
function tl(cmd){cmd=cmd||"";
 for(const[re,t]of TL_ALL){const m=cmd.match(re);
  if(m)return t.replace(/\$(\d)/g,(_,i)=>m[i]||"")}
 const all=cmd.split(/[;\n]/).map(s=>s.trim()).filter(Boolean);
 const segs=all.slice(0,4);
 const g1=p=>{for(const[re,t]of TL){const m=p.match(re);
   if(m)return t.replace(/\$(\d)/g,(_,i)=>m[i]||"")}return null};
 const out=segs.map(seg=>{const parts=seg.split("|").map(s=>s.trim());
  let g=g1(parts[0])||(parts.length>1&&g1(parts[parts.length-1]))||"执行shell";
  if(parts.length>1)g+="(经管道)";return g});
 let res=out.join(" → ");
 if(all.length>4)res+=" → …(共"+all.length+"段)";
 return res}

let _rigsSig='';
async function pollRigs(){try{const d=await jget('/api/rigs');
 const sig=JSON.stringify(d.rigs);if(sig===_rigsSig)return;_rigsSig=sig;
 const box=$('rigs');box.textContent='';
 for(const r of d.rigs){
  const row=document.createElement('div');row.className='rig';
  const nm=document.createElement('b');nm.textContent=r.name;row.appendChild(nm);
  const mk=(label,o,jumpable)=>{const s=document.createElement('span');
   s.className='chip '+(o&&o.ok?'ok':'bad');
   s.textContent=label+': '+(o?(o.ok?'✔ '+o.detail:'✘ '+(o.detail||'')):'—');
   if(jumpable&&o&&o.tab!==null&&o.tab!==undefined){s.classList.add('jump');
    s.title='点击切到该串口';s.onclick=()=>sel(o.tab)}
   row.appendChild(s)};
  mk('控制器',r.controller);mk('受控['+(r.target.name||'?')+']',r.target,true);
  const u=document.createElement('span');
  u.className='chip '+(r.target.usb?'ok':'warn');
  u.textContent='USB: '+(r.target.usb||'未见');row.appendChild(u);
  const pw=document.createElement('span');pw.className='chip warn';
  pw.textContent='电源:'+(r.power.method||'?')+(r.power.channel?('→'+r.power.channel):'(未绑定)');
  row.appendChild(pw);
  const acts=document.createElement('span');
  const actDefs=[['on','开机',null],['off','关机',r.name+': 真断电, 确认?'],
   ['coldboot','冷启动',r.name+': 冷启动=真断电→放电→上电, 确认?']];
  if(r.target.enter_loader_cmd)
   actDefs.push(['enter-loader','进Loader',r.name+': 下发 '+r.target.enter_loader_cmd+' 进 Loader 模式?']);
  actDefs.push(['catch-uboot','救砖进Loader',r.name+': 冷启动后抢 U-Boot 菜单并进 Loader(内核已 panic/卡死时用, 会断电重启, 约30秒), 确认?']);
  const ver=r.verified||{};
  for(const[a,lbl,cfm]of actDefs){
   const v=ver[a];
   const mark=v?(v.ok?' ✓':' ✗'):' ⚠';
   const b=document.createElement('button');b.className='rbtn';
   b.textContent=lbl+mark;
   b.title=v?(v.at+' 验证: '+(v.evidence||'')+(v.ok?'':' (未通过)'))
             :'未验证 —— 跑 rigctl '+r.name+' verify 后此处会标 ✓';
   if(v&&!v.ok)b.style.borderColor='#c93c37';
   if(!v)b.style.opacity='0.75';
   b.onclick=async()=>{if(cfm&&!confirm(cfm))return;
    try{await jpost(`/api/rigs/${r.name}/${a}`,{});
     note(r.name+' '+lbl+' 已下发(结果见状态条末行)')}
    catch(e){note('失败: '+e.message)}};
   acts.appendChild(b)}
  row.appendChild(acts);
  if(r.last){const l=document.createElement('span');l.className='last';l.textContent='↳ '+r.last;
   row.appendChild(l)}
  box.appendChild(row)}}catch(e){$('rigs').textContent='rig 状态获取失败: '+e.message}}

function openPasteBox(){                        // 保底粘贴入口: 真 textarea, 任何浏览器都能 Ctrl+V 进去
 const b=$('pbox');b.style.display='block';b.value='';b.focus();
 $('pbtns').style.display='block'}
function closePasteBox(){$('pbox').style.display='none';$('pbtns').style.display='none';term.focus()}
async function sendPasteBox(){
 const v=$('pbox').value;if(!v)return;
 closePasteBox();await smartPaste(v)}

function devOf(t){const m=(t||'').match(/usb-([\d.:]+)/);
 if(m){const g=m[1].split(':');return g.length>1?g[1]:g[0]}
 const b=(t||'').split('/').pop();return /^tty/.test(b)?b:null}
let _aiSig='';
async function pollAI(){try{const d=await jget('/api/audit?n=30');
 const sig=JSON.stringify((d.records||[]).map(r=>[r.t,r.board,r.action,r.src]));
 if(sig===_aiSig)return;_aiSig=sig;
 for(const r of (d.records||[])){
  if(r.action!=='send'||r.src==='web')continue;      // 网页自己的不标, 只标 AI
  if(pollAI.seen===undefined)pollAI.seen=[];
  const key=r.t+'|'+(r.out||'');if(pollAI.seen.includes(key))continue;
  pollAI.seen.push(key);if(pollAI.seen.length>400)pollAI.seen=pollAI.seen.slice(-200);
  if(active===null)continue;const t=tabs.find(x=>x.index===active);
  if(!t||devOf(t.port)!==r.dev)continue;
  const s=state[active];if(!s)continue;
  const fmt=(r.out||'').replace(/; /g,';\r\n    ');  // 长组合命令按 ; 折行解析
  const g=tl(r.out||'');const gc=/⚠/.test(g)?'\x1b[91m':'\x1b[90m';
  const plain='\r\n⟦AI➤ '+fmt+'⟧\r\n'+(g?'⟦翻译⟧ '+g+'\r\n':'');
  term.write('\r\n\x1b[33m⟦AI➤ '+fmt+'⟧\x1b[0m\r\n'+
             (g?gc+'⟦翻译⟧ '+g+'\x1b[0m\r\n':''));
  s.buf+=plain;
  if(s.buf.length>200000)s.buf=s.buf.slice(-150000);
  $('ai').textContent=($('ai').textContent+'\n'+r.t+' ['+r.board+'] AI发 '+fmt
   +(g?'  ┋ '+g.slice(0,60):'')).split('\n').slice(-8).join('\n')}}
 catch(e){}}
$('ai').ondblclick=()=>$('ai').style.display=$('ai').style.display==='none'?'block':'none';
$('rigs').ondblclick=()=>$('rigs').classList.toggle('folded');
$('lock').onclick=()=>{term.clearSelection();resumeFollow()};
let _fitT=null;
function fitSoon(){if(_fitT)return;_fitT=setTimeout(()=>{_fitT=null;try{fit.fit()}catch(e){}},150)}
// fit 直接挂 observer 会与终端 resize 互相触发形成振荡循环(每轮全量重排=持续卡顿),
// 去抖合并到 150ms 一帧
new ResizeObserver(fitSoon).observe($('rigs'));
new ResizeObserver(fitSoon).observe($('term'));
setInterval(pollTabs,3000);setInterval(pollAI,2500);setInterval(pollRigs,5000);
streamLoop();pollTabs();pollAI();pollRigs();
</script></body></html>"""


def _list_serial_ports():
    try:
        from serial.tools import list_ports
        return [p.device for p in list_ports.comports()]
    except ImportError:
        return []            # 网关主机没装 pyserial 时不阻塞 status/tabs, 只少了枚举


def main():
    parser = argparse.ArgumentParser(description="serial_debug 多实例聚合网关")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址, 默认 127.0.0.1")
    parser.add_argument("--listen", type=int, default=8300, metavar="PORT",
                        help="网关监听端口, 默认 8300(须在扫描区间外)")
    parser.add_argument("--scan-min", type=int, default=8321, metavar="PORT",
                        help="实例扫描起始端口(含), 默认 8321")
    parser.add_argument("--scan-max", type=int, default=8400, metavar="PORT",
                        help="实例扫描结束端口(含), 默认 8400")
    parser.add_argument("--probe-timeout", type=float, default=1.0,
                        help="实例探测超时秒数, 默认 1.0")
    parser.add_argument("--forward-timeout", type=float, default=30.0,
                        help="转发请求超时秒数(大块读取要留足), 默认 30")
    parser.add_argument("--token", default="", help="网关访问令牌, 非空时请求须带 X-Agent-Token")
    parser.add_argument("--boards", default=BOARDS_DEFAULT, metavar="FILE",
                        help=f"板名映射 JSON, 默认 {BOARDS_DEFAULT}")
    parser.add_argument("--audit", default=AUDIT_DEFAULT, metavar="FILE",
                        help=f"AI 活动审计 JSONL(空串关闭), 默认 {AUDIT_DEFAULT}")
    parser.add_argument("--tls-port", type=int, default=8443, metavar="PORT",
                        help="浏览器 HTTPS 入口端口(配合 --tls-cert), 默认 8443")
    parser.add_argument("--tls-cert", default="", metavar="FILE",
                        help="TLS 证书 PEM; 给出后启用 HTTPS 入口(HTTP API 口不变)")
    parser.add_argument("--tls-key", default="", metavar="FILE", help="TLS 私钥 PEM")
    parser.add_argument("--ignore-ports", default="8321", metavar="PORTS",
                        help="扫描时忽略的端口(逗号分隔; 兼容门/转发器收编会造重复Tab), 默认 8321")
    args = parser.parse_args()

    hub = Hub(args.host, args.listen, args.scan_min, args.scan_max,
              probe_timeout=args.probe_timeout, forward_timeout=args.forward_timeout,
              token=args.token, boards_path=args.boards, audit_path=args.audit,
              ignore_ports={int(x) for x in args.ignore_ports.split(",") if x.strip()})
    server = ThreadingHTTPServer((args.host, args.listen), _Handler)
    server.hub = hub
    server.daemon_threads = True
    if args.tls_cert:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(args.tls_cert, args.tls_key)
        tls_srv = ThreadingHTTPServer((args.host, args.tls_port), _Handler)
        tls_srv.hub = hub
        tls_srv.daemon_threads = True
        tls_srv.socket = ctx.wrap_socket(tls_srv.socket, server_side=True)
        threading.Thread(target=tls_srv.serve_forever,
                         kwargs={"poll_interval": 0.5}, daemon=True).start()
        print(f"[hub] HTTPS 入口 https://{args.host}:{args.tls_port}/ "
              f"(自签证书——浏览器首次访问点'继续前往', 之后右键/中键可直接读剪贴板粘贴)",
              flush=True)
    print(f"[hub] ZOps 网关 v{HUB_VERSION} (聚合 serial_debug 实例) 监听 "
          f"http://{args.host}:{args.listen} (扫描 {args.scan_min}..{args.scan_max})", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        hub._pool.shutdown(wait=False)
        server.server_close()


if __name__ == "__main__":
    main()
