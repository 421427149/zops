#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_install_skill.py —— deploy/install_skill.py 自包含测试: 临时目录里装一遍再核对。

不碰真设备、不联网、不写用户真实的技能目录: 每个用例都把技能装进本脚本新建的
临时目录(结束时整个删掉), 然后读装出来的 SKILL.md, 核对占位符是否都换成了本机值。

覆盖:
    I1 网关地址取用顺序: --hub > 环境变量 ZOPS_HUB > 默认本机, 末尾斜杠去掉;
    I2 什么都不给: 装成功, 技能里没有残留占位符, 三个示例命令带默认地址,
       并提示"网关不在本机就带 --hub 重装";
    I3 --hub 指定: 三个示例命令都带上这个地址(末尾斜杠不带入), 不再提示重装;
    I4 环境变量 ZOPS_HUB 生效; 同时给了 --hub 时以 --hub 为准;
    I5 重复安装幂等: 上一次留下的多余文件/子目录被清掉, 目录本身不删;
    I6 残留占位符查得出来(技能里新增占位符而安装器不认识时, 安装必须报错,
       不能装出一份 AI 照抄会失败的技能);
    I7 目标目录正被别的程序打开着时重装仍然成功(仅 Windows): 2026-11-28 在工位机上
       "先删目录再复制"的旧做法删完就建不回来, ZCode 的技能当场丢失。
    I8 技能是给在另一台机器上执行命令的 AI 看的: --dir / --cmd / --adbq 按命令实际执行的
       那台机器写(DSH Super Agent 的工人在调试台 Linux 上跑命令, 技能文件却放在 Windows 上)。

风格参考 test_zops_cli.py(check()/fails 计数, 末行打总结)。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
INSTALL = os.path.join(HERE, "deploy", "install_skill.py")
sys.dont_write_bytecode = True     # 导入安装器别在 deploy/ 下留 __pycache__
sys.path.insert(0, os.path.dirname(INSTALL))
import install_skill  # noqa: E402  只为直接测 hub_for / leftover_placeholders

real = sys.stdout
fails = []


def check(name, cond, detail=""):
    if cond:
        real.write("  ok  %s\n" % name)
    else:
        real.write("FAIL  %s  %s\n" % (name, detail))
        fails.append(name)


def run_install(target, *extra, env_hub=None):
    """子进程里跑安装器; env_hub=None 表示把 ZOPS_HUB 从环境里摘掉。"""
    env = {k: v for k, v in os.environ.items() if k != "ZOPS_HUB"}
    if env_hub is not None:
        env["ZOPS_HUB"] = env_hub
    env["PYTHONUTF8"] = "1"
    return subprocess.run([sys.executable, INSTALL, "--target", target] + list(extra),
                          capture_output=True, text=True, encoding="utf-8",
                          env=env, timeout=60)


def skill_text(target):
    with open(os.path.join(target, "SKILL.md"), encoding="utf-8") as handle:
        return handle.read()


def brief(proc):
    return "rc=%s out=%r err=%r" % (proc.returncode, proc.stdout[-160:], proc.stderr[-160:])


DEFAULT = install_skill.DEFAULT_HUB

real.write("== I1 网关地址取用顺序 ==\n")
check("I1 --hub 优先, 末尾斜杠去掉",
      install_skill.hub_for("http://a:8300/", {"ZOPS_HUB": "http://b:8300"}) == "http://a:8300")
check("I1 没给 --hub 用环境变量 ZOPS_HUB",
      install_skill.hub_for("", {"ZOPS_HUB": "http://b:8300"}) == "http://b:8300")
check("I1 都没有则默认本机",
      install_skill.hub_for("", {}) == DEFAULT,
      install_skill.hub_for("", {}))
check("I1 只有空白也算没给",
      install_skill.hub_for("  ", {"ZOPS_HUB": "  "}) == DEFAULT)

