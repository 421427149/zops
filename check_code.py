#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_code.py —— 提交前代码自检: 语法 + 重复定义 + 常见拼接事故。

为什么需要它(血训 2026-11-09 ~ 11-16): 在写补丁时用"字符串拼接 + 锚点插入"改代码,
多次把锚点行重复粘进去(如 `def usb_alive(target):def usb_alive(target):`), 这类
错误 py_compile 只能抓到一部分; 更阴的是"同名函数定义两次"——后一个静默覆盖前一个,
不报错但逻辑已错。

检查项:
  1. 语法(py_compile)
  2. 顶层函数/类重复定义(静默覆盖, 必须报)
  3. 同一行出现两个 def/class(拼接事故特征)
  4. 内嵌 JS 的语法(node --check, 仅当 agent_hub.py 里有 UI_HTML)

用法: python3 check_code.py [文件...]      # 默认检查本目录全部 .py
"""

import ast
import os
import py_compile
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FILES = ["agent_hub.py", "rigctl.py", "serial_keeper.py", "serial_debug.py",
                 "agent_http.py", "adbq.py", "mcp_bridge.py", "hub_audit_view.py",
                 "start_zops.sh", "test_topology.py", "test_property.py", "test_mutation.py"]


def check_syntax(path):
    """编译到临时文件检查语法(不写 __pycache__, 不动源目录)。"""
    import tempfile
    try:
        with tempfile.NamedTemporaryFile(suffix=".pyc", delete=True) as tf:
            py_compile.compile(path, doraise=True, cfile=tf.name)
        return []
    except py_compile.PyCompileError as e:
        return [f"语法错误: {str(e).strip().splitlines()[-1][:160]}"]


def check_dup_defs(path):
    """顶层 def/class 重名 → 后者静默覆盖前者, 必须报。"""
    try:
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    except SyntaxError:
        return []          # 语法错已由上一项报
    seen, dups = {}, []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            if node.name in seen:
                dups.append(f"重复定义 {node.name} (第 {seen[node.name]} 行与第 {node.lineno} 行)")
            else:
                seen[node.name] = node.lineno
    return dups


def check_splice_accidents(path):
    """同一行两个 def/class —— 字符串拼接事故的特征。"""
    out = []
    for i, line in enumerate(open(path, encoding="utf-8"), 1):
        if len(re.findall(r"\b(?:def|class)\s+\w+\s*[(:]", line)) > 1:
            out.append(f"第 {i} 行疑似拼接重复: {line.strip()[:100]}")
    return out


def check_embedded_js(path):
    """agent_hub.py 内嵌前端 JS 的语法(node --check)。"""
    src = open(path, encoding="utf-8").read()
    if "UI_HTML" not in src:
        return []
    m = re.search(r'UI_HTML\s*=\s*r"""(.*?)"""', src, re.S)
    if not m:
        return ["找不到 UI_HTML 字符串"]
    js = "\n".join(re.findall(r"<script>(.*?)</script>", m.group(1), re.S))
    tmp = "/tmp/_zops_check_ui.js"
    open(tmp, "w", encoding="utf-8").write(js)
    try:
        r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True, timeout=20)
        return [] if r.returncode == 0 else [f"内嵌 JS 语法错误: {r.stderr.strip()[:200]}"]
    except FileNotFoundError:
        return []          # 无 node 时跳过(由 test_ui_smoke.py 兜底)


def main():
    files = sys.argv[1:] or DEFAULT_FILES
    files = [os.path.join(HERE, f) for f in files]
    total = 0
    for path in files:
        if not os.path.isfile(path):
            print(f"⚠ 跳过(不存在): {os.path.basename(path)}")
            continue
        if path.endswith(".sh"):
            r = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
            problems = [] if r.returncode == 0 else [f"shell 语法错误: {r.stderr.strip()[:160]}"]
        else:
            problems = (check_syntax(path) + check_dup_defs(path)
                        + check_splice_accidents(path) + check_embedded_js(path))
        name = os.path.basename(path)
        if problems:
            total += len(problems)
            print(f"✘ {name}")
            for p in problems:
                print(f"    - {p}")
        else:
            print(f"✔ {name}")
    if total:
        print(f"\n共 {total} 处问题 —— 禁止提交")
        sys.exit(1)
    print("\nCODE CHECK OK")


if __name__ == "__main__":
    main()
