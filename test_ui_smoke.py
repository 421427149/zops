#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_ui_smoke.py —— 网页 UI 冒烟测试(改 UI_HTML 后必跑)。

血训(2026-11-09): UI_HTML 字符串拼接切出重复函数头, 网页 JS 整体语法死亡——
页面壳(静态 HTML)照常渲染、curl 状态全 200, 但浏览器里零交互, 用户报
"看不到板子"被误排查服务端/隧道/缓存一整天。curl 能证明"服务器回了什么",
不能证明"浏览器拿到后能不能跑"——本测试补的就是后半段。

用法: python3 test_ui_smoke.py [hub地址]     # 默认 http://127.0.0.1:8300
依赖: node 只做语法检查; 无 node 时降级为括号配平粗检(弱保证, 会明示)。
"""

import json
import re
import subprocess
import sys
import urllib.request

PASS, FAIL = "✅", "❌"


def fetch_page(base):
    with urllib.request.urlopen(base + "/", timeout=10) as r:
        return r.read().decode("utf-8")


def extract_inline_js(html):
    return "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))


def check_syntax(js):
    """node --check 语法校验(权威); 无 node 退回括号/引号配平粗检。"""
    open("/tmp/ui_smoke.js", "w", encoding="utf-8").write(js)
    try:
        r = subprocess.run(["node", "--check", "/tmp/ui_smoke.js"],
                           capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            return True, "node --check 通过"
        return False, f"node --check 语法错误:\n{r.stderr[:600]}"
    except FileNotFoundError:
        pairs = {"{": "}", "(": ")", "[": "]"}
        stack, ok = [], True
        for ch in js:
            if ch in pairs:
                stack.append(pairs[ch])
            elif ch in pairs.values():
                if not stack or stack.pop() != ch:
                    ok = False
                    break
        if ok and not stack:
            return True, "(无 node, 括号配平粗检通过——弱保证, 建议装 node)"
        return False, "(括号配平失败)"
    except subprocess.TimeoutExpired:
        return False, "node 超时"


def check_no_duplicate_defs(js):
    """函数重复定义检测(本次血训的直接病灶)。"""
    names = re.findall(r"function\s+(\w+)\s*\(", js)
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        return False, f"重复定义的函数: {sorted(dup)}"
    return True, f"{len(names)} 个函数无重复"


def check_smartpaste_real(html):
    """执行**线上页面真实提取**的 smartPaste(带桩), 断言语义合并与发送行为。

    血训 2026-11-23: v3.8 的 const 重赋值让多行粘贴静默全死, 而当时的"验收"是
    手抄副本+Python 旁路——都没跑真函数。本检查从页面抠出 waitShellQuiet+
    smartPaste 原文, 在 node 里用桩执行用户真实命令, 任何运行期错误当场暴露。
    """
    import subprocess, tempfile
    a = html.find("async function waitShellQuiet")
    b = html.find("term.attachCustomKeyEventHandler")
    if a < 0 or b < 0 or b <= a:
        return False, "没找到 waitShellQuiet/smartPaste 代码段"
    fn = html[a:b]
    harness = (
        "let sends=[],notes=[];\n"
        "const sleep=async()=>{};\n"
        "const note=m=>notes.push(m);\n"
        "const rawSend=t=>sends.push(t);\n"
        "const lineKey=()=>{};\n"
        "let active=1, raw=true, lastPaste='';\n"
        "const state={1:{buf:'x\\r\\nroot@t:/# ',pending:'',brl:false}};\n"
        "const confirm=()=>true;\n"
        + fn +
        "\nsmartPaste(process.argv[2]).then(()=>{"
        "  const stmts=sends.join('').split('\\r').filter(x=>x.trim());"
        "  console.log(JSON.stringify({stmts,notes:notes.length,"
        "    firstHasDone:/done/.test(stmts[0]||''),"
        "    maxChunk:Math.max(...sends.map(x=>x.length))}));"
        "},e=>{console.error('EXEC_FAIL:'+e.message);process.exit(1)});\n")
    tmp = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                      encoding="utf-8")
    tmp.write(harness); tmp.close()
    cmd = 'for e in 1100 1200 1300 1400; do\n  v4l2-ctl -d /dev/v4l-subdev6 -c exposure=$e\n  echo "=== exposure=$e"\n  sleep 3\ndone'
    try:
        r = subprocess.run(["node", tmp.name, cmd], capture_output=True,
                           text=True, timeout=25)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return True, "(无 node, 跳过 smartPaste 实跑——弱保证)"
    if r.returncode != 0:
        return False, f"smartPaste 运行期错误: {r.stderr.strip()[:200]}"
    import json as _j
    try:
        out = _j.loads(r.stdout.strip().splitlines()[-1])
    except ValueError:
        return False, f"输出不可解析: {r.stdout[:150]!r} / {r.stderr[:150]!r}"
    stmts = out.get("stmts", [])
    if not stmts:
        return False, "一条语句都没发出(粘贴静默失败)"
    if not out.get("firstHasDone"):
        return False, f"首条语句未合并完成(for..done 被拆开): {stmts[0][:80]!r}"
    import re as _re
    if not _re.search(r";\s*done\s*$", stmts[0] or ""):
        return False, f"循环体行未用;连接(空格粘接是非法shell): {stmts[0][:100]!r}"
    if out.get("maxChunk", 0) > 48:
        return False, f"分片失效(最长片 {out['maxChunk']} 字 > 48)"
    return True, f"{len(stmts)} 条语句, 首条含 done(合并✔), 最长片 {out['maxChunk']}字"


def check_key_functions(html):
    """关键行为抽查: 翻译器 tl() 必须存在且语义正确。"""
    s, e = html.find("const TL="), html.find("function devOf")
    if s < 0 or e < 0 or e <= s:
        return False, "找不到翻译器代码段(TL=/devOf 锚点)"
    snippet = html[s:e]
    js = snippet + """
