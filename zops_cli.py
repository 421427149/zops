#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zops_cli.py —— ZOps 一条命令入口: AI 一次调用完成「查板 → 发令 → 等特征 → 取应答」。

为什么存在: 此前 AI 操作一块板要自己拼三四步(查 Tab → 发命令 → 轮询读 → 找特征),
每接入一个 AI 就重造一遍、各有各的错法。本工具把 AGENTS.md 的铁律内建在流程里:
先列 Tab 再寻址(铁律1)、只认板名, ttyUSBx/COMx 直接拒绝(铁律3)、发完立即长轮询
不 sleep 死等(铁律5)、控电只放行幂等安全的 on(铁律4/8)——AI 想违反都没处违反。
串口读写一律经网关 hub(铁律11): 本工具不开串口、不直连 832x 实例端口。

用法:
    python3 zops_cli.py run --board "3588 A" --send "uname -a" --expect "Linux" --json
    python3 zops_cli.py boards --json
    python3 zops_cli.py rig rig-A on --json

网关: 默认 http://127.0.0.1:8300, 可用 --hub 或环境变量 ZOPS_HUB 覆盖;
令牌: --token 或环境变量 ZOPS_TOKEN。
退出码: 0=等到/成功  1=超时未等到  2=环境或用法错误(网关不通/板名不认识/参数错)。
完整契约见仓库 AGENTS.md(12 条铁律), 与本工具描述冲突时以 AGENTS.md 为准。
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode

DEFAULT_HUB = "http://127.0.0.1:8300"
EXIT_OK = 0          # 等到了 / 成功
EXIT_TIMEOUT = 1     # 超时没等到
EXIT_ENV = 2         # 环境或用法错误(网关不通/板名不认识/参数错)
SINGLE_POLL_CAP_MS = 5000   # 铁律5: 单次长轮询上限 5s, 总时长由 --timeout 控制
READ_CHUNK = 256 * 1024     # 每次长轮询最多取多少账本字节
QUIET_S = 0.8               # 无 --expect 时: 静默这么久视为应答已完
HTTP_TIMEOUT_S = 6.0        # 普通请求(非长轮询)超时秒数
HUMAN_OUTPUT_CAP = 4000     # 人读模式下输出展示上限(超出取尾部)

# 铁律3: 端口名重插即变号, 一律拒绝按它寻址(ttyUSB0/ttyACM0/ttyS2/ttyHS0/COM3, 可带 /dev/ 前缀)
PORT_NAME_RE = re.compile(r"^(?:/dev/)?(?:tty(?:USB|ACM|HS|O|S)\d+|com\d+)$", re.IGNORECASE)
FORBIDDEN_RIG_ACTIONS = ("off", "coldboot", "enter-loader", "catch-uboot", "bind")


def self_command(sub: str) -> str:
    """按"这次是怎么被调起来的"拼出一条能照抄的命令: 绝对路径, 换目录、换平台都能跑。"""
    return '"%s" "%s" %s' % (sys.executable or "python", os.path.abspath(sys.argv[0]), sub)


class CliError(Exception):
    """环境/用法错误: 退出码 2, reason 进 JSON, message 给人看, 不抛调用栈。"""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason
        self.message = message


# ------------------------------------------------------------ HTTP(只经 hub)

def _brief(exc):
    """异常压成一行短串, 避免调用栈漏进输出。"""
    text = " ".join(str(exc).split())
    return (text or exc.__class__.__name__)[:160]


