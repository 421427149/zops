#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_topology.py —— 拓扑鲁棒性回归: 按 ZOps 的设计模型仿真插拔/挪线。

设计模型(用户 2026-11-25 明确):
    * 2 块电源控制板**固定**(治具 T536 走 GPIO / 单片机 H503 走通道), 不换口;
    * 开发板可随意插拔, 每块 = 1 个串口 + 1 个 USB 下载口; 插拔/挪线是常态。

仿真做法: 用一个假世界(World)代替硬件——
    * 控制器的电源动作经 rigctl.act_power 打到 World(断电/上电);
    * 每块开发板有 SoC 序列号(烧死, 不随线变) + shell(能答 /proc/uptime);
    * 串口线/下载线是"可挪的插头", 测试里随时把它插到别的物理口;
    * hub/实例/lsusb/adb 全部由 World 提供(rigctl 的这些入口被 monkeypatch)。
然后跑 rigctl 的真实代码路径(relocate / bind-dl / usb_mode_for / adbq),
断言"线怎么挪, rig 都还指向自己那块板"。

不碰真设备。用法: python3 test_topology.py
"""

import json
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import rigctl as R          # noqa: E402

PASS, FAIL = "✔", "✘"
RESULTS = []
PATCHED = ["RIGS_DEFAULT", "HUB_DEFAULT", "hub_tabs", "_rk_usb_by_chain", "_adb_devices_l",
           "_lsusb", "find_instance", "act_power", "_probe_uptime", "_api",
           "h503_detect", "h503_power"]


# ----------------------------------------------------------------- 假世界

class Board:
    """一块开发板: 固定身份(序列号), 可变位置(串口口/下载口)。"""

    def __init__(self, bid, serial):
        self.bid = bid
        self.serial = serial
        self.on = True
        self.boot_t = None                   # 由 World 设(虚拟时钟)
        self.serial_port = None              # 串口线当前插的物理口
        self.dl_port = None                  # 下载线当前插的物理口
        self.broken = False                  # 系统坏了(无 shell)

    def uptime(self):
        if not self.on:
            return 0
        return int(self.clk.time() - self.boot_t)


class VClock:
    """虚拟时钟: 板子 uptime 和 rigctl 的 sleep 都基于它(测试秒跑, 行为等同真实时间)。"""

    def __init__(self):
        self.now = 100000.0

    def time(self):
        return self.now

    def sleep(self, sec):
        self.now += float(sec)


class World:
    """假硬件: 2 控制器 + N 开发板 + hub 实例视图 + USB 总线视图。"""

    def __init__(self):
        self.boards = {}
        self.rails = {"rig-A": set(), "rig-B": set()}   # 电源轨 → 挂在上面的板
        self.clk = VClock()

    def add(self, bid, serial):
        b = Board(bid, serial)
        b.clk = self.clk
        self.boards[bid] = b
        return b

    def power(self, rig_name, on):
        self.clk.sleep(6)                     # 模拟断电放电
        for b in self.rails.get(rig_name, ()):
            if on and not b.on:
                b.boot_t = self.clk.time()    # 上电 = uptime 归零
            b.on = on

    # ---- hub 实例视图(keeper: 串口线上着就有实例)
    def tabs(self):
        out = []
        idx = 0
        for b in sorted(self.boards.values(), key=lambda x: x.serial_port or ""):
            if b.serial_port:
                out.append({"index": idx, "title": f"board-{b.bid}",
                            "port": f"/dev/serial/by-path/pci-0000:00:00.0-usb-0:{b.serial_port}:1.0-port0",
                            "agent_port": 9000 + idx, "open": True,
                            "baud": 1500000, "rx_count": 100, "title_": b.bid})
                idx += 1
        return out

    def board_of_port(self, ap):
        for t in self.tabs():
            if t["agent_port"] == ap:
                for b in self.boards.values():
                    if b.serial_port and t["port"].endswith(f":{b.serial_port}:1.0-port0"):
                        return b
        return None

    # ---- USB 总线视图(下载线插着且板子有电才有 2207)
    def rk_usb(self):
        out = {}
        for b in self.boards.values():
            if b.dl_port and b.on:
                out[b.dl_port] = {"pid": "0006", "serial": b.serial}
        return out

    def adb_lines(self):
        lines = []
        for chain, d in self.rk_usb().items():
            lines.append(f"{d['serial']}       device usb:3-{chain} product:x model:x")
        return "\n".join(lines) + "\n"


def install(world, tmpdir):
    """把 rigctl 的硬件入口全部接到假世界。"""
    if not hasattr(main, "_orig"):
        main._orig = {k: getattr(R, k) for k in PATCHED}
    rigs = {
        "rig-A": {"controller": {"name": "治具", "console": "1.4.2", "baud": 115200,
                                 "power": {"method": "sysfs-gpio", "gpios": [196, 197, 388, 389],
                                           "on_value": 1, "off_value": 0, "discharge_s": 6}},
                  "target": {"name": "3588 A", "console": None}},
        "rig-B": {"controller": {"name": "H503", "cmd_port": "/dev/null",
                                 "power": {"method": "h503", "channel": "12V5", "on_value": 1,
                                           "off_value": 0, "discharge_s": 6}},
                  "target": {"name": "3588 B", "console": None}},
    }
    path = os.path.join(tmpdir, "rigs.json")
    json.dump(rigs, open(path, "w"), ensure_ascii=False)

    R.RIGS_DEFAULT = path
    R.HUB_DEFAULT = "http://fake"
    R.hub_tabs = world.tabs
    R._rk_usb_by_chain = world.rk_usb
    R._adb_devices_l = world.adb_lines
    R._lsusb = lambda: ""
    for b in world.boards.values():          # 已上电的板假装跑了 500s
        if b.boot_t is None:
            b.boot_t = world.clk.time() - 500
    R.time.time = world.clk.time              # 虚拟时钟: sleep 推进时间
    R.time.sleep = world.clk.sleep
    real_api = R._api
    def fake_api(base, method, path, body=None, timeout=10):
        m = re.search(r":(\d+)$", base)
        b = world.board_of_port(int(m.group(1))) if m else None
        if b is not None and path == "/api/info":
            return {"ok": True, "data": {"capture_bytes": 100}}
        return {"ok": True, "data": {}}
    R._api = fake_api
    R._probe_uptime = lambda ap: (world.board_of_port(ap).uptime()
                                  if world.board_of_port(ap) and not world.board_of_port(ap).broken
                                  else None)

    def act_power(rig_name, rig, value, label):
        world.power(rig_name, value == 1)
        print(f"{rig_name}: {label} [仿真]")

    R.act_power = act_power
    # find_instance: 按 by-path 链路匹配 world.tabs
    R.find_instance = lambda key: (lambda hits: (hits[0]["agent_port"], hits[0]) if hits else None)(
        [t for t in world.tabs() if key and R.port_matches(t["port"], key)])
    # bind-dl 相关(默认桩, 用例可再覆盖)
    R.h503_detect = lambda p: ("json-v3.5", "x")
    R.h503_power = lambda *a, **k: "ok"
    return path


def _rel(w, rig_name):
    allr = R.load_rigs(R.RIGS_DEFAULT)
    return R.relocate_console(rig_name, allr[rig_name], R.RIGS_DEFAULT, allr)


def console_of(rig_name):
    return json.load(open(R.RIGS_DEFAULT, encoding="utf-8"))[rig_name]["target"].get("console")


def usb_text(rig_name):
    rigs = R.load_rigs(R.RIGS_DEFAULT)
    return R.usb_mode_for(rigs[rig_name])


# ----------------------------------------------------------------- 用例

def case(name):
    def deco(fn):
        RESULTS.append((name, fn))
        return fn
    return deco


@case("初始: 两块板各接 rig, relocate 找到各自控制台")
def t_initial(w):
    a, b = w.boards["A"], w.boards["B"]
    a.serial_port, a.dl_port = "1.4.3", "1.3"
    b.serial_port, b.dl_port = "1.2", "1.4.4"
    _rel(w, "rig-A"); _rel(w, "rig-B")
    assert console_of("rig-A") == "1.4.3", console_of("rig-A")
    assert console_of("rig-B") == "1.2", console_of("rig-B")


@case("两块板的串口线对调: relocate 后各归各(不串台)")
def t_swap_console(w):
    t_initial(w)
    a, b = w.boards["A"], w.boards["B"]
    a.serial_port, b.serial_port = b.serial_port, a.serial_port     # 对调
    _rel(w, "rig-A"); _rel(w, "rig-B")
    assert console_of("rig-A") == "1.2", f"rig-A 串台到了 {console_of('rig-A')}"
    assert console_of("rig-B") == "1.4.3", f"rig-B 串台到了 {console_of('rig-B')}"


def _bind_all(w):
    """等价于两块板都跑过 bind-dl: 各自的下载口+序列号已登记。"""
    t_initial(w)
    rigs = R.load_rigs(R.RIGS_DEFAULT)
    rigs["rig-A"]["target"]["download_chain"] = w.boards["A"].dl_port
    rigs["rig-A"]["target"]["adb_serial"] = w.boards["A"].serial
    rigs["rig-B"]["target"]["download_chain"] = w.boards["B"].dl_port
    rigs["rig-B"]["target"]["adb_serial"] = w.boards["B"].serial
    R.save_rigs(R.RIGS_DEFAULT, rigs)


@case("下载线挪到别的口: usb_mode_for 按序列号自动追踪, 不串台")
def t_dl_moved(w):
    _bind_all(w)
    w.boards["A"].dl_port = "1.4.1"        # A 的下载线挪走
    ta, tb = usb_text("rig-A"), usb_text("rig-B")
    assert w.boards["A"].serial[:12] in ta and "1.4.1" in ta, ta
    assert w.boards["B"].serial[:12] in tb and "1.4.4" in tb, tb
    assert w.boards["B"].serial[:12] not in ta and w.boards["A"].serial[:12] not in tb, "下载口串台"


@case("下载口旧位置被别人的板占了: 不许冒领")
def t_dl_imposter(w):
    t_dl_moved(w)
    w.boards["A"].dl_port = "1.3"                                   # A 回到原口… 再拔掉
    w.boards["A"].dl_port = None
    w.boards["B"].dl_port = "1.3"                                   # B 的线插到 A 的登记口
    rigs = R.load_rigs(R.RIGS_DEFAULT)
    rigs["rig-A"]["target"].pop("adb_serial")                      # 无序列号 → 只能靠登记口
    R.save_rigs(R.RIGS_DEFAULT, rigs)
    ta = usb_text("rig-A")
    assert ta is None or ta.startswith("?"), f"rig-A 冒领了 B 的设备: {ta}"


@case("开发板拔掉: rig 如实报无(不指向任何别的板)")
def t_unplugged(w):
    _bind_all(w)
    w.boards["A"].serial_port = None
    w.boards["A"].dl_port = None
    rigs = R.load_rigs(R.RIGS_DEFAULT)
    assert R.find_instance(rigs["rig-A"]["target"]["console"]) is None
    assert usb_text("rig-A") is None, usb_text("rig-A")
    assert usb_text("rig-B") is not None          # 另一块不受影响


@case("板子断电: 下载口消失, 控制台冻结, 都如实")
def t_powered_off(w):
    _bind_all(w)
    w.power("rig-A", False)
    assert usb_text("rig-A") is None
    assert usb_text("rig-B") is not None


@case("板子系统坏了(无 shell): relocate 用账本兜底或明确报错, 不指错板")
def t_broken_board(w):
    t_initial(w)
    w.boards["B"].broken = True
    w.boards["A"].serial_port, w.boards["B"].serial_port = "1.2", "1.4.3"   # 顺便对调
    try:
        _rel(w, "rig-A")                     # 落盘版调用(与 CLI/自动重定位一致)
        got = console_of("rig-A")
        assert got == "1.2", f"rig-A 指到了 {got}"
    except SystemExit as e:
        # 允许"明确失败", 但绝不能指到别的板
        assert console_of("rig-A") != "1.4.3", "rig-A 指到了 B 的板"


@case("两块板都没 shell(都卡在启动/emergency): 账本兜底分支仍要定位到自己的板")
def t_both_broken(w):
    """uptime 全探不到 → 走 relocate 的'账本灌入'兜底分支(最危险的路径)。"""
    t_initial(w)
    w.boards["A"].broken = True
    w.boards["B"].broken = True
    w.boards["A"].serial_port, w.boards["B"].serial_port = "1.2", "1.4.3"   # 对调
    # 仿真账本: 断电期间不变, 上电后该板的口灌入启动日志
    cap = {9000: 100, 9001: 100}
    class _Clk2:
        pass
    def power(rig_name, on):
        w.clk.sleep(6)
        for b in w.rails.get(rig_name, ()):
            if on and not b.on:
                # 上电 → 它所在串口口的账本暴涨(灌入启动日志)
                for t in w.tabs():
                    if t["port"].endswith(f":{b.serial_port}:1.0-port0"):
                        cap[t["agent_port"]] += 50000
            b.on = on
    R.act_power = lambda n, r, v, l: power(n, v == 1)
    R._api = lambda base, method, path, body=None, timeout=10: (
        {"ok": True, "data": {"capture_bytes": cap[int(base.rsplit(":", 1)[1])]}}
        if path == "/api/info" else {"ok": True, "data": {}})
    R._probe_uptime = lambda ap: None
    try:
        _rel(w, "rig-A")
        got = console_of("rig-A")
        assert got == "1.2", f"rig-A 指到了 {got}(应为 1.2, 即 A 板当前所在口)"
    except SystemExit as e:
        raise AssertionError(f"应当靠账本兜底成功, 却失败了: {e}")


@case("电源线被改接(控制器控不到板): relocate 明确失败且不改登记")
def t_rail_lost(w):
    t_initial(w)
    before = console_of("rig-A")
    w.rails["rig-A"] = set()                       # A 的电源线不归治具管了
    try:
        _rel(w, "rig-A")
        raise AssertionError("应当失败(控制器控不到任何板)")
    except SystemExit:
        pass
    assert console_of("rig-A") == before, "失败时不该改登记"


@case("find_target_instance: 登记口失效 → 自动重定位到自己的板")
def t_auto_relocate(w):
    t_initial(w)
    a = w.boards["A"]
    a.serial_port = "1.4.4"                        # A 的线挪到 1.4.4
    rigs = R.load_rigs(R.RIGS_DEFAULT)
    allr = R.load_rigs(R.RIGS_DEFAULT)
    hit = R.find_target_instance("rig-A", allr["rig-A"], R.RIGS_DEFAULT, allr)
    assert hit and console_of("rig-A") == "1.4.4", console_of("rig-A")
    board = w.board_of_port(hit[0])
    assert board is a, "自动重定位指到了别的板"


@case("bind-dl: 首次绑定 + 二次复核通过")
def t_bind_dl(w):
    t_initial(w)
    w.rails["rig-B"] = {w.boards["B"]}
    R.h503_detect = lambda p: ("json-v3.5", "x")
    R.h503_power = lambda *a, **k: "ok"
    allr = R.load_rigs(R.RIGS_DEFAULT)
    R.act_bind_download("rig-B", allr["rig-B"], R.RIGS_DEFAULT, allr)
    rigs = R.load_rigs(R.RIGS_DEFAULT)
    t = rigs["rig-B"]["target"]
    assert t["download_chain"] == "1.4.4", t.get("download_chain")
    assert t["adb_serial"] == w.boards["B"].serial, t.get("adb_serial")
    # A 的下载口信息不得被污染
    assert rigs["rig-A"]["target"].get("download_chain") != "1.4.4"


@case("bind-dl: 控制器控不到板(差分命中不了) → 明确失败, 不写错误绑定")
def t_bind_dl_fail(w):
    t_initial(w)
    w.rails["rig-B"] = set()
    R.h503_detect = lambda p: ("json-v3.5", "x")
    R.h503_power = lambda *a, **k: "ok"
    try:
        allr = R.load_rigs(R.RIGS_DEFAULT)
        R.act_bind_download("rig-B", allr["rig-B"], R.RIGS_DEFAULT, allr)
        raise AssertionError("应当失败")
    except SystemExit:
        pass
    t = R.load_rigs(R.RIGS_DEFAULT)["rig-B"]["target"]
    assert "download_chain" not in t, "失败时写入了错误绑定"


@case("adbq: 线挪了且 boards.json 过期(只有 rigs.json 更新) → 仍按序列号找对自己的板")
def t_adbq(w):
    _bind_all(w)
    # 两根下载线**对调**, 并且 boards.json 完全没更新(最坏情况)
    w.boards["A"].dl_port, w.boards["B"].dl_port = "1.4.4", "1.3"
    import tempfile as _tf
    bp = os.path.join(_tf.mkdtemp(), "boards.json")
    old = {"pci-0000:00:00.0-usb-0:1.4.3:1.0-port0": {"name": "3588 A", "download_chain": "1.3"},
           "pci-0000:00:00.0-usb-0:1.2:1.0-port0": {"name": "3588 B", "download_chain": "1.4.4"}}
    json.dump(old, open(bp, "w"), ensure_ascii=False)
    # boards.json 放到 rigs.json 旁边(load_boards 按 boards 路径找同目录的 rigs.json)
    bp = os.path.join(os.path.dirname(R.RIGS_DEFAULT), "boards.json")
    json.dump(old, open(bp, "w"), ensure_ascii=False)
    import adbq
    adbq.adb_devices = lambda: [{"serial": d["serial"], "chain": c, "state": "device"}
                                for c, d in w.rk_usb().items()]
    boards = adbq.load_boards(bp)
    devs = adbq.adb_devices()
    for nm, want in (("3588 A", w.boards["A"].serial), ("3588 B", w.boards["B"].serial)):
        _, d = adbq.resolve(nm, devs, boards)
        assert d["serial"] == want, f"{nm} 解析到了别人的板 {d['serial']}(应 {want})"
    # A 板断电 → 如实报不在线, 不会退而去用 B
    w.power("rig-A", False)
    devs = adbq.adb_devices()
    try:
        adbq.resolve("3588 A", devs, boards)
        raise AssertionError("A 板断电了不该解析成功")
    except SystemExit as e:
        assert "不在线" in str(e), e
    _, d = adbq.resolve("3588 B", devs, boards)
    assert d["serial"] == w.boards["B"].serial


@case("三块板同时在线(多出个第三方 RK 板): 两个 rig 互不串台、也不认领第三方")
def t_third_party(w):
    _bind_all(w)
    w.add("X", "deadbeefcafe0001")            # 同事插了块别的 RK 板, 下载线在 1.4.1
    w.boards["X"].serial_port, w.boards["X"].dl_port = "1.4.1", "1.4.1"
    w.boards["X"].dl_port = "1.1"
    assert w.boards["A"].serial[:12] in usb_text("rig-A")
    assert w.boards["B"].serial[:12] in usb_text("rig-B")
    assert "deadbeef" not in usb_text("rig-A") and "deadbeef" not in usb_text("rig-B")


@case("换了一块新开发板(新序列号), 旧线还在原口: 旧登记不认领新板")
def t_board_replaced(w):
    _bind_all(w)
    # A 板被换成另一块(序列号不同), 插在同一个下载口 1.3
    w.boards["A"].serial = "0000newboard0001"
    ta = usb_text("rig-A")
    # 正确行为: 绝不能把新板**认领**成旧 A 板(返回 None 或以 ? 开头的存疑提示)
    assert ta is None or ta.startswith("?"), f"旧登记把新板当成了旧 A 板: {ta}"
    # 旧 A 板的序列号没出现在结论里(它已经不在线了)
    assert "ba27" not in (ta or ""), ta


@case("板子进 Loader(adb 不列, PID=350b): 序列号仍在, 仍按序列号认主")
def t_loader_mode(w):
    _bind_all(w)
    R._rk_usb_by_chain = lambda: {w.boards["B"].dl_port: {"pid": "350b", "serial": w.boards["B"].serial}}
    tb = usb_text("rig-B")
    assert "350b" in tb and "Loader" in tb and w.boards["B"].serial[:12] in tb, tb
    assert usb_text("rig-A") is None, f"A 板没在总线上, 不该认领 B 的 Loader 设备: {usb_text('rig-A')}"


@case("连续两次 relocate(板子刚被冷启动 uptime≈0): 第二次也要成功")
def t_double_relocate(w):
    t_initial(w)
    a = w.boards["A"]
    a.serial_port = "1.4.4"
    _rel(w, "rig-A")
    assert console_of("rig-A") == "1.4.4", console_of("rig-A")
    a.serial_port = "1.4.1"                  # 紧接着又挪一次, 板子 uptime 还很小
    _rel(w, "rig-A")
    assert console_of("rig-A") == "1.4.1", f"第二次重定位失败/指错: {console_of('rig-A')}"


@case("两根串口线插在同一个 hub 但只有一块板上电: 断电的那块 relocate 明确失败")
def t_one_unpowered(w):
    t_initial(w)
    w.boards["B"].on = False                 # B 板没上电
    w.rails["rig-B"] = set()
    try:
        _rel(w, "rig-B")
        raise AssertionError("B 板没电不该重定位成功")
    except SystemExit:
        pass
    assert console_of("rig-A") == "1.4.3", "操作 rig-B 不该动 rig-A 的登记"


# ----------------------------------------------------------------- main

def run_one(name):
    """在**独立子进程**里跑一个用例(天然零残留, 不需要还原 monkeypatch)。"""
    import subprocess
    code = ("import sys;sys.argv=['x'];sys.path.insert(0,%r);"
            "import test_topology as T, tempfile;"
            "w=T.World();w.add('A','ba27980e967d3a93');w.add('B','45ef5567472a8f0c');"
            "w.rails={'rig-A':{w.boards['A']},'rig-B':{w.boards['B']}};"
            "td=tempfile.mkdtemp();T.install(w,td);"
            "fn=dict(T.RESULTS)[%r];"
            "fn(w);print('CASE_OK')") % (HERE, name)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       timeout=120)
    return r


def main():
    ok = 0
    only = sys.argv[1:]          # 可只跑名字含关键字的用例
    for name, fn in RESULTS:
        if only and not any(k in name for k in only):
            continue
        r = run_one(name)
        out = [l for l in r.stdout.splitlines()
               if not l.startswith(("[relocate]", "rig-", "  ")) and "读账本" not in l]
        if r.returncode == 0 and "CASE_OK" in r.stdout:
            print(f"{PASS} {name}")
            ok += 1
        else:
            err = [l for l in r.stderr.splitlines() if l.strip()]
            tail = err[-1] if err else (out[-1] if out else f"退出码 {r.returncode}")
            print(f"{FAIL} {name}\n     {tail[:300]}")
    print(f"\n拓扑回归: {ok}/{len(RESULTS) if not only else '?'} 通过")
    return 0 if ok == (len(RESULTS) if not only else ok) else 1


if __name__ == "__main__":
    sys.exit(main())
