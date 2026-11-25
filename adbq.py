#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""adbq.py —— 按板名操作 adb 的包装器(多板防串台)。

背景: RK 板的 USB adb 序列号大量重复甚至为空, 多板同插时 `adb -s 序列号` 失效,
照着 -s 打就会打到错的板上(串口侧"认错板子"的 adb 版)。本工具用与串口防线同一套
身份体系: `adb devices -l` 每台设备自带 `usb:1-x.y.z` 物理链路, 与 boards.json 里
by-path 键的 hub 链路段(如 1.4.4)是同一个东西——板名映射一份通吃串口/adb 两界。

用法:
    adbq boards                      # 列在线 adb 设备 + 板名解析结果(先看这个)
    adbq TQ3588 shell ls /           # 按板名执行任意 adb 子命令(自动解析成 adb -s)
    adbq TQ3588 -n shell ls /        # 只打印解析后的命令, 不执行
    adbq --selftest                  # 内嵌自检(解析器, 不碰真设备)

约定与边界:
    * boards.json 里只有 by-path 键(含 "usb-")能参与 adb 解析; ttyUSBx 过渡键不行
      (它不携带物理链路信息, 这也是尽快迁 by-path 的又一个理由);
    * 板名可写全名也可写前缀(唯一匹配即可, 如 TQ3 匹配 TQ3588);
    * 不带板名直接跑 adb 的"广播"语义(如 adb install)在多板时是事故源, 本工具
      强制要求板名, 不提供广播;
    * adb 版本旧(如 Debian 源 29.x)遇到新 adbd 可能 unauthorized, 与本工具无关,
      需换 platform-tools。
