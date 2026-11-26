#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""install_skill.py —— 把 zops 技能装到本机 AI 技能目录（幂等，重复运行覆盖旧副本）。

给各种 AI 入口（ZCode / Claude Code / Codex / DSH Super Agent worker）自动发现用：
源是仓库里的 skills/zops（含 SKILL.md），目标默认 ~/.agents/skills/zops，
其他入口用 --target 指到各自的技能目录，例如：

    python3 deploy/install_skill.py                            # 装 ~/.agents/skills/zops
    python3 deploy/install_skill.py --target ~/.claude/skills/zops   # Claude Code
    python3 deploy/install_skill.py --target ~/.codex/skills/zops    # Codex

只复制 skills/zops 目录本身，不动其他文件；无第三方依赖，不联网。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys


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
    args = parser.parse_args()
    dest = os.path.abspath(os.path.expanduser(args.target))
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if os.path.isdir(dest):
        shutil.rmtree(dest)          # 幂等：先清旧副本再整目录复制，不留陈旧文件
    shutil.copytree(src, dest)
    n_files = sum(len(filenames) for _root, _dirs, filenames in os.walk(dest))
    print("已安装 zops 技能: %s -> %s (%d 个文件)" % (src, dest, n_files))
    print("重新打开 AI 会话后即可自动发现；触发词：串口/开发板/板子/UART/console/adb/哪块板在线")
    return 0


if __name__ == "__main__":
    sys.exit(main())
