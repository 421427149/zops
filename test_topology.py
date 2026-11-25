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
        self.tx = 100_000                    # 已发到串口的累计字节(账本来源)
        self._last_clk = None
        self.serial_port = None              # 串口线当前插的物理口
        self.dl_port = None                  # 下载线当前插的物理口
        self.broken = False                  # 系统坏了(无 shell)

    def uptime(self):
        """板子对 `/proc/uptime` 的回答。没电 = 不应答(None), 不是 0。

        之前没电返回 0 是**错误的建模**: 真机没电的板根本不会回显, 而"刚上电
        的活板"才会答 0~几秒。两者都给 0 会让 relocate 无法区分"没电"和"刚上电"
        (属性测试 300 轮抓出: 修了串台就误伤刚上电的板, 反之亦然)。
        """
        if not self.on:
            return None
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
        self.tick()

    def tick(self):
        """推进时间带来的串口输出: 活着的板 ~50B/s 缓慢吐字; 冷启动后 10s 内灌 ~30KB 启动日志。"""
        now = self.clk.time()
        for b in self.boards.values():
            if b._last_clk is None:
                b._last_clk = now
            dt = now - b._last_clk
            if b.on:
                b.tx += int(dt * 50)
                age = now - (b.boot_t or now)
                if age < 10:                  # 启动日志洪流
                    b.tx += int(dt * 3000)
            b._last_clk = now

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
            world.tick()
            return {"ok": True, "data": {"capture_bytes": b.tx}}
        return {"ok": True, "data": {}}
    R._api = fake_api
    def _probe(ap):
        b = world.board_of_port(ap)
        if b is None or b.broken:          # 没接串口线 / 没shell → 不应答
            return None
        return b.uptime()                  # 没电 → None; 活着 → 秒数(刚上电≈0)
    R._probe_uptime = _probe

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
    # 正确行为: 绝不能把新板**认领**成旧 A 板。"不认领"有三种合法形态:
    #   None(口上没设备) / "?…无法确认" / "✘…是别的板…不认领"
    def claims(t):            # 被认领 = 返回了一条**肯定的模式结论**(不以 ? ✘ 开头)
        return bool(t) and not t.startswith(("?", "✘"))
    assert not claims(ta), f"旧登记把新板当成了旧 A 板: {ta!r}"
    assert "Loader" not in (ta or "") and "运行态" not in ta, ta   # 没给出任何模式结论


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


def _quiet_boot(w):
    """让冷启动日志变少: 上电后只有缓慢的 50B/s, 灌不到 5KB。
    对应真机: 安静内核 / quiet 启动 / console 没从 DDR 阶段开始输出。
    这时证据B(账本灌入)不够用, 只能靠证据A(uptime 回落)。"""
    orig_tick = w.tick
    def tick():
        now = w.clk.time()
        for b in w.boards.values():
            if b._last_clk is None:
                b._last_clk = now
            dt = now - b._last_clk
            if b.on:
                b.tx += int(dt * 50)           # 没有启动日志洪流
            b._last_clk = now
    w.tick = tick


@case("冷启动日志很少(<5KB)的板: 账本证据不够, 必须靠 uptime 回落证据定位")
def t_quiet_boot(w):
    t_initial(w)
    _quiet_boot(w)
    w.boards["A"].serial_port = "1.4.4"
    _rel(w, "rig-A")
    got = console_of("rig-A")
    assert got == "1.4.4", f"安静启动的板没能重定位: {got}"


@case("登记口被别板占且本板不在线: 兜底的冒领校验必须拒绝")
def t_fallback_impersonation(w):
    _bind_all(w)
    w.boards["A"].dl_port = None                  # A 的下载线拔了
    w.boards["B"].dl_port = "1.3"                 # B 的线插到 A 的登记口 1.3
    rigs = R.load_rigs(R.RIGS_DEFAULT)
    rigs["rig-A"]["target"]["download_chain"] = "1.3"
    rigs["rig-A"]["target"]["adb_serial"] = w.boards["A"].serial      # A 登记过序列号
    R.save_rigs(R.RIGS_DEFAULT, rigs)
    ta = usb_text("rig-A")
    # 正确行为: 明确报"登记口上是别的板, 不认领"——既不能认领, 也不能静默 None(那是
    # "口上没设备"的语义, 会让人以为线没插)
    assert ta and ta.startswith("✘") and "别的板" in ta, f"rig-A 没有拒绝占口的别板: {ta!r}"
    assert w.boards["B"].serial[:12] in ta and w.boards["A"].serial[:12] in ta, ta   # 两边序列号都报出来
    assert "bind-dl" in ta, "没给出下一步指引"
    tb = usb_text("rig-B")
    assert w.boards["B"].serial[:12] in tb and not tb.startswith(("✘", "?")), tb