"""

import json
import os
import re
import subprocess
import sys

BOARDS_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "boards.json")
ADB = os.environ.get("ADBQ_ADB", "adb")
DEVICE_STATES = ("device", "offline", "unauthorized", "recovery", "sideload", "bootloader", "host", "authorizing")


def hub_chain_of_usb(usb_prop):
    """adb 的 usb 属性("1-1.4.4" = 总线1, 链路1.4.4) -> 链路段 "1.4.4"。"""
    if not usb_prop:
        return None
    m = re.match(r"^\d+-(.+)$", usb_prop.strip())
    return m.group(1) if m else None


def hub_chain_of_key(key):
    """boards.json 的 by-path 键(pci-...-usb-0:1.4.4:1.0-port0) -> 链路段 "1.4.4"。

    与 agent_hub.Hub._jack_of 同一规则: 取 "usb-" 后第一个冒号段(=hub 链)。
    非 by-path 键(ttyUSBx 等)返回 None, 不参与 adb 解析。
    """
    m = re.search(r"usb-([\d.:]+)", key)
    if not m:
        return None
    segs = m.group(1).split(":")
    return segs[1] if len(segs) > 1 else segs[0]


def load_boards(boards_path, rigs_path=None):
    """板名登记 → {板名: {"serial": SoC序列号或None, "chains": [下载口链路…]}}

    身份依据优先级(与 rigctl.usb_mode_for 同一套, 避免两处各说各话):
      ① 序列号(烧在 SoC 里, 线挪到哪都追得到) —— rigs.json 的 target.adb_serial
         (电力差分实证) 优先, 其次 boards.json 的 adb_serial;
      ② 下载口链路 boards.json 的 download_chain;
      ③ 板名条目的键(控制台口) —— 仅兜底"adb 线与控制台同口"的旧布局。
    rigs.json 与 boards.json 不一致时(线被挪过、只更新了一边), 序列号说了算。
    """
    def read(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}
    mapping = read(boards_path)
    boards = {}
    for key, entry in mapping.items():
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        b = boards.setdefault(entry["name"], {"serial": None, "chains": []})
        if entry.get("adb_serial"):
            b["serial"] = entry["adb_serial"]
        if entry.get("download_chain"):
            b["chains"].append(entry["download_chain"])
        ch = hub_chain_of_key(key)
        if ch:
            b["chains"].append(ch)
    if rigs_path is None:
        rigs_path = os.path.join(os.path.dirname(os.path.abspath(boards_path)), "rigs.json")
    for rig in read(rigs_path).values():
        if not isinstance(rig, dict):
            continue
        t = rig.get("target") or {}
        name, sn = t.get("name"), t.get("adb_serial")
        if not name or not sn:
            continue
        b = boards.setdefault(name, {"serial": None, "chains": []})
        b["serial"] = sn                        # rigs.json(差分实证)优先
        if t.get("download_chain") and t["download_chain"] not in b["chains"]:
            b["chains"].append(t["download_chain"])
    return boards


def load_chain_names(boards_path):
    """兼容旧调用: {链路: 板名}(仅用于 `adbq boards` 的展示)。"""
    out = {}
    for name, b in load_boards(boards_path).items():
        for c in b["chains"]:
            out.setdefault(c, name)
    return out


def adb_devices():
    """adb devices -l -> [{serial, state, usb, model, product, chain}]。"""
    try:
        out = subprocess.run([ADB, "devices", "-l"], capture_output=True, text=True,
                             timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[adbq] adb 不可用({e}), 检查 PATH 或 ADBQ_ADB 环境变量", file=sys.stderr)
        sys.exit(2)
    devices = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[1] not in DEVICE_STATES:
            continue
        props = {}
        for token in parts[2:]:
            if ":" in token:
                k, _, v = token.partition(":")
                props[k] = v
        devices.append({"serial": parts[0], "state": parts[1],
                        "usb": props.get("usb"), "model": props.get("model"),
                        "product": props.get("product"),
                        "chain": hub_chain_of_usb(props.get("usb"))})
    return devices


def resolve(board, devices, boards):
    """板名(或唯一前缀) -> 目标设备; 身份以序列号为准, 链路仅兜底。

    解析不出/不唯一/不在线时给出可执行的报错。
    """
    names = sorted(boards)
    candidates = [n for n in names if n.startswith(board)]
    if not candidates:
        raise SystemExit(f"[adbq] 登记里没有板名 {board!r}(现有: {', '.join(names) or '无'})")
    if len(candidates) > 1:
        raise SystemExit(f"[adbq] 前缀 {board!r} 歧义: {candidates}, 请写全名")
    target = candidates[0]
    info = boards[target]
    want = info.get("serial")
    if want:                                     # ① 序列号: 线挪到哪都认得
        for d in devices:
            if d["serial"] == want:
                return target, d
        online = ", ".join(f"{d['serial'][:12]}@{d['chain']}" for d in devices) or "无设备在线"
        raise SystemExit(f"[adbq] 板 {target}(serial={want[:12]}) 不在线(在线: {online}); "
                         "线被挪/板子未上电/未进系统时属正常")
    hits = [d for d in devices if d["chain"] in info["chains"]]   # ② 链路兜底
    if not hits:
        online = ", ".join(f"{d['chain'] or '?'}({d['serial'][:12]})" for d in devices) or "无设备在线"
        raise SystemExit(f"[adbq] 板 {target} 不在线(在线: {online}); "
                         "该板未登记序列号——跑 rigctl <组合名> bind-dl 后按序列号寻址更可靠")
    if len(hits) > 1:
        raise SystemExit(f"[adbq] 板 {target} 的链路 {info['chains']} 命中多台"
                         f"({[d['serial'] for d in hits]}), 请人工核对 adbq boards")
    return target, hits[0]


def cmd_boards(boards_path):
    devices = adb_devices()
    boards = load_boards(boards_path)
    chain_names = {}
    for name, bi in boards.items():
        for c in bi["chains"]:
            chain_names.setdefault(c, name)
    if not devices:
        print("(无 adb 设备在线)")
        return
    for d in devices:
        name = chain_names.get(d["chain"]) or "?"
        print(f"{name:<16} state={d['state']:<12} chain={d['chain'] or '?':<8} "
              f"serial={d['serial'][:16]:<16} model={d['model'] or '-'} "
              f"usb={d['usb'] or '-'}")
    if any(chain_names.get(d["chain"]) is None for d in devices if d["chain"]):
        print("(带 ? 的设备: boards.json 无此链路的 by-path 键, 补一行即可)")


def selftest():
    """内嵌自检: 解析器逻辑(重复序列号/链路映射/前缀歧义), 不碰真设备。"""
    assert hub_chain_of_usb("1-1.4.4") == "1.4.4"
    assert hub_chain_of_usb(None) is None
    assert hub_chain_of_usb("2-1") == "1"
    key = "pci-0000:0c:00.3-usb-0:1.4.4:1.0-port0"
    assert hub_chain_of_key(key) == "1.4.4", hub_chain_of_key(key)
    assert hub_chain_of_key("ttyUSB0") is None
    # 无序列号的板(只能靠链路): 两台同序列号 RK 板靠物理口区分
    boards = {"TQ3588": {"serial": None, "chains": ["1.2"]},
              "TQ3588B": {"serial": None, "chains": ["1.4.4"]},
              "TQ3562": {"serial": None, "chains": ["1.4.1"]}}
    devices = [{"serial": "20080411", "state": "device", "usb": "1-1.4.4",
                "model": None, "product": None, "chain": "1.4.4"},
               {"serial": "20080411", "state": "device", "usb": "1-1.4.1",
                "model": None, "product": None, "chain": "1.4.1"}]
    name, dev = resolve("TQ3562", devices, boards)
    assert (name, dev["chain"]) == ("TQ3562", "1.4.1")
    name, dev = resolve("TQ3588B", devices, boards)
    assert dev["chain"] == "1.4.4", "同序列号必须仍能选中对的板"
    try:
        resolve("TQ3588", devices, boards)       # TQ3588 是 TQ3588B 的前缀但也是全名
        raise AssertionError("前缀歧义未报错")
    except SystemExit as e:
        assert "歧义" in str(e), e
    # 有序列号的板: 线挪到别的口, 登记链路已过期, 仍按序列号找对
    boards2 = {"3588 A": {"serial": "ba27", "chains": ["1.3"]},
               "3588 B": {"serial": "45ef", "chains": ["1.4.4"]}}
    moved = [{"serial": "45ef", "chain": "1.3"}, {"serial": "ba27", "chain": "1.4.4"}]
    for nm, want in (("3588 A", "ba27"), ("3588 B", "45ef")):
        _, d = resolve(nm, moved, boards2)
        assert d["serial"] == want, f"{nm} 解析到了 {d['serial']}"
    # 登记口被别人的板占了且本板没序列号 → 不认领(报不在线)
    boards3 = {"X": {"serial": None, "chains": ["1.3"]}, "Y": {"serial": None, "chains": []}}
    try:
        resolve("Y", [{"serial": "zz", "chain": "1.3"}], boards3)
        raise AssertionError("未登记链路的板不该命中任何设备")
    except SystemExit as e:
        assert "不在线" in str(e), e
    print("selftest OK: 链路解析/重复序列号消歧/前缀歧义/序列号追踪挪线/不认领 全部通过")


def main():
    args = sys.argv[1:]
    boards_path = os.environ.get("ADBQ_BOARDS", BOARDS_DEFAULT)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip())
        return
    if args[0] == "--selftest":
        selftest()
        return
    if args[0] == "boards":
        cmd_boards(boards_path)
        return
    dry = False
    if args[0] == "-n":
        dry = True
        args = args[1:]
    if len(args) < 2:
        print(__doc__.strip())
        sys.exit(2)
    board, adb_args = args[0], args[1:]
    if adb_args and adb_args[0] == "-n":     # 板名后再接 -n 也认(与用法示例一致)
        dry = True
        adb_args = adb_args[1:]
    devices = adb_devices()
    boards = load_boards(boards_path)
    name, dev = resolve(board, devices, boards)
    cmd = [ADB, "-s", dev["serial"], *adb_args]
    if dry:
        print(" ".join(cmd))
        return
    sys.exit(subprocess.run(cmd).returncode)


if __name__ == "__main__":
    main()