tmp_root = tempfile.mkdtemp(prefix="zops-skill-test-")
try:
    real.write("== I2 默认安装(不给 --hub, 不设 ZOPS_HUB) ==\n")
    t1 = os.path.join(tmp_root, "one", "zops")
    proc = run_install(t1)
    text = skill_text(t1) if os.path.isfile(os.path.join(t1, "SKILL.md")) else ""
    check("I2 退出码 0", proc.returncode == 0, brief(proc))
    check("I2 技能里没有残留占位符", "{{" not in text,
          text[text.find("{{"):text.find("{{") + 60] if "{{" in text else "")
    n_default = text.count("--hub %s " % DEFAULT)
    check("I2 三个示例命令都带默认网关地址", n_default == 3, "实际 %d 处" % n_default)
    check("I2 命令入口填成了本仓库里的路径", HERE in text)
    check("I2 没给地址时提示'不在本机就带 --hub 重装'",
          "不在本机就带 --hub 重装" in proc.stdout, brief(proc))
    t1b = os.path.join(tmp_root, "one-blank", "zops")
    proc = run_install(t1b, "--hub", "   ")
    text = skill_text(t1b) if proc.returncode == 0 else ""
    check("I2 --hub 只给空白也算没给(落回默认地址并提示重装)",
          proc.returncode == 0 and text.count("--hub %s " % DEFAULT) == 3
          and "--hub  " not in text and "不在本机就带 --hub 重装" in proc.stdout,
          brief(proc))

    real.write("== I3 --hub 指定地址 ==\n")
    t2 = os.path.join(tmp_root, "two", "zops")
    proc = run_install(t2, "--hub", "http://192.168.1.167:8300/")
    text = skill_text(t2) if proc.returncode == 0 else ""
    n_given = text.count("--hub http://192.168.1.167:8300 ")
    check("I3 退出码 0", proc.returncode == 0, brief(proc))
    check("I3 指定地址写进三个示例命令(末尾斜杠不带入)", n_given == 3,
          "实际 %d 处" % n_given)
    check("I3 示例里不再出现本机默认地址", ("--hub %s " % DEFAULT) not in text)
    check("I3 给了地址就不再提示重装", "不在本机就带 --hub 重装" not in proc.stdout)

    real.write("== I4 环境变量 ZOPS_HUB ==\n")
    t3 = os.path.join(tmp_root, "three", "zops")
    proc = run_install(t3, env_hub="http://100.70.188.19:8300")
    text = skill_text(t3) if proc.returncode == 0 else ""
    check("I4 环境变量生效", proc.returncode == 0
          and text.count("--hub http://100.70.188.19:8300 ") == 3, brief(proc))
    proc = run_install(t3, "--hub", "http://192.168.1.167:8300",
                       env_hub="http://100.70.188.19:8300")
    text = skill_text(t3) if proc.returncode == 0 else ""
    check("I4 同时给 --hub 时以 --hub 为准", proc.returncode == 0
          and text.count("--hub http://192.168.1.167:8300 ") == 3
          and "100.70.188.19" not in text, brief(proc))

    real.write("== I5 重复安装幂等 ==\n")
    stale = os.path.join(t3, "stale-from-old-version.md")
    with open(stale, "w", encoding="utf-8") as handle:
        handle.write("旧版本留下的文件 {{ZOPS_CMD}}\n")
    old_dir = os.path.join(t3, "old-subdir")
    os.makedirs(old_dir)
    with open(os.path.join(old_dir, "a.txt"), "w", encoding="utf-8") as handle:
        handle.write("旧版本留下的子目录\n")
    proc = run_install(t3, "--hub", "http://192.168.1.167:8300")
    check("I5 重复安装成功且清掉旧副本的多余文件",
          proc.returncode == 0 and not os.path.exists(stale), brief(proc))
    check("I5 旧副本里多出来的子目录也清掉", not os.path.exists(old_dir))
    check("I5 清完之后技能文件还在且是对的", skill_text(t3).count("--hub http://192.168.1.167:8300 ") == 3)

    real.write("== I6 残留占位符 ==\n")
    with open(os.path.join(t3, "extra.md"), "w", encoding="utf-8") as handle:
        handle.write("新占位符 {{ZOPS_SOMETHING_NEW}} 没人填\n")
    left = install_skill.leftover_placeholders(t3)
    check("I6 残留占位符查得出来",
          len(left) == 1 and "ZOPS_SOMETHING_NEW" in left[0], str(left))
    check("I6 干净的副本查不出残留", install_skill.leftover_placeholders(t2) == [])

    real.write("== I8 技能是给在另一台机器上执行命令的 AI 看的(--dir / --cmd / --adbq) ==\n")
    t5 = os.path.join(tmp_root, "five", "zops")
    remote_dir = "/custom_def/empty/串口工具"
    remote_cmd = "python3 " + remote_dir + "/zops_cli.py"
    remote_adbq = "python3 " + remote_dir + "/adbq.py"
    local_cmd = install_skill.command_for(HERE, "zops_cli.py", "zops.cmd")
    proc = run_install(t5, "--hub", "http://127.0.0.1:8300", "--dir", remote_dir,
                       "--cmd", remote_cmd, "--adbq", remote_adbq)
    text = skill_text(t5) if proc.returncode == 0 else ""
    check("I8 退出码 0", proc.returncode == 0, brief(proc))
    n_remote = text.count(remote_cmd + " ")
    check("I8 三个示例命令换成命令实际执行的那台机器上的写法", n_remote == 3,
          "实际 %d 处" % n_remote)
    check("I8 不再出现安装技能这台机器上的路径", local_cmd not in text and HERE not in text)
    check("I8 仓库目录和 adb 入口也换了",
          ("仓库（`" + remote_dir + "`）") in text and (remote_adbq + " <板名>") in text)
    check("I8 没有残留占位符", "{{" not in text)
    proc = run_install(t5, "--cmd", "   ")
    text = skill_text(t5) if proc.returncode == 0 else ""
    check("I8 覆盖值只给空白算没给, 落回本机写法", proc.returncode == 0 and local_cmd in text,
          brief(proc))

    real.write("== I7 目标目录正被别的程序打开着时重装(仅 Windows) ==\n")
    if os.name != "nt":
        real.write("  跳过(非 Windows)\n")
    else:
        import ctypes
        from ctypes import wintypes
        t4 = os.path.join(tmp_root, "four", "zops")
        run_install(t4, "--hub", "http://192.168.1.167:8300")
        create_file = ctypes.windll.kernel32.CreateFileW
        create_file.restype = wintypes.HANDLE
        create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        GENERIC_READ, SHARE_READ_WRITE, OPEN_EXISTING, BACKUP_SEMANTICS = 0x80000000, 0x3, 3, 0x02000000
        # 打开目标目录且不给"共享删除": 这期间目录本身删不掉, 就像正在运行的 AI 盯着技能目录
        handle = create_file(t4, GENERIC_READ, SHARE_READ_WRITE, None, OPEN_EXISTING,
                             BACKUP_SEMANTICS, None)
        locked = handle not in (None, 0, wintypes.HANDLE(-1).value)
        try:
            proc = run_install(t4, "--hub", "http://192.168.1.167:8300")
            text = skill_text(t4) if os.path.isfile(os.path.join(t4, "SKILL.md")) else ""
            check("I7 测试前提: 目录确实被锁住", locked)
            check("I7 被占用时重装仍然成功, 技能文件完好",
                  locked and proc.returncode == 0
                  and text.count("--hub http://192.168.1.167:8300 ") == 3,
                  brief(proc) + " | 技能文件长度 %d" % len(text))
        finally:
            if locked:
                ctypes.windll.kernel32.CloseHandle(handle)
finally:
    shutil.rmtree(tmp_root, ignore_errors=True)

real.write("\nZOPS INSTALL TEST %s  (%d 项失败)\n" % ("OK" if not fails else "FAILED", len(fails)))
sys.exit(0 if not fails else 1)