def hub_request(method, base, path, body=None, token="", timeout=HTTP_TIMEOUT_S):
    """向 hub 发一次 HTTP 请求, 返回 (status, data字段, 错误串)。

    status 为 None = 网络层失败(网关不通/超时), 错误串以 hub_unreachable 开头;
    业务错误(HTTPError/ok=false)返回状态码与 error 文本。全程只解析 {"ok","data"/"error"}
    包络(与 agent_http/agent_hub 一致), 不抛调用栈。
    """
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(base + path, data=data, method=method)
    request.add_header("Accept", "application/json")
    if body is not None:
        request.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        request.add_header("X-Agent-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        try:
            raw = error.read()
        except Exception:
            raw = b""
        try:
            payload = json.loads(raw.decode("utf-8"))
            msg = payload.get("error") if isinstance(payload, dict) else None
        except (ValueError, UnicodeDecodeError):
            msg = None
        return error.code, None, msg or ("HTTP %s" % error.code)
    except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
        return None, None, "hub_unreachable: %s" % _brief(error)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return status, None, "hub_error: 响应不是合法 JSON"
    if not isinstance(payload, dict):
        return status, None, "hub_error: 响应不是 JSON 对象"
    if not payload.get("ok"):
        return status, payload.get("data"), payload.get("error") or "未知错误"
    return status, payload.get("data"), None


def resolve_hub(args):
    """--hub/--token 与环境变量 ZOPS_HUB/ZOPS_TOKEN 两级取用。"""
    hub = args.hub or os.environ.get("ZOPS_HUB") or DEFAULT_HUB
    token = args.token if args.token is not None else (os.environ.get("ZOPS_TOKEN") or "")
    return hub.rstrip("/"), token


# ------------------------------------------------------------ 匹配

def find_matched_line(output, send_text, regex, count_echo):
    """在输出里找第一条命中 regex 的行。

    默认跳过与所发命令相同的回显行(串口会把命令本身回显回来, 可能还带提示符前缀,
    如 "root@board:~# uname -a"); --count-echo 时跳过行为关闭。
    """
    cmd = send_text.strip()
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not count_echo and cmd and (line == cmd or line.endswith(cmd)):
            continue   # 命令回显, 不作为特征命中
        if regex.search(line):
            return line
    return None


# ------------------------------------------------------------ run

def cmd_run(args):
    base, token = resolve_hub(args)
    t0 = time.monotonic()
    board = (args.board or "").strip()
    result = {"ok": False, "board": board, "tab": None, "matched": None,
              "output": "", "elapsed_ms": 0, "reason": None,
              "capture_consistent": None}

    def emit(code, human=None, diag=None):
        """按 --json 与否输出并返回退出码; 诊断信息永远走 stderr(JSON 模式也不吞)。"""
        result["elapsed_ms"] = int(round((time.monotonic() - t0) * 1000))
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        elif human:
            for line in human:
                print(line)
        if diag:
            for line in diag:
                print(line, file=sys.stderr)
        return code

    regex = None
    if args.expect is not None:
        try:
            regex = re.compile(args.expect)
        except re.error as error:
            raise CliError("bad_regex", "--expect 不是合法正则: %s" % error)
    if not args.send.strip():
        raise CliError("bad_usage", "--send 不能为空(要发送的内容)")
    if args.timeout <= 0:
        raise CliError("bad_usage", "--timeout 必须是正数(秒)")

    # 铁律1: 动手前先 list_tabs(序号随实例上下线而变, 不用上次记的)
    status, data, err = hub_request("GET", base, "/api/tabs", token=token)
    if err:
        result["reason"] = "hub_unreachable" if status is None else "hub_error"
        return emit(EXIT_ENV, diag=[
            "✘ 网关不可用(%s): %s" % (base, err),
            "  先 curl %s/api/status 排查; 不要自己起串口实例、不要直连 832x(铁律11)" % base])
    tabs = (data or {}).get("tabs") or []
    names = [t.get("title") for t in tabs if t.get("title")]
    avail = "、".join(names) if names else "(当前无在线板子)"

    # 铁律3: 端口名重插即变号, 直接拒绝并给出可用板名
    if PORT_NAME_RE.match(board):
        result["reason"] = "port_name_rejected"
        return emit(EXIT_ENV, diag=[
            "✘ 拒绝按端口名 %r 寻址(铁律3: ttyUSBx/COMx 重插即重编号)" % board,
            "  当前可用板名: %s" % avail,
            "  先 %s 看板名, 再按板名 run" % self_command("boards")])
    # 铁律2: 只认板名(title)
    tab = next((t for t in tabs if (t.get("title") or "").strip() == board), None)
    if tab is None:
        result["reason"] = "no_such_board"
        return emit(EXIT_ENV, diag=[
            '✘ 没有叫 "%s" 的板。当前可用: %s' % (board, avail),
            "  板名以 boards --json 的 title 为准(hint 是证据, 铁律2)"])
    idx = int(tab.get("index"))
    result["tab"] = idx

    # 记发送位置 = 账本当前末尾; capture_consistent 原样带出, 不吞
    status, info, err = hub_request("GET", base, "/api/tabs/%d/info" % idx, token=token)
    if err:
        result["reason"] = "hub_unreachable" if status is None else "api_error"
        return emit(EXIT_ENV, diag=["✘ 读 Tab %d 信息失败: %s" % (idx, err)])
    start = int(info.get("capture_bytes") or 0)
    consistent = info.get("capture_consistent")
    result["capture_consistent"] = consistent

    # 发送(经 hub, 铁律11)
    status, _sdata, err = hub_request("POST", base, "/api/tabs/%d/send" % idx,
                                      body={"text": args.send}, token=token)
    if err:
        result["reason"] = "hub_unreachable" if status is None else "api_error"
        return emit(EXIT_ENV, diag=["✘ 发送失败(tab %d): %s" % (idx, err)])

    # 铁律5: 长轮询等待, 不 sleep 死等; 单次上限 5s, 总时长由 --timeout 控制
    deadline = time.monotonic() + args.timeout
    cursor = start
    output_parts = []
    matched = None
    last_new = time.monotonic()
    while True:
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        wait_ms = max(0, min(remaining_ms, SINGLE_POLL_CAP_MS))
        status, rdata, err = hub_request(
            "GET", base,
            "/api/tabs/%d/receive?%s" % (idx, urlencode({
                "offset": cursor, "max_bytes": READ_CHUNK, "wait_ms": wait_ms})),
            token=token, timeout=wait_ms / 1000.0 + HTTP_TIMEOUT_S)
        if err:
            result["output"] = "".join(output_parts)
            result["reason"] = "hub_unreachable" if status is None else "api_error"
            return emit(EXIT_ENV, diag=["✘ 读取应答失败(tab %d): %s" % (idx, err)])
        cursor = int(rdata.get("next_offset") or cursor)
        chunk = rdata.get("text") or ""
        if chunk:
            output_parts.append(chunk)
            last_new = time.monotonic()
            if regex is not None:
                matched = find_matched_line("".join(output_parts), args.send,
                                            regex, args.count_echo)
                if matched is not None:
                    break
        if time.monotonic() >= deadline:
            break
        if regex is None and not chunk and output_parts \
                and time.monotonic() - last_new >= QUIET_S:
            break   # 无 --expect: 应答已静默, 视为读完

    output = "".join(output_parts)
    result["output"] = output
    result["matched"] = matched
    if regex is not None and matched is None:
        result["reason"] = "timeout"
        human = ["✘ [%s] tab %d · %dms · 超时未匹配 /%s/"
                 % (board, idx, result["elapsed_ms"], args.expect)]
        if output:
            human += ["--- 自发送起收到的输出(尾部) ---", output[-HUMAN_OUTPUT_CAP:]]
        return emit(EXIT_TIMEOUT, human=human)
    result["ok"] = True
    human = ["✔ [%s] tab %d · %dms%s" % (board, idx, result["elapsed_ms"],
                                         (" · 命中: %s" % matched) if matched else "")]
    if output:
        human.append(output[-HUMAN_OUTPUT_CAP:])
    diag = None
    if consistent is False:
        diag = ["⚠ capture_consistent=false: 串口实收与账本不一致, 输出可能不完整"
                "(详见 %s/api/tabs/%d/info)" % (base, idx)]
    return emit(EXIT_OK, human=human, diag=diag)


# ------------------------------------------------------------ boards

def cmd_boards(args):
    """GET /api/tabs + GET /api/rigs 合成一份板子清单(铁律1: 动手前先看)。"""
    base, token = resolve_hub(args)
    status, tabs_data, err = hub_request("GET", base, "/api/tabs", token=token)
    if err:
        raise CliError("hub_unreachable" if status is None else "hub_error",
                       "取 Tab 列表失败: %s" % err)
    status, rigs_data, err = hub_request("GET", base, "/api/rigs", token=token)
    if err:
        raise CliError("hub_unreachable" if status is None else "hub_error",
                       "取 rig 状态失败: %s" % err)
    rigs = {}
    for rig in (rigs_data or {}).get("rigs") or []:
        if rig.get("name"):
            rigs[rig["name"]] = rig
    boards = []
    for t in (tabs_data or {}).get("tabs") or []:
        rig = rigs.get(t.get("rig")) if t.get("rig") else None
        flash = (rig or {}).get("flash") or {}
        boards.append({
            "tab": t.get("index"),
            "title": t.get("title"),
            "name_source": t.get("name_source"),
            "hint": t.get("hint"),
            "port": t.get("port"),
            "rig": t.get("rig"),
            "flash_state": flash.get("state"),
            "flash_serial": flash.get("serial"),
        })
    used = {b["rig"] for b in boards if b["rig"]}
    for name, rig in rigs.items():
        if name in used:
            continue
        flash = rig.get("flash") or {}
        target = rig.get("target") or {}
        boards.append({"tab": None, "title": target.get("name") or name,
                       "name_source": None, "hint": None, "port": None,
                       "rig": name, "flash_state": flash.get("state"),
                       "flash_serial": flash.get("serial")})
    if args.json:
        print(json.dumps({"ok": True, "hub": base, "boards": boards},
                         ensure_ascii=False))
        return EXIT_OK
    print("ZOps 网关 %s · %d 块板/组合" % (base, len(boards)))
    print("%4s  %-14s %-10s %-10s %-9s %s" %
          ("tab", "板名", "来源", "rig", "flash", "hint"))
    for b in boards:
        print("%4s  %-14.14s %-10.10s %-10.10s %-9.9s %s" %
              ("-" if b["tab"] is None else b["tab"],
               b["title"] or "?", b["name_source"] or "-", b["rig"] or "-",
               b["flash_state"] or "-", b["hint"] or ""))
    return EXIT_OK


# ------------------------------------------------------------ rig

def cmd_rig(args):
    base, token = resolve_hub(args)
    name, action = args.name, args.action
    if action == "on":   # 铁律4/8: 只放行幂等安全的 on
        status, data, err = hub_request(
            "POST", base, "/api/rigs/%s/on" % quote(name, safe=""),
            body={}, token=token)
        if err:
            reason = "hub_unreachable" if status is None else (
                "no_such_rig" if status == 404 else "api_error")
            raise CliError(reason, "rig %s on 失败: %s" % (name, err))
        started = bool((data or {}).get("started"))
        note = ("on 已下发(异步执行); 结果以 GET /api/rigs 的 last 字段为准"
                "(铁律8: 以回读为准, 不信命令返回值)")
        if args.json:
            print(json.dumps({"ok": started, "rig": name, "action": "on",
                              "reason": None if started else "not_started",
                              "note": note}, ensure_ascii=False))
        else:
            print("✔ rig %s on 已下发(异步执行)。%s" % (name, note))
        return EXIT_OK if started else EXIT_ENV
    if action in FORBIDDEN_RIG_ACTIONS:
        # 铁律4/8: off/coldboot/bind 会真断电, enter-loader/catch-uboot 会真重启
        raise CliError(
            "power_action_forbidden",
            "%s 是真实断电或重启操作, 需要人来做; 或任务明确要求后走 "
            "POST /api/rigs/%s/%s。本命令只放行幂等安全的 on(铁律4/8)。"
            % (action, name, action))
    raise CliError("bad_action",
                   "未知动作 %r; 只放行 on。%s 属于真实断电/重启/绑定操作, "
                   "本命令一律不放行(铁律4/8)。"
                   % (action, ", ".join(FORBIDDEN_RIG_ACTIONS)))


# ------------------------------------------------------------ 命令行

def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--hub", default=None, metavar="URL",
                        help="网关地址, 默认 %s(或环境变量 ZOPS_HUB)" % DEFAULT_HUB)
    common.add_argument("--token", default=None, metavar="TOKEN",
                        help="网关访问令牌(或环境变量 ZOPS_TOKEN)")
    common.add_argument("--json", action="store_true",
                        help="输出一行 JSON(省略时给人看短文本)")
    parser = argparse.ArgumentParser(
        prog="zops",
        description="ZOps 一条命令入口: 一条命令做成一件事(查板/发令等特征/控电), "
                    "AGENTS.md 铁律内建; 串口读写一律经网关 hub, 本工具不开串口。",
        epilog="完整契约见仓库 AGENTS.md(11 条铁律)。网关不通先 "
               "curl http://<主机>:8300/api/status, 不要自己起串口实例。")
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="{run,boards,rig}")
    run = sub.add_parser(
        "run", parents=[common],
        help="向一块板发命令并等特征出现(查板→发令→长轮询→取应答, 一条命令闭环)")
    run.add_argument("--board", required=True, metavar="板名",
                     help="板名(Tab title); ttyUSBx/COMx 会被直接拒绝(铁律3)")
    run.add_argument("--send", required=True, metavar="命令",
                     help="要发送的一行命令(自动带行尾)")
    run.add_argument("--expect", metavar="正则",
                     help="要等的特征(逐行搜索); 省略则发完等应答静默即返回")
    run.add_argument("--timeout", type=float, default=10.0, metavar="秒",
                     help="总等待上限, 默认 10(单次长轮询上限 5 秒, 铁律5)")
    run.add_argument("--count-echo", action="store_true",
                     help="不跳过命令回显行(默认跳过与所发命令相同的回显再匹配)")
    run.set_defaults(func=cmd_run)
    boards = sub.add_parser(
        "boards", parents=[common],
        help="列出板子清单(title/hint/来源/tab/rig/flash)——动手前先看(铁律1)")
    boards.set_defaults(func=cmd_boards)
    rig = sub.add_parser(
        "rig", parents=[common],
        help="控电组合操作(只放行幂等安全的 on; 断电/重启须人来, 铁律4/8)")
    rig.add_argument("name", metavar="组合名",
                     help="rig 名称, 如 rig-A(见 boards --json)")
    rig.add_argument("action",
                     help="只允许 on; off/coldboot/enter-loader/catch-uboot/bind 一律拒绝")
    rig.set_defaults(func=cmd_rig)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or EXIT_OK
    except CliError as error:
        payload = {"ok": False, "reason": error.reason, "error": error.message}
        if getattr(args, "json", False):
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print("✘ %s" % error.message, file=sys.stderr)
        return EXIT_ENV
    except KeyboardInterrupt:
        print("中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
