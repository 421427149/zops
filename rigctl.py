#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rigctl.py —— 控制组合(rig)一条命令操控: T536 治具 GPIO 控受控板电源。

背景: "通过一个板子控制开发板"(rk-board-access §1D)的整条链——鉴别串口、进治具
控制台、发 GPIO 控电命令、回受控板验证——AI 每次现推导要十几个回合, 组合一多
还会认错对象。本工具把 rigs.json 登记的组合做成一等公民, 一条命令机械执行:

    rigctl status              # 所有组合: 控制台/受控串口/USB 判活 一行一结论
    rigctl online              # 在线检测(只读): 现在哪些板在线、各是哪个 rig; --json 给机器用
    rigctl rig-A status        # 单个组合同上
    rigctl rig-A on            # 开机(4 路 GPIO 全写 1, 幂等)
    rigctl rig-A off           # 关机(全写 0)
    rigctl rig-A coldboot      # 关机 → 放电 → 开机(冷启动, 全程约 discharge_s+5s)
    rigctl rig-A enter-loader  # 板型核对 → reboot loader → 轮询USB判定模式(全自动)
    rigctl rig-A verify        # 逐动作真跑+硬证据判定, 结果写入 rigs.json 的 verified
                               #   --only off,on 只验部分; --recover 验完冷启动回系统
    rigctl rig-A catch-uboot   # 救砖: 冷启动+连发Ctrl+C抓U-Boot菜单+选[3]进Loader
                               #   (内核已panic时唯一入口; 加 --menu 停在菜单不选)
    rigctl rig-A relocate      # 控制台口被挪走后重新定位(电力差分, 会重启板子)并回写
    rigctl rig-A bind-dl       # 绑定USB下载口(断电差分), 会开USB口通道并重启板子
    rigctl rig-A bind-console [--write]  # 不断电: 经 adb 让板子往 console 打标记, 核对/回写控制台口
    rigctl rig-A bind          # 重探测受控板: 关电盯哪个串口灭→开电盯哪个醒,
                               # 结论自动写回 rigs.json 的 target(换板/挪线后跑这个)
    rigctl --selftest          # 链路归一化自检, 不碰设备

设计约定:
    * 控制通道走 serial_debug 实例的 agent HTTP(经 hub 发现, 不抢串口);
      治具控制台实例必须在线(本机约定 1.4.2@115200 → 8324);
    * 受控板是**槽位不是常量**: target 随接线/刷机而变, bind 重探测一次落盘;
    * bind 会真的断电再上电受控板(这就是探测原理), 请在可接受时才跑;
    * 身份坐标系与 boards.json/adbq 同源(by-path 链路段), 两套组合并存时
      各挂各的 hub 分支, 物理上串不了。
"""

import contextlib
import fcntl
import http.client
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
RIGS_DEFAULT = os.path.join(HERE, "rigs.json")
BOARDS_DEFAULT = os.path.join(HERE, "boards.json")   # 板名表(测试换成临时副本, 不碰真表)
HUB_DEFAULT = os.environ.get("RIGCTL_HUB", "http://127.0.0.1:8300")
READ_TAIL_BYTES = 4096          # 回读尾窗大小(足够覆盖命令回显)
SETTLE_S = 0.8                  # 发命令后等回显落账的时间
BIND_WATCH_S = 6                # bind 每阶段盯账本活动的秒数
# URLError(含 HTTPError 超类)/OSError(含 socket 超时)/HTTPException(非 HTTP 对端)
NET_ERRORS = (urllib.error.URLError, OSError, http.client.HTTPException)


class RigError(SystemExit):
    """带退出码的业务错误, 消息前缀 [rigctl]。"""

    def __init__(self, code, message):
        super().__init__(message)      # SystemExit 只带消息 → str(e) 干净可读
        self.code = code


# ---------------------------------------------------------------- 身份归一化

def hub_chain(port_str):
    """串口标识 → hub 链路段。三种写法都认:
    pci-...-usb-0:1.4.2:1.0-port0 → "1.4.2"; "1.4.2" → 原样; "ttyUSB3" → None。
    与 adbq.hub_chain_of_key / agent_hub._jack_of 同一规则。"""
    if not port_str:
        return None
    m = re.search(r"usb-([\d.:]+)", port_str)
    if m:
        segs = m.group(1).split(":")
        return segs[1] if len(segs) > 1 else segs[0]
    if re.fullmatch(r"\d+(\.\d+)*", port_str.strip()):
        return port_str.strip()
    return None


def serial_node(port_str):
    """串口标识 → 当前设备节点名(ttyUSBn): by-path 解析软链, ttyUSBx 原样。
    实例无论用 by-path 还是 ttyUSBx 启动都能跟登记键对上——防止"登记键是
    by-path、实例却报 ttyUSBn"(或反之)造成的假性失配。"""
    if not port_str:
        return None
    if port_str.startswith("/dev/"):
        port_str = port_str[5:]
    if port_str.startswith("tty"):
        return port_str
    ch = hub_chain(port_str)
    if not ch:
        return os.path.basename(port_str)
    try:
        for entry in os.listdir("/dev/serial/by-path"):
            if hub_chain(entry) == ch:
                return os.path.basename(os.path.realpath(f"/dev/serial/by-path/{entry}"))
    except OSError:
        pass
    return None


def port_matches(port_str, console_key):
    """实例上报的 port 是否就是登记的 console 键: 链路段相等 或 设备节点相等。"""
    if not port_str:
        return False
    ch, ck = hub_chain(port_str), hub_chain(console_key)
    if ch and ck:
        return ch == ck
    np, nk = serial_node(port_str), serial_node(console_key)
    return bool(np and nk and np == nk)


# ---------------------------------------------------------------- rigs 登记

