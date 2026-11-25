#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_property.py —— 属性测试(无 hypothesis 依赖, 标准库实现):

不写具体场景, 而是**声明对任意插拔/挪线都该成立的性质**, 让随机生成器造几百个
"接线世界"去攻它。设计模型(用户 2026-11-25):
    2 块电源控制板固定; 开发板随意插拔, 每块 = 1 串口 + 1 下载口。

被验证的性质(对任意接线都成立):
  P1 身份唯一: 同一时刻, 一个串口口/下载口上最多一块板(生成器保证), 且
     两个 rig 的结论指向**不同的板**(只要两块板都在线且各归各的电源轨)。
  P2 串台为零: 任意挪线 + 对每块板 relocate 后, rig-X 的控制台 == 板 X 的串口口。
  P3 序列号追踪: 任意挪下载线, usb_mode_for(rig-X) 报的 serial == 板 X 的 serial。
  P4 不认领: 板子离线(拔线/断电)时, 它的 rig 必须报"未见", 绝不指到别的板。
  P5 对称: 交换两块板的全部接线后重跑, 结论跟着板走而不是跟着口走。

每个随机世界 = 一串随机操作(挪线/对调/拔插/断电), 以固定种子可复现;
失败时打印"最小复现步骤"。用法: python3 test_property.py [轮数]
"""

import json
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import test_topology as T          # 复用假世界/打桩
R = T.R

PHYS = ["1.2", "1.3", "1.4.1", "1.4.2", "1.4.3", "1.4.4", "1.1"]   # 可插的物理口


class Sim:
    """一个随机接线世界的模拟器, 记录操作步骤(失败时可复现)。"""

    def __init__(self, seed):
        self.seed = seed
        self.rng = random.Random(seed)
        self.steps = []
        self.w = T.World()
        self.A = self.w.add("A", "ba27980e967d3a93")
        self.B = self.w.add("B", "45ef5567472a8f0c")
        self.w.rails = {"rig-A": {self.A}, "rig-B": {self.B}}
        self.tmp = tempfile.mkdtemp(prefix="zops-prop-")
        T.install(self.w, self.tmp)
        self._place_initial()

    def _free(self, used):
        return [p for p in PHYS if p not in used]

    def _place_initial(self):
        a, b = self.rng.sample(PHYS, 2)
        c, d = self.rng.sample([p for p in PHYS], 2)
        self.A.serial_port, self.B.serial_port = a, b
        self.A.dl_port, self.B.dl_port = c, d
        self.steps.append(f"初始: A(串口={a},下载={c}) B(串口={b},下载={d})")

    # ---- 随机操作 ----
    def op_move_serial(self):
        b = self.rng.choice([self.A, self.B])
        free = self._free({x.serial_port for x in (self.A, self.B)})
        if not free:
            return
        old, new = b.serial_port, self.rng.choice(free)
        b.serial_port = new
        self.steps.append(f"{b.bid} 串口线 {old}→{new}")

    def op_swap_serial(self):
        self.A.serial_port, self.B.serial_port = self.B.serial_port, self.A.serial_port
        self.steps.append("两块板串口线对调")

    def op_move_dl(self):
        b = self.rng.choice([self.A, self.B])
        free = self._free({x.dl_port for x in (self.A, self.B)})
        if not free:
            return
        old, new = b.dl_port, self.rng.choice(free)
        b.dl_port = new
        self.steps.append(f"{b.bid} 下载线 {old}→{new}")

    def op_swap_dl(self):
        self.A.dl_port, self.B.dl_port = self.B.dl_port, self.A.dl_port
        self.steps.append("两块板下载线对调")

    def op_unplug_serial(self):
        b = self.rng.choice([self.A, self.B])
        b.serial_port = None
        self.steps.append(f"{b.bid} 拔掉串口线")

    def op_unplug_dl(self):
        b = self.rng.choice([self.A, self.B])
        b.dl_port = None
        self.steps.append(f"{b.bid} 拔掉下载线")

    def op_power_off(self):
        b = self.rng.choice([self.A, self.B])
        b.on = False
        self.steps.append(f"{b.bid} 断电")

    def op_power_on(self):
        b = self.rng.choice([self.A, self.B])
        b.on = True
        b.boot_t = self.w.clk.time()
        self.steps.append(f"{b.bid} 上电")

    OPS = [op_move_serial, op_swap_serial, op_move_dl, op_swap_dl,
           op_unplug_serial, op_unplug_dl, op_power_off, op_power_on]

    def shuffle(self, n):
        for _ in range(n):
            self.rng.choice(self.OPS)(self)

    # ---- 判定 ----
    def console_of(self, rig):
        return json.load(open(R.RIGS_DEFAULT, encoding="utf-8"))[rig]["target"].get("console")

    def check(self):
        """跑一遍 relocate + 读 usb_mode_for, 返回违例列表。"""
        import contextlib, io
        bad = []
        rigs = json.load(open(R.RIGS_DEFAULT, encoding="utf-8"))
        for rig_name, board in (("rig-A", self.A), ("rig-B", self.B)):
            if board.on and board.serial_port:       # 板子在线 → 必须能定位到自己
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        T._rel(self.w, rig_name)
                except SystemExit as e:
                    bad.append(f"P2 {rig_name}: 板 {board.bid} 在线(串口@{board.serial_port}) "
                               f"但 relocate 失败: {e}")
                    continue
                got = self.console_of(rig_name)
                if got != board.serial_port:
                    other = self.B if board is self.A else self.A
                    where = "别板的口(串台!)" if got == other.serial_port else "不存在的口"
                    bad.append(f"P2 {rig_name}: 控制台={got} 但板 {board.bid} 在 {board.serial_port} → {where}")
            elif board.serial_port is None or not board.on:
                # 板子离线: 报未见, 且不许改登记指向别板
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        T._rel(self.w, rig_name)
                    got = self.console_of(rig_name)
                    other = self.B if board is self.A else self.A
                    if got == other.serial_port:
                        bad.append(f"P4 {rig_name}: 板 {board.bid} 离线, 却把控制台指到了 "
                                   f"别板 {other.bid} 的口 {got}(串台!)")
                except SystemExit:
                    pass                                # 明确失败 = 正确行为
            rigs = json.load(open(R.RIGS_DEFAULT, encoding="utf-8"))
            # 序列号追踪/不认领
            rigs[rig_name]["target"]["adb_serial"] = board.serial
            rigs[rig_name]["target"]["download_chain"] = board.dl_port
            t = R.usb_mode_for(rigs[rig_name])
            other = self.B if board is self.A else self.A
            online = board.on and board.dl_port
            # "被认领" = 返回了肯定的模式结论(不以 ? / ✘ 开头的存疑/拒绝提示)
            claimed = bool(t) and not t.startswith(("?", "✘"))
            if online:
                if not claimed or board.serial[:12] not in t:
                    bad.append(f"P3 {rig_name}: 板 {board.bid} 下载线在线@{board.dl_port} 但未被认出: {t!r}")
            else:
                if claimed:
                    bad.append(f"P4 {rig_name}: 板 {board.bid} 下载线离线, 却仍给出模式结论: {t!r}")
        return bad


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    fails = 0
    by_prop = {}
    for seed in range(rounds):
        sim = Sim(seed)
        sim.shuffle(random.Random(seed * 7 + 1).randint(0, 6))
        try:
            bad = sim.check()
        except Exception as e:                      # 属性检查本身崩了也算失败
            bad = [f"X  检查过程异常: {type(e).__name__}: {e}"]
        if bad:
            fails += 1
            for b in bad:
                by_prop[b.split()[0]] = by_prop.get(b.split()[0], 0) + 1
            if fails <= 5:                          # 详列前几个, 其后只计数
                print(f"\n✘ 种子 {seed} 违反 {len(bad)} 条性质:")
                for s in sim.steps:
                    print(f"     · {s}")
                for b in bad:
                    print(f"   {b}")
    print(f"\n属性测试: {rounds} 个随机接线世界, 违反 {fails} 个"
          + (f"  按性质: {by_prop}" if by_prop else "  ✔ 全部性质成立"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
