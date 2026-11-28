#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""install_skill.py —— 把 zops 技能装到本机 AI 技能目录（幂等，重复运行覆盖旧副本）。

给各种 AI 入口（ZCode / Claude Code / Codex / DSH Super Agent worker）自动发现用：
源是仓库里的 skills/zops（含 SKILL.md），目标默认 ~/.agents/skills/zops，
其他入口用 --target 指到各自的技能目录，例如：

    python3 deploy/install_skill.py                            # 装 ~/.agents/skills/zops
    python3 deploy/install_skill.py --target ~/.claude/skills/zops   # Claude Code
    python3 deploy/install_skill.py --target ~/.codex/skills/zops    # Codex
    python3 deploy/install_skill.py --hub http://192.168.1.167:8300  # 网关不在本机

只复制 skills/zops 目录本身，不动其他文件；无第三方依赖，不联网。
复制后把 SKILL.md 里的占位符换成这台机器上的真实值——技能装到别处或别的机器上，
示例里的相对路径和"默认网关"都不一定对：
    {{ZOPS_DIR}}   本仓库所在目录
    {{ZOPS_CMD}}   一条命令入口 zops_cli.py
    {{ZOPS_ADBQ}}  按板名 adb 的 adbq.py
    {{ZOPS_HUB}}   这台机器用的网关地址（--hub > 环境变量 ZOPS_HUB > 本机 8300）。
                   写进技能而不只靠环境变量，是因为 AI 会话常常拿不到新设的环境变量
                   （桌面程序不重启就还是旧环境），示例命令里直接带 --hub 才稳。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys

DEFAULT_HUB = "http://127.0.0.1:8300"
PLACEHOLDER = re.compile(r"\{\{[A-Z_]+\}\}")


def _quoted(path: str) -> str:
    return '"%s"' % path


def hub_for(cli_hub: str = "", env: dict | None = None) -> str:
    """这台机器的网关地址：命令行 --hub > 环境变量 ZOPS_HUB > 默认本机。

    只有空白的值算没给（否则会把空地址写进技能文件）；末尾斜杠统一去掉。
    """
    env = os.environ if env is None else env
    for candidate in (cli_hub, env.get("ZOPS_HUB"), DEFAULT_HUB):
        value = (candidate or "").strip().rstrip("/")
        if value:
            return value
    return DEFAULT_HUB


def command_for(repo: str, script: str, launcher_name: str = "") -> str:
    """这台机器上运行仓库里某个脚本的命令。Windows 上有同名 .cmd 启动器就优先用它。"""
    if os.name == "nt":
        launcher = os.path.join(repo, launcher_name or (script + ".cmd"))
        if launcher_name and os.path.isfile(launcher):
            return _quoted(launcher)
        return 'python %s' % _quoted(os.path.join(repo, script))
    return 'python3 %s' % _quoted(os.path.join(repo, script))


def sync_dir(src: str, dest: str) -> list:
    """把 src 的内容同步到 dest：覆盖同名文件、删掉 dest 里多出来的，但不删 dest 目录本身。

    不用"先 rmtree 再 copytree"：Windows 上目标目录正被别的程序打开着时（比如正在
    运行的 AI 盯着技能目录），删掉后立刻重建会报"拒绝访问"——结果是旧技能没了、新的
    也没装上（2026-11-28 在工位机上踩过）。先写新文件、后清旧文件，中途失败旧技能还在。
    返回没删掉的旧文件（被占用），不影响新技能生效。
    """
    keep = set()
    for root, _dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target = dest if rel == os.curdir else os.path.join(dest, rel)
        os.makedirs(target, exist_ok=True)
        keep.add(os.path.normcase(target))
        for name in files:
            path = os.path.join(target, name)
            shutil.copy2(os.path.join(root, name), path)
            keep.add(os.path.normcase(path))
    stuck = []
    for root, dirs, files in os.walk(dest, topdown=False):
        for name in files + dirs:
            path = os.path.join(root, name)
            if os.path.normcase(path) in keep:
                continue
            try:
                if name in dirs:
                    os.rmdir(path)          # 只删空目录；里面还有东西说明没删干净，留着
                else:
                    os.remove(path)
            except OSError:
                stuck.append(os.path.relpath(path, dest))
    return stuck