def load_rigs(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise RigError(2, f"rigs.json 读取失败: {e}")
    return {k: v for k, v in data.items() if not k.startswith("_")}


def save_rigs(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)          # 原子替换, 半写状态不会污染登记表


def console_chain_of_rig(rig, role):
    return rig.get(role, {}).get("console")


# ---------------------------------------------------------------- hub / 实例

def _api(base, method, path, body=None, timeout=10):
    req = urllib.request.Request(base + path,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def hub_tabs():
    try:
        return _api(HUB_DEFAULT, "GET", "/api/tabs")["data"]["tabs"]
    except NET_ERRORS as e:
        raise RigError(2, f"hub({HUB_DEFAULT}) 不可达: {e}; 实例必须经 hub 登记(先起 agent_hub.py)")


def find_instance(console_key):
    """按 console 键在 hub Tab 里找实例, 返回 (agent_port, tab) 或 None。"""
    for tab in hub_tabs():
        if port_matches(tab.get("port"), console_key):
            return tab["agent_port"], tab
    return None


def find_target_instance(rig_name, rig, rigs_path=None, all_rigs=None):
    """受控板串口实例: 登记口在 → 直接用; 不在 → 电力差分自动重定位并回写。

    所有需要往受控板发命令的路径(status/enter-loader/catch-uboot/verify)都走这里,
    保证"线挪了"之后 rig 仍然指向**自己那块板**的串口, 不会借用别的口。
    """
    key = console_chain_of_rig(rig, "target")
    hit = find_instance(key) if key else None
    if hit:
        return hit
    if not key or (rig.get("target") or {}).get("console_missing_at"):
        pass
    try:
        return relocate_console(rig_name, rig, rigs_path, all_rigs)
    except SystemExit as e:
        raise RigError(3, f"{rig_name}: 登记控制台口 {key} 无实例, 且自动重定位失败({e})")


_PROBE_SEQ = [0]


def _probe_uptime(ap):
    """问板子 uptime 秒数; 板子没 shell/没电/超时返回 None。

    细节(都是真机会踩的):
      * 每次用**唯一标记** `__UP<seq>_<n>__`: 窗口里可能残留上一次的回显,
        不带序号会读到陈旧值;
      * 只在"本次发送之后"新增的账本里找(按 offset 读), 且窗口放大到 16KB:
        板子正刷内核日志时 800 字节窗口会把回显冲出去 → 误判成不在线。
    """
    _PROBE_SEQ[0] += 1
    seq = _PROBE_SEQ[0]
    try:
        base = _api(f"http://127.0.0.1:{ap}", "GET", "/api/info")["data"]["capture_bytes"]
        _api(f"http://127.0.0.1:{ap}", "POST", "/api/tabs/0/send",
             {"text": f"echo __UP{seq}_$(cut -d. -f1 /proc/uptime)__"})
        time.sleep(1.6)
        d = _api(f"http://127.0.0.1:{ap}", "GET",
                 f"/api/tabs/0/receive?offset={base}&max_bytes=16384")["data"]
        m = re.search(rf"__UP{seq}_(\d+)__[^_]", d.get("text") or "")
        return int(m.group(1)) if m else None
    except Exception as e:                      # 记下来, 不静默
        print(f"[relocate] uptime 探针 @{ap} 失败: {e}", flush=True)
        return None

def relocate_console(rig_name, rig, rigs_path=None, all_rigs=None):
    """登记的控制台口失效时(线被挪走), 用**电力差分**把控制台重新找出来并回写。

    只认**观察到的重启事件**, 两类硬证据任一成立即可:
      (a) uptime 回落: 断电前已跑了一阵(u0 足够大), 上电后远低于「没断电应有的值」;
      (b) 断电静默→上电恢复: 断电期间该串口口几乎没吐字, 上电后才灌入输出。
          不要求断电前是活的(板子卡死时正是最需要它)。
    为什么要两个: 「刚上电的板」(u0≈0)被重启后 uptime 反而变大, 证据(a)对它
    天然无效, 只能靠(b); 板子没起到 shell 时探不到 uptime, 也只能靠(b)。
    没电/一直静默的口两类证据都不满足 → 不认领; 找不到就明确失败, 绝不退而
    求其次指到别的板(2026-11-25 拓扑回归+属性测试实测)。
    """
    p = (rig.get("controller") or {}).get("power") or {}

    def caps():
        out = {}
        for t in hub_tabs():
            try:
                out[t["agent_port"]] = _api(f"http://127.0.0.1:{t['agent_port']}", "GET",
                                            "/api/info")["data"]["capture_bytes"]
            except Exception as e:
                print(f"[relocate] 读账本 @{t['agent_port']} 失败: {e}", flush=True)
        return out

    aps = [t["agent_port"] for t in hub_tabs()]
    t0 = time.time()
    before = {ap: _probe_uptime(ap) for ap in aps}
    c0 = caps()
    act_power(rig_name, rig, p.get("off_value", 0), "关机[重定位控制台]")
    time.sleep(4)
    c_off = caps()                               # 断电期间: 本 rig 的板应已静默
    time.sleep(2)
    act_power(rig_name, rig, p.get("on_value", 1), "开机[重定位控制台]")
    time.sleep(14)                               # 等它起到 shell 再探

    hit = None
    for _ in range(12):                          # 最多再等 ~36s
        elapsed = time.time() - t0
        c_now = caps()
        for ap in aps:
            # 证据 A(uptime 回落): 断电前已跑了一会儿(u0 足够大), 重启后远低于"没断电应有值"。
            #   注意: 刚上电的板 u0≈0, 重启后 u1 反而 >u0 —— 对它这个证据天然无效,
            #   必须靠证据 B。(2026-11-25 属性测试 300 轮抓出, 之前两次打补丁都错在这)
            u0, u1 = before.get(ap), _probe_uptime(ap)
            if u0 and u1 is not None and u1 < u0 + elapsed - 5 and u1 <= max(60, elapsed + 5):
                hit = ap; break
            # 证据 B(断电静默 → 上电后恢复输出): 与 uptime 数值无关。
            #   断电期间该口几乎没吐字(quiet), 且**上电之后**才出现大量输出(burst)。
            #   这本身就是因果证据: 没接板/板子没电的口, 不会因为这次上电凭空冒出
            #   启动日志。不要求「断电前是活的」——板子卡死(没 shell、串口静默)时
            #   正是最需要靠冷启动+重定位来救的时候。
            #   burst 阈值取大(>5000): 冷启动日志是洪流级, 靠它区别于
            #   「别的板恰好吐了几个字」。
            if c0.get(ap) is None or c_off.get(ap) is None or c_now.get(ap) is None:
                continue
            quiet = c_off[ap] - c0[ap] <= 200            # 断电期间静默
            burst = c_now[ap] - c_off[ap] > 5000         # 上电后才灌入
            if quiet and burst:
                hit = ap; break
        if hit:
            break
        time.sleep(3)
    if not hit:
        raise RigError(4, f"{rig_name}: 断电重启后没有观察到任何串口'被重启'的证据"
                          "(uptime 归零 / 断电静默后灌入启动日志); "
                          "可能: 电源线未接在该控制器上 / 板子未上电 / 串口线未接")
    tab = next((t for t in hub_tabs() if t["agent_port"] == hit), None)
    if not tab:
        raise RigError(4, "找到目标但 hub 里查不到该实例")
    chain = hub_chain(tab["port"]) or tab["port"]
    old = (rig.get("target") or {}).get("console")
    rig.setdefault("target", {})["console"] = chain
    rig["target"]["console_relocated_at"] = time.strftime("%Y-%m-%d %H:%M")
    rig["target"]["console_relocated_from"] = old
    if rigs_path and all_rigs is not None:
        save_rigs(rigs_path, all_rigs)
    print(f"{rig_name}: ✔ 控制台重定位 {old} → {chain} (实例{hit}, 已观察到重启证据)", flush=True)
    return hit, tab

def send_and_read(agent_port, cmd, settle=SETTLE_S):
    """经实例向控制台发一行命令, 读回尾窗文本(含回显)。"""
    base = f"http://127.0.0.1:{agent_port}"
    try:
        _api(base, "POST", "/api/tabs/0/send", {"text": cmd})
        time.sleep(settle)
        return _api(base, "GET",
                    f"/api/tabs/0/receive?max_bytes={READ_TAIL_BYTES}&tail=1")["data"]["text"]
    except NET_ERRORS as e:
        raise RigError(3, f"实例 {agent_port} 读写失败: {e}")


def power_cmdline(rig, value):
    """生成 4 路 GPIO 同值操作的 sysfs 一行命令(保持型, 带 RC 哨兵)。"""
    p = rig["controller"]["power"]
    gs = " ".join(str(g) for g in p["gpios"])
    return (f"for g in {gs}; do echo $g > /sys/class/gpio/export 2>/dev/null; "
            f"echo out > /sys/class/gpio/gpio$g/direction 2>/dev/null; "
            f"echo {value} > /sys/class/gpio/gpio$g/value; done; "
            f"echo __PWR_RC=$?__")


# ---------------------------------------------------------------- 动作

# ------------------------- H503 双协议(A5 二进制=现固件 / JSON V3.5=新固件)

# A5: 4字节帧 A5+通道+状态+校验(前三和&0xFF), 回帧 A5+通道+错误码+校验; 探活=截断3字节帧。
# JSON V3.5(协议源=tqtools/QSerial 仓 power_board_protocol.py): 行式JSON,
#   {"ch":名,"st":0/1} 控制, {"qr":名} 查询, 回带 code(0成功); 通道名大小写敏感。
A5_CH_BY_NAME = {"USB": 0x01, "5V2": 0x02, "5V3": 0x03, "12V4": 0x04, "12V5": 0x05}
A5_CH_NAME = {0x01: "USB0", 0x02: "5V2", 0x03: "5V3", 0x04: "12V4", 0x05: "12V5"}
JSON_V35_CHANNELS = ("USB0", "USB1", "5V2", "5V3", "12V4", "12V5", "RST_USB",
                     "PB3", "PB4", "PB5", "PB6", "PB7", "PB8", "PB12", "PB13",
                     "PA8", "PA9", "PA10", "PA15")
H503_ACK = {0: "成功", 1: "长度错", 2: "校验错", 3: "状态错"}


@contextlib.contextmanager
def _h503_lock(cmd_port):
    """H503 命令口的跨进程互斥锁。

    网关(线程)与 rigctl(子进程)都会开这个 CDC 口; 撞上时后者 open 直接失败。
    用 flock 让它们排队——flock 对同进程的不同 fd 之间也生效, 所以线程也覆盖。
    """
    import hashlib
    tag = hashlib.md5(cmd_port.encode()).hexdigest()[:8]
    path = f"/tmp/zops-h503-{tag}.lock"
    try:
        ef = open(path, "w")
    except OSError:
        yield                      # 锁文件建不了就退化为无锁(不阻塞功能)
        return
    try:
        fcntl.flock(ef, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(ef, fcntl.LOCK_UN)
        finally:
            ef.close()


def _h503_xact(cmd_port, frame, timeout=1.5, retries=3):
    """独占打开命令口发一帧读回帧(CDC 无波特率意义), 用完即关。

    重试是必要的: 网关(网页 rig 状态条)每 5s 也会打开同一命令口探测, 两边撞上时
    先到者拿到口、后来者 open 直接失败(实测偶发"命令口打不开")。
    """
    import serial
    last = None
    for i in range(retries):
        try:
            with _h503_lock(cmd_port):        # 与网关排队, 避免同时 open 撞车
                with serial.Serial(cmd_port, timeout=timeout) as s:
                    s.reset_input_buffer()
                    s.write(frame)
                    return s.read(64)
        except (serial.SerialException, OSError, ValueError) as e:
            last = e
            time.sleep(0.25 * (i + 1))
    raise OSError(f"命令口 {cmd_port} 打不开(重试{retries}次): {last}")


def h503_detect(cmd_port):
    """双协议探测: 先 JSON(新固件会回 JSON 行), 再 A5(截断帧应答 a50101a7)。"""
    try:
        reply = _h503_xact(cmd_port, b'{"qr":"USB0"}\n')
        line = reply.split(b"\n")[0].strip()
        if line.startswith(b"{"):
            return "json-v3.5", line.decode("utf-8", "replace")
    except (OSError, ValueError):
        return None, "命令口打不开"
    try:
        reply = _h503_xact(cmd_port, bytes([0xA5, 0x01, 0x00]))
    except (OSError, ValueError) as e:
        return None, f"命令口读写失败: {e}"
    if reply[:3].hex() == "a50101":
        return "a5-bin", f"探活应答 {reply.hex()}"
    return None, f"未知应答 {reply.hex()!r}"


def h503_power(cmd_port, channel, state, protocol="a5-bin"):
    """按协议发电源命令。channel 用通道名(5V2/12V4/USB...), A5 内部译为 0x0X。"""
    if channel is None:
        raise RigError(2, "电源通道未绑定: 先跑 bind 差分或人工确认后填 rigs.json")
    if protocol == "json-v3.5":
        import json as _json
        # 旧配置里通道可能写成 A5 时代的数字(1=USB、2=5V2…), 自动映射成名字,
        # 否则会发出 {"ch":1,...} 被固件按参数错误(code=2)整条拒绝。
        if not isinstance(channel, str):
            channel = A5_CH_NAME.get(int(channel))
            if channel is None:
                raise RigError(2, f"JSON 协议要求通道名, 收到 {channel!r}")
        if channel not in JSON_V35_CHANNELS:
            raise RigError(2, f"未知通道 {channel!r}; 可用: {', '.join(JSON_V35_CHANNELS)}")
        cmd = _json.dumps({"ch": channel, "st": int(state)}, separators=(",", ":"))
        reply = _h503_xact(cmd_port, cmd.encode() + b"\n")
        try:
            resp = _json.loads(reply.split(b"\n")[0].strip().decode("utf-8", "replace"))
        except ValueError:
            raise RigError(4, f"回帧非 JSON: {reply[:64]!r}")
        code = resp.get("code")
        if code != 0:
            raise RigError(4, f"设备回 code={code}: {resp}")
        return f"JSON 回帧 {resp}"
    ch = A5_CH_BY_NAME.get(channel)
    if ch is None:
        try:
            ch = int(str(channel), 16) if isinstance(channel, str) else int(channel)
        except ValueError:
            raise RigError(2, f"未知通道名 {channel!r}(A5 协议可用: {list(A5_CH_BY_NAME)})")
    body = bytes([0xA5, ch, int(state) & 1])
    reply = _h503_xact(cmd_port, body + bytes([sum(body) & 0xFF]))
    if len(reply) < 4 or reply[0] != 0xA5 or reply[1] != ch:
        raise RigError(4, f"回帧异常: {reply.hex()!r}")
    if reply[2] != 0:
        raise RigError(4, f"回帧错误码 {reply[2]}({H503_ACK.get(reply[2], '?')})")
    return f"回帧 {reply.hex()} 成功"


def act_power(rig_name, rig, value, label):
    ctrl = rig.get("controller") or {}
    if str(ctrl.get("power", {}).get("method", "")).startswith("h503"):
        proto, _ = h503_detect(ctrl["cmd_port"])
        if not proto:
            raise RigError(3, "H503 命令口双协议探测均失败")
        # 上电顺序纪律: H503 的 USB 口供电先开再开目标板通道
        usb_ch = ctrl.get("usb_port_channel")
        if value == ctrl["power"].get("on_value", 1) and usb_ch is not None:
            h503_power(ctrl["cmd_port"], usb_ch, ctrl["power"].get("on_value", 1), proto)
        msg = h503_power(ctrl["cmd_port"], ctrl["power"].get("channel"), value, proto)
        print(f"{rig_name}: {label} 完成({proto}, 通道 {ctrl['power'].get('channel')}, {msg})")
        return
    hit = find_instance(console_chain_of_rig(rig, "controller"))
    if not hit:
        raise RigError(3, f"{rig_name} 的控制器控制台({console_chain_of_rig(rig, 'controller')}) "
                          "没有在线实例; 先起: serial_debug.py -p <by-path> -b 115200 --agent-http <port>")
    agent_port, _tab = hit
    gpios = rig["controller"]["power"]["gpios"]
    gs = " ".join(str(g) for g in gpios)
    readback = (f"for g in {gs}; do echo -n \"$g:\"; "
                f"cat /sys/class/gpio/gpio$g/value 2>/dev/null || echo NA; done; "
                f"echo __GPIO_END__")
    # rc=0 只说明循环跑完, 不代表继电器真到位 —— 必须回读电平(曾因此把板子留在断电态)
    for attempt in range(1, 4):
        out = send_and_read(agent_port, power_cmdline(rig, value))
        m = re.search(r"__PWR_RC=(\d+)__", out)
        if not m:
            raise RigError(4, f"控制台无 {label} 哨兵回显(尾窗: {out[-120:]!r}); "
                              "检查控制台是否被占用/登录态")
        if m.group(1) != "0":
            raise RigError(4, f"{label} 命令 rc={m.group(1)} 非零, GPIO 写入失败")
        rb = send_and_read(agent_port, readback, settle=1.0)
        vals = dict(re.findall(r"(\d+):([01])", rb))
        wrong = {g: v for g, v in vals.items() if int(v) != int(value)}
        if vals and not wrong:
            print(f"{rig_name}: {label} 完成并回读确认(GPIO {gs} 全为 {value}, "
                  f"第{attempt}次, 经实例 {agent_port})")
            return
        print(f"{rig_name}: {label} 第{attempt}次回读不符 {wrong or '无回读'}, 重试...",
              flush=True)
        time.sleep(0.5)
    raise RigError(4, f"{label} 连续 3 次回读不到 GPIO={value} —— 继电器可能未到位, "
                      f"板子当前可能仍处于断电态(请人工确认)")


def _ensure_power_on(rig_name, rig):
    """最终兜底: 确保板子处于通电态(失败重试 3 次)。

    用途: 验证/冷启动/救砖等流程中间任何异常都不应把板子留在断电态
    (实测踩过: 流程中途失败 → 板子断电, 用户以为"被搞死了")。
    """
    p = rig["controller"]["power"]
    for i in range(3):
        try:
            act_power(rig_name, rig, p.get("on_value", 1), f"兜底上电(第{i+1}次)")
            return True
        except SystemExit as e:
            print(f"[warn] {rig_name}: 兜底上电失败({e}), 重试", flush=True)
            time.sleep(1)
        except Exception as e:
            print(f"[warn] {rig_name}: 兜底上电异常({e}), 重试", flush=True)
            time.sleep(1)
    print(f"[ERROR] {rig_name}: 无法恢复供电 —— 请人工确认板子电源!", flush=True)
    return False


def _lsusb():
    try:
        return subprocess.run(["lsusb"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


USB_PID_MODE = {"0006": "运行态(adb gadget)", "350b": "Loader 下载态", "320b": "MaskROM"}


def usb_mode():
    """主机侧 2207 设备的 PID → 模式判定(无 upgrade_tool 时按 PID 映射, 见闸门口诀)。"""
    pids = re.findall(r"ID\s+2207:([0-9a-f]{4})", _lsusb(), re.I)
    if not pids:
        return None
    pid = pids[0].lower()
    return f"2207:{pid} → {USB_PID_MODE.get(pid, '未知PID')}"


def _wait_prompt(base, timeout=90):
    """等板子 shell 可用——用哨兵法, 不看提示符。

    为什么不用"输出末尾是提示符"判断: 这块板的内核日志(dwc3/android_work)会
    持续刷屏, 提示符永远不在末尾, 按末尾判定会一直等不到(实测 90s 超时)。
    改为发 `echo 标记` 并找标记回显: 只要有回显, 说明 shell 已能执行命令。
    """
    marker = "__ZOPS_RDY__"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _api(base, "POST", "/api/tabs/0/send",
                 {"text": f"echo {marker}", "nl": True}, timeout=3)
        except Exception:
            time.sleep(3)
            continue
        time.sleep(2.5)
        try:
            txt = _read_offset(base, 2000)
        except Exception:
            continue
        if marker in txt:
            return True
        time.sleep(2)
    return False


def act_enter_loader(rig_name, rig):
    """全自动进 Loader: 板型核对 → reboot loader → 轮询 USB 枚举判定模式。

    依据本 SDK rk3588s.dtsi 的 reboot-mode 节点: mode-loader=BOOT_BL_DOWNLOAD,
    `reboot loader` 是内核正式支持路径; MaskROM 无软件入口(mode-maskrom 不存在)。
    """
    thit = find_target_instance(rig_name, rig)   # 登记口失效时自动重定位(线被挪走)
    ap = thit[0]
    base = f"http://127.0.0.1:{ap}"
    print(f"{rig_name}: 等 shell 提示符就绪(最多 90s; 刚起来时命令会被启动日志吞掉)...",
          flush=True)
    if not _wait_prompt(base):
        raise RigError(4, "90s 内未见 shell 提示符(板子还在启动/未进系统); "
                          "等系统起好再试, 或用 catch-uboot")
    model = send_and_read(ap, "cat /proc/device-tree/model", settle=1.0)
    print(f"{rig_name}: 板型核对: {(model or '').strip()[-80:]!r}")
    cmd = (rig.get("target") or {}).get("enter_loader_cmd")
    if cmd:
        print(f"{rig_name}: 用固件自带入口 {cmd!r}(init 横幅记载)...")
        send_and_read(ap, cmd, settle=1.0)
    else:
        send_and_read(ap, "reboot loader", settle=1.0)
    print(f"{rig_name}: 已下发, 盯 USB 枚举判定模式(至多 45s; loader 态串口静默属正常)...")
    for _ in range(24):
        time.sleep(2)
        mode = usb_mode_for(rig)
        if mode:
            break
    if not mode:
        print(f"{rig_name}: 45s 未见本板枚举(adb 序列号/350b/320b 均无) —— "
              f"下载线未接主机时 Loader 态不可见, 属预期; 烧录需接下载线+upgrade_tool/RKDevTool")
        return
    print(f"{rig_name}: USB {mode}")
    if "Loader" in mode:
        print(f"{rig_name}: 已在 Loader 态; 烧录后复位: python3 rigctl.py {rig_name} coldboot")


def _adb_devices_l():
    try:
        return subprocess.run(["adb", "devices", "-l"], capture_output=True, text=True,
                              timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


SYSFS_USB = "/sys/bus/usb/devices"        # USB 设备的 sysfs 根(测试可替换)


def _rk_usb_by_chain():
    """枚举总线上全部 Rockchip(2207) 设备: {链路段: {"pid","serial"}}。

    链路段从 sysfs 设备名换算(3-1.3.1.2 → "1.3.1.2"), 与 by-path/adb 的链路
    坐标系同源——判定"哪个口的 2207 是哪块板"从此精确到物理口, 不再类型级
    扫 lsusb(曾因此被 SSI_17 冒充)。
    """
    out = {}
    try:
        names = os.listdir(SYSFS_USB)           # 测试可换成临时目录(假 sysfs)
    except OSError:
        return out
    for name in names:
        if "-" not in name or ":" in name:      # 跳过接口级条目(3-1.2:1.0)
            continue
        base = f"{SYSFS_USB}/{name}"
        try:
            if open(f"{base}/idVendor").read().strip() != "2207":
                continue
            pid = open(f"{base}/idProduct").read().strip().lower()
            try:
                sn = open(f"{base}/serial").read().strip()
            except OSError:
                sn = ""
        except OSError:
            continue
        chain = name.split("-", 1)[1]           # 3-1.3.1.2 → 1.3.1.2
        out[chain] = {"pid": pid, "serial": sn}
    return out


def usb_mode_for(rig):
    """判定受控板下载口模式——以 adb 序列号认主, 杜绝总线上其他 2207 设备污染判定
    (2026-11-08 实训: SSI_17 设备占 2207:0006, 曾让 rig-A 被误判为运行态)。
    运行态: adb devices -l 按序列号匹配(链路一并报出); Loader/MaskROM: adb 不列,
    扫 lsusb 的 350b/320b(需下载线接主机才可见, 无 upgrade_tool 时按 PID 口诀)。"""
    tgt = rig.get("target") or {}
    dl = tgt.get("download_chain")
    # ① 序列号优先: 序列号烧在 SoC 里, 线挪到哪个口都追得到(2026-11-24 实测: 挪线后
    #    登记口失效, 但按序列号扫总线立刻找到新口) —— 挪线自动适应的关键
    adb_serial = tgt.get("adb_serial", "")
    if adb_serial:
        for chain, dev in _rk_usb_by_chain().items():
            if dev.get("serial") == adb_serial:
                pid = dev["pid"]
                moved = f"(口{dl}→{chain} 已自动追踪)" if dl and chain != dl else f"@口{chain}"
                return (f"2207:{pid} → {USB_PID_MODE.get(pid, '未知PID')}"
                        f", serial={adb_serial[:12]} {moved}")
    if dl:
        # ② 登记口兜底。走到这里说明"按序列号在总线上没找到本板"。登记口上若有设备,
        #    按它与本 rig 登记身份的关系分**互斥的三种**情况处理(正向写法, 每个分支
        #    行为唯一确定——之前的排除法写法里"别板设备"与兜底同样返回 None, 属等价
        #    变异, 变异测试无法证明该判断有效):
        dev = _rk_usb_by_chain().get(dl)
        if not dev:
            return None                           # 口上无 2207(线未插/USB口通道未供电)
        if dev["serial"] and adb_serial and dev["serial"] != adb_serial:
            # 口上是一块**别的板**(挪线后旧口被占; 本板在 Loader 态时 adb 序列号比对
            # 仍有效)。明确报"被占", 不认领 —— 这是防刷机撞号的最后一道闸。
            return (f"✘登记下载口 {dl} 上是别的板(serial={dev['serial'][:12]}, "
                    f"本 rig 登记 {adb_serial[:12]}), 不认领; 本板可能离线或被挪线, "
                    f"跑 bind-dl 重绑")
        if not adb_serial:
            # 本 rig 没登记序列号: 口上有带序列号的设备, 但无从确认是不是本板 → 不认领
            return (f"?口{dl} 有 2207:{dev['pid']} serial={dev['serial'][:12]} —— "
                    f"本 rig 未登记序列号, 无法确认归属; 跑 bind-dl 或在 rigs.json 填 "
                    f"target.adb_serial 后才算数")
        return (f"2207:{dev['pid']} → {USB_PID_MODE.get(dev['pid'], '未知PID')} "
                f"@下载口{dl}(设备无序列号, 按登记口认定)")
    # ③ 没有任何身份依据(未绑定下载口): **宁可报未见, 也不按类型(2207:350b)认领**。
    #    旧逻辑"总线上找到任意 350b 就算本 rig"是 SSI_17 冒充与刷机会话撞号的根源。
    pids = re.findall(r"ID\s+2207:([0-9a-f]{4})", _lsusb(), re.I)
    if pids:
        return (f"?未绑定下载口(总线有 {len(pids)} 个 2207 设备但不认领); "
                f"先跑 bind-dl")
    return None


def _diff_snapshot():
    """观察源快照: adb 序列号 + lsusb VID:PID + hub 各实例账本字节数。"""
    devs = {}
    for line in _adb_devices_l().splitlines():
        m = re.match(r"^(\S+)\s+\S+\s+usb:(\d+-[\d.]+)", line)
        if m:
            devs[f"adb:{m.group(1)}"] = "在线"
    for line in _lsusb().splitlines():
        m = re.search(r"ID\s+([0-9a-f]{4}):([0-9a-f]{4})", line, re.I)
        if m:
            devs[f"usb:{m.group(1).lower()}:{m.group(2).lower()}"] = "枚举"
    for t in hub_tabs():
        try:
            cb = _api(f"http://127.0.0.1:{t['agent_port']}", "GET", "/api/info",
                      timeout=5)["capture_bytes"]
            devs[f"tty:{t['agent_port']}({t['title']})"] = cb
        except Exception:
            pass
    return devs


def act_bind_h503(rig_name, rig, rigs_path, all_rigs, confirmed=False):
    """H503 通道↔板绑定: 断电差分法(金标准, skill 固化的 2026-10-27 事故对策)。

    ⚠ 破坏性: 逐路断电观察, 且 H503 无状态查询命令——扫完各路保持 OFF,
    原上电状态不可恢复(需人工按需重开)。故必须 --yes 显式确认。
    """
    ctrl = rig["controller"]
    if not confirmed:
        raise RigError(2, f"差分会逐路断电且结束后保持全 OFF(原状态不可恢复); "
                          f"确认接好线后跑: rigctl.py {rig_name} bind --yes")
    proto, _ = h503_detect(ctrl["cmd_port"])
    if not proto:
        raise RigError(3, "H503 命令口双协议探测均失败")
    binding = None
    for name in ("USB", "5V2", "5V3", "12V4", "12V5"):   # USB=板载USB2/USB3口供电
        a = _diff_snapshot()
        h503_power(ctrl["cmd_port"], name, 0, proto)
        time.sleep(5)
        b = _diff_snapshot()
        gone = [k for k, v in a.items() if k not in b and not isinstance(v, int)]
        gone_tty = [k for k in a if k not in b and k.startswith("tty:")]
        print(f"通道 {name} 断电后消失: {gone + gone_tty or '(无变化→空通道)'}")
        if gone or gone_tty:
            binding = name
            print(f"{rig_name}: 绑定命中 → 通道 {name} (记录进 rigs.json)")
            for k in gone:
                if k.startswith("adb:"):
                    rig["target"]["adb_serial"] = k[4:]
            for k in gone_tty:
                ap = k.split("(")[0][4:]
                tab = next((t for t in hub_tabs() if str(t["agent_port"]) == ap), None)
                if tab:
                    rig["target"]["console"] = hub_chain(tab["port"]) or tab["port"]
                    rig["target"]["baud"] = tab["baud"]
            rig["target"]["name"] = f"通道{name}目标板"
            rig["target"]["bound_at"] = time.strftime("%Y-%m-%d %H:%M")
            rig["target"]["bound_by"] = "rigctl bind 差分"
            break
        # 空通道保持 OFF, 继续下一路
    ctrl["power"]["channel"] = binding
    save_rigs(rigs_path, all_rigs)
    if binding is None:
        print(f"{rig_name}: 0x02..0x05 全部无差分 —— 没探到目标板; "
              f"确认目标板电源线接的是 H503 哪路(5V2/5V3/12V4/12V5), 或直接人工填 channel")
    else:
        print(f"{rig_name}: 现在可 status/on/off/coldboot 全套操作; "
              f"注意各路当前为 OFF(差分副作用), 按需 python3 rigctl.py {rig_name} on")


def _read_offset(base, back=4000):
    """读账本尾部 back 字节(绝对 offset 读, 不动游标), 返回清理过 ANSI 的文本。"""
    info = _api(base, "GET", "/api/info", timeout=8)["data"]
    end = info.get("capture_bytes") or 0
    off = max(0, end - back)
    d = _api(base, "GET", f"/api/tabs/0/receive?offset={off}&max_bytes={back}",
             timeout=10)["data"]
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", d.get("text") or "")


def act_catch_uboot(rig_name, rig, select_usb=True):
    """抓 U-Boot 救砖: 冷启动 + 在倒计时窗口内连发 Ctrl+C 进 U-Boot 菜单。

    适用场景: 内核 panic/卡死(rootfs 进不去, 没有 shell 可用), 此时 in-system 的
    `loader` 命令无效——唯一入口是 U-Boot 的 3 秒 autoboot 倒计时
    (`Hit key to stop autoboot('CTRL+C')`)。抓到后菜单里 [3] USB update images
    = 进 rockusb/Loader(2207:350b)。

    成功率取决于 CH340 恢复速度: 目标板供电的适配器在断电期间掉线, 上电后要等
    它重新枚举并打开, 这期间倒计时可能已过——故上电后先等实例 open, 再密集 spam。
    """
    thit = find_target_instance(rig_name, rig)   # 登记口失效时自动重定位
    ap = thit[0]
    base = f"http://127.0.0.1:{ap}"
    p = rig["controller"]["power"]
    act_power(rig_name, rig, p.get("off_value", 0), "关机")
    wait = int(p.get("discharge_s", 20))
    print(f"{rig_name}: 放电 {wait}s ...", flush=True)
    time.sleep(wait)
    act_power(rig_name, rig, p.get("on_value", 1), "开机")
    # 等串口实例重新打开(CH340 可能随断电掉线, 自动重连中)
    ready = False
    for _ in range(40):
        try:
            if _api(base, "GET", "/api/info", timeout=3)["data"].get("open"):
                ready = True
                break
        except Exception:
            pass
        time.sleep(0.3)
    print(f"{rig_name}: 串口{'就绪' if ready else '未就绪(仍尝试)'}, 密集抢按键覆盖倒计时 12s...",
          flush=True)
    # Ctrl+C 与回车混发: RK 的 autoboot 提示是 'CTRL+C', 但部分变体(如 FastBoot-MIPI)
    # 用任意键/回车拦停, 混发两类把命中率拉满。
    for i in range(96):          # ~12s
        key = "\x03" if i % 3 else "\r"
        try:
            _api(base, "POST", "/api/tabs/0/send", {"text": key, "nl": False}, timeout=3)
        except Exception:
            pass
        time.sleep(0.125)
    time.sleep(0.6)
    text = _read_offset(base, 20000)   # spam 期间菜单会重绘多次, 窗口取大防挤出
    if "U-boot MENU" in text or "USB update images" in text:
        print(f"{rig_name}: ✔ 已拦截 U-Boot, 进入菜单", flush=True)
        if not select_usb:
            print(f"{rig_name}: 停在菜单, 可手动选 [3] USB update images")
            return
        for _ in range(3):        # 菜单可能重绘, 多按几次
            _api(base, "POST", "/api/tabs/0/send", {"text": "3", "nl": False}, timeout=3)
            time.sleep(0.4)
        print(f"{rig_name}: 已选 [3] USB update images, 等 2207:350b ...", flush=True)
        for _ in range(20):
            time.sleep(1)
            pids = [x.lower() for x in re.findall(r"2207:([0-9a-f]{4})", _lsusb(), re.I)]
            if "350b" in pids:
                print(f"{rig_name}: ✔ Loader 模式就绪 (2207:350b) —— 可用 upgrade_tool/RKDevTool 烧录")
                return
        print(f"{rig_name}: ✘ 未见 350b(菜单可能超时或需再抓一次); 尾部: {text[-200:]!r}")
        return
    print(f"{rig_name}: ✘ 未捕获菜单(倒计时已过/串口就绪太慢); 尾部: {text[-260:]!r}")
    print(f"{rig_name}: 重试建议: 再跑一次 catch-uboot(多试几次命中率更高)")


def _h503_state(rig, channel):
    """读某通道当前通断(JSON 协议可查; A5/GPIO 治具返回 None 表示"无法回读")。"""
    ctrl = rig.get("controller") or {}
    if not str((ctrl.get("power") or {}).get("method", "")).startswith("h503") \
            or not ctrl.get("cmd_port") or channel is None:
        return None                     # GPIO 治具没有通道回读能力
    proto, _ = h503_detect(ctrl["cmd_port"])
    if proto != "json-v3.5":
        return None
    import json as _json
    import serial
    with _h503_lock(ctrl["cmd_port"]):
        with serial.Serial(ctrl["cmd_port"], timeout=1.2) as s2:
            s2.reset_input_buffer()
            s2.write(_json.dumps({"qr": channel}).encode() + b"\n")
            r = s2.read(200).decode("utf-8", "replace").strip()
    try:
        return _json.loads(r).get("st")
    except ValueError:
        return None


def _rx_of(rig):
    """受控板串口实例的实收字节数(判"板子是否有输出"), 拿不到返回 None。"""
    hit = find_instance(console_chain_of_rig(rig, "target"))
    if not hit:
        return None
    try:
        return _api(f"http://127.0.0.1:{hit[0]}", "GET", "/api/info",
                    timeout=6)["data"]["rx_total"]
    except Exception:
        return None


def _usb_has(pid):
    return any(x.lower() == pid.lower()
               for x in re.findall(r"2207:([0-9a-f]{4})", _lsusb(), re.I))


def _rig_in_mode(rig, pid):
    """**本 rig 登记的那块板**是否处于该 USB 模式(Loader=350b)。

    必须按序列号认主: 只看"总线上有没有 350b"的话, 别的板在 Loader 态就会让
    本 rig 的验证误判为通过(两块板都在时必然撞号)。
    """
    tgt = rig.get("target") or {}
    sn = tgt.get("adb_serial")
    devs = _rk_usb_by_chain()
    if sn:
        return any(d["pid"].lower() == pid.lower() and d["serial"] == sn
                   for d in devs.values())
    dl = tgt.get("download_chain")             # 无序列号: 退回登记口(弱证据)
    d = devs.get(dl) if dl else None
    return bool(d and d["pid"].lower() == pid.lower())


def act_bind_download(rig_name, rig, rigs_path, all_rigs):
    """绑定受控板的 USB 下载口(烧录口): 断电差分, 结论写回 rigs.json/boards.json。

    原理: 先确保 H503 的 USB 口通道供电(下载线多半插在那里), 快照全部 2207 设备
    (按物理链路); 断板子电 6s 再快照——**消失的那个口**就是本板下载口; 上电等它
    回来并记录 PID/serial。绑定后 usb_mode_for 精确到口, 不再被别的 RK 板冒充。
    """
    ctrl = rig.get("controller") or {}
    p = ctrl.get("power") or {}
    method = str(p.get("method", ""))
    usb_ch = ctrl.get("usb_port_channel")
    cmd_port = ctrl.get("cmd_port")
    if method.startswith("h503") and not p.get("channel"):
        raise RigError(3, "电源通道未绑定, 先跑 bind")   # 通道校验仅 H503; GPIO 治具用 gpios
    if method.startswith("h503") and usb_ch:      # USB 口供电仅 H503 有; GPIO 治具直接差分
        h503_power(cmd_port, usb_ch, 1, h503_detect(cmd_port)[0] or "json-v3.5")
        print(f"{rig_name}: 已开 USB 口通道 {usb_ch}(下载线供电)", flush=True)
        time.sleep(3)

    before = _rk_usb_by_chain()
    if before:
        print(f"{rig_name}: 当前 2207 设备: "
              f"{ {k: v['pid'] for k, v in before.items()} }", flush=True)
    act_power(rig_name, rig, p.get("off_value", 0), "关机[差分]")
    time.sleep(6)
    dark = _rk_usb_by_chain()
    gone = [c for c in before if c not in dark]
    act_power(rig_name, rig, p.get("on_value", 1), "开机[差分]")
    back, waited = {}, 0
    while waited < 40:                        # 等它重新枚举(0006 要等系统起来)
        time.sleep(4); waited += 4
        back = _rk_usb_by_chain()
        if set(gone) & set(back):
            break
    dl = [c for c in gone if c in back]
    if len(dl) > 1:
        # 多个口同时消失又回来 = 差分不干净(比如同通道上还挂着别的设备/集线器),
        # 瞎选第一个会绑错板 —— 必须人工分辨后手动填 download_chain
        raise RigError(4, f"差分命中 {len(dl)} 个口 {dl} 不唯一, 拒绝绑定; "
                          "请人工确认后直接在 rigs.json 填 target.download_chain")
    if not dl:
        tip = "、".join(gone) or "无设备消失"
        raise RigError(4, f"差分未命中: 断电后消失的口={tip} 且 40s 内未回来。"
                          "若 gone 为空: 下载线未插/USB口通道不对; 若没回来: 板子没起")
    chain = dl[0]
    dev = back[chain]
    tgt = rig["target"]
    tgt["download_chain"] = chain
    tgt["download_pid_now"] = dev["pid"]
    if dev["serial"]:
        tgt["adb_serial"] = dev["serial"]
    tgt["download_bound_at"] = time.strftime("%Y-%m-%d %H:%M")
    tgt["download_bound_by"] = "bind-dl 断电差分"
    save_rigs(rigs_path, all_rigs)
    try:
        bp = BOARDS_DEFAULT
        b = json.load(open(bp, encoding="utf-8"))
        ck = tgt.get("console")
        for k, v in b.items():
            if not k.startswith("_") and hub_chain(k) == ck:
                v["download_chain"] = chain
                if dev["serial"]:
                    v["adb_serial"] = dev["serial"]
        json.dump(b, open(bp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        open(bp, "a", encoding="utf-8").write("\n")
    except Exception as e:
        print(f"[warn] boards.json 同步失败: {e}", flush=True)
    # 二次复核: 再断电一次, 登记的口必须再次消失——否则说明差分是巧合, 回滚
    print(f"{rig_name}: 二次复核(再断电一次验证同一个口)…", flush=True)
    act_power(rig_name, rig, p.get("off_value", 0), "关机[复核]")
    time.sleep(6)
    check = _rk_usb_by_chain()
    act_power(rig_name, rig, p.get("on_value", 1), "开机[复核]")
    if chain in check:
        tgt.pop("download_chain", None); tgt.pop("download_pid_now", None)
        save_rigs(rigs_path, all_rigs)
        raise RigError(4, f"复核失败: 断电后口 {chain} 仍在(第一次差分是巧合), "
                          "已回滚绑定; 请检查是否有其他设备同电")
    mode = USB_PID_MODE.get(dev["pid"], dev["pid"])
    print(f"{rig_name}: ✔ 下载口绑定并复核通过 → 物理口 {chain} "
          f"(当前 {mode}, serial={dev['serial'][:16] or '无'})，已写入 rigs/boards",
          flush=True)


def act_verify(rig_name, rig, rigs_path, all_rigs, only=None, recover=False):
    """逐个动作真跑一遍并用硬证据判定, 结果写回 rigs.json 的 `verified`。

    证据分级(不靠"命令返回成功"):
      * on/off      → 通道通断回读(JSON 协议可查) 或 控制台 rc 哨兵
      * coldboot    → 受控串口实收字节是否增长(板子真的重启了)
      * enter-loader / catch-uboot → lsusb 是否出现 2207:350b(真进 Loader)
    没验过的动作, 网页按钮会显示 ⚠ —— 避免"按钮摆着但点了没用"。
    """
    acts = only or ["off", "on", "coldboot", "enter-loader", "catch-uboot"]
    p = rig["controller"]["power"]
    ch = p.get("channel")
    results = {}
    print(f"{rig_name}: 验证明细 ————————————————", flush=True)
    try:
      for act in acts:
          ok, why = False, ""
          try:
              if act == "off":
                  act_power(rig_name, rig, p.get("off_value", 0), "关机")
                  st = _h503_state(rig, ch)
                  ok = (st == 0) if st is not None else True
                  why = f"通道回读 st={st}" if st is not None else "控制台 rc=0"
              elif act == "on":
                  act_power(rig_name, rig, p.get("on_value", 1), "开机")
                  st = _h503_state(rig, ch)
                  ok = (st == 1) if st is not None else True
                  why = f"通道回读 st={st}" if st is not None else "控制台 rc=0"
              elif act == "coldboot":
                  before = _rx_of(rig) or 0
                  act_power(rig_name, rig, p.get("off_value", 0), "关机")
                  wait = int(p.get("discharge_s", 20))
                  print(f"{rig_name}: 放电 {wait}s ...", flush=True)
                  time.sleep(wait)
                  act_power(rig_name, rig, p.get("on_value", 1), "开机")
                  grew = 0
                  for _ in range(20):
                      time.sleep(2)
                      now = _rx_of(rig)
                      if now and now > before + 200:
                          grew = now - before
                          break
                  ok = grew > 0
                  why = f"串口新增 {grew} 字节(板子重启)" if ok else "40s 内串口无新增"
              elif act == "enter-loader":
                  act_enter_loader(rig_name, rig)
                  ok = _rig_in_mode(rig, "350b")   # 按本 rig 的板判定, 不看总线上任意 350b
                  why = "lsusb 2207:350b" if ok else \
                      "未见 350b —— 若下载线未接主机则无法验证(命令本身可能已生效)"
              elif act == "catch-uboot":
                  act_catch_uboot(rig_name, rig, select_usb=True)
                  ok = _rig_in_mode(rig, "350b")   # 按本 rig 的板判定, 不看总线上任意 350b
                  why = "lsusb 2207:350b" if ok else \
                      "未见 350b —— 该板 U-Boot 可能无 autoboot 倒计时(按键会直接落到 Linux shell)"
              else:
                  why = "未知动作"
          except SystemExit as e:
              why = f"失败: {e}"
          except Exception as e:
              why = f"异常: {e}"
          results[act] = {"ok": ok, "at": time.strftime("%Y-%m-%d %H:%M"),
                          "evidence": why}
          print(f"  {'✔' if ok else '✘'} {act:<13} {why}", flush=True)

    finally:
        if recover:
            print(f"{rig_name}: 验证后恢复系统(冷启动回系统)...", flush=True)
            try:
                act_power(rig_name, rig, p.get("off_value", 0), "关机")
                time.sleep(int(p.get("discharge_s", 20)))
                act_power(rig_name, rig, p.get("on_value", 1), "开机")
                print(f"{rig_name}: 已上电, 板子开始启动", flush=True)
            except SystemExit as e:
                print(f"{rig_name}: 恢复失败: {e}", flush=True)
            _ensure_power_on(rig_name, rig)      # 无论如何保证通电
        else:
            _ensure_power_on(rig_name, rig)      # 验证完也不许留断电

    rig.setdefault("verified", {}).update(results)
    save_rigs(rigs_path, all_rigs)
    passed = sum(1 for v in results.values() if v["ok"])
    print(f"{rig_name}: {passed}/{len(results)} 项通过, 结果已写入 rigs.json", flush=True)


def usb_alive(target):
    vidpid = (target.get("alive_check") or {}).get("usb_vid_pid")
    if not vidpid:
        return None
    try:
        out = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    prefix = vidpid.lower().replace(":", "")
    for line in out.splitlines():
        m = re.search(r"ID\s+([0-9a-f]{4}):([0-9a-f]{4})", line, re.I)
        if m and (m.group(1) + m.group(2)).startswith(prefix.split(":")[0][:4]):
            if f"{m.group(1)}:{m.group(2)}" == vidpid.lower():
                return line.strip()
    return False


def act_status(rig_name, rig):
    parts = []
    ctrl = rig.get("controller") or {}
    if str(ctrl.get("power", {}).get("method", "")).startswith("h503"):
        proto, msg = h503_detect(ctrl["cmd_port"])
        ch = ctrl["power"].get("channel")
        parts.append(f"控制器{ctrl.get('name','H503')} "
                     f"{'✔'+proto+'('+msg+')' if proto else '✘双协议探测失败('+msg+')'}"
                     + (f" 通道{ch}" if ch is not None else " 通道未绑定"))
        thit = find_instance(console_chain_of_rig(rig, "target"))
        tgt = rig.get("target") or {}
        if thit:
            parts.append(f"受控{tgt.get('name','?')} 串口 ✔(实例{thit[0]})")
        else:
            parts.append(f"受控{tgt.get('name','?')} 串口 ✘ 无实例")
        mode = usb_mode_for(rig)
        parts.append(f"USB {mode}" if mode else "USB 未见本板(未接下载线/未枚举)")
        print(f"{rig_name}: " + " | ".join(parts))
        return
    hit = find_instance(console_chain_of_rig(rig, "controller"))
    if hit:
        _ap, tab = hit
        parts.append(f"控制器{rig['controller']['name']} ✔(实例{hit[0]} @{tab['baud']} open={tab['open']})")
    else:
        parts.append(f"控制器控制台 ✘ 无实例")
    thit = find_instance(console_chain_of_rig(rig, "target"))
    tgt = rig["target"]
    if thit:
        _ap, tab = thit
        parts.append(f"受控{tgt['name']} 串口 ✔(实例{thit[0]} rx={tab['rx_count']})")
    else:
        parts.append(f"受控{tgt['name']} 串口 ✘ 无实例")
    mode = usb_mode_for(rig)
    if mode:
        parts.append(f"USB {mode} ✔")
    else:
        parts.append("USB 未见本板(下载线未接主机/未枚举)")
    print(f"{rig_name}: " + " | ".join(parts))


def _capture_grow_map(candidates, seconds):
    """轮询各实例 capture_bytes, 返回 {agent_port: 是否有增长}。"""
    marks = {}
    for ap in candidates:
        try:
            marks[ap] = _api(f"http://127.0.0.1:{ap}", "GET", "/api/info", timeout=5)["capture_bytes"]
        except NET_ERRORS:
            marks[ap] = None
    time.sleep(seconds)
    grew = {}
    for ap in candidates:
        try:
            now = _api(f"http://127.0.0.1:{ap}", "GET", "/api/info", timeout=5)["capture_bytes"]
        except NET_ERRORS:
            now = None
        grew[ap] = (marks[ap] is not None and now is not None and now > marks[ap])
    return grew


def act_bind(rig_name, rig, rigs_path, all_rigs):
    """重探测受控槽位: 关电→盯谁灭→开电→盯谁醒→写回 target。"""
    ctrl = find_instance(console_chain_of_rig(rig, "controller"))
    if not ctrl:
        raise RigError(3, "控制器控制台无在线实例, 无法 bind")
    ctrl_ap = ctrl[0]
    # 候选 = hub 里除控制器自己外的全部实例(受控板控制台必然插在主机上)
    cands = [t["agent_port"] for t in hub_tabs() if t["agent_port"] != ctrl_ap]
    if not cands:
        raise RigError(3, "hub 里没有候选实例(受控板串口实例必须在线)")
    print(f"{rig_name}: bind 开始, 候选实例 {cands}(会真实断电再上电受控板)")
    act_power(rig_name, rig, rig["controller"]["power"]["off_value"], "关电")
    dark = {ap for ap, g in _capture_grow_map(cands, BIND_WATCH_S).items() if not g}
    act_power(rig_name, rig, rig["controller"]["power"]["on_value"], "开电")
    boot = {ap for ap, g in _capture_grow_map(cands, BIND_WATCH_S).items() if g}
    hits = dark & boot if dark else boot
    if len(hits) != 1:
        raise RigError(4, f"探测不唯一: 灭={sorted(dark)} 醒={sorted(boot)}; "
                          "把受控板串口实例起好、其余口静默后重试, 或手改 rigs.json")
    ap = hits.pop()
    tab = next(t for t in hub_tabs() if t["agent_port"] == ap)
    old = console_chain_of_rig(rig, "target")
    new_chain = hub_chain(tab["port"])
    if old and old != new_chain:
        rig.setdefault("_history", []).append(
            {"target": f"{rig['target'].get('name', '?')} console@{old}", "until": time.strftime("%Y-%m-%d %H:%M"),
             "note": "bind 重探测迁移"})
    rig["target"]["console"] = new_chain
    rig["target"]["baud"] = tab["baud"]
    rig["target"]["bound_at"] = time.strftime("%Y-%m-%d")
    rig["target"]["bound_by"] = f"rigctl bind(实例{ap})"
    save_rigs(rigs_path, all_rigs)
    print(f"{rig_name}: 受控槽位 → {tab['port']}(@{tab['baud']}, 实例{ap}), 已写回 rigs.json")


# ---------------------------------------------------------------- 入口

# ---------------------------------------------------------------- 在线检测

RK_LOADER_PIDS = ("350b", "350e")       # 3588 系 Loader=350b, 3576=350e; MaskROM=320b
_MODE_LABEL = {"system": "运行态", "loader": "Loader", "maskrom": "MaskROM"}


def _rk_mode(pid):
    """RK 设备 PID → 运行态(system) / Loader / MaskROM。"""
    pid = (pid or "").lower()
    if pid == "320b":
        return "maskrom"
    return "loader" if pid in RK_LOADER_PIDS else "system"


def online_report(rigs=None, bus=None, tabs=None):
    """在线检测(只读、不动板子): 现在哪些开发板在线、各是哪个 rig 的板。

    判据 = 下载口上的 SoC 序列号(烧在 SoC 里, 线挪到哪都认得):
      * 总线上的序列号 = 某 rig 登记的 target.adb_serial → 该 rig 的板在线;
      * 不属于任何 rig → 未登记设备(只能报在线, 叫不出是哪块——认它要靠 bind-dl 的电力差分);
      * rig 登记的序列号不在总线上 → 离线(下载线没接 / 板子没电 / 换过板而登记没跟上)。
    控制台一侧只陈述事实(登记口上有没有实例), 不做任何探测。
    rigctl online / hub /api/rigs / MCP zops_rigs / SessionStart hook / 热插拔 都走这一个函数。
    """
    rigs = rigs if rigs is not None else load_rigs(RIGS_DEFAULT)
    bus = _rk_usb_by_chain() if bus is None else bus
    tabs = hub_tabs() if tabs is None else tabs
    owner = {(r.get("target") or {}).get("adb_serial"): n for n, r in rigs.items()
             if (r.get("target") or {}).get("adb_serial")}
    found, unregistered = {}, []
    for chain, dev in sorted(bus.items()):
        sn = dev.get("serial") or ""
        item = {"serial": sn or None, "mode": _rk_mode(dev.get("pid")), "download_chain": chain}
        if sn in owner:
            found[owner[sn]] = item
        else:
            unregistered.append(item)
    boards = []
    for name, rig in rigs.items():
        tgt = rig.get("target") or {}
        hit = found.get(name)
        con = tgt.get("console")
        tab = next((t for t in tabs if con and port_matches(t.get("port"), con)), None)
        boards.append({
            "rig": name, "name": tgt.get("name"),
            "online": hit is not None,
            "mode": hit["mode"] if hit else None,
            "serial": tgt.get("adb_serial"),
            "download_chain": hit["download_chain"] if hit else None,   # 离线=没观测到, 不拿登记值冒充
            "console": con, "console_instance": tab["agent_port"] if tab else None,
        })
    on = [b for b in boards if b["online"]]
    off = [b["rig"] for b in boards if not b["online"]]
    parts = []
    if on:
        parts.append("在线 " + "、".join(
            f"{b['rig']} {b['name']}({_MODE_LABEL[b['mode']]})" for b in on))
    if off:
        parts.append("离线 " + "、".join(off))
    parts.append(f"未登记设备 {len(unregistered)} 个")
    return {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "boards": boards,
            "unregistered": unregistered, "summary": "; ".join(parts)}


def print_online(rep):
    print(f"ZOps 在线检测 {rep['at']}")
    print(f"  {rep['summary']}")
    for b in rep["boards"]:
        state = _MODE_LABEL[b["mode"]] if b["online"] else "离线"
        con = f"控制台={b['console']}" + (f"(实例{b['console_instance']})" if b["console_instance"] else "(无实例)")
        dl = f"下载口={b['download_chain']}" if b["download_chain"] else "下载口=未登记"
        print(f"  {b['rig']:<8} {str(b['name']):<10} {state:<6} serial={str(b['serial'])[:12]}  {dl}  {con}")
    for u in rep["unregistered"]:
        extra = f" serial={u['serial'][:12]}" if u["serial"] else " (设备无序列号)"
        print(f"  未登记  {'—':<10} {_MODE_LABEL[u['mode']]:<6} 下载口={u['download_chain']}{extra}"
              "  ← 不属于任何 rig; 认它: rigctl <组合名> bind-dl")


# ---------------------------------------------------------------- 标记法绑定控制台

def find_console_by_nonce(rig):
    """不断电, 找出本 rig 受控板的串口控制台在哪个口(板子须在运行态且 adb 可用)。

    做法: 经下载口 adb 让板子往 /dev/console 打一个唯一标记, 看哪个串口实例收到它。
    标记只可能从这块板的 console 出来, 所以是硬证据; 代价只是板子 console 上多一行
    无害的 __ZOPSC_…__。relocate(电力差分)要断电重启板子, 能用这个就别用它。
    返回 (收到标记的 tab 列表, 标记)。
    """
    tgt = rig.get("target") or {}
    sn = tgt.get("adb_serial")
    if not sn or not any(d.get("serial") == sn for d in _rk_usb_by_chain().values()):
        raise RigError(3, f"登记的板(serial={str(sn)[:12]})不在线, 无法用标记法定位控制台")
    tabs = hub_tabs()
    base = {}
    for t in tabs:
        try:
            base[t["agent_port"]] = _api(f"http://127.0.0.1:{t['agent_port']}", "GET",
                                         "/api/info")["data"]["capture_bytes"]
        except Exception as e:
            print(f"[bind-console] 读账本 @{t['agent_port']} 失败: {e}", flush=True)
    marker = f"__ZOPSC_{os.getpid() % 100000}_{int(time.time()) % 100000}__"
    r = subprocess.run(["adb", "-s", sn, "shell", f"echo {marker} > /dev/console"],
                       capture_output=True, text=True, timeout=20)
    err = (r.stdout + r.stderr).strip()
    if r.returncode != 0 or "error" in err.lower() or "not found" in err.lower():
        raise RigError(3, f"adb -s {sn[:12]} 写标记失败: {err[:140] or '退出码 %d' % r.returncode}")
    time.sleep(1.5)
    hits = []
    for t in tabs:
        b0 = base.get(t["agent_port"])
        if b0 is None:
            continue
        try:
            d = _api(f"http://127.0.0.1:{t['agent_port']}", "GET",
                     f"/api/tabs/0/receive?offset={b0}&max_bytes=8000")["data"]
        except Exception as e:
            print(f"[bind-console] 读回 @{t['agent_port']} 失败: {e}", flush=True)
            continue
        if marker in (d.get("text") or ""):
            hits.append(t)
    return hits, marker


def act_bind_console(rig_name, rig, rigs_path=None, all_rigs=None, write=False):
    """用标记法核对本 rig 的控制台口; write=True 时把结果回写 rigs.json / boards.json。"""
    hits, marker = find_console_by_nonce(rig)
    tgt = rig.setdefault("target", {})
    old = tgt.get("console")
    if len(hits) != 1:                           # 标记法: 必须恰好命中一个串口实例
        raise RigError(4, f"{rig_name}: 标记 {marker} 命中 {len(hits)} 个串口实例(应为 1)。"
                          "0 个 = 控制台线没接/该口没起实例/收方向不通, 不是标记法能解决的")
    tab = hits[0]
    chain = hub_chain(tab["port"]) or tab["port"]
    same = old == chain
    print(f"{rig_name}: 标记 {marker} 只出现在 {tab['port']} (实例{tab['agent_port']}) → "
          f"控制台口 = {chain}; 登记值 = {old} ({'一致' if same else '不一致'})", flush=True)
    if not write:
        if not same:
            print(f"{rig_name}: 未改登记; 加 --write 回写", flush=True)
        return chain, tab
    tgt["console"] = chain
    tgt["console_bound_at"] = time.strftime("%Y-%m-%d %H:%M")
    tgt["console_bound_by"] = "标记法(adb→console)"
    if old and old != chain:
        tgt["console_relocated_from"] = old
    if rigs_path and all_rigs is not None:
        save_rigs(rigs_path, all_rigs)
    # boards.json: 板名条目跟着控制台口走(旧口上的同名条目删掉, 免得日后别的适配器插上去被冒名)
    try:
        bp = BOARDS_DEFAULT
        b = json.load(open(bp, encoding="utf-8"))
        name, sn = tgt.get("name"), tgt.get("adb_serial")
        for k in [k for k in b if not k.startswith("_") and name and isinstance(b[k], dict)
                  and b[k].get("name") == name and hub_chain(k) != chain]:
            del b[k]
        port = tab.get("port") or ""
        key = port.rsplit("/", 1)[-1] if "by-path/" in port else None
        if not key:
            try:
                key = next(e for e in os.listdir("/dev/serial/by-path") if hub_chain(e) == chain)
            except (OSError, StopIteration):
                key = None
        if key:
            entry = b.get(key) if isinstance(b.get(key), dict) else {}
            entry["name"] = name
            for f in ("adb_serial", "download_chain"):   # 与 rigs.json 保持同一份身份信息
                if tgt.get(f):
                    entry[f] = tgt[f]
            entry["note"] = (f"rig 控制台口; {tgt['console_bound_at']} 标记法实证"
                             f"(serial {str(sn)[:12]})" + (f", 由 {old} 挪来" if old and old != chain else ""))
            b[key] = entry
        json.dump(b, open(bp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        open(bp, "a", encoding="utf-8").write("\n")
    except Exception as e:
        print(f"[warn] boards.json 同步失败: {e}", flush=True)
    print(f"{rig_name}: ✔ 控制台登记 {old} → {chain}, 已写入 rigs/boards", flush=True)
    return chain, tab


def registry_sanity(rigs=None):
    """登记表自检: 找出物理上不可能/自相矛盾的登记, 返回问题列表(空=没问题)。

    曾经手滑把 rig-A 的 download_chain 写成和 console 同一个口(1.4.3)——串口线和
    USB 下载线不可能插在同一个物理口。这类笔误不会报错, 只会让"按登记口兜底"
    的判断悄悄失效, 所以单独查。
    """
    rigs = rigs if rigs is not None else load_rigs(RIGS_DEFAULT)
    problems = []
    for name, rig in rigs.items():
        t = rig.get("target") or {}
        c, dl = t.get("console"), t.get("download_chain")
        if c and dl and c == dl:
            problems.append(f"{name}: 控制台口与下载口登记成了同一个口({c}) —— 物理上不可能, "
                            f"请核对后改 rigs.json 的 target.download_chain")
    # 跨 rig: 一个物理口只能插一根线(串口线/下载线各算), 两个 rig 登记同一个口必有一错
    owners = {}
    for name, rig in rigs.items():
        t = rig.get("target") or {}
        for kind, key in (("控制台", "console"), ("下载口", "download_chain")):
            v = t.get(key)
            if v:
                owners.setdefault(v, []).append(f"{name}.{kind}")
    for port, who in owners.items():
        if len(who) > 1:
            problems.append(f"物理口 {port} 被重复登记: {who} —— 一个口只能插一根线, "
                            f"必有一处是过期的, 请按序列号追踪/差分结果更正")
    return problems


def selftest():
    assert hub_chain("pci-0000:0c:00.3-usb-0:1.4.2:1.0-port0") == "1.4.2"
    assert hub_chain("1.4.2") == "1.4.2"
    assert hub_chain("ttyUSB3") is None
    assert port_matches("pci-0000:0c:00.3-usb-0:1.4.2:1.0-port0", "1.4.2")
    assert port_matches("/dev/ttyUSB3", "ttyUSB3")
    assert not port_matches("pci-0000:0c:00.3-usb-0:1.4.4:1.0-port0", "1.4.2")
    # 节点解析: 实例报 ttyUSBn、登记键是 by-path 链路(或反之)也必须对上
    # (不写死具体口——线位随时会变; 取当前实际存在的任一单口适配器验证)
    chains = sorted({hub_chain(x) for x in os.listdir("/dev/serial/by-path")
                     if x.endswith("-port0")} - {None}) if os.path.isdir("/dev/serial/by-path") else []
    if chains:
        c = chains[0]
        n = serial_node(c)
        assert n and n.startswith("tty"), f"{c} 节点解析失败: {n}"
        assert port_matches("/dev/" + n, c), f"节点匹配异常: {n} vs {c}"
        print(f"selftest OK: 链路归一化/节点解析({c}→{n})/匹配 全过")
    else:
        print("selftest OK: 链路归一化/匹配 全过 (无 by-path 适配器, 跳过节点解析)")

    # 登记表自检: 控制台口 ≠ 下载口(手滑写成同口曾让按口兜底悄悄失效)
    bad = registry_sanity({"rig-X": {"target": {"console": "1.4", "download_chain": "1.4"}},
                           "rig-Y": {"target": {"console": "1.2", "download_chain": "1.3"}}})
    assert bad and any("rig-X" in x and "同一个口" in x for x in bad), bad
    assert registry_sanity({"rig-Y": {"target": {"console": "1.2", "download_chain": "1.3"}}}) == []
    # 跨 rig 重复登记同一个口
    dup = registry_sanity({"rig-P": {"target": {"console": "1.2", "download_chain": "1.3"}},
                           "rig-Q": {"target": {"console": "1.4", "download_chain": "1.3"}}})
    assert len(dup) == 1 and "重复登记" in dup[0], dup
    real_bad = registry_sanity()
    if real_bad:
        print("⚠ 登记表自检发现问题:")
        for x in real_bad:
            print("   ", x)


def main():
    args = sys.argv[1:]
    rigs_path = os.environ.get("RIGCTL_RIGS", RIGS_DEFAULT)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip())
        return
    if args[0] == "--selftest":
        selftest()
        return
    rigs = load_rigs(rigs_path)
    if not args:
        raise RigError(2, f"没有组合登记: {rigs_path}")
    name = args[0] if args[0] in rigs else None
    action = args[1] if name else args[0]
    if name is None:
        # 无组合名: status 广播 / online 在线检测
        if action == "online":
            rep_ = online_report(rigs=rigs)
            if "--json" in args:
                print(json.dumps(rep_, ensure_ascii=False, indent=1))
            else:
                if "--context" in args:      # SessionStart hook 用: 带说明头的注入文本
                    print("【ZOps 板子在线状态 · 本机 hook 开局注入】这是开局时刻的快照, 之后插拔不会更新;"
                          " 最新状态用: python3 " + os.path.abspath(__file__) + " online (或 MCP 工具 zops_rigs)")
                print_online(rep_)
            return
        if action != "status":
            raise RigError(2, f"未知组合 {args[0]!r}(现有: {', '.join(rigs)})")
        for n, r in rigs.items():
            act_status(n, r)
        return
    if len({n for n in rigs if n.startswith(name)}) > 1 and name not in rigs:
        raise RigError(2, f"组合前缀 {name!r} 歧义")
    if name not in rigs:
        raise RigError(2, f"没有组合 {name!r}(现有: {', '.join(rigs)})")
    rig = rigs[name]
    action = args[1] if len(args) > 1 else "status"
    if action == "status":
        act_status(name, rig)
    elif action == "on":
        act_power(name, rig, rig["controller"]["power"]["on_value"], "开机")
    elif action == "off":
        act_power(name, rig, rig["controller"]["power"]["off_value"], "关机")
    elif action == "coldboot":
        p = rig["controller"]["power"]
        act_power(name, rig, p["off_value"], "关机")
        wait = int(p.get("discharge_s", 20))
        print(f"{name}: 放电等待 {wait}s ...")
        time.sleep(wait)
        act_power(name, rig, p["on_value"], "开机")
        print(f"{name}: 冷启动已触发, 盯受控串口看 boot 洪流")
    elif action == "bind":
        if str((rig.get("controller") or {}).get("power", {}).get("method", "")).startswith("h503"):
            act_bind_h503(name, rig, rigs_path, rigs, confirmed="--yes" in args)
        else:
            act_bind(name, rig, rigs_path, rigs)
    elif action == "enter-loader":
        act_enter_loader(name, rig)
    elif action == "catch-uboot":
        act_catch_uboot(name, rig, select_usb="--menu" not in args)
    elif action == "bind-dl":
        act_bind_download(name, rig, rigs_path, rigs)
    elif action == "bind-console":
        act_bind_console(name, rig, rigs_path, rigs, write="--write" in args)
    elif action == "relocate":
        find_target_instance(name, rig, rigs_path, rigs) or relocate_console(name, rig, rigs_path, rigs)
    elif action == "verify":
        only = None
        if "--only" in args:
            only = args[args.index("--only") + 1].split(",")
        act_verify(name, rig, rigs_path, rigs, only=only,
                   recover="--recover" in args)
    else:
        raise RigError(2, f"未知动作 {action!r}(status/on/off/coldboot/bind/bind-dl/bind-console/relocate/verify)")


if __name__ == "__main__":
    try:
        main()
    except RigError as e:
        print(f"[rigctl] {e}", file=sys.stderr)
        sys.exit(e.code)
