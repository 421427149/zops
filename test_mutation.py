#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_mutation.py —— 核心逻辑定向变异测试(标准库实现, 不依赖 mutmut)。

变异测试(mutation testing): 故意把代码改坏一点点(翻转比较符/逻辑运算/条件恒真假),
看现有测试能不能抓到。**测试全过 ≠ 逻辑被验证**——只有"把代码改坏时测试会红"，
才证明那段逻辑真的被测到了。它枯燥、慢、但专治"看起来有测试其实没牙"。

本文件只对**身份认定与重定位**的核心判断做定向变异(不是全量随机变异——全量
跑一次 40 分钟且大半是日志语句的噪声)。每个变异 = (文件, 行号锚点, 变异后的整行)。

行号会随代码漂移, 所以变异点用**锚点子串**定位(锚点找不到 → 直接报错, 不静默跳过)。

用法: python3 test_mutation.py            # 全部(约 8~10 分钟)
      python3 test_mutation.py 冒领 证据A   # 只跑描述含关键字的变异
"""

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = HERE

# (文件, 锚点子串(在该行内查找), 变异后的整行, 描述)
MUTS = [
    # ---- usb_mode_for: 身份认定(最关键) ----
    ("rigctl.py", 'if dev["serial"] and adb_serial and dev["serial"] != adb_serial:',
     '        if False:', "冒领校验整个失效(别板占口不再被拒)"),
    ("rigctl.py", 'if dev["serial"] and adb_serial and dev["serial"] != adb_serial:',
     '        if dev["serial"] or adb_serial or dev["serial"] != adb_serial:', "冒领校验 and→or"),
    ("rigctl.py", 'if dev["serial"] and adb_serial and dev["serial"] != adb_serial:',
     '        if dev["serial"] and adb_serial and dev["serial"] == adb_serial:', "冒领校验 ==↔!= 反转"),
    ("rigctl.py", 'if dev["serial"] and adb_serial and dev["serial"] != adb_serial:',
     '        if False:\n            pass\n        if True:', "冒领分支恒不触发"),
    ("rigctl.py", 'if dev.get("serial") == adb_serial:',
     '            if dev.get("serial") != adb_serial:', "序列号追踪 ==→!="),
    ("rigctl.py", 'if adb_serial:', '    if False:', "跳过序列号追踪"),
    ("rigctl.py", 'if not adb_serial:',
     '        if False:', "没登记序列号时不再'无法确认'而是照认(应被拒)"),
    ("rigctl.py", 'if pids:', '    if False:', "未绑定时不再提示/不拒绝"),
    # ---- relocate_console: 重定位证据 ----
    ("rigctl.py", 'if u0 and u1 is not None and u1 < u0 + elapsed - 5 and u1 <= max(60, elapsed + 5):',
     '            if u0 and u1 is not None and u1 <= u0 + elapsed - 5 and u1 <= max(60, elapsed + 5):', "证据A 阈值 <→<=(等价变异,允许存活)"),
    ("rigctl.py", 'if u0 and u1 is not None and u1 < u0 + elapsed - 5 and u1 <= max(60, elapsed + 5):',
     '            if u0 or u1 is not None or u1 < u0 + elapsed - 5 or u1 <= max(60, elapsed + 5):', "证据A and→or(过松)"),
    ("rigctl.py", 'if u0 and u1 is not None and u1 < u0 + elapsed - 5 and u1 <= max(60, elapsed + 5):',
     '            if True:', "证据A 恒真(任何口都算)"),
    ("rigctl.py", 'if u0 and u1 is not None and u1 < u0 + elapsed - 5 and u1 <= max(60, elapsed + 5):',
     '            if False:', "证据A 恒假"),
    ("rigctl.py", 'quiet = c_off[ap] - c0[ap] <= 200',
     '            quiet = c_off[ap] - c0[ap] > 200', "证据B 静默判定反转"),
    ("rigctl.py", 'burst = c_now[ap] - c_off[ap] > 5000',
     '            burst = c_now[ap] - c_off[ap] <= 5000', "证据B 灌入判定反转"),
    ("rigctl.py", 'if quiet and burst:', '            if quiet or burst:', "证据B and→or"),
    ("rigctl.py", 'if quiet and burst:', '            if True:', "证据B 恒真"),
    ("rigctl.py", 'if quiet and burst:', '            if False:', "证据B 恒假"),
    # ---- 其它核心 ----
    ("rigctl.py", 'return bool(np and nk and np == nk)', '    return bool(np or nk or np == nk)', "口匹配 and→or(任何实例冒充任何登记)"),
    ("rigctl.py", 'read().strip() != "2207":',
     '            if open(f"{base}/idVendor").read().strip() == "2207":', "USB 枚举 VID 过滤反转"),
    ("adbq.py", 'if d["serial"] == want:', '            if d["serial"] != want:', "adbq 序列号比对 ==→!="),
    ("adbq.py", 'if want:', '    if False:', "adbq 跳过序列号优先"),
    ("adbq.py", 'b["serial"] = sn', '        pass', "adbq rigs.json 序列号不生效"),
]
# 允许存活的等价变异(改了行为上没差别, 或差别仅在 1 个单位的边界上)
EQUIVALENT = {"证据A 阈值 <→<=(等价变异,允许存活)"}


def run_in(work, test, args=()):
    try:
        r = subprocess.run([sys.executable, test, *args], cwd=work, capture_output=True,
                           text=True, timeout=270)
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def locate(lines, anchor):
    """锚点子串 → 行号; 找不到或找到多行都报错(不猜)。"""
    hits = [i for i, l in enumerate(lines) if anchor in l]
    if len(hits) != 1:
        raise SystemExit(f"[test_mutation] 锚点 {anchor!r} 命中 {len(hits)} 行(应恰好 1 行)—— "
                         f"代码改了? 请同步更新变异点")
    return hits[0]


def main():
    only = sys.argv[1:]
    killed = total = 0
    survivors = []
    for f, anchor, newline, desc in MUTS:
        if only and not any(k in desc for k in only):
            continue
        work = tempfile.mkdtemp(prefix="zops-mut-")
        try:
            for name in os.listdir(ROOT):
                src = os.path.join(ROOT, name)
                if name.endswith((".py", ".json")):
                    shutil.copy2(src, work)
            shutil.copytree(os.path.join(ROOT, "webui"), os.path.join(work, "webui"),
                            dirs_exist_ok=True)
            path = os.path.join(work, f)
            lines = open(path, encoding="utf-8").read().split("\n")
            ln = locate(lines, anchor)
            lines[ln] = newline
            open(path, "w", encoding="utf-8").write("\n".join(lines))
            if subprocess.run([sys.executable, "-m", "py_compile", path],
                              capture_output=True).returncode != 0:
                raise SystemExit(f"[test_mutation] 变异 {desc!r} 生成了语法错误的代码: {newline!r}")
            total += 1
            t_ok = run_in(work, "test_topology.py")
            p_ok = run_in(work, "test_property.py", ("150",))
            dead = not (t_ok and p_ok)
            killed += dead
            mark = "✔ 杀死" if dead else ("≈ 等价" if desc in EQUIVALENT else "✘ 存活")
            print(f"{mark}  {desc:<34} 拓扑={'过' if t_ok else '抓到'} 属性={'过' if p_ok else '抓到'}",
                  flush=True)
            if not dead and desc not in EQUIVALENT:
                survivors.append(desc)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    if not total:
        print("没有匹配的变异点"); return 2
    need = total - len([d for d in EQUIVALENT if not only])
    print(f"\n核心定向变异: {killed}/{total} 被杀死 = {100 * killed / total:.0f}%")
    if survivors:
        print("未杀死的非等价变异(= 这些逻辑没被测试真正验证):")
        for d in survivors:
            print(f"   - {d}")
        return 1
    print("非等价变异全部被杀死 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