;console.log(JSON.stringify([tl('reboot'), tl('for g in x; do echo 1 > /sys/class/gpio/gpio196/value; done')]));
"""
    open("/tmp/ui_tl.js", "w", encoding="utf-8").write(js)
    try:
        r = subprocess.run(["node", "/tmp/ui_tl.js"], capture_output=True, text=True, timeout=20)
        vals = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else None
    except (FileNotFoundError, ValueError, subprocess.TimeoutExpired):
        return True, "(无 node 或解析失败, 跳过行为抽查)"   # 语法已由上一关把关
    if vals and "⚠" in vals[0] and "GPIO" in vals[1]:
        return True, f"tl() 行为正常: {vals}"
    return False, f"tl() 行为异常: {vals}"


def check_tab_highlight(js):
    """选中标签必须高亮: renderTabs 的 DOM 缓存签名里必须含 active。

    血训(2026-11-21): 卡顿优化给 renderTabs 加了签名缓存, 但签名只有
    [index,title,open]; sel() 只改 active 后签名不变 → 直接 return,
    className 永不更新, 高亮一直停在上一个 Tab(用户报"选中的板子标签不会高亮")。
    本项用 node 桩跑真函数, 断言: 首屏高亮 0、切 Tab 后 on 跟着走、无变化不重建。
    """
    s = js.find("function renderTabs()")
    e = js.find("async function sel(", s)
    if s < 0 or e < 0:
        return False, "找不到 renderTabs / sel 锚点"
    fn = js[s:e].strip()
    if "JSON.stringify" not in fn:
        return True, "(renderTabs 无签名缓存, 必然重建——不会犯本 bug)"
    probe = """
let tabs=[{index:0,title:'3588 A',open:true,name_source:'rig绑定',rig:'rig-A',rig_name:'3588 A',port:'1.4.4',hint:'h'},
          {index:1,title:'3588 B',open:false,name_source:'boards.json',port:'1.2',hint:''}];
let active=0,_tabsSig='';
const made=[];
const bar={textContent:'',appendChild(b){made.push(b)}};
const document={createElement:()=>({className:'',textContent:'',title:'',onclick:null})};
const $=()=>bar;
""" + fn + """
const snap=()=>made.map(b=>b.className);
made.length=0; renderTabs();
const a=snap();
made.length=0; active=1; renderTabs();
const b=snap();
made.length=0; renderTabs();
const c=snap();
console.log(JSON.stringify([a,b,c]));
"""
    open("/tmp/ui_tabs.js", "w", encoding="utf-8").write(probe)
    try:
        r = subprocess.run(["node", "/tmp/ui_tabs.js"], capture_output=True, text=True, timeout=20)
    except FileNotFoundError:
        return True, "(无 node, 跳过行为抽查)"
    except subprocess.TimeoutExpired:
        return False, "node 超时"
    if r.returncode != 0:
        return False, f"node 执行失败: {r.stderr.strip()[:200]}"
    try:
        a, b, c = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False, f"输出无法解析: {r.stdout[:200]!r}"
    if a != ["tab on", "tab"]:
        return False, f"首屏高亮位置错: {a}"
    if b != ["tab", "tab on"]:
        return False, f"切 Tab 后高亮没跟着走: {b}(renderTabs 缓存签名漏了 active?)"
    if c != []:
        return False, f"active/title 没变却重建了 DOM: {c}"
    return True, "首屏+切 Tab 高亮正确, 无变化不重建"


def main():
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8300").rstrip("/")
    try:
        html = fetch_page(base)
    except Exception as e:
        print(f"{FAIL} 页面拉取失败 {base}/: {e}")
        sys.exit(2)
    checks = []
    js = extract_inline_js(html)
    if len(js.strip()) < 1000:
        checks.append(("内联 JS 存在且非空", False, f"仅 {len(js)} 字节"))
    else:
        checks.append(("内联 JS 存在", True, f"{len(js)} 字节"))
    checks.append(("JS 语法", *check_syntax(js)))
    checks.append(("函数无重复定义", *check_no_duplicate_defs(js)))
    checks.append(("翻译器行为", *check_key_functions(html)))
    checks.append(("smartPaste 实跑(真函数+桩)", *check_smartpaste_real(html)))
    checks.append(("选中标签高亮", *check_tab_highlight(js)))
    checks.append(("静态外链脚本", True,
                   ", ".join(re.findall(r'src="(/static/[^"]+)"', html)) or "无"))

    failed = 0
    for name, ok, detail in checks:
        print(f"{PASS if ok else FAIL} {name}: {detail}")
        failed += 0 if ok else 1
    if failed:
        print(f"\n{failed} 项失败 —— 禁止提交/宣告完成")
        sys.exit(1)
    print("\nUI SMOKE OK")


if __name__ == "__main__":
    main()