def fill_placeholders(dest: str, repo: str, hub: str = "") -> int:
    values = {
        "{{ZOPS_DIR}}": repo,
        "{{ZOPS_CMD}}": command_for(repo, "zops_cli.py", "zops.cmd"),
        "{{ZOPS_ADBQ}}": command_for(repo, "adbq.py"),
        "{{ZOPS_HUB}}": hub_for(hub),
    }
    changed = 0
    for root, _dirs, filenames in os.walk(dest):
        for name in filenames:
            if not name.endswith(".md"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            filled = text
            for key, value in values.items():
                filled = filled.replace(key, value)
            if filled != text:
                with open(path, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(filled)
                changed += 1
    return changed


def leftover_placeholders(dest: str) -> list:
    """装好后还留着 {{...}} 的文件——占位符没填全，AI 照抄示例就会失败。"""
    found = []
    for root, _dirs, filenames in os.walk(dest):
        for name in filenames:
            if not name.endswith(".md"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as handle:
                hit = PLACEHOLDER.search(handle.read())
            if hit:
                found.append("%s 里的 %s" % (os.path.relpath(path, dest), hit.group(0)))
    return found


def main():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = os.path.join(repo, "skills", "zops")
    if not os.path.isfile(os.path.join(src, "SKILL.md")):
        print("[ERROR] 找不到技能源 %s（应含 SKILL.md）；请在仓库内运行本脚本"
              % src, file=sys.stderr)
        return 2
    default_target = os.path.join(os.path.expanduser("~"), ".agents", "skills", "zops")
    parser = argparse.ArgumentParser(
        description="安装 zops 技能到本机 AI 技能目录（幂等，覆盖旧副本）")
    parser.add_argument("--target", default=default_target, metavar="DIR",
                        help="目标 zops 技能目录（最终目录），默认 %s" % default_target)
    parser.add_argument("--hub", default="", metavar="URL",
                        help="这台机器的 ZOps 网关地址，写进技能文件；默认取环境变量 ZOPS_HUB，"
                             "都没有则按网关在本机算（%s）" % DEFAULT_HUB)
    args = parser.parse_args()
    dest = os.path.abspath(os.path.expanduser(args.target))
    hub = hub_for(args.hub)
    hub_given = bool((args.hub or os.environ.get("ZOPS_HUB") or "").strip())
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    stuck = sync_dir(src, dest)
    filled = fill_placeholders(dest, repo, hub)
    n_files = sum(len(filenames) for _root, _dirs, filenames in os.walk(dest))
    print("已安装 zops 技能: %s -> %s (%d 个文件，%d 个填好了本机路径)"
          % (src, dest, n_files, filled))
    print("本机命令入口: %s" % command_for(repo, "zops_cli.py", "zops.cmd"))
    print("本机网关地址: %s%s" % (hub, "" if hub_given else
                                "（没给 --hub 也没设 ZOPS_HUB，按网关在本机算；不在本机就带 --hub 重装）"))
    left = leftover_placeholders(dest)
    if left:
        print("[ERROR] 技能里还有没替换的占位符: %s" % "; ".join(left), file=sys.stderr)
        return 2
    if stuck:
        print("[提示] 这些旧文件被占用没删掉，不影响使用: %s" % "; ".join(stuck), file=sys.stderr)
    print("重新打开 AI 会话后即可自动发现；触发词：串口/开发板/板子/UART/console/adb/哪块板在线")
    return 0


if __name__ == "__main__":
    sys.exit(main())