@case("未绑定下载口的 rig: 总线上有别的 2207 设备也不认领, 且提示先 bind-dl")
def t_unbound_hint(w):
    t_initial(w)
    w.boards["A"].dl_port = "1.3"; w.boards["B"].dl_port = "1.4.4"
    rigs = R.load_rigs(R.RIGS_DEFAULT)
    for n in ("rig-A", "rig-B"):
        rigs[n]["target"].pop("download_chain", None)
        rigs[n]["target"].pop("adb_serial", None)
    R.save_rigs(R.RIGS_DEFAULT, rigs)
    import subprocess as _sp
    R._lsusb = lambda: "Bus 003 Device 5: ID 2207:350b Fuzhou Rockchip\nBus 003 Device 6: ID 2207:0006 Fuzhou Rockchip\n"
    for n in ("rig-A", "rig-B"):
        t = usb_text(n)
        assert t is None or t.startswith("?"), f"{n} 认领了无主的 2207 设备: {t}"
        assert t and "bind-dl" in t, f"{n} 未给出'先跑 bind-dl'的指引: {t!r}"


@case("USB 枚举: 只收 VID=2207 的设备级条目, 跳过接口级, 读对 PID/序列号")
def test_usb_vendor_filter(w):
    import tempfile as _tf
    fake = _tf.mkdtemp()
    def mk(name, vid, pid, sn=None):
        # 注意: 目录名**保留冒号** —— 真实 sysfs 的接口级条目就叫 `3-1.4:1.0`,
        # 程序正是靠名字里的 ':' 识别并跳过它; 之前把冒号换成下划线是错的测试数据
        d = os.path.join(fake, name)
        os.makedirs(d)
        open(os.path.join(d, "idVendor"), "w").write(vid + "\n")
        open(os.path.join(d, "idProduct"), "w").write(pid + "\n")
        if sn is not None:
            open(os.path.join(d, "serial"), "w").write(sn + "\n")
    mk("3-1.3", "2207", "350B", "aaaa1111")        # RK 板, Loader 态
    mk("3-1.4", "1a86", "7523", "bbbb2222")        # CH340 串口线 —— 不是 RK, 不能收
    mk("3-1.4:1.0", "2207", "0006", "cccc3333")    # 接口级条目(同名设备已收) —— 必须跳过
    mk("3-1.5", "0483", "5740")                    # STM32 —— 不是 RK, 且没有 serial 文件
    mk("usb1", "1d6b", "0002")                     # 根集线器, 名字里没有 '-'
    R.SYSFS_USB = fake
    # install() 已把 _rk_usb_by_chain 换成假世界版本; 这里要测**真实**枚举逻辑, 还原之
    import types as _types
    real_src = open(os.path.join(HERE, "rigctl.py"), encoding="utf-8").read()
    ns = {"__name__": "rigctl_real", "__file__": os.path.join(HERE, "rigctl.py")}
    exec(compile(real_src, ns["__file__"], "exec"), ns)     # 干净的 rigctl 副本
    ns["SYSFS_USB"] = fake
    out = ns["_rk_usb_by_chain"]()
    assert set(out) == {"1.3"}, f"枚举结果不对(多了非RK/接口级, 或少了RK): {out}"
    assert out["1.3"]["pid"] == "350b", out        # PID 归一成小写
    assert out["1.3"]["serial"] == "aaaa1111", out
    ns["SYSFS_USB"] = "/nonexistent/sysfs"           # sysfs 读不到 → 空字典, 不抛异常
    assert ns["_rk_usb_by_chain"]() == {}


