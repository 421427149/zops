#!/usr/bin/python3
# ZOps SessionStart 注入器（zcode / claude code 通用）：新会话开局时把"现在哪些板子在线"
# 作为 additionalContext 注入对话。状态来自 `rigctl.py online --context`（只读：扫 USB 总线
# 上的 SoC 序列号，不动板子）。这是开局时刻的快照，之后插拔不会更新；最新状态用 MCP 工具
# zops_rigs 或 `python3 rigctl.py online`。
# zcode 输入：hook_event_name = SessionStart（JSON 经 stdin）
# 输出 schema 依据 @zcode/contracts hooks：
#   {"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"..."}}
# 任何失败一律静默放行（fail-open：注入器坏掉、ZOps 没起、hub 不可达，都不得打断会话）。
import json
import os
import subprocess
import sys

RIGCTL = os.environ.get("ZOPS_RIGCTL", "/custom_def/empty/串口工具/rigctl.py")


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        event = data.get("hook_event_name") or data.get("hookEventName")
        if event != "SessionStart":        # 只认明确的本事件(空输入/未知事件都不注入)
            sys.exit(0)
        r = subprocess.run([sys.executable, RIGCTL, "online", "--context"],
                           capture_output=True, text=True, timeout=4)
        text = (r.stdout or "").strip()
        if r.returncode != 0 or not text:
            sys.exit(0)
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": text,
            }
        }, ensure_ascii=False))
    except Exception:
        sys.exit(0)
    sys.exit(0)


if __name__ == "__main__":
    main()