@case("口匹配: 不同口/解析不出的一边绝不匹配(否则任何实例都能冒充任何登记)")
def test_port_matches_strict(w):
    P = "pci-0000:00:00.0-usb-0:"
    # ① 两边都能解析出链路段: 必须相等
    assert R.port_matches(P + "1.4.3:1.0-port0", "1.4.3") is True
    assert R.port_matches(P + "1.4.3:1.0-port0", "1.4.4") is False, "不同链路段被当成同一个口"
    assert R.port_matches(P + "1.4.3:1.0-port0", P + "1.4.4:1.0-port0") is False
    assert R.port_matches("1.4.3", "1.4.3") is True
    # ② 退到设备节点比较: 假装 by-path 目录(1.4.3 → ttyUSB7)
    import os as _os
    fake = _os.path.join(_os.sep, "tmp", "zops-bypath-fake")
    _os.makedirs(fake, exist_ok=True)
    link = _os.path.join(fake, "pci-0000:00:00.0-usb-0:1.4.3:1.0-port0")
    try:
        if _os.path.lexists(link):
            _os.remove(link)
        _os.symlink("../../ttyUSB7", link)
    except OSError:
        pass
    real_listdir, real_realpath = _os.listdir, _os.path.realpath
    real_bp = "/dev/serial/by-path"
    import types as _t
    # 临时把 serial_node 里的 by-path 根指到假目录: 用干净副本 + 改源码里的常量
    src = open(_os.path.join(HERE, "rigctl.py"), encoding="utf-8").read()
    src = src.replace('os.listdir("/dev/serial/by-path")', f'os.listdir({fake!r})')
    src = src.replace('os.path.realpath(f"/dev/serial/by-path/{entry}")',
                      f'os.path.realpath(f"{fake}/{{entry}}")')
    ns = {"__name__": "rigctl_pm", "__file__": os.path.join(HERE, "rigctl.py")}
    exec(compile(src, ns["__file__"], "exec"), ns)
    pm, sn = ns["port_matches"], ns["serial_node"]
    assert sn("1.4.3") == "ttyUSB7", sn("1.4.3")           # 前提: 假目录解析得出节点
    assert sn("1.4.4") is None, sn("1.4.4")                # 假目录里没有 1.4.4
    assert pm("/dev/ttyUSB7", "1.4.3") is True, "节点相同应匹配"
    assert pm("/dev/ttyUSB9", "1.4.3") is False, "节点不同却匹配了"
    # 关键: 一边解析不出(None) → 绝不能匹配(变异 and→or 会在这里露馅)
    assert pm("/dev/ttyUSB7", "1.4.4") is False, "登记键解析不出节点, 却匹配了实例"
    assert pm("", "1.4.3") is False
    assert pm(None, "1.4.3") is False
    try:
        _os.remove(link); _os.rmdir(fake)
    except OSError:
        pass


@case("verify 的 Loader 判定: 别的板在 Loader 态时, 本 rig 不得被判为通过")
def test_rig_in_mode_ownership(w):
    _bind_all(w)
    w.boards["B"].dl_port = "1.3"                 # B 在 1.3, 进 Loader 态
    R._rk_usb_by_chain = lambda: {"1.3": {"pid": "350b", "serial": w.boards["B"].serial}}
    rigs = R.load_rigs(R.RIGS_DEFAULT)
    assert R._rig_in_mode(rigs["rig-B"], "350b") is True, "自己的板在 Loader, 应判通过"
    assert R._rig_in_mode(rigs["rig-A"], "350b") is False, \
        "A 的板根本不在线, 却因为总线上有 B 的 350b 被判为通过(撞号)"
    # 两块板都在, 只有 A 在 Loader
    R._rk_usb_by_chain = lambda: {"1.4.4": {"pid": "350b", "serial": w.boards["A"].serial},
                                  "1.3": {"pid": "0006", "serial": w.boards["B"].serial}}
    assert R._rig_in_mode(rigs["rig-A"], "350b") is True
    assert R._rig_in_mode(rigs["rig-B"], "350b") is False
    # 运行态不算 Loader
    R._rk_usb_by_chain = lambda: {"1.4.4": {"pid": "0006", "serial": w.boards["A"].serial}}
    assert R._rig_in_mode(rigs["rig-A"], "350b") is False
    # MaskROM(320b) 与 Loader(350b) 不混
    R._rk_usb_by_chain = lambda: {"1.4.4": {"pid": "320b", "serial": w.boards["A"].serial}}
    assert R._rig_in_mode(rigs["rig-A"], "350b") is False
    assert R._rig_in_mode(rigs["rig-A"], "320b") is True
    # 同口但序列号是别人的 → 不算
    R._rk_usb_by_chain = lambda: {"1.3": {"pid": "350b", "serial": "someoneelse0000"}}
    assert R._rig_in_mode(rigs["rig-B"], "350b") is False, "同口不同序列号被当成了本板"


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
